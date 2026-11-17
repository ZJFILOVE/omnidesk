"""Handlers for `llm.chat.v1` and `llm.translate.v1`."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult

try:
    from .magpie_common import fail_result
except ImportError:
    from magpie_common import fail_result  # type: ignore[no-redef]

ChatCaller = Callable[[dict[str, Any], Any], Coroutine[Any, Any, CapabilityResult]]
_TAG_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _extract_tag(text: str, tag: str) -> str:
    pat = _TAG_RE_CACHE.get(tag)
    if pat is None:
        pat = re.compile(rf"<{tag}>\s*(.*?)\s*</{tag}>", re.DOTALL)
        _TAG_RE_CACHE[tag] = pat
    match = pat.search(text)
    return match.group(1).strip() if match else ""


def _parse_structured_json(reply: str) -> Any:
    raw = reply.strip()
    if not raw:
        return None
    candidates = [raw, *[block.strip() for block in re.findall(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)]]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return None


def _build_response_format(req: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    resp_fmt = req.get("response_format")
    if resp_fmt is not None:
        if not isinstance(resp_fmt, Mapping):
            return None, "response_format 必须为 JSON 对象"
        return dict(resp_fmt), None
    schema = req.get("json_schema")
    if schema is not None:
        if not isinstance(schema, Mapping) or not schema:
            return None, "json_schema 必须为非空 JSON 对象"
        if "schema" in schema and "name" in schema:
            return {"type": "json_schema", "json_schema": dict(schema)}, None
        name = str(req.get("schema_name") or "structured_output").strip() or "structured_output"
        return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": dict(schema)}}, None
    if req.get("json_mode") is True:
        return {"type": "json_object"}, None
    return None, None


def _format_glossary(raw: Any) -> tuple[list[tuple[str, str]], str]:
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, Mapping):
        for src, dst in raw.items():
            s, d = str(src).strip(), str(dst).strip()
            if s and d:
                pairs.append((s, d))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping):
                s = str(item.get("source") or item.get("term") or "").strip()
                d = str(item.get("target") or item.get("translation") or "").strip()
                if s and d:
                    pairs.append((s, d))
    if not pairs:
        return [], ""
    return pairs, "\n".join(f"- {src} => {dst}" for src, dst in pairs)


def _parse_batch_translations(reply: str, expected: int) -> list[str] | None:
    """Parse a single-call batch translation reply as an ordered string array."""
    parsed = _parse_structured_json(reply)
    if isinstance(parsed, Mapping):
        for key in ("translations", "translated_texts", "results", "segments"):
            value = parsed.get(key)
            if isinstance(value, list):
                parsed = value
                break
    if not isinstance(parsed, list) or len(parsed) != expected:
        return None
    out: list[str] = []
    for item in parsed:
        if isinstance(item, Mapping):
            text = item.get("translation") or item.get("translated") or item.get("text")
        else:
            text = item
        value = str(text or "").strip()
        if not value:
            return None
        out.append(value)
    return out


def _build_batch_translate_request(
    segments: list[dict[str, Any]],
    *,
    sys_prompt: str,
    target_lang: str,
    source_lang: str,
    shared_ctx: str,
    before_global: str,
    after_global: str,
    req: Mapping[str, Any],
) -> dict[str, Any]:
    payload_segments: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        entry: dict[str, Any] = {"text": seg["text"]}
        seg_ctx = seg.get("context") or shared_ctx
        before = seg.get("context_before") or (segments[i - 1]["text"] if i > 0 else before_global)
        after = (
            seg.get("context_after")
            or (segments[i + 1]["text"] if i + 1 < len(segments) else after_global)
        )
        if seg_ctx:
            entry["context"] = seg_ctx
        if before:
            entry["context_before"] = before
        if after:
            entry["context_after"] = after
        payload_segments.append(entry)
    payload = {
        "source_language": source_lang,
        "target_language": target_lang,
        "segments": payload_segments,
    }
    user_lines = [
        "Translate the `text` field of every segment.",
        f"Return ONLY a JSON array of {len(segments)} translated strings in the same order.",
        "Do not translate the JSON keys and do not add commentary or code fences.",
        json.dumps(payload, ensure_ascii=False),
    ]
    chat_req: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "\n".join(user_lines)},
        ],
        "temperature": req.get("temperature", 0.2),
    }
    for opt_key in ("max_tokens", "reasoning_effort", "route_override", "model", "timeout_ms", "timeout_sec"):
        if req.get(opt_key) is not None:
            chat_req[opt_key] = req[opt_key]
    return chat_req


def _resolve_timeout_sec(req: Mapping[str, Any], default_timeout_ms: int) -> tuple[float | None, str | None]:
    if req.get("timeout_sec") is not None:
        raw = req["timeout_sec"]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or float(raw) <= 0:
            return None, "timeout_sec 必须为正数"
        return float(raw), None
    if req.get("timeout_ms") is not None:
        raw_ms = req["timeout_ms"]
        if isinstance(raw_ms, bool) or not isinstance(raw_ms, (int, float)) or float(raw_ms) <= 0:
            return None, "timeout_ms 必须为正数"
        return float(raw_ms) / 1000.0, None
    return max(1.0, default_timeout_ms / 1000.0), None


async def handle_chat(
    req: Mapping[str, Any],
    *,
    gateway: Any,
    gateway_url: str,
    gateway_url_error: str | None,
    default_group: str,
    default_timeout_ms: int,
    secret: str | None,
    stream_sink: Any = None,
) -> CapabilityResult:
    """Execute `llm.chat.v1` against OpenAI-compatible `/v1/chat/completions`."""
    if not isinstance(req, Mapping):
        return fail_result("llm.chat.v1 请求必须为 JSON 对象", error_code="invalid_argument", secret=secret)
    if gateway_url_error:
        return fail_result(gateway_url_error, error_code="invalid_argument", secret=secret)

    messages: list[dict[str, Any]] = []
    sys_prompt = req.get("system_prompt")
    if isinstance(sys_prompt, str) and sys_prompt.strip():
        messages.append({"role": "system", "content": sys_prompt.strip()})

    raw_messages = req.get("messages")
    if raw_messages is not None:
        if not isinstance(raw_messages, list) or not raw_messages:
            return fail_result("llm.chat.v1 messages 必须为非空数组", error_code="invalid_argument", secret=secret)
        for item in raw_messages:
            if not isinstance(item, Mapping):
                return fail_result("messages 元素必须为对象", error_code="invalid_argument", secret=secret)
            role = str(item.get("role") or "").strip()
            content = item.get("content")
            if not role or not isinstance(content, (str, list)) or (isinstance(content, str) and not content.strip()):
                return fail_result("messages 每条消息均须包含有效的 role 与非空 content", error_code="invalid_argument", secret=secret)
            messages.append({"role": role, "content": content})
    elif isinstance(req.get("prompt"), str) and req["prompt"].strip():
        messages.append({"role": "user", "content": req["prompt"]})
    else:
        return fail_result("llm.chat.v1 需要非空的 messages 或 prompt", error_code="invalid_argument", secret=secret)

    resp_fmt, fmt_err = _build_response_format(req)
    if fmt_err:
        return fail_result(fmt_err, error_code="invalid_argument", secret=secret)
    timeout_sec, to_err = _resolve_timeout_sec(req, default_timeout_ms)
    if to_err or timeout_sec is None:
        return fail_result(to_err or "无效超时配置", error_code="invalid_argument", secret=secret)

    route_used = str(req.get("model") or req.get("route_override") or f"group/{default_group}").strip() or f"group/{default_group}"
    want_stream = bool(req.get("stream", False)) and resp_fmt is None
    body: dict[str, Any] = {"model": route_used, "messages": messages, "stream": want_stream}

    if req.get("temperature") is not None:
        temp = req["temperature"]
        if isinstance(temp, bool) or not isinstance(temp, (int, float)):
            return fail_result("temperature 必须为数值", error_code="invalid_argument", secret=secret)
        body["temperature"] = float(temp)
    if req.get("max_tokens") is not None:
        mt = req["max_tokens"]
        if isinstance(mt, bool) or not isinstance(mt, int) or mt <= 0:
            return fail_result("max_tokens 必须为正整数", error_code="invalid_argument", secret=secret)
        body["max_tokens"] = mt
    if isinstance(req.get("reasoning_effort"), str) and req["reasoning_effort"].strip():
        body["reasoning_effort"] = req["reasoning_effort"].strip()
    if resp_fmt is not None:
        body["response_format"] = resp_fmt

    res = await gateway.chat_completions(
        gateway_url, body, route_used=route_used, secret=secret, timeout_sec=timeout_sec, stream_sink=stream_sink
    )
    if not res.ok:
        return res
    if resp_fmt is not None:
        parsed = _parse_structured_json(str(res.data.get("reply") or ""))
        updated = {**res.data, "structured": parsed, "json_valid": parsed is not None}
        if parsed is not None:
            updated["parsed_json"] = parsed
        return CapabilityResult.ok(updated)
    return res


def _normalize_translate_segments(req: Mapping[str, Any]) -> tuple[bool, list[dict[str, Any]], str | None]:
    batch_raw = next((req[k] for k in ("texts", "segments", "items") if k in req and req[k] is not None), None)
    if batch_raw is None:
        raw_text = req.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            return False, [], "llm.translate.v1 需要非空的 text 或 texts"
        return False, [{"index": 0, "id": "0", "text": raw_text}], None
    if not isinstance(batch_raw, list) or not batch_raw:
        return True, [], "批量翻译的 texts/segments 必须为非空数组"
    segments: list[dict[str, Any]] = []
    for idx, entry in enumerate(batch_raw):
        if isinstance(entry, str):
            if not entry.strip():
                return True, [], f"第 {idx} 段文本为空"
            segments.append({"index": idx, "id": str(idx), "text": entry})
        elif isinstance(entry, Mapping):
            seg_text = entry.get("text")
            if not isinstance(seg_text, str) or not seg_text.strip():
                return True, [], f"第 {idx} 段缺少非空 text"
            segments.append({
                "index": idx,
                "id": str(entry.get("id") or idx),
                "text": seg_text,
                "context": str(entry.get("context") or ""),
                "context_before": str(entry.get("context_before") or ""),
                "context_after": str(entry.get("context_after") or ""),
            })
        else:
            return True, [], f"第 {idx} 段格式无效"
    return True, segments, None


async def handle_translate(
    req: Mapping[str, Any],
    *,
    call_chat: ChatCaller,
    secret: str | None,
) -> CapabilityResult:
    """Execute `llm.translate.v1` for single or batch text with glossary locking."""
    if not isinstance(req, Mapping):
        return fail_result("llm.translate.v1 请求必须为 JSON 对象", error_code="invalid_argument", secret=secret)
    is_batch, segments, seg_err = _normalize_translate_segments(req)
    if seg_err:
        return fail_result(seg_err, error_code="invalid_argument", secret=secret)

    target_lang = str(req.get("target_language") or req.get("target_lang") or "zh-CN").strip() or "zh-CN"
    source_lang = str(req.get("source_language") or req.get("source_lang") or "auto").strip() or "auto"
    shared_ctx = str(req.get("context") or "").strip()
    before_global = str(req.get("context_before") or "").strip()
    after_global = str(req.get("context_after") or "").strip()
    bilingual_notes = bool(req.get("bilingual_notes", False))

    glossary_pairs, glossary_str = _format_glossary(req.get("glossary"))
    sys_parts = ["You are a precise technical and literary translator."]
    if bool(req.get("preserve_formatting", True)):
        sys_parts.append("Preserve paragraph breaks, inline markup, code/identifiers, and punctuation.")
    sys_parts.append(
        "Output <translation>...</translation> followed by concise <notes>...</notes>."
        if bilingual_notes
        else "Output ONLY the translated target segment inside <translation>...</translation> tags."
    )
    sys_prompt = " ".join(sys_parts)
    if glossary_str:
        sys_prompt += "\n\nMandatory Glossary (lock these terms strictly to the exact target strings below):\n" + glossary_str

    results: list[dict[str, Any]] = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    served_model = route_group = last_notes = ""

    if is_batch and len(segments) > 1 and not bilingual_notes:
        batch_sys_parts = ["You are a precise technical and literary translator."]
        if bool(req.get("preserve_formatting", True)):
            batch_sys_parts.append(
                "Preserve paragraph breaks, inline markup, code/identifiers, and punctuation."
            )
        batch_sys_parts.append(
            "Output ONLY a JSON array of translated strings in the same order and length "
            "as the input segments. No commentary, no markdown fences."
        )
        batch_sys_prompt = " ".join(batch_sys_parts)
        if glossary_str:
            batch_sys_prompt += (
                "\n\nMandatory Glossary (lock these terms strictly to the exact target strings below):\n"
                + glossary_str
            )
        batch_res = await call_chat(
            _build_batch_translate_request(
                segments,
                sys_prompt=batch_sys_prompt,
                target_lang=target_lang,
                source_lang=source_lang,
                shared_ctx=shared_ctx,
                before_global=before_global,
                after_global=after_global,
                req=req,
            ),
            None,
        )
        if not batch_res.ok:
            return batch_res
        parsed_batch = _parse_batch_translations(
            str(batch_res.data.get("reply") or ""), len(segments)
        )
        if parsed_batch is not None:
            if isinstance(batch_res.data.get("usage"), Mapping):
                for ukey in total_usage:
                    if isinstance(batch_res.data["usage"].get(ukey), int):
                        total_usage[ukey] += batch_res.data["usage"][ukey]
            served_model = str(
                batch_res.data.get("served_model") or batch_res.data.get("model") or served_model
            )
            route_group = str(
                batch_res.data.get("route_group")
                or batch_res.data.get("route_used")
                or route_group
            )
            results = [
                {
                    "index": seg["index"],
                    "id": seg["id"],
                    "source": seg["text"],
                    "translation": translated,
                }
                for seg, translated in zip(segments, parsed_batch)
            ]

    if not results:
        for i, seg in enumerate(segments):
            seg_before = seg.get("context_before") or (segments[i - 1]["text"] if is_batch and i > 0 else before_global)
            seg_after = seg.get("context_after") or (segments[i + 1]["text"] if is_batch and i + 1 < len(segments) else after_global)
            seg_ctx = seg.get("context") or shared_ctx
            user_lines = [f"Source language: {source_lang}", f"Target language: {target_lang}"]
            if seg_ctx:
                user_lines.append(f"<context>\n{seg_ctx}\n</context>")
            if seg_before:
                user_lines.append(f"<context_before>\n{seg_before}\n</context_before>")
            user_lines.append(f"<target_text>\n{seg['text']}\n</target_text>")
            if seg_after:
                user_lines.append(f"<context_after>\n{seg_after}\n</context_after>")

            chat_req: dict[str, Any] = {
                "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "\n".join(user_lines)}],
                "temperature": req.get("temperature", 0.2),
            }
            for opt_key in ("max_tokens", "reasoning_effort", "route_override", "model", "timeout_ms", "timeout_sec"):
                if req.get(opt_key) is not None:
                    chat_req[opt_key] = req[opt_key]

            chat_res = await call_chat(chat_req, None)
            if not chat_res.ok:
                return chat_res
            raw_reply = str(chat_res.data.get("reply") or "")
            translated = _extract_tag(raw_reply, "translation") or raw_reply.strip()
            if bilingual_notes:
                last_notes = _extract_tag(raw_reply, "notes")
            served_model = str(chat_res.data.get("served_model") or chat_res.data.get("model") or served_model)
            route_group = str(chat_res.data.get("route_group") or chat_res.data.get("route_used") or route_group)
            if isinstance(chat_res.data.get("usage"), Mapping):
                for ukey in total_usage:
                    if isinstance(chat_res.data["usage"].get(ukey), int):
                        total_usage[ukey] += chat_res.data["usage"][ukey]
            results.append({"index": seg["index"], "id": seg["id"], "source": seg["text"], "translation": translated})

    joined = "\n".join(seg["text"] for seg in segments)
    applied = [{"source": s, "target": d} for s, d in glossary_pairs if s in joined]
    base_data: dict[str, Any] = {
        "source_language": source_lang,
        "source_lang": source_lang,
        "target_language": target_lang,
        "target_lang": target_lang,
        "glossary_locked": bool(glossary_pairs),
        "glossary_applied": applied,
        "served_model": served_model,
        "model": served_model,
        "route_group": route_group,
        "usage": total_usage,
    }
    if is_batch:
        return CapabilityResult.ok({
            **base_data,
            "translations": [r["translation"] for r in results],
            "results": results,
            "count": len(results),
        })
    base_data.update({"translation": results[0]["translation"], "reply": results[0]["translation"]})
    if last_notes:
        base_data["notes"] = last_notes
    return CapabilityResult.ok(base_data)
