"""Complete, resumable, fail-closed context collection for `ai.agent.v1` and external agents."""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omnidesk_sdk import CapabilityResult

try:
    from .magpie_common import fail_result
except ImportError:
    from magpie_common import fail_result  # type: ignore[no-redef]

CallCapability = Callable[..., Any]

CONTEXT_CAPABILITIES = frozenset(
    {
        "web.extract.v1",
        "web.fetch.v1",
        "book.source.v1",
        "subtitle.fetch.v1",
        "subtitle.sync.v1",
        "speech.asr.v1",
        "text.source.v1",
        "text.comments.v1",
        "media.source.v1",
        "media.youtube.v1",
        "media.bilibili.v1",
        "book.bilingual_pdf.v1",
    }
)

_SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
    "gateway_key",
)


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in _SENSITIVE_KEY_PARTS):
                continue
            out[key_text] = _sanitize(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


class ContextSnapshots:
    """Session-local immutable snapshots. Expired cursors fail explicitly, never refetch."""

    def __init__(self) -> None:
        self.entries: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

    def clear(self) -> None:
        self.entries.clear()

    def page(self, data: dict[str, Any], size: int) -> dict[str, Any]:
        key = uuid.uuid4().hex
        self.entries[key] = (time.monotonic(), data)
        while len(self.entries) > 32:
            self.entries.popitem(last=False)
        return self.read(f"{key}:0", size)

    def read(self, cursor: str, size: int) -> dict[str, Any]:
        key, raw_offset = cursor.split(":")
        created, data = self.entries[key]
        if time.monotonic() - created > 3600:
            del self.entries[key]
            raise KeyError(key)
        start = int(raw_offset)
        text = data["context"]
        if start < 0 or start > len(text):
            raise ValueError("invalid offset")
        end = min(start + size, len(text))
        # Keep whole paragraphs when possible, with a lossless fallback for long ones.
        boundary = text.rfind("\n\n", start + size // 2, end)
        if end < len(text) and boundary > start:
            end = boundary + 2
        self.entries.move_to_end(key)
        return {**data, "context": text[start:end], "chars": end - start,
                "total_chars": len(text), "offset": start, "snapshot_id": key,
                "has_more": end < len(text), "next_cursor": f"{key}:{end}" if end < len(text) else None,
                "images": data.get("images", []) if start == 0 else [], "max_chars": size}


_DEFAULT_SNAPSHOTS = ContextSnapshots()


def _detach_images(data: Any, images: list[dict[str, str]]) -> Any:
    if isinstance(data, Mapping):
        result = {}
        for key, value in data.items():
            if key in {"image_url", "left_image_url", "right_image_url", "translated_image_url"} and isinstance(value, str) and value.startswith("data:image/"):
                images.append({"url": value})
                result[key] = f"[image {len(images)}]"
            else:
                result[key] = _detach_images(value, images)
        return result
    if isinstance(data, list):
        return [_detach_images(item, images) for item in data]
    return data


def _render_item(label: str, capability: str, data: Any) -> str:
    header = f"## {label} ({capability})"
    if isinstance(data, Mapping):
        preferred = next(
            (data[key] for key in ("markdown", "content", "text", "body") if isinstance(data.get(key), str) and data[key]),
            None,
        )
        if preferred:
            metadata = {k: data[k] for k in ("title", "url", "page_number", "total_pages") if k in data}
            body = (json.dumps(metadata, ensure_ascii=False) + "\n" if metadata else "") + preferred
        else:
            body = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        body = json.dumps(data, ensure_ascii=False, indent=2)
    return f"{header}\n{body}".strip()


def _read_allowed(capability: str, payload: Mapping[str, Any]) -> bool:
    action = str(payload.get("action") or "")
    if capability == "web.fetch.v1":
        return action in {"", "fetch"} and str(payload.get("method") or "GET").upper() in {"GET", "HEAD"}
    actions = {
        "book.source.v1": {"list_books", "fetch_spread", "get_spread", "get_book", "book_info", "list_vocab", "probe_format"},
        "book.bilingual_pdf.v1": {"status", "list_jobs", "get_job"},
        "media.youtube.v1": {"fetch_chapters", "fetch_subtitles", "fetch_lyrics"},
        "media.bilibili.v1": {"fetch_chapters", "fetch_subtitles", "fetch_danmaku"},
        "text.comments.v1": {"", "fetch_comments", "fetch_child_comments", "fetch_more_comments", "expand_more", "fetch_thread"},
        "subtitle.sync.v1": {"status", "get", "read"},
    }
    return capability in CONTEXT_CAPABILITIES and (capability not in actions or action in actions[capability])


def _normalize_items(req: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = req.get("items")
    if raw is None:
        raw = req.get("context_refs")
    if raw is None:
        raw = req.get("context_items")
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return [item for item in raw if isinstance(item, Mapping)]
    if isinstance(req.get("capability"), str):
        return [req]
    return []


async def collect_context(
    req: Mapping[str, Any],
    *,
    call_capability: CallCapability,
    snapshots: ContextSnapshots | None = None,
) -> CapabilityResult:
    """Collect whitelisted domain context without dropping source text."""
    if not isinstance(req, Mapping):
        return fail_result("context.collect.v1 请求必须为 JSON 对象", error_code="invalid_argument", secret=None)

    snapshots = snapshots if snapshots is not None else _DEFAULT_SNAPSHOTS
    max_chars = req.get("max_chars")
    if max_chars is not None and (isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1):
        return fail_result("max_chars 必须为正整数；省略时返回完整内容", error_code="invalid_argument", secret=None)
    if req.get("cursor") is not None:
        try:
            return CapabilityResult.ok(snapshots.read(str(req["cursor"]), max_chars or 20000))
        except (KeyError, ValueError, TypeError):
            return fail_result("上下文游标无效或快照已过期，请重新收集", error_code="context_expired", secret=None)
    items = _normalize_items(req)
    if not items or len(items) > 12:
        return fail_result("需要 1–12 个上下文引用", error_code="invalid_argument", secret=None)

    try:
        timeout_sec = float(req.get("per_item_timeout_sec") or 15.0)
    except (TypeError, ValueError):
        return fail_result("per_item_timeout_sec 必须为数字", error_code="invalid_argument", secret=None)
    if not math.isfinite(timeout_sec):
        return fail_result("timeout 必须为有限数字", error_code="invalid_argument", secret=None)
    timeout_sec = max(0.5, min(timeout_sec, 120.0))

    images: list[dict[str, str]] = []
    sem = asyncio.Semaphore(min(6, len(items)))

    async def _collect_one(index: int, item: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        capability = str(item.get("capability") or "").strip()
        label = str(item.get("label") or f"context-{index + 1}").strip() or f"context-{index + 1}"
        provider = str(item.get("provider_plugin_id") or "").strip() or None
        payload = item.get("payload")
        if not capability:
            return "", {}, {"index": index, "label": label, "error_code": "invalid_argument", "error": "缺少 capability"}
        if not _read_allowed(capability, payload if isinstance(payload, Mapping) else {}):
            return (
                "",
                {},
                {
                    "index": index,
                    "label": label,
                    "capability": capability,
                    "error_code": "forbidden_capability",
                    "error": f"不允许收集 {capability}",
                },
            )
        if payload is None:
            payload = {}
        if not isinstance(payload, Mapping):
            return (
                "",
                {},
                {
                    "index": index,
                    "label": label,
                    "capability": capability,
                    "error_code": "invalid_argument",
                    "error": "payload 必须为 JSON 对象",
                },
            )

        payload = dict(payload)
        if capability == "book.source.v1":
            payload["record_progress"] = False
        async with sem:
            try:
                if provider:
                    awaitable = call_capability(capability, dict(payload), plugin_id=provider)
                else:
                    awaitable = call_capability(capability, dict(payload))
                result = await asyncio.wait_for(awaitable, timeout=timeout_sec)
            except asyncio.TimeoutError:
                return (
                    "",
                    {},
                    {
                        "index": index,
                        "label": label,
                        "capability": capability,
                        "provider_plugin_id": provider or "",
                        "error_code": "timeout",
                        "error": f"收集超时（{timeout_sec:.1f}s）",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - capability faults must stay isolated
                return (
                    "",
                    {},
                    {
                        "index": index,
                        "label": label,
                        "capability": capability,
                        "provider_plugin_id": provider or "",
                        "error_code": "capability_error",
                        "error": str(exc),
                    },
                )

        if not getattr(result, "ok", False):
            return (
                "",
                {},
                {
                    "index": index,
                    "label": label,
                    "capability": capability,
                    "provider_plugin_id": provider or "",
                    "error_code": str(getattr(result, "data", {}).get("error_code") or "capability_error"),
                    "error": str(getattr(result, "error", "") or "能力调用失败"),
                },
            )

        clean = _detach_images(_sanitize(getattr(result, "data", {}) or {}), images)
        block = _render_item(label, capability, clean)
        return (
            block,
            {
                "index": index,
                "label": label,
                "capability": capability,
                "requested_provider_plugin_id": provider or "",
                "provider_plugin_id": str(getattr(result, "provider_plugin_id", "") or ""),
                "chars": len(block),
            },
            None,
        )

    outcomes = await asyncio.gather(*(_collect_one(index, item) for index, item in enumerate(items)))
    blocks = [block for block, _, _ in outcomes if block]
    results = [result for _, result, _ in outcomes if result]
    errors = [error for _, _, error in outcomes if error is not None]

    if not blocks:
        return fail_result(
            "无法收集任何上下文",
            error_code="context_unavailable",
            data={"errors": errors, "allowed_capabilities": sorted(CONTEXT_CAPABILITIES)},
            secret=None,
        )

    text = "\n\n".join(blocks)
    data = {"context": text, "items": results, "errors": errors, "images": images,
            "chars": len(text), "total_chars": len(text), "max_chars": max_chars,
            "has_more": False, "next_cursor": None}
    if max_chars is not None:
        data = snapshots.page(data, max_chars)
    return CapabilityResult.ok(data)
