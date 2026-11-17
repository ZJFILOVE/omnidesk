"""Bilingual translation (skipping code & LaTeX formulas) and AI companion reading via llm.*.v1."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from omnidesk_sdk import CapabilityResult, CommentNode, PluginContext, TextArticle, TextParagraph

if __package__:
    from .zhihu_errors import json_str, json_str_chain, redact_zhihu_secrets
else:
    from zhihu_errors import json_str, json_str_chain, redact_zhihu_secrets  # type: ignore[no-redef]

_SKIP_PARAGRAPH_KINDS = frozenset({"code", "formula", "image"})
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _looks_mostly_chinese(text: str) -> bool:
    if not text:
        return False
    cjk_count = len(_CJK_RE.findall(text))
    ascii_letters = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    return cjk_count > 0 and cjk_count >= ascii_letters


def _extract_batch_translations(data: Mapping[str, Any], expected: int) -> list[str] | None:
    for key in ("translations", "translated_texts", "results", "segments"):
        val = data.get(key)
        if isinstance(val, list) and len(val) == expected:
            out: list[str] = []
            for item in val:
                if isinstance(item, Mapping):
                    txt = str(item.get("translation") or item.get("translated") or item.get("text") or "").strip()
                else:
                    txt = str(item or "").strip()
                if not txt:
                    return None
                out.append(txt)
            return out
    reply = data.get("reply") or data.get("translated") or data.get("translation")
    if isinstance(reply, str) and reply.strip():
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list) and len(parsed) == expected:
                out = [
                    str(x.get("translation") or x.get("translated") or x.get("text") or "").strip()
                    if isinstance(x, Mapping)
                    else str(x or "").strip()
                    for x in parsed
                ]
                if all(out):
                    return out
        except json.JSONDecodeError:
            lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
            if len(lines) == expected:
                return lines
            if expected == 1:
                return [stripped]
    return None


async def _call_translate_batch(
    ctx: PluginContext,
    texts: Sequence[str],
    *,
    title: str,
    target_lang: str,
) -> tuple[list[str] | None, str]:
    if not texts:
        return [], ""
    if not ctx.capabilities.has("llm.translate.v1"):
        return None, "llm.translate.v1_unavailable"

    results: list[str] = []
    for start in range(0, len(texts), 20):
        chunk = list(texts[start : start + 20])
        req: dict[str, Any] = {
            "action": "translate_batch" if len(chunk) > 1 else "translate",
            "domain": "text",
            "context": title,
            "source_lang": "auto",
            "target_lang": target_lang,
            "preserve_formatting": True,
        }
        if len(chunk) == 1:
            req["text"] = chunk[0]
        else:
            req["texts"] = chunk
            req["segments"] = [{"id": str(i), "text": txt} for i, txt in enumerate(chunk)]

        res = await ctx.capabilities.call("llm.translate.v1", req)
        if not res.ok:
            code = str((res.data or {}).get("error_code") or "error")
            return None, f"llm.translate.v1_failed:{code}"
        parsed = _extract_batch_translations(res.data or {}, len(chunk))
        if parsed is None:
            return None, "llm.translate.v1_invalid_response"
        results.extend(parsed)
    return results, ""


async def translate_article_if_enabled(
    ctx: PluginContext | None,
    article: TextArticle,
    *,
    auto_translate: bool,
    target_lang: str = "zh-CN",
) -> tuple[TextArticle, dict[str, Any]]:
    """Translate article headings/paragraphs/quotes via `llm.translate.v1`, skipping code and formulas."""
    meta: dict[str, Any] = {"translated": False, "translation_degraded": False, "degraded_reason": ""}
    if not auto_translate:
        return article, meta
    if ctx is None or not ctx.capabilities.has("llm.translate.v1"):
        meta["translation_degraded"] = True
        meta["degraded_reason"] = "llm.translate.v1_unavailable"
        return article, meta

    target_is_zh = target_lang.strip().lower().startswith("zh")
    translatable_indices: list[int] = []
    texts_to_translate: list[str] = []

    need_title = (
        bool(article.title.strip())
        and not article.title_zh
        and not (target_is_zh and _looks_mostly_chinese(article.title))
    )
    if need_title:
        texts_to_translate.append(article.title)

    for idx, p in enumerate(article.paragraphs):
        if p.kind in _SKIP_PARAGRAPH_KINDS or not p.original.strip() or p.translated.strip():
            continue
        if target_is_zh and _looks_mostly_chinese(p.original):
            continue
        translatable_indices.append(idx)
        texts_to_translate.append(p.original)

    if not texts_to_translate:
        return article, meta

    translated_list, reason = await _call_translate_batch(
        ctx, texts_to_translate, title=article.title, target_lang=target_lang
    )
    if translated_list is None:
        meta["translation_degraded"] = True
        meta["degraded_reason"] = reason
        return article, meta

    cursor = 0
    new_title_zh = article.title_zh
    if need_title:
        new_title_zh = translated_list[0]
        cursor = 1

    new_paragraphs: list[TextParagraph] = list(article.paragraphs)
    for idx, tr_text in zip(translatable_indices, translated_list[cursor:]):
        new_paragraphs[idx] = new_paragraphs[idx].model_copy(update={"translated": tr_text})

    meta["translated"] = True
    return article.model_copy(update={"title_zh": new_title_zh, "paragraphs": new_paragraphs}), meta


async def translate_comments_if_enabled(
    ctx: PluginContext | None,
    comments: list[CommentNode],
    *,
    auto_translate: bool,
    title: str = "",
    target_lang: str = "zh-CN",
) -> list[CommentNode]:
    """Recursively translate non-code comment bodies when `auto_translate` is enabled."""
    if not auto_translate or not comments or ctx is None or not ctx.capabilities.has("llm.translate.v1"):
        return comments

    target_is_zh = target_lang.strip().lower().startswith("zh")
    flat_texts: list[str] = []

    def _collect(nodes: Sequence[CommentNode]) -> None:
        for n in nodes:
            body = n.body.strip()
            is_only_code = body.startswith("```") and body.endswith("```") and body.count("```") == 2
            if (
                body
                and not is_only_code
                and not n.translated_body
                and not (target_is_zh and _looks_mostly_chinese(body))
                and len(flat_texts) < 30
            ):
                flat_texts.append(body)
            if n.replies:
                _collect(n.replies)

    _collect(comments)
    if not flat_texts:
        return comments

    translated, _ = await _call_translate_batch(ctx, flat_texts, title=title, target_lang=target_lang)
    if translated is None:
        return comments

    mapping = dict(zip(flat_texts, translated))

    def _apply(nodes: Sequence[CommentNode]) -> list[CommentNode]:
        out: list[CommentNode] = []
        for n in nodes:
            tr = mapping.get(n.body.strip(), n.translated_body)
            replies = _apply(n.replies) if n.replies else []
            out.append(n.model_copy(update={"translated_body": tr, "replies": replies}))
        return out

    return _apply(comments)


def build_article_companion_context(article: TextArticle) -> tuple[str, int]:
    """Build bounded companion context matching v1 formatting and counting LaTeX formulas."""
    formula_count = 0
    lines: list[str] = [f"标题: {article.title}"]
    if article.authors:
        lines.append(f"作者: {article.authors}")
    if article.subtitle:
        lines.append(f"来源: {article.subtitle}")
    lines.append("\n正文:")

    char_count = sum(len(x) for x in lines)
    for p in article.paragraphs:
        if p.kind == "heading":
            seg = f"\n## {p.original}"
        elif p.kind == "formula":
            formula_count += 1
            seg = f"$$\n{p.original}\n$$"
        elif p.kind == "code":
            seg = f"```\n{p.original}\n```"
        elif p.kind == "quote":
            seg = f"> {p.original}"
        else:
            seg = p.original
        lines.append(seg)
        char_count += len(seg) + 1
        if char_count > 12000:
            break

    if article.comments:
        lines.append("\n高赞评论摘录:")
        for c in article.comments[:5]:
            badge = f" · {c.role_badge}" if c.role_badge else ""
            lines.append(f"- [{c.author}{badge} | ▲{c.score}] {c.body}")

    return "\n".join(lines) + "\n", formula_count


async def run_companion_llm(
    ctx: PluginContext | None,
    article: TextArticle,
    req: Mapping[str, Any],
    *,
    secrets: Sequence[str] = (),
) -> CapabilityResult:
    """Execute companion summarization / key points / formula explanation / Q&A via `llm.chat.v1`."""
    if ctx is None:
        return CapabilityResult.fail("PluginContext unavailable", error_code="service_offline")
    if not ctx.capabilities.has("llm.chat.v1"):
        return CapabilityResult.fail(
            "未检测到 llm.chat.v1 提供插件（请安装并启用 plugin-magpie-agent）",
            error_code="service_offline",
        )
    if not article.title.strip() and not article.paragraphs:
        return CapabilityResult.fail("Cannot run companion reading on empty article", error_code="invalid_argument")

    mode = json_str(req, "mode", "") or json_str(req, "action", "") or "summarize"
    if mode == "companion":
        mode = "summarize"
    question = json_str_chain(req, ("question", "prompt"))

    context_text, formula_count = build_article_companion_context(article)
    if mode == "key_points":
        instruction = (
            "请提炼这篇知乎问答/专栏的核心论点、关键证据与结论（分点精炼输出），"
            "保留所有 LaTeX 公式原貌（$...$ / $$...$$）。"
        )
    elif mode == "explain_formulas":
        instruction = (
            "请逐一解释文中出现的核心数学公式与符号含义，结合上下文说明推导直觉与适用条件，保持 LaTeX 格式规范。"
        )
    elif mode == "ask" or question:
        if not question:
            return CapabilityResult.fail("ask 模式需要提供 question 或 prompt 参数", error_code="invalid_argument")
        instruction = f"请基于以下知乎文章正文与评论区内容，准确回答读者的问题：\n{question}"
    else:
        instruction = (
            "请作为深度阅读伴读助手，给出：1) 一句话主旨；2) 核心脉络与技术/逻辑要点；"
            "3) 评论区争议或补充观点；4) 值得进一步思考的问题。保留 LaTeX 公式。"
        )

    llm_req = {
        "domain": "text",
        "context": article.title,
        "system_prompt": (
            "你是 OmniDesk 知乎深度阅读伴读助手。回答需克制、结构清晰、忠于原文，"
            "切勿编造文中未提及的引用，公式请保留 LaTeX 源码。"
        ),
        "prompt": f"{instruction}\n\n---\n{context_text}",
        "temperature": 0.3,
    }
    llm_res = await ctx.capabilities.call("llm.chat.v1", llm_req)
    if not llm_res.ok:
        err_msg = redact_zhihu_secrets(llm_res.error or "llm.chat.v1 invocation failed", secrets)
        err_code = str((llm_res.data or {}).get("error_code") or "service_offline")
        return CapabilityResult.fail(err_msg, error_code=err_code)

    reply = ""
    if isinstance(llm_res.data, Mapping):
        for k in ("reply", "text", "content", "translation"):
            val = llm_res.data.get(k)
            if isinstance(val, str) and val.strip():
                reply = val
                break
    if not reply:
        return CapabilityResult.fail("llm.chat.v1 returned empty reply", error_code="service_offline")

    model = ""
    if isinstance(llm_res.data, Mapping):
        model = str(llm_res.data.get("model") or "")

    return CapabilityResult.ok(
        {
            "article_id": article.id,
            "title": article.title,
            "mode": mode,
            "reply": reply,
            "model": model,
            "formula_count": formula_count,
            "comment_count": len(article.comments),
        }
    )
