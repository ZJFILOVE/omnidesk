"""Loopback-only HTTP bridge from the bundled Qwen Chrome MV3 extension to `llm.translate.v1`."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import re
import secrets
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from omnidesk_sdk import PluginContext

if __package__:
    from .cdp_client import LoopbackWebSocketCdpTransport, _http_get_loopback
    from .security import ValidationProblem, validate_safe_path
else:  # pragma: no cover - direct module import fallback
    from cdp_client import LoopbackWebSocketCdpTransport, _http_get_loopback
    from security import ValidationProblem, validate_safe_path

_MAX_BODY_BYTES = 1024 * 1024
_MAX_SEGMENTS = 256
_MAX_HTML_BYTES = 64000
_INLINE_TAGS = frozenset(
    "a b strong i em u s small sub sup span mark abbr cite q bdi bdo del ins br".split()
)
_SUPPORTED_LANGS = {
    "zh-CN": "Simplified Chinese",
    "zh-TW": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
}
_SLOT_RE = re.compile(r'<t\s+id=["\'](\d{1,4})["\']\s*>(.*?)</t>', re.DOTALL)


def extract_fragment_slots(html: str) -> dict[str, str]:
    """Validate `<t id="N">...</t>` fragment and return `{slot_id: text}`."""
    if not isinstance(html, str) or not html or len(html) > 12000 or "<!" in html or "<?" in html:
        raise ValueError("网页段落格式无效或过长。")
    try:
        root = ET.fromstring("<root>" + html + "</root>")
    except ET.ParseError:
        raise ValueError("网页段落不是有效的 HTML 片段。") from None
    slots: dict[str, str] = {}

    def walk(node: ET.Element, depth: int = 0) -> None:
        if depth > 40:
            raise ValueError("网页段落嵌套过深。")
        if node.tag == "t":
            key = node.get("id", "")
            if set(node.attrib) != {"id"} or not re.fullmatch(r"\d{1,4}", key) or key in slots or len(node):
                raise ValueError("网页文字编号无效。")
            slots[key] = node.text or ""
        elif node.tag not in _INLINE_TAGS | {"root"} or node.attrib or (node.text or "").strip():
            raise ValueError("网页段落包含不支持的结构。")
        if (node.tail or "").strip():
            raise ValueError("文字必须保留在原编号中。")
        for child in node:
            walk(child, depth + 1)

    walk(root)
    if not slots or len(slots) > 200:
        raise ValueError("网页段落没有可翻译文字或文字节点过多。")
    return slots


def _parse_slot_tags(translated_html: str, expected_keys: list[str]) -> dict[str, str] | None:
    found = {m.group(1): m.group(2) for m in _SLOT_RE.finditer(translated_html)}
    if set(found.keys()) != set(expected_keys):
        return None
    return found


def _extract_translations(res_data: dict[str, Any], expected_count: int) -> list[str] | None:
    items = res_data.get("translations")
    if isinstance(items, list) and len(items) == expected_count:
        out: list[str] = []
        for item in items:
            if isinstance(item, dict):
                val = item.get("translated_text") or item.get("text") or ""
            else:
                val = str(item or "")
            out.append(val)
        return out
    single = res_data.get("translated_text") or res_data.get("text")
    if expected_count == 1 and isinstance(single, str):
        return [single]
    return None


async def translate_segments_via_capability(
    ctx: PluginContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Translate extension `<t id="N">` segments using OmniDesk `llm.translate.v1`."""
    if not ctx.capabilities.has("llm.translate.v1"):
        raise RuntimeError("llm.translate.v1 不可用，请先启用 plugin-magpie-agent。")
    target = str(payload.get("target") or "zh-CN")
    if target not in _SUPPORTED_LANGS:
        raise ValueError("请选择支持的目标语言。")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= _MAX_SEGMENTS:
        raise ValueError("单次网页段落数量无效。")

    parsed: list[tuple[str, str, dict[str, str]]] = []
    total_size = 0
    seen_ids: set[str] = set()
    for seg in raw_segments:
        if not isinstance(seg, dict):
            raise ValueError("网页段落字段无效。")
        seg_id = str(seg.get("id") or "")
        html = seg.get("html")
        if not seg_id or seg_id in seen_ids or not isinstance(html, str):
            raise ValueError("网页段落编号或内容无效。")
        seen_ids.add(seg_id)
        slots = extract_fragment_slots(html)
        total_size += len(html)
        parsed.append((seg_id, html, slots))
    if total_size > _MAX_HTML_BYTES:
        raise ValueError("本批网页文字过长。")

    title = str(payload.get("title") or "")[:300]
    context = str(payload.get("context") or "")[:6000]
    shared_ctx = "\n".join(p for p in (title, context) if p)

    req_segments: list[dict[str, str]] = []
    for seg_id, html, slots in parsed:
        if len(slots) == 1:
            only_text = next(iter(slots.values()))
            req_segments.append({"id": seg_id, "text": only_text})
        else:
            req_segments.append({"id": seg_id, "text": html})

    res = await ctx.capabilities.call(
        "llm.translate.v1",
        {
            "segments": req_segments,
            "target_language": target,
            "source_language": "auto",
            "context": shared_ctx,
            "preserve_formatting": True,
        },
    )
    if not res.ok:
        raise RuntimeError(res.error or "llm.translate.v1 调用失败。")

    translated_list = _extract_translations(res.data, len(parsed))
    if translated_list is None:
        raise RuntimeError("llm.translate.v1 返回格式无效。")

    output: list[dict[str, Any]] = []
    failed_segments: list[str] = []
    for (seg_id, _html, slots), translated_raw in zip(parsed, translated_list, strict=True):
        if len(slots) == 1:
            only_key = next(iter(slots.keys()))
            output.append({"id": seg_id, "slots": {only_key: translated_raw}})
            continue
        mapped = _parse_slot_tags(translated_raw, list(slots.keys()))
        if mapped is not None:
            output.append({"id": seg_id, "slots": mapped})
            continue
        # Fallback: translate each non-empty slot individually so inline markup stays intact.
        non_empty = [(k, v) for k, v in slots.items() if v.strip()]
        if not non_empty:
            output.append({"id": seg_id, "slots": dict(slots)})
            continue
        sub_res = await ctx.capabilities.call(
            "llm.translate.v1",
            {
                "segments": [{"id": k, "text": v} for k, v in non_empty],
                "target_language": target,
                "source_language": "auto",
                "context": shared_ctx,
                "preserve_formatting": True,
            },
        )
        sub_texts = _extract_translations(sub_res.data, len(non_empty)) if sub_res.ok else None
        if sub_texts is None:
            failed_segments.append(seg_id)
            continue
        slot_out = dict(slots)
        for (k, _), t in zip(non_empty, sub_texts, strict=True):
            slot_out[k] = t
        output.append({"id": seg_id, "slots": slot_out})

    return {
        "segments": output,
        "failedSegments": failed_segments,
        "failure": "部分段落翻译失败，请重试。" if failed_segments else "",
    }


class LoopbackTranslateBridge:
    """Ephemeral `127.0.0.1` HTTP bridge serving `/health` and `/translate` for Chrome MV3."""

    def __init__(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._token = secrets.token_urlsafe(24)
        self._server: asyncio.AbstractServer | None = None
        self._port: int = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self._port}" if self._port else ""

    @property
    def token(self) -> str:
        return self._token

    async def ensure_started(self) -> tuple[str, str]:
        if self._server is not None and self._port > 0:
            return self.endpoint, self._token
        self._server = await asyncio.start_server(self._handle_conn, host="127.0.0.1", port=0)
        sock = self._server.sockets[0]
        self._port = int(sock.getsockname()[1])
        return self.endpoint, self._token

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
            self._port = 0

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return
            parts = request_line.decode("latin-1").strip().split(" ")
            if len(parts) < 2:
                await self._send_json(writer, 400, {"detail": "Bad request"})
                return
            method, path = parts[0].upper(), parts[1].split("?", 1)[0]
            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not line or line in (b"\r\n", b"\n"):
                    break
                if b":" in line:
                    k, v = line.decode("latin-1").split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if method == "OPTIONS":
                await self._send_json(writer, 204, None)
                return

            auth = headers.get("authorization", "")
            expected = f"Bearer {self._token}"
            if not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
                await self._send_json(writer, 401, {"detail": "配对密钥无效。"})
                return

            if method == "GET" and path == "/health":
                if not self._ctx.capabilities.has("llm.translate.v1"):
                    await self._send_json(writer, 503, {"detail": "llm.translate.v1 不可用，请先启用 plugin-magpie-agent。"})
                    return
                await self._send_json(writer, 200, {"ok": True, "model": "omnidesk:llm.translate.v1"})
                return

            if method == "POST" and path == "/translate":
                content_len = int(headers.get("content-length") or 0)
                if content_len <= 0 or content_len > _MAX_BODY_BYTES:
                    await self._send_json(writer, 400, {"detail": "请求体大小无效。"})
                    return
                raw_body = await asyncio.wait_for(reader.readexactly(content_len), timeout=10.0)
                payload = json.loads(raw_body.decode("utf-8"))
                result = await translate_segments_via_capability(self._ctx, payload)
                await self._send_json(writer, 200, result)
                return

            await self._send_json(writer, 404, {"detail": "Not found"})
        except ValueError as exc:
            await self._send_json(writer, 400, {"detail": str(exc)})
        except Exception as exc:
            await self._send_json(writer, 502, {"detail": str(exc)})
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _send_json(self, writer: asyncio.StreamWriter, status: int, body: dict[str, Any] | None) -> None:
        reason = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 502: "Bad Gateway", 503: "Service Unavailable"}.get(status, "Error")
        payload = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Authorization, Content-Type\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(head + payload)
        await writer.drain()


def resolve_extension_dirs(plugin_dir: Path) -> list[Path]:
    """Return all validated bundled MV3 extension directories (bridge + Qwen translator)."""
    dirs: list[Path] = []
    for rel in ("resources/mv3-extension", "resources/qwen-translator-extension"):
        try:
            ext_dir = validate_safe_path(rel, base_dir=plugin_dir, check_symlinks=True)
        except ValidationProblem:
            continue
        manifest = ext_dir / "manifest.json"
        if ext_dir.is_dir() and manifest.is_file() and not manifest.is_symlink():
            dirs.append(ext_dir)
    return dirs


async def bootstrap_extensions_over_cdp(
    cdp_port: int,
    ext_dirs: list[Path],
    endpoint: str,
    token: str,
) -> None:
    """Load unpacked MV3 extensions via CDP and inject the loopback translate bridge config."""
    if cdp_port <= 0 or not ext_dirs:
        return
    transport = LoopbackWebSocketCdpTransport()
    browser_ws = ""
    for _ in range(25):
        try:
            status, raw = await _http_get_loopback("127.0.0.1", cdp_port, "/json/version", timeout_sec=0.5)
            if status == 200:
                info = json.loads(raw.decode("utf-8"))
                browser_ws = str(info.get("webSocketDebuggerUrl") or "")
                if browser_ws:
                    break
        except Exception:
            pass
        await asyncio.sleep(0.2)
    if not browser_ws:
        return

    qwen_ext_id = ""
    for ext_dir in ext_dirs:
        with contextlib.suppress(Exception):
            res = await transport._execute(
                browser_ws, [("Extensions.loadUnpacked", {"path": str(ext_dir)})], timeout_sec=3.0
            )
            ext_id = str(res.get("result", {}).get("id") or "")
            if ext_dir.name == "qwen-translator-extension" and ext_id:
                qwen_ext_id = ext_id
    if not qwen_ext_id or not endpoint or not token:
        return

    expr = (
        f"chrome.storage.local.set({json.dumps({'endpoint': endpoint, 'token': token})})"
    )
    for _ in range(15):
        with contextlib.suppress(Exception):
            targets = await transport.list_targets("127.0.0.1", cdp_port, timeout_sec=1.0)
            sw = next(
                (t for t in targets if f"chrome-extension://{qwen_ext_id}/" in str(t.get("url") or "")),
                None,
            )
            if sw and sw.get("webSocketDebuggerUrl"):
                await transport.evaluate(
                    str(sw["webSocketDebuggerUrl"]), expr, await_promise=True, timeout_sec=2.0
                )
                return
        await asyncio.sleep(0.2)

