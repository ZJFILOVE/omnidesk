"""Bilingual post/comment translation (skipping code & formulas) and AI companion reading."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from omnidesk_sdk import CapabilityResult, CommentNode, PluginContext, TextArticle, TextParagraph

if __package__:
    from .reddit_security import redact_reddit_secrets
else:
    from reddit_security import redact_reddit_secrets

_SKIP_PARAGRAPH_KINDS = frozenset({"code", "formula", "image"})
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _looks_mostly_chinese(text: str) -> bool:
    if not text:
        return False
    cjk_count = len(_CJK_RE.findall(text))
    ascii_letters = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    return cjk_count > 0 and cjk_count >= ascii_letters


def _is_pure_code_or_formula(text: str) -> bool:
    s = text.strip()
    if not s:
        return True
    if (s.startswith("```") and s.endswith("```") and s.count("```") == 2) or (
        s.startswith("~~~") and s.endswith("~~~") and s.count("~~~") == 2
    ):
        return True
    if (s.startswith("$$") and s.endswith("$$") and len(s) >= 4) or (
        s.startswith(r"\[") and s.endswith(r"\]") and len(s) >= 4
    ):
        return True
    if len(s) > 2 and s.startswith("$") and s.endswith("$") and s[1] != "$" and "\n" not in s:
        return True
    return False


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
    """Translate title & prose blocks via `llm.translate.v1`, strictly skipping code and formula blocks."""
    meta = {"translated": False, "translation_degraded": False, "degraded_reason": ""}
    if not auto_translate:
        return article, meta
    if ctx is None or not ctx.capabilities.has("llm.translate.v1"):
        meta["translation_degraded"] = True
        meta["degraded_reason"] = "llm.translate.v1_unavailable"
        return article, meta

    translatable_indices: list[int] = []
    texts_to_translate: list[str] = []
    need_title = bool(article.title.strip()) and not article.title_zh and not _looks_mostly_chinese(article.title)
    if need_title:
        texts_to_translate.append(article.title)

    for idx, p in enumerate(article.paragraphs):
        if p.kind in _SKIP_PARAGRAPH_KINDS or not p.original.strip() or p.translated.strip():
            continue
        if _looks_mostly_chinese(p.original):
            continue
        translatable_indices.append(idx)
        texts_to_translate.append(p.original)

    if not texts_to_translate:
        return article, meta

    translated_list, reason = await _translate_texts(
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
    max_nodes: int = 50,
) -> tuple[list[CommentNode], bool, str]:
    """Recursively translate non-code/non-formula comments; returns `(comments, degraded, reason)`."""
    if not auto_translate or not comments:
        return comments, False, ""
    if ctx is None or not ctx.capabilities.has("llm.translate.v1"):
        return comments, True, "llm.translate.v1_unavailable"

    flat_texts: list[str] = []
    seen_texts: set[str] = set()

    def _collect(nodes: Sequence[CommentNode]) -> None:
        for n in nodes:
            body = n.body.strip()
            if (
                body
                and not _is_pure_code_or_formula(body)
                and not n.translated_body
                and not _looks_mostly_chinese(body)
                and body not in seen_texts
                and len(flat_texts) < max_nodes
            ):
                seen_texts.add(body)
                flat_texts.append(body)
            if n.replies:
                _collect(n.replies)

    _collect(comments)
    if not flat_texts:
        return comments, False, ""

    translated, reason = await _translate_texts(ctx, flat_texts, title=title, target_lang=target_lang)
    if translated is None:
        return comments, True, reason

    mapping = dict(zip(flat_texts, translated))

    def _apply(nodes: Sequence[CommentNode]) -> list[CommentNode]:
        out: list[CommentNode] = []
        for n in nodes:
            tr = mapping.get(n.body.strip(), n.translated_body)
            replies = _apply(n.replies) if n.replies else []
            out.append(n.model_copy(update={"translated_body": tr, "replies": replies}))
        return out

    return _apply(comments), False, ""


def build_companion_context(article: TextArticle) -> tuple[str, int, int]:
    """Build bounded thread context preserving code/formula blocks and high-score comment trees."""
    code_blocks = 0
    formula_blocks = 0
    lines: list[str] = [f"标题: {article.title}"]
    if article.authors:
        lines.append(f"作者: {article.authors}")
    if article.subtitle:
        lines.append(f"版块与热度: {article.subtitle}")
    lines.append("\n正文:")

    char_count = sum(len(x) for x in lines)
    for p in article.paragraphs:
        if p.kind == "heading":
            seg = f"\n## {p.original}"
        elif p.kind == "code":
            code_blocks += 1
            lang = p.language or ""
            seg = f"```{lang}\n{p.original}\n```"
        elif p.kind == "formula":
            formula_blocks += 1
            seg = f"$$\n{p.original}\n$$"
        elif p.kind == "quote":
            seg = f"> {p.original}"
        else:
            seg = p.original
        lines.append(seg)
        char_count += len(seg) + 1
        if char_count > 12000:
            break

    if article.comments:
        lines.append("\n讨论树高赞评论:")
        budget = 10

        def _append_comments(nodes: Sequence[CommentNode], depth: int) -> None:
            nonlocal budget
            for c in nodes:
                if budget <= 0:
                    return
                budget -= 1
                indent = "  " * depth
                badge = f" · {c.role_badge}" if c.role_badge else ""
                lines.append(f"{indent}- [{c.author}{badge} | ▲{c.score}] {c.body}")
                if c.replies and depth < 4:
                    _append_comments(c.replies, depth + 1)

        _append_comments(article.comments, 0)

    return "\n".join(lines) + "\n", code_blocks, formula_blocks


async def run_companion_llm(
    ctx: PluginContext | None,
    article: TextArticle,
    *,
    mode: str = "summarize",
    question: str = "",
    secrets: Sequence[str] = (),
) -> CapabilityResult:
    """Summarize or answer reader questions on a Reddit thread via `llm.chat.v1`."""
    if ctx is None:
        return CapabilityResult.fail("PluginContext unavailable", error_code="service_offline")
    if not ctx.capabilities.has("llm.chat.v1"):
        return CapabilityResult.fail(
            "未检测到 llm.chat.v1 提供插件（请安装并启用 plugin-magpie-agent）",
            error_code="service_offline",
        )
    if not article.title.strip() and not article.paragraphs:
        return CapabilityResult.fail("Cannot run companion reading on empty Reddit thread", error_code="invalid_argument")

    ctx_text, code_blocks, formula_blocks = build_companion_context(article)
    clean_q = (question or "").strip()
    if mode == "ask" or clean_q:
        instruction = f"请结合以下 Reddit 帖子正文与多级讨论树回复，准确回答读者问题：\n{clean_q}"
    else:
        instruction = (
            "请作为技术社区伴读助手，提炼该 Reddit 讨论帖的：1) 核心技术背景与主旨；"
            "2) 楼主关键实验/代码要点；3) 评论区最高赞共识与反驳意见（标注 u/作者 与 OP/MOD 身份）。"
            "保留所有代码块与 LaTeX 公式。"
        )

    llm_res = await ctx.capabilities.call(
        "llm.chat.v1",
        {
            "domain": "text",
            "context": article.title,
            "prompt": f"{instruction}\n\n---\n{ctx_text}",
            "temperature": 0.3,
        },
    )
    if not llm_res.ok:
        err_msg = redact_reddit_secrets(llm_res.error or "llm.chat.v1 invocation failed", secrets)
        err_code = str((llm_res.data or {}).get("error_code") or "service_offline")
        return CapabilityResult.fail(err_msg, error_code=err_code)

    reply = ""
    if isinstance(llm_res.data, Mapping):
        for k in ("reply", "text", "content", "translation", "answer"):
            val = llm_res.data.get(k)
            if isinstance(val, str) and val.strip():
                reply = val
                break
    if not reply:
        return CapabilityResult.fail("llm.chat.v1 returned empty reply", error_code="service_offline")

    return CapabilityResult.ok(
        {
            "article_id": article.id,
            "title": article.title,
            "mode": mode,
            "reply": reply,
            "code_blocks": code_blocks,
            "formula_blocks": formula_blocks,
            "comment_count": len(article.comments),
        }
    )
