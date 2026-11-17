"""Pure-stdlib loopback-only (127.0.0.1) Chrome DevTools Protocol HTTP + WebSocket client."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import struct
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urlsplit

if __package__:
    from .security import ValidationProblem, same_origin, validate_cdp_ws_url
else:  # pragma: no cover - direct module import fallback
    from security import ValidationProblem, same_origin, validate_cdp_ws_url

_WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HTTP_BYTES = 2 * 1024 * 1024
_MAX_WS_MESSAGE_BYTES = 16 * 1024 * 1024


class CdpError(RuntimeError):
    """Structured CDP failure mapped to CapabilityResult error codes."""

    def __init__(self, message: str, *, error_code: str = "service_offline") -> None:
        self.error_code = error_code
        super().__init__(message)


class CdpTransport(Protocol):
    """Injectable transport abstraction for CDP HTTP target discovery and WebSocket evaluation."""

    async def list_targets(self, host: str, port: int, *, timeout_sec: float = 5.0) -> list[dict[str, Any]]:
        """Return CDP target descriptors from `/json/list`."""

    async def close_browser(self, ws_url: str, *, timeout_sec: float = 2.0) -> None:
        """Ask Chrome to flush profile state and exit normally."""

    async def evaluate(
        self,
        ws_url: str,
        expression: str,
        *,
        await_promise: bool = False,
        return_by_value: bool = True,
        navigate_url: str | None = None,
        timeout_sec: float = 15.0,
    ) -> dict[str, Any]:
        """Execute `Runtime.evaluate` (and optional `Page.navigate`) on a CDP target."""


def _enforce_loopback_host(host: str) -> str:
    if host != "127.0.0.1":
        raise CdpError("安全限制: CDP 仅允许连接 127.0.0.1", error_code="permission_denied")
    return host


def _decode_chunked_body(raw: bytes) -> bytes:
    out = bytearray()
    pos = 0
    while pos < len(raw):
        crlf = raw.find(b"\r\n", pos)
        if crlf == -1:
            break
        chunk_size = int(raw[pos:crlf].split(b";", 1)[0].strip(), 16)
        pos = crlf + 2
        if chunk_size == 0:
            break
        out.extend(raw[pos : pos + chunk_size])
        pos += chunk_size + 2
    return bytes(out)


def _parse_http_headers(header_blob: bytes) -> tuple[int, dict[str, str]]:
    lines = header_blob[:-4].decode("latin-1", errors="replace").split("\r\n")
    status_parts = lines[0].split(" ", 2)
    status = int(status_parts[1]) if len(status_parts) >= 2 and status_parts[1].isdigit() else 500
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status, headers


async def _http_get_loopback(host: str, port: int, path: str, *, timeout_sec: float) -> tuple[int, bytes]:
    _enforce_loopback_host(host)
    try:
        async with asyncio.timeout(timeout_sec):
            reader, writer = await asyncio.open_connection(host, port)
            try:
                req = (
                    f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    "Connection: close\r\nAccept: application/json\r\n\r\n"
                ).encode("ascii")
                writer.write(req)
                await writer.drain()
                status, headers = _parse_http_headers(await reader.readuntil(b"\r\n\r\n"))
                if "content-length" in headers and headers["content-length"].isdigit():
                    length = int(headers["content-length"])
                    if length > _MAX_HTTP_BYTES:
                        raise CdpError("CDP 响应超出大小上限", error_code="service_offline")
                    body = await reader.readexactly(length)
                else:
                    raw = await reader.read(_MAX_HTTP_BYTES + 1)
                    if len(raw) > _MAX_HTTP_BYTES:
                        raise CdpError("CDP 响应超出大小上限", error_code="service_offline")
                    body = _decode_chunked_body(raw) if "chunked" in headers.get("transfer-encoding", "").lower() else raw
                return status, body
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
    except TimeoutError:
        raise CdpError("连接 CDP HTTP 端点超时", error_code="timeout") from None
    except CdpError:
        raise
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as exc:
        raise CdpError(f"CDP 服务不可达: {type(exc).__name__}", error_code="service_offline") from None


def _encode_ws_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.extend([0x80 | 126, *struct.pack("!H", length)])
    else:
        header.extend([0x80 | 127, *struct.pack("!Q", length)])
    mask = os.urandom(4)
    header.extend(mask)
    return bytes(header) + bytes(b ^ mask[idx & 3] for idx, b in enumerate(payload))


async def _read_ws_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    b1, b2 = await reader.readexactly(2)
    fin, opcode, masked, length = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", await reader.readexactly(8))
    if length > _MAX_WS_MESSAGE_BYTES:
        raise CdpError("CDP WebSocket 帧超出大小限制", error_code="service_offline")
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length)
    if masked:
        payload = bytes(b ^ mask[idx & 3] for idx, b in enumerate(payload))
    return fin, opcode, payload


async def _recv_ws_text(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
    fragments: list[bytes] = []
    while True:
        fin, opcode, payload = await _read_ws_frame(reader)
        if opcode == 0x8:
            raise CdpError("CDP WebSocket 连接已关闭", error_code="service_offline")
        if opcode == 0x9:
            writer.write(_encode_ws_frame(0xA, payload[:125]))
            await writer.drain()
            continue
        if opcode == 0xA:
            continue
        if opcode in (0x0, 0x1, 0x2):
            fragments.append(payload)
            if sum(len(chunk) for chunk in fragments) > _MAX_WS_MESSAGE_BYTES:
                raise CdpError("CDP 消息总长度超出限制", error_code="service_offline")
            if fin:
                return b"".join(fragments).decode("utf-8", errors="replace")


async def _send_cdp_rpc(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    msg_id: int,
    method: str,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    writer.write(_encode_ws_frame(0x1, json.dumps({"id": msg_id, "method": method, "params": dict(params)}).encode()))
    await writer.drain()
    while True:
        raw_text = await _recv_ws_text(reader, writer)
        try:
            msg = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise CdpError(f"CDP 返回无效 JSON: {exc}", error_code="service_offline") from None
        if isinstance(msg, Mapping) and msg.get("id") == msg_id:
            return dict(msg)


class LoopbackWebSocketCdpTransport:
    """Default stdlib HTTP/WebSocket CDP transport strictly pinned to 127.0.0.1."""

    async def list_targets(self, host: str, port: int, *, timeout_sec: float = 5.0) -> list[dict[str, Any]]:
        _enforce_loopback_host(host)
        status, body = await _http_get_loopback(host, port, "/json/list", timeout_sec=timeout_sec)
        if status == 404:
            status, body = await _http_get_loopback(host, port, "/json", timeout_sec=timeout_sec)
        if status != 200:
            raise CdpError(f"CDP /json/list 返回 HTTP {status}", error_code="service_offline")
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            raise CdpError("CDP 目标列表 JSON 解析失败", error_code="service_offline") from None
        if not isinstance(parsed, list):
            raise CdpError("CDP 目标列表格式无效", error_code="service_offline")
        return [dict(item) for item in parsed if isinstance(item, Mapping)]

    async def evaluate(
        self,
        ws_url: str,
        expression: str,
        *,
        await_promise: bool = False,
        return_by_value: bool = True,
        navigate_url: str | None = None,
        timeout_sec: float = 15.0,
    ) -> dict[str, Any]:
        commands = []
        if navigate_url:
            commands.append(("Page.navigate", {"url": navigate_url}))
        commands.append(("Runtime.evaluate", {"expression": expression, "returnByValue": return_by_value, "awaitPromise": await_promise}))
        return await self._execute(ws_url, commands, timeout_sec=timeout_sec)

    async def close_browser(self, ws_url: str, *, timeout_sec: float = 2.0) -> None:
        await self._execute(ws_url, [("Browser.close", {})], timeout_sec=timeout_sec)

    async def get_cookies(self, ws_url: str, url: str, *, timeout_sec: float = 5.0) -> list[dict[str, Any]]:
        response = await self._execute(ws_url, [("Network.getCookies", {"urls": [url]})], timeout_sec=timeout_sec)
        return response.get('result', {}).get('cookies', [])

    async def _execute(self, ws_url: str, commands: list[tuple[str, dict[str, Any]]], *, timeout_sec: float) -> dict[str, Any]:
        try:
            host, port, path = validate_cdp_ws_url(ws_url)
        except ValidationProblem as exc:
            raise CdpError(str(exc), error_code=exc.error_code) from None

        try:
            async with asyncio.timeout(timeout_sec):
                reader, writer = await asyncio.open_connection(host, port)
                try:
                    ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
                    writer.write(
                        (
                            f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                            f"Connection: Upgrade\r\nSec-WebSocket-Key: {ws_key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                        ).encode("ascii")
                    )
                    await writer.drain()
                    status, resp_headers = _parse_http_headers(await reader.readuntil(b"\r\n\r\n"))
                    expected = base64.b64encode(
                        hashlib.sha1((ws_key + _WS_MAGIC_GUID).encode("ascii")).digest()
                    ).decode("ascii")
                    if status != 101 or resp_headers.get("sec-websocket-accept", "") != expected:
                        raise CdpError("CDP WebSocket 握手校验失败", error_code="service_offline")

                    response: dict[str, Any] = {}
                    for rpc_id, (method, params) in enumerate(commands, 1):
                        response = await _send_cdp_rpc(reader, writer, rpc_id, method, params)
                        if method == "Page.navigate":
                            nav_err, nav_res = response.get("error"), response.get("result")
                            if isinstance(nav_err, Mapping) and nav_err.get("code") != -32601:
                                raise CdpError(f"CDP 页面导航失败: {nav_err.get('message', 'error')}", error_code="service_offline")
                            if isinstance(nav_res, Mapping) and nav_res.get("errorText"):
                                raise CdpError(f"页面加载失败: {nav_res['errorText']}", error_code="service_offline")
                    with contextlib.suppress(Exception):
                        writer.write(_encode_ws_frame(0x8, b""))
                        await writer.drain()
                    return response
                finally:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
        except TimeoutError:
            raise CdpError("CDP 操作超时", error_code="timeout") from None
        except CdpError:
            raise
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as exc:
            raise CdpError(f"CDP 通信失败: {type(exc).__name__}", error_code="service_offline") from None


def _normalized_url_key(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    base = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path or '/'}"
    return f"{base}?{parsed.query}" if parsed.query else base


def select_extract_target(targets: Sequence[Mapping[str, Any]], url: str) -> tuple[dict[str, Any], bool]:
    """Choose a page target for `web.extract.v1`, returning `(target, needs_navigation)`."""
    pages = [dict(t) for t in targets if str(t.get("type") or "page") == "page" and isinstance(t.get("webSocketDebuggerUrl"), str)]
    if not pages:
        raise CdpError("CDP 未发现可用的 page 目标", error_code="service_offline")
    req_key = _normalized_url_key(url)
    for target in pages:
        if _normalized_url_key(str(target.get("url") or "")) == req_key:
            return target, False
    for target in pages:
        t_url = str(target.get("url") or "")
        if t_url.startswith(("http://", "https://")) and same_origin(t_url, url):
            return target, True
    return pages[0], True


def select_same_origin_target(targets: Sequence[Mapping[str, Any]], url: str) -> dict[str, Any]:
    """Choose an existing page target sharing the exact origin of `url`, failing closed otherwise."""
    pages = [dict(t) for t in targets if str(t.get("type") or "page") == "page" and isinstance(t.get("webSocketDebuggerUrl"), str)]
    if not pages:
        raise CdpError("CDP 未发现可用的 page 目标", error_code="service_offline")
    req_key = _normalized_url_key(url)
    matches: list[dict[str, Any]] = []
    for target in pages:
        t_url = str(target.get("url") or "").strip()
        if not t_url.startswith(("http://", "https://")):
            continue
        try:
            if same_origin(t_url, url):
                if _normalized_url_key(t_url) == req_key:
                    return target
                matches.append(target)
        except ValidationProblem:
            continue
    if matches:
        return matches[0]
    raise CdpError("安全限制: 受管 Chrome 中不存在与目标 URL 同源的页面上下文，拒绝跨域 fetch", error_code="permission_denied")


def extract_eval_value(eval_response: Mapping[str, Any]) -> Any:
    """Extract `Runtime.evaluate` value or raise `CdpError` on CDP/JS exceptions."""
    rpc_err = eval_response.get("error")
    if isinstance(rpc_err, Mapping):
        raise CdpError(f"CDP 执行错误: {rpc_err.get('message', 'unknown error')}", error_code="service_offline")
    result_obj = eval_response.get("result")
    if not isinstance(result_obj, Mapping):
        raise CdpError("CDP 返回缺少 result 字段", error_code="service_offline")
    if "exceptionDetails" in result_obj:
        exc_details = result_obj.get("exceptionDetails")
        msg = str(exc_details.get("text") or "页面脚本执行异常") if isinstance(exc_details, Mapping) else "页面脚本执行异常"
        raise CdpError(msg, error_code="service_offline")
    inner = result_obj.get("result")
    if not isinstance(inner, Mapping):
        raise CdpError("CDP Runtime.evaluate 缺少 inner result", error_code="service_offline")
    return None if inner.get("type") == "undefined" else inner.get("value")
