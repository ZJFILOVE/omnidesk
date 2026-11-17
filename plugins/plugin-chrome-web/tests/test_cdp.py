"""Offline tests for loopback CDP `web.extract.v1`, `web.fetch.v1`, and optional translate."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import importlib.util
import json
import struct
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnidesk_sdk import CapabilityResult, PluginContext, invoke_lifecycle_hook  # noqa: E402

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_chrome_web_cdp_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ws_text_frame(text: str) -> bytes:
    payload = text.encode("utf-8")
    n = len(payload)
    hdr = bytes([0x81, n]) if n < 126 else (bytes([0x81, 126]) + struct.pack("!H", n) if n < 65536 else bytes([0x81, 127]) + struct.pack("!Q", n))
    return hdr + payload


async def _read_ws_frame(reader: asyncio.StreamReader) -> tuple[int, str]:
    b1, b2 = await reader.readexactly(2)
    opcode, masked, length = b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", await reader.readexactly(8))
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length)
    if masked:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return opcode, payload.decode("utf-8", errors="replace")


class FakeCdpServer:
    """Offline loopback HTTP + RFC6455 WebSocket fake CDP server on 127.0.0.1:0."""

    def __init__(self, targets: list[dict[str, Any]], eval_handler: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]) -> None:
        self._targets, self._eval_handler = targets, eval_handler
        self._server: asyncio.AbstractServer | None = None
        self.port, self.recorded_rpcs = 0, []

    async def __aenter__(self) -> FakeCdpServer:
        self._server = await asyncio.start_server(self._handle_conn, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        for target in self._targets:
            target["webSocketDebuggerUrl"] = f"ws://127.0.0.1:{self.port}/devtools/page/{target.get('id', 't1')}"
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            lines = (await reader.readuntil(b"\r\n\r\n"))[:-4].decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for line in lines[1:] if ":" in line for k, v in [line.split(":", 1)]}
            if headers.get("upgrade", "").lower() == "websocket":
                accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + _WS_MAGIC).encode("ascii")).digest()).decode("ascii")
                writer.write(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode("ascii"))
                await writer.drain()
                while True:
                    try:
                        opcode, text = await _read_ws_frame(reader)
                    except (asyncio.IncompleteReadError, ConnectionResetError):
                        break
                    if opcode == 0x8:
                        break
                    msg = json.loads(text)
                    self.recorded_rpcs.append(msg)
                    rpc_id, rpc_method, params = msg["id"], msg["method"], msg.get("params", {})
                    if rpc_method == "Page.navigate":
                        resp: dict[str, Any] = {"id": rpc_id, "result": {"frameId": "frame-1"}}
                    elif rpc_method == "Runtime.evaluate":
                        outcome = self._eval_handler(path, params)
                        resp = {"id": rpc_id, "result": await outcome if asyncio.iscoroutine(outcome) else outcome}
                    else:
                        resp = {"id": rpc_id, "error": {"code": -32601, "message": "Method not found"}}
                    writer.write(_ws_text_frame(json.dumps(resp)))
                    await writer.drain()
                return
            if method == "GET" and path in {"/json/list", "/json"}:
                body = json.dumps(self._targets).encode("utf-8")
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii") + body)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def _setup_plugin(tmp_path: Path) -> tuple[Any, PluginContext]:
    module = _load_plugin_module()
    plugin = module.create_plugin()
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    ctx = PluginContext(
        manifest["id"],
        plugin_dir=PLUGIN_DIR,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        config_schema=manifest["config_schema"],
    )
    await invoke_lifecycle_hook(plugin.on_load, ctx)
    return plugin, ctx


@pytest.mark.asyncio
async def test_extract_cdp_rendered_dom_translate_offline_and_timeout(tmp_path: Path) -> None:
    rendered_html = (
        "<html><head><title>Paper Title</title><script>window.__secret='bad';</script></head>"
        "<body><h1>Heading One</h1><p>First paragraph.</p>"
        "<a href='/docs/paper.pdf'>PDF Link</a><a href='javascript:alert(1)'>Bad XSS Link</a></body></html>"
    )
    delay_sec = 0.0

    async def _eval(_path: str, params: dict[str, Any]) -> dict[str, Any]:
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)
        if "outerHTML" in params.get("expression", ""):
            return {"result": {"type": "string", "value": rendered_html}}
        return {"result": {"type": "undefined"}}

    plugin, ctx = await _setup_plugin(tmp_path)

    async with FakeCdpServer([{"id": "p1", "type": "page", "url": "https://journal.example/article"}], _eval) as srv:
        res = await ctx.capabilities.call(
            "web.extract.v1",
            {"action": "extract", "url": "https://journal.example/article", "cdp_port": srv.port, "translate": True},
        )
        assert res.ok is True
        assert res.data["source"] == "cdp"
        assert res.data["title"] == "Paper Title"
        assert res.data["url"] == "https://journal.example/article"
        assert "window.__secret" not in res.data["text"]
        assert "First paragraph." in res.data["text"]
        assert res.data["links"] == [{"text": "PDF Link", "href": "https://journal.example/docs/paper.pdf"}]
        assert res.data["text_only"] is True
        assert res.data["translated"] is False
        assert all(seg["translated"] is None for seg in res.data["bilingual_segments"])

        async def _mock_translate(req: dict[str, Any]) -> CapabilityResult:
            return CapabilityResult.ok({"translations": [f"译:{t}" for t in req["texts"]]})

        ctx.register_capability("llm.translate.v1", _mock_translate)
        tr_res = await ctx.capabilities.call(
            "web.extract.v1",
            {"action": "extract", "url": "https://journal.example/article", "cdp_port": srv.port, "translate": True},
        )
        assert tr_res.ok is True
        assert tr_res.data["text_only"] is False
        assert tr_res.data["translated"] is True
        assert tr_res.data["bilingual_segments"][0]["translated"] == "译:Heading One"

        # Failed llm.translate.v1 degrades cleanly with text_only=True
        ctx.register_capability(
            "llm.translate.v1",
            lambda _req: CapabilityResult.fail("quota exceeded", error_code="quota_exceeded"),
        )
        deg_res = await ctx.capabilities.call(
            "web.extract.v1",
            {"action": "extract", "url": "https://journal.example/article", "cdp_port": srv.port, "translate": True},
        )
        assert deg_res.ok is True
        assert deg_res.data["text_only"] is True
        assert deg_res.data["translated"] is False
        assert all(seg["translated"] is None for seg in deg_res.data["bilingual_segments"])

        # Page.navigate runs when target is on about:blank; injected transport also works cleanly
        class _InjectedTransport:
            def __init__(self) -> None:
                self.navigate_urls: list[str | None] = []

            async def list_targets(self, host: str, port: int, *, timeout_sec: float = 5.0) -> list[dict[str, Any]]:
                assert host == "127.0.0.1"
                return [{"id": "t2", "type": "page", "url": "about:blank", "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/page/t2"}]

            async def evaluate(self, ws_url: str, expression: str, *, await_promise: bool = False, return_by_value: bool = True, navigate_url: str | None = None, timeout_sec: float = 15.0) -> dict[str, Any]:
                self.navigate_urls.append(navigate_url)
                return {"id": 1, "result": {"result": {"type": "string", "value": "<html><title>Nav</title><p>Hello</p></html>"}}}

        injected = _InjectedTransport()
        orig_transport = plugin._cdp_transport
        plugin.set_cdp_transport(injected)
        nav_res = await ctx.capabilities.call(
            "web.extract.v1",
            {"action": "extract", "url": "https://journal.example/other", "cdp_port": srv.port},
        )
        assert nav_res.ok is True
        assert injected.navigate_urls == ["https://journal.example/other"]
        assert nav_res.data["title"] == "Nav"
        plugin.set_cdp_transport(orig_transport)

        delay_sec = 0.5
        to_res = await ctx.capabilities.call(
            "web.extract.v1",
            {"action": "extract", "url": "https://journal.example/article", "cdp_port": srv.port, "timeout_ms": 60},
        )
        assert to_res.ok is False
        assert to_res.data["error_code"] == "timeout"

        bad_cdp = await ctx.capabilities.call(
            "web.extract.v1",
            {"action": "extract", "url": "https://journal.example/article", "cdp_url": "http://192.168.1.2:9222"},
        )
        assert bad_cdp.ok is False
        assert bad_cdp.data["error_code"] == "permission_denied"

        bad_scheme = await ctx.capabilities.call(
            "web.extract.v1",
            {"action": "extract", "url": "file:///etc/shadow", "cdp_port": srv.port},
        )
        assert bad_scheme.ok is False
        assert bad_scheme.data["error_code"] == "invalid_argument"

    offline_res = await ctx.capabilities.call(
        "web.extract.v1",
        {"action": "extract", "url": "https://journal.example/article", "cdp_port": srv.port},
    )
    assert offline_res.ok is False
    assert offline_res.data["error_code"] == "service_offline"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()


@pytest.mark.asyncio
async def test_fetch_same_origin_methods_size_cap_and_fail_closed(tmp_path: Path) -> None:
    delay_sec = 0.0
    oversize_mode = False
    cross_redirect_mode = False

    async def _eval(_path: str, params: dict[str, Any]) -> dict[str, Any]:
        assert params.get("awaitPromise") is True
        if delay_sec > 0:
            await asyncio.sleep(delay_sec)
        if oversize_mode:
            return {"result": {"type": "object", "value": {"ok": True, "status": 200, "headers": {}, "body": "x" * 2048}}}
        if cross_redirect_mode:
            return {
                "result": {
                    "type": "object",
                    "value": {"ok": True, "status": 200, "url": "https://evil.example/leak", "headers": {}, "body": "no"},
                }
            }
        return {
            "result": {
                "type": "object",
                "value": {
                    "ok": True,
                    "status": 201,
                    "url": "https://app.example.test/api/v1/items",
                    "headers": {"content-type": "application/json"},
                    "body": '{"created":true}',
                },
            }
        }

    plugin, ctx = await _setup_plugin(tmp_path)

    async with FakeCdpServer([{"id": "p1", "type": "page", "url": "https://app.example.test/home"}], _eval) as srv:
        ok_res = await ctx.capabilities.call(
            "web.fetch.v1",
            {
                "action": "fetch",
                "url": "https://app.example.test/api/v1/items",
                "method": "POST",
                "headers": {"Accept": "application/json", "X-Client": "omnidesk"},
                "body": {"q": "stage3"},
                "cdp_port": srv.port,
            },
        )
        assert ok_res.ok is True
        assert ok_res.data["status"] == 201
        assert ok_res.data["headers"]["content-type"] == "application/json"
        assert ok_res.data["body"] == '{"created":true}'
        assert srv.recorded_rpcs[-1]["params"]["awaitPromise"] is True

        cross_res = await ctx.capabilities.call(
            "web.fetch.v1",
            {"action": "fetch", "url": "https://other.example.test/api", "cdp_port": srv.port},
        )
        assert cross_res.ok is False
        assert cross_res.data["error_code"] == "permission_denied"

        bad_scheme = await ctx.capabilities.call(
            "web.fetch.v1",
            {"action": "fetch", "url": "javascript:fetch('/')", "cdp_port": srv.port},
        )
        assert bad_scheme.ok is False
        assert bad_scheme.data["error_code"] == "invalid_argument"

        bad_hdr = await ctx.capabilities.call(
            "web.fetch.v1",
            {"action": "fetch", "url": "https://app.example.test/api", "headers": {"Cookie": "a=b"}, "cdp_port": srv.port},
        )
        assert bad_hdr.ok is False
        assert bad_hdr.data["error_code"] == "permission_denied"

        oversize_mode = True
        big_res = await ctx.capabilities.call(
            "web.fetch.v1",
            {"action": "fetch", "url": "https://app.example.test/api", "max_bytes": 512, "cdp_port": srv.port},
        )
        assert big_res.ok is False
        assert big_res.data["error_code"] == "validation_error"
        oversize_mode = False

        cross_redirect_mode = True
        redir_res = await ctx.capabilities.call(
            "web.fetch.v1",
            {"action": "fetch", "url": "https://app.example.test/api", "cdp_port": srv.port},
        )
        assert redir_res.ok is False
        assert redir_res.data["error_code"] == "permission_denied"
        cross_redirect_mode = False

        # GET with empty body="" succeeds; GET with non-empty body and localhost CDP host are rejected
        get_ok = await ctx.capabilities.call(
            "web.fetch.v1",
            {"action": "fetch", "url": "https://app.example.test/api/v1/items", "method": "GET", "body": "", "cdp_port": srv.port},
        )
        assert get_ok.ok is True
        bad_get_body = await ctx.capabilities.call(
            "web.fetch.v1",
            {"action": "fetch", "url": "https://app.example.test/api", "method": "GET", "body": "x", "cdp_port": srv.port},
        )
        assert bad_get_body.ok is False
        assert bad_get_body.data["error_code"] == "invalid_argument"
        bad_host = await ctx.capabilities.call(
            "web.fetch.v1",
            {"action": "fetch", "url": "https://app.example.test/api", "cdp_url": f"http://localhost:{srv.port}"},
        )
        assert bad_host.ok is False
        assert bad_host.data["error_code"] == "permission_denied"

        delay_sec = 0.4
        to_res = await ctx.capabilities.call(
            "web.fetch.v1",
            {"action": "fetch", "url": "https://app.example.test/api", "timeout_ms": 60, "cdp_port": srv.port},
        )
        assert to_res.ok is False
        assert to_res.data["error_code"] == "timeout"

    offline_res = await ctx.capabilities.call(
        "web.fetch.v1",
        {"action": "fetch", "url": "https://app.example.test/api", "cdp_port": srv.port},
    )
    assert offline_res.ok is False
    assert offline_res.data["error_code"] == "service_offline"

    await invoke_lifecycle_hook(plugin.on_unload)
    await ctx.acleanup()
