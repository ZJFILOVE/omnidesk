"""Bounded, fail-closed context collection for `ai.agent.v1` and external agents."""

from __future__ import annotations

import asyncio
import json
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


def _render_item(label: str, capability: str, data: Any, limit: int) -> str:
    header = f"## {label} ({capability})"
    if isinstance(data, Mapping):
        preferred = next(
            (data[key] for key in ("markdown", "content", "text", "summary", "body") if isinstance(data.get(key), str)),
            None,
        )
        if preferred:
            body = preferred
        else:
            title = data.get("title") or data.get("name")
            body = (f"title: {title}\n" if title else "") + json.dumps(data, ensure_ascii=False, indent=2)
    else:
        body = json.dumps(data, ensure_ascii=False, indent=2)
    text = f"{header}\n{body}".strip()
    if len(text) > limit:
        text = text[: max(0, limit - 16)].rstrip() + "\n...[truncated]"
    return text


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
) -> CapabilityResult:
    """Collect whitelisted domain context and return one bounded markdown blob."""
    if not isinstance(req, Mapping):
        return fail_result("context.collect.v1 请求必须为 JSON 对象", error_code="invalid_argument", secret=None)

    items = _normalize_items(req)
    if not items:
        return fail_result(
            "context.collect.v1 需要 items/context_refs 数组",
            error_code="invalid_argument",
            data={"allowed_capabilities": sorted(CONTEXT_CAPABILITIES)},
            secret=None,
        )
    if len(items) > 12:
        return fail_result("context.collect.v1 单次最多收集 12 个上下文", error_code="invalid_argument", secret=None)

    try:
        max_chars = int(req.get("max_chars") or 20000)
    except (TypeError, ValueError):
        return fail_result("max_chars 必须为整数", error_code="invalid_argument", secret=None)
    max_chars = max(1000, min(max_chars, 120000))

    try:
        timeout_sec = float(req.get("per_item_timeout_sec") or 15.0)
    except (TypeError, ValueError):
        return fail_result("per_item_timeout_sec 必须为数字", error_code="invalid_argument", secret=None)
    timeout_sec = max(0.5, min(timeout_sec, 120.0))

    per_item_limit = max(1000, max_chars // max(1, len(items)))
    sem = asyncio.Semaphore(min(6, len(items)))

    async def _collect_one(index: int, item: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        capability = str(item.get("capability") or "").strip()
        label = str(item.get("label") or f"context-{index + 1}").strip() or f"context-{index + 1}"
        provider = str(item.get("provider_plugin_id") or "").strip() or None
        payload = item.get("payload")
        if not capability:
            return "", {}, {"index": index, "label": label, "error_code": "invalid_argument", "error": "缺少 capability"}
        if capability not in CONTEXT_CAPABILITIES or (
            capability in {"media.youtube.v1", "media.bilibili.v1"}
            and (not isinstance(payload, Mapping) or payload.get("action") not in {"fetch_chapters", "fetch_subtitles", "fetch_lyrics"})
        ):
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

        clean = _sanitize(getattr(result, "data", {}) or {})
        block = _render_item(label, capability, clean, per_item_limit)
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
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 16)].rstrip() + "\n...[truncated]"
    return CapabilityResult.ok(
        {
            "context": text,
            "items": results,
            "errors": errors,
            "chars": len(text),
            "max_chars": max_chars,
        }
    )
