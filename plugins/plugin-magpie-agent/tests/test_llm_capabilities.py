"""Offline tests for `llm.chat.v1`, `llm.translate.v1`, and `ai.agent.v1`."""

from __future__ import annotations

import http.server
import importlib.util
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]
for _p in (str(REPO_ROOT), str(REPO_ROOT / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from omnidesk_core import OmnideskPaths, PluginManager  # noqa: E402
from omnidesk_sdk import PluginContext, invoke_lifecycle_hook  # noqa: E402


def _load_plugin_module() -> Any:
    spec = importlib.util.spec_from_file_location("plugin_magpie_agent_llm_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _MockGatewayState:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.auth_headers: list[str] = []
        self.mode = "ok"  # ok | slow | rate_429 | quota_429 | unauthorized
        self.secret = "sk-super-secret-token-777"


def _start_mock_gateway(state: _MockGatewayState) -> tuple[http.server.ThreadingHTTPServer, str, threading.Thread]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw) if raw else {}
            state.requests.append(body)
            state.auth_headers.append(self.headers.get("Authorization", ""))

            if state.mode == "slow":
                time.sleep(0.45)
                if self.wfile.closed:
                    return
            if state.mode == "rate_429":
                err = json.dumps({"error": {"code": "rate_limit_exceeded", "message": f"Rate limit hit for {state.secret}"}}).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            if state.mode == "quota_429":
                err = json.dumps({"error": {"code": "insufficient_quota", "message": f"Quota exhausted key={state.secret}"}}).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return
            if state.mode == "unauthorized":
                err = json.dumps({"error": {"code": "unauthorized", "message": f"Bad Bearer {state.secret}"}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)
                return

            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for part in ("Hel", "lo ", "Magpie!"):
                    chunk = json.dumps({
                        "model": "codex/gpt-6-astra",
                        "choices": [{"index": 0, "delta": {"content": part}}],
                    })
                    self.wfile.write(f"data: {chunk}\n\n".encode("utf-8"))
                    self.wfile.flush()
                fin = json.dumps({
                    "model": "codex/gpt-6-astra",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
                })
                self.wfile.write(f"data: {fin}\n\ndata: [DONE]\n\n".encode("utf-8"))
                self.wfile.flush()
                return

            messages = body.get("messages") or []
            sys_msg = str(messages[0].get("content", "")) if messages else ""
            last_user = str(messages[-1].get("content", "")) if messages else ""

            if "response_format" in body:
                reply = json.dumps({"status": "ok", "items": ["alpha"], "count": 1})
            elif "<target_text>" in last_user:
                seg = last_user.split("<target_text>\n", 1)[1].split("\n</target_text>", 1)[0]
                if "OmniDesk => OmniDesk" in sys_msg:
                    reply = f"<translation>译文:{seg}</translation>"
                else:
                    reply = f"<translation>RAW:{seg}</translation>"
            else:
                reply = f"REPLY:{body.get('model')}:{last_user[:40]}"

            resp = json.dumps({
                "id": "chatcmpl-1",
                "model": "antigravity/gemini-4-carbon:high",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}", thread


def _closed_loopback_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_chat_sync_streaming_structured_and_error_mappings(tmp_path: Path) -> None:
    state = _MockGatewayState()
    server, base_url, thread = _start_mock_gateway(state)
    try:
        module = _load_plugin_module()
        plugin = module.create_plugin()
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
        ctx.set_config("gateway_url", base_url)
        ctx.set_secret("gateway_key", state.secret)
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # 1. Non-streaming chat with default model group/omnidesk-active
        res = await ctx.capabilities.call(
            "llm.chat.v1",
            {"messages": [{"role": "user", "content": "Hello OmniDesk"}], "temperature": 0.3, "max_tokens": 128},
        )
        assert res.ok is True
        assert res.data["reply"].startswith("REPLY:group/omnidesk-active:")
        assert res.data["served_model"] == "antigravity/gemini-4-carbon:high"
        assert res.data["route_group"] == "group/omnidesk-active"
        assert res.data["usage"]["total_tokens"] == 15
        assert state.requests[-1]["temperature"] == 0.3
        assert state.requests[-1]["max_tokens"] == 128
        assert state.auth_headers[-1] == f"Bearer {state.secret}"

        # 2. JSON structured output
        json_res = await ctx.capabilities.call(
            "llm.chat.v1",
            {"prompt": "return json", "json_schema": {"type": "object", "properties": {"status": {"type": "string"}}}},
        )
        assert json_res.ok is True
        assert json_res.data["structured"] == {"status": "ok", "items": ["alpha"], "count": 1}
        assert json_res.data["parsed_json"] == json_res.data["structured"]

        # 3. Streaming SSE via SDK call_stream
        chunks: list[dict[str, Any]] = []
        s_res = await ctx.capabilities.call_stream(
            "llm.chat.v1", {"prompt": "stream please", "stream": True}, on_chunk=chunks.append
        )
        assert s_res.ok is True
        assert s_res.data["reply"] == "Hello Magpie!"
        assert s_res.data["streaming"] is True
        assert [c["data"]["text"] for c in chunks] == ["Hel", "lo ", "Magpie!"]

        # 4. 429 rate_limited + secret redaction
        state.mode = "rate_429"
        r429 = await ctx.capabilities.call("llm.chat.v1", {"prompt": "hi"})
        assert r429.ok is False
        assert r429.data["error_code"] == "rate_limited"
        assert state.secret not in r429.error
        assert "[REDACTED]" in r429.error

        # 5. 429 quota_exceeded + secret redaction
        state.mode = "quota_429"
        q429 = await ctx.capabilities.call("llm.chat.v1", {"prompt": "hi"})
        assert q429.ok is False
        assert q429.data["error_code"] == "quota_exceeded"
        assert state.secret not in q429.error
        assert state.secret not in json.dumps(q429.data)

        # 6. 401 secret redaction
        state.mode = "unauthorized"
        u401 = await ctx.capabilities.call("llm.chat.v1", {"prompt": "hi"})
        assert u401.ok is False
        assert state.secret not in u401.error

        # 7. Timeout -> timeout (both sync and SSE streaming)
        state.mode = "slow"
        to_res = await ctx.capabilities.call("llm.chat.v1", {"prompt": "slow", "timeout_ms": 120})
        assert to_res.ok is False
        assert to_res.data["error_code"] == "timeout"

        sse_to = await ctx.capabilities.call_stream("llm.chat.v1", {"prompt": "slow sse", "stream": True, "timeout_ms": 120})
        assert sse_to.ok is False
        assert sse_to.data["error_code"] == "timeout"

        # 8. Connection refused -> service_offline
        ctx.set_config("gateway_url", _closed_loopback_url())
        off_res = await ctx.capabilities.call("llm.chat.v1", {"prompt": "offline"})
        assert off_res.ok is False
        assert off_res.data["error_code"] == "service_offline"
        assert state.secret not in off_res.error

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_core_plugin_manager_streaming_chat_works(tmp_path: Path) -> None:
    state = _MockGatewayState()
    server, base_url, thread = _start_mock_gateway(state)
    try:
        paths = OmnideskPaths.from_env({
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        })
        mgr = PluginManager(plugins_dir=PLUGIN_DIR.parent, paths=paths)
        mgr.config_store.write("plugin-magpie-agent", {"gateway_url": base_url})
        mgr.secret_vault.set("plugin-magpie-agent", "gateway_key", state.secret)
        await mgr.load_plugin(PLUGIN_DIR)

        chunks = [c async for c in mgr.capabilities.stream("llm.chat.v1", {"prompt": "hi", "stream": True})]
        deltas = [c.data for c in chunks if c.event == "delta"]
        assert deltas == ["Hel", "lo ", "Magpie!"]
        assert chunks[-1].done is True
        assert chunks[-1].result is not None and chunks[-1].result.data["reply"] == "Hello Magpie!"
        await mgr.unload_plugin("plugin-magpie-agent")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_translate_and_agent_capabilities(tmp_path: Path) -> None:
    state = _MockGatewayState()
    server, base_url, thread = _start_mock_gateway(state)
    try:
        module = _load_plugin_module()
        plugin = module.create_plugin()
        manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
        ctx = PluginContext(manifest["id"], plugin_dir=PLUGIN_DIR, config_dir=tmp_path, config_schema=manifest["config_schema"])
        ctx.set_config("gateway_url", base_url)
        ctx.set_secret("gateway_key", state.secret)
        await invoke_lifecycle_hook(plugin.on_load, ctx)

        # Translate: empty inputs rejected with invalid_argument
        for bad in ({"text": ""}, {"text": "   "}, {"texts": []}, {"texts": ["ok", "  "]}):
            bad_tr = await ctx.capabilities.call("llm.translate.v1", bad)
            assert bad_tr.ok is False
            assert bad_tr.data["error_code"] == "invalid_argument"

        # Translate single with glossary & context
        tr1 = await ctx.capabilities.call(
            "llm.translate.v1",
            {
                "text": "OmniDesk routes requests via Magpie.",
                "target_language": "zh-CN",
                "context": "Architecture overview",
                "glossary": {"OmniDesk": "OmniDesk", "Magpie": "Magpie"},
            },
        )
        assert tr1.ok is True
        assert tr1.data["translation"] == "译文:OmniDesk routes requests via Magpie."
        assert tr1.data["glossary_locked"] is True
        assert len(tr1.data["glossary_applied"]) == 2

        # Translate batch
        tr_batch = await ctx.capabilities.call(
            "llm.translate.v1",
            {
                "texts": ["First OmniDesk sentence.", "Second sentence."],
                "target_language": "zh-CN",
                "glossary": {"OmniDesk": "OmniDesk"},
            },
        )
        assert tr_batch.ok is True
        assert tr_batch.data["count"] == 2
        assert tr_batch.data["translations"] == ["译文:First OmniDesk sentence.", "译文:Second sentence."]
        assert tr_batch.data["usage"]["total_tokens"] == 30

        # Agent actions: summarize / qa / etymology / parse_sentence
        for act in ("summarize", "qa", "etymology", "parse_sentence"):
            ag_res = await ctx.capabilities.call(
                "ai.agent.v1",
                {
                    "action": act,
                    "domain": "book",
                    "context": "Paper section 2",
                    "selection": "ephemeral",
                    "prompt": f"run {act}",
                },
            )
            assert ag_res.ok is True
            assert ag_res.data["action"] == act
            assert ag_res.data["domain"] == "book"
            assert ag_res.data["citations_grounded"] is True
            sys_sent = state.requests[-1]["messages"][0]["content"]
            assert "NEVER fabricate citations" in sys_sent
            assert "<selection>" in sys_sent

        # Agent bad action & empty input
        assert (await ctx.capabilities.call("ai.agent.v1", {"action": "bad"})).data["error_code"] == "invalid_argument"
        assert (await ctx.capabilities.call("ai.agent.v1", {"action": "qa"})).data["error_code"] == "invalid_argument"

        # Verify ai.agent.v1 streaming via llm.chat.v1
        ag_chunks: list[dict[str, Any]] = []
        ag_stream = await ctx.capabilities.call_stream(
            "ai.agent.v1", {"action": "qa", "domain": "text", "prompt": "stream qa", "stream": True}, on_chunk=ag_chunks.append
        )
        assert ag_stream.ok is True
        assert ag_stream.data["reply"] == "Hello Magpie!"
        assert [c["data"]["text"] for c in ag_chunks] == ["Hel", "lo ", "Magpie!"]

        # Verify translate propagates gateway 429 rate_limited
        state.mode = "rate_429"
        tr_429 = await ctx.capabilities.call("llm.translate.v1", {"text": "Hello", "target_language": "zh-CN"})
        assert tr_429.ok is False and tr_429.data["error_code"] == "rate_limited"
        assert state.secret not in tr_429.error
        state.mode = "ok"

        # Verify ai.agent.v1 routes through llm.chat.v1 capability registry
        called_via_cap: list[dict[str, Any]] = []
        ctx.capabilities.register(
            "llm.chat.v1",
            lambda r: (called_via_cap.append(dict(r)), {"reply": "via-cap", "served_model": "m", "route_group": "g"})[1],
            plugin_id="plugin-magpie-agent",
        )
        ag_cap = await ctx.capabilities.call("ai.agent.v1", {"action": "qa", "prompt": "check"})
        assert ag_cap.ok is True and ag_cap.data["reply"] == "via-cap"
        assert len(called_via_cap) == 1

        await invoke_lifecycle_hook(plugin.on_unload)
        await ctx.acleanup()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
