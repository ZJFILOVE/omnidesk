"""Bilingual translation (skipping code & formulas) and AI companion summary for plugin-v2ex."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from omnidesk_sdk import CapabilityResult, CommentNode, PluginContext, TextArticle, TextParagraph

if __package__:
    from .v2ex_security import redact_sensitive
else:
    from v2ex_security import redact_sensitive

_SKIP_PARAGRAPH_KINDS = frozenset({"code", "formula", "image"})
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_MAX_COMMENT_TRANSLATIONS = 30
_BATCH_CHUNK_SIZE = 20


def looks_mostly_chinese(text: str) -> bool:
    """Return True if text contains predominantly CJK characters compared to ASCII letters."""
    if not text:
        return False
    cjk = len(_CJK_RE.findall(text))
    ascii_letters = sum(1 for ch in text if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))
    return cjk > 0 and cjk >= ascii_letters


def _needs_translation(text: str, target_lang: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    if target_lang.strip().lower().startswith("zh") and looks_mostly_chinese(cleaned):
        return False
    return True


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

    single = data.get("translation") or data.get("translated") or data.get("reply") or data.get("content")
    if isinstance(single, str) and single.strip():
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", single.strip())
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


async def _translate_via_chat_fallback(
    ctx: PluginContext,
    texts: Sequence[str],
    *,
    title: str,
    target_lang: str,
) -> tuple[list[str] | None, str]:
    if not ctx.capabilities.has("llm.chat.v1"):
        return None, "llm.translate.v1_unavailable"

    results: list[str] = []
    for txt in texts:
        res = await ctx.capabilities.call(
            "llm.chat.v1",
            {
                "action": "complete",
                "domain": "text",
                "context": title,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"Translate accurately into {target_lang}. "
                            "Preserve technical terms, identifiers, and formatting. Output only the translation."
                        ),
                    },
                    {"role": "user", "content": txt},
                ],
                "temperature": 0.2,
            },
        )
        if not res.ok or not isinstance(res.data, Mapping):
            code = str((res.data or {}).get("error_code") or "error")
            return None, f"llm.chat.v1_failed:{code}"
        translated = ""
        for k in ("translation", "translated", "reply", "content", "text"):
            val = res.data.get(k)
            if isinstance(val, str) and val.strip():
                translated = val.strip()
                break
        if not translated:
            return None, "llm.chat.v1_empty_response"
        results.append(translated)
    return results, ""


async def _translate_texts(
    ctx: PluginContext,
    texts: Sequence[str],
    *,
    title: str,
    target_lang: str,
) -> tuple[list[str] | None, str]:
    if not texts:
        return [], ""
    if not ctx.capabilities.has("llm.translate.v1"):
        return await _translate_via_chat_fallback(ctx, texts, title=title, target_lang=target_lang)

    results: list[str] = []
    for start in range(0, len(texts), _BATCH_CHUNK_SIZE):
        chunk = list(texts[start : start + _BATCH_CHUNK_SIZE])
        req: dict[str, Any] = {
            "action": "translate_batch" if len(chunk) > 1 else "translate",
            "domain": "text",
            "context": title,
            "source_lang": "auto",
            "source_language": "auto",
            "target_lang": target_lang,
            "target_language": target_lang,
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
        parsed = _extract_batch_translations(res.data, len(chunk))
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
    """Translate article title and non-code/non-formula paragraphs via `llm.translate.v1`."""
    lang = (target_lang or "zh-CN").strip() or "zh-CN"
    meta: dict[str, Any] = {
        "translated": False,
        "translation_degraded": False,
        "degraded_reason": "",
        "target_lang": lang,
    }
    if not auto_translate:
        return article, meta
    if ctx is None or (not ctx.capabilities.has("llm.translate.v1") and not ctx.capabilities.has("llm.chat.v1")):
        meta["translation_degraded"] = True
        meta["degraded_reason"] = "llm.translate.v1_unavailable"
        return article, meta

    texts_to_translate: list[str] = []
    need_title = not article.title_zh and _needs_translation(article.title, lang)
    if need_title:
        texts_to_translate.append(article.title)

    translatable_indices: list[int] = []
    for idx, p in enumerate(article.paragraphs):
        if p.kind in _SKIP_PARAGRAPH_KINDS or p.translated.strip():
            continue
        if not _needs_translation(p.original, lang):
            continue
        translatable_indices.append(idx)
        texts_to_translate.append(p.original)

    if not texts_to_translate:
        meta["translated"] = bool(article.title_zh or any(p.translated for p in article.paragraphs))
        return article, meta

    translated_list, reason = await _translate_texts(
        ctx, texts_to_translate, title=article.title, target_lang=lang
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
) -> tuple[list[CommentNode], dict[str, Any]]:
    """Translate non-code reply bodies recursively when translation is enabled."""
    lang = (target_lang or "zh-CN").strip() or "zh-CN"
    meta: dict[str, Any] = {
        "translated": False,
        "translation_degraded": False,
        "degraded_reason": "",
        "target_lang": lang,
    }
    if not auto_translate or not comments:
        return comments, meta
    if ctx is None or (not ctx.capabilities.has("llm.translate.v1") and not ctx.capabilities.has("llm.chat.v1")):
        meta["translation_degraded"] = True
        meta["degraded_reason"] = "llm.translate.v1_unavailable"
        return comments, meta

    flat_texts: list[str] = []

    def _collect(nodes: Sequence[CommentNode]) -> None:
        for n in nodes:
            body = n.body.strip()
            is_only_code = body.startswith("```") and body.endswith("```") and body.count("```") == 2
            if (
                body
                and not is_only_code
                and not n.translated_body
                and _needs_translation(body, lang)
                and len(flat_texts) < _MAX_COMMENT_TRANSLATIONS
            ):
                flat_texts.append(body)
            if n.replies:
                _collect(n.replies)

    _collect(comments)
    if not flat_texts:
        return comments, meta

    translated, reason = await _translate_texts(ctx, flat_texts, title=title, target_lang=lang)
    if translated is None:
        meta["translation_degraded"] = True
        meta["degraded_reason"] = reason
        return comments, meta

    mapping = dict(zip(flat_texts, translated))

    def _apply(nodes: Sequence[CommentNode]) -> list[CommentNode]:
        out: list[CommentNode] = []
        for n in nodes:
            tr = mapping.get(n.body.strip(), n.translated_body)
            replies = _apply(n.replies) if n.replies else []
            out.append(n.model_copy(update={"translated_body": tr, "replies": replies}))
        return out

    meta["translated"] = True
    return _apply(comments), meta


def build_thread_companion_context(article: TextArticle) -> tuple[str, int, int]:
    """Build bounded thread context for AI summarization while counting code & formula blocks."""
    code_blocks = 0
    formula_blocks = 0
    lines: list[str] = [f"标题: {article.title}"]
    if article.authors:
        lines.append(f"作者: {article.authors}")
    if article.subtitle:
        lines.append(f"元信息: {article.subtitle}")
    lines.append("\n主题内容:")

    char_count = sum(len(x) for x in lines)
    for p in article.paragraphs:
        if p.kind == "heading":
            seg = f"\n## {p.original}"
        elif p.kind == "code":
            code_blocks += 1
            seg = f"```\n{p.original}\n```"
        elif p.kind == "formula":
            formula_blocks += 1
            seg = p.original
        elif p.kind == "quote":
            seg = f"> {p.original}"
        else:
            seg = p.original
        lines.append(seg)
        char_count += len(seg) + 1
        if char_count > 12000:
            break

    if article.comments:
        lines.append("\n精选回复:")
        for c in article.comments[:8]:
            badge = f" · {c.role_badge}" if c.role_badge else ""
            seg = f"- [{c.author}{badge} | ♥{c.score}] {c.body}"
            lines.append(seg)
            char_count += len(seg) + 1
            for r in c.replies[:3]:
                rseg = f"  - [回复 {r.author}] {r.body}"
                lines.append(rseg)
                char_count += len(rseg) + 1
            if char_count > 16000:
                break

    return "\n".join(lines) + "\n", code_blocks, formula_blocks


async def summarize_article_via_llm(
    ctx: PluginContext | None,
    article: TextArticle,
    *,
    question: str = "",
    secrets: Sequence[str] = (),
) -> CapabilityResult:
    """Summarize or answer questions about a V2EX topic via `llm.chat.v1`."""
    if ctx is None:
        return CapabilityResult.fail("PluginContext unavailable", error_code="service_offline")
    if not ctx.capabilities.has("llm.chat.v1"):
        return CapabilityResult.fail(
            "未检测到 llm.chat.v1 提供插件（请安装并启用 plugin-magpie-agent）",
            error_code="service_offline",
        )
    if not article.title.strip() and not article.paragraphs:
        return CapabilityResult.fail("无法总结空主题", error_code="invalid_argument")

    context_text, code_blocks, formula_blocks = build_thread_companion_context(article)
    clean_q = (question or "").strip()
    instruction = (
        f"请基于以下 V2EX 主题与评论讨论，准确回答用户问题：\n{clean_q}"
        if clean_q
        else "请总结这篇 V2EX 帖子：1) 核心问题或分享结论；2) 关键方案与技术要点；3) 回复楼层中的高质量补充、共识或分歧。保留代码块与公式原貌。"
    )

    llm_res = await ctx.capabilities.call(
        "llm.chat.v1",
        {
            "domain": "text",
            "context": article.title,
            "system_prompt": "你是 OmniDesk V2EX 技术社区伴读助手。总结需客观精炼、忠于原帖与评论事实，保留代码与公式。",
            "prompt": f"{instruction}\n\n---\n{context_text}",
            "temperature": 0.3,
        },
    )
    if not llm_res.ok:
        err_msg = redact_sensitive(llm_res.error or "llm.chat.v1 调用失败", secrets)
        err_code = str((llm_res.data or {}).get("error_code") or "service_offline")
        return CapabilityResult.fail(err_msg, error_code=err_code)

    reply = ""
    if isinstance(llm_res.data, Mapping):
        for k in ("reply", "text", "content", "answer"):
            val = llm_res.data.get(k)
            if isinstance(val, str) and val.strip():
                reply = val
                break
    if not reply:
        return CapabilityResult.fail("llm.chat.v1 返回了空内容", error_code="service_offline")

    return CapabilityResult.ok(
        {
            "article_id": article.id,
            "title": article.title,
            "reply": reply,
            "code_blocks": code_blocks,
            "formula_blocks": formula_blocks,
            "comment_count": len(article.comments),
        }
    )
