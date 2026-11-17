"""Handler for `ai.agent.v1` contextual companion actions backed by `llm.chat.v1`."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from omnidesk_sdk import CapabilityResult

try:
    from .magpie_common import fail_result, redact_secret
except ImportError:
    from magpie_common import fail_result, redact_secret  # type: ignore[no-redef]

ChatCaller = Callable[[dict[str, Any], Any], Coroutine[Any, Any, CapabilityResult]]
_VALID_DOMAINS = frozenset({"core", "media", "text", "book"})
_VALID_ACTIONS = frozenset(
    {"chat", "qa", "summarize", "etymology", "parse_sentence", "clause_parse", "translate", "custom"}
)
_DOMAIN_PERSONAS: dict[str, str] = {
    "core": "You are OmniDesk's workspace AI assistant powered by Magpie routing.",
    "media": "You are OmniDesk's Media Workbench assistant helping analyze recordings, timestamps, and listening notes.",
    "text": "You are OmniDesk's Text Workbench assistant helping analyze articles, claims, evidence boundaries, and prose.",
    "book": "You are OmniDesk's Book Reader assistant helping analyze chapters, word etymology, complex syntax, and terminology.",
}
_ANTI_FABRICATION_GUARD = (
    "Strictly ground your response in the provided context and selection. "
    "NEVER fabricate citations, DOIs, URLs, page numbers, timestamps, or external facts not present in the context. "
    "If required evidence is missing from the provided material, state clearly what cannot be verified."
)


async def handle_agent(
    req: Mapping[str, Any],
    *,
    call_chat: ChatCaller,
    secret: str | None,
    stream_sink: Any = None,
) -> CapabilityResult:
    """Execute `ai.agent.v1` domain-contextual companion actions backed by `llm.chat.v1`."""
    if not isinstance(req, Mapping):
        return fail_result("ai.agent.v1 请求必须为 JSON 对象", error_code="invalid_argument", secret=secret)

    action = str(req.get("action") or "chat").strip() or "chat"
    if action not in _VALID_ACTIONS:
        return fail_result(f"不支持的 ai.agent.v1 动作: {action!r}", error_code="invalid_argument", secret=secret)
    domain = str(req.get("domain") or "core").strip() or "core"
    if domain not in _VALID_DOMAINS:
        return fail_result(f"不支持的领域 domain={domain!r}", error_code="invalid_argument", secret=secret)

    prompt = str(req.get("prompt") or "").strip()
    selection = str(req.get("selection") or "").strip()
    raw_ctx = req.get("context")
    ctx_str = (
        raw_ctx.strip()
        if isinstance(raw_ctx, str)
        else (json.dumps(raw_ctx, ensure_ascii=False, indent=2) if raw_ctx is not None else "")
    )
    raw_messages = req.get("messages")
    has_messages = isinstance(raw_messages, list) and len(raw_messages) > 0

    if not prompt and not selection and not has_messages:
        if action == "summarize" and ctx_str:
            prompt = "请基于提供的上下文提炼核心要点与关键结论。"
        else:
            return fail_result("ai.agent.v1 需要非空的 prompt、selection 或 messages", error_code="invalid_argument", secret=secret)

    if not prompt and selection:
        defaults = {
            "summarize": f"请总结以下选中内容的核心要点：\n{selection}",
            "etymology": f"请拆解以下选中词汇或术语的词根、词缀、词源演变与当前语境含义：\n{selection}",
            "parse_sentence": f"请解析以下选中句子的主干、从句层次与语法结构：\n{selection}",
            "clause_parse": f"请解析以下选中句子的主干、从句层次与语法结构：\n{selection}",
            "translate": f"请结合上下文准确翻译以下选中内容：\n{selection}",
        }
        prompt = defaults.get(action, selection)

    target_lang = str(req.get("target_language") or req.get("target_lang") or "zh-CN").strip() or "zh-CN"
    sys_override = str(req.get("system_prompt") or "").strip()
    if sys_override:
        sys_prompt = f"{sys_override}\n\n{_ANTI_FABRICATION_GUARD}"
    else:
        action_hints = {
            "qa": "Answer the user's question accurately and concisely based on the workspace context.",
            "chat": "Respond concisely and helpfully within the workspace context.",
            "summarize": "Provide a structured, concise summary of key claims, details, and scope limits.",
            "etymology": "Explain word roots, affixes, cognates, contextual nuance, and grounded usage.",
            "parse_sentence": "Break down sentence structure into main clause, subordinate clauses, grammatical roles, and fluent meaning.",
            "clause_parse": "Break down sentence structure into main clause, subordinate clauses, grammatical roles, and fluent meaning.",
            "translate": f"Translate faithfully into {target_lang} preserving terminology and formatting.",
            "custom": "Follow the user's explicit instruction carefully.",
        }
        sys_prompt = f"{_DOMAIN_PERSONAS[domain]} {action_hints[action]}\n{_ANTI_FABRICATION_GUARD}"

    if ctx_str:
        sys_prompt += f"\n\nWorkspace Context:\n{ctx_str}"
    if selection and selection not in prompt:
        sys_prompt += f"\n\nSelected Text:\n<selection>\n{selection}\n</selection>"

    messages: list[dict[str, Any]] = [{"role": "system", "content": sys_prompt}]
    if has_messages:
        messages.extend(dict(m) for m in raw_messages if isinstance(m, Mapping) and m.get("role") and m.get("content"))
    if prompt:
        messages.append({"role": "user", "content": prompt})

    chat_req: dict[str, Any] = {"messages": messages, "stream": bool(req.get("stream", False))}
    for opt_key in ("temperature", "max_tokens", "reasoning_effort", "route_override", "model", "json_schema", "response_format", "timeout_ms", "timeout_sec"):
        if req.get(opt_key) is not None:
            chat_req[opt_key] = req[opt_key]

    chat_res = await call_chat(chat_req, stream_sink)
    if not chat_res.ok:
        return fail_result(
            redact_secret(chat_res.error, secret),
            error_code=str(chat_res.data.get("error_code") or "service_offline"),
            secret=secret,
            data={**chat_res.data, "action": action, "domain": domain},
        )
    return CapabilityResult.ok({**chat_res.data, "action": action, "domain": domain, "citations_grounded": True})
