"""Optional bilingual translation helper consuming `llm.translate.v1` or `llm.chat.v1`."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from omnidesk_sdk import CommentNode, PluginContext, TextArticle, TextFeedItem, TextParagraph

_SKIP_KINDS = frozenset({"code", "formula", "image"})
_BATCH_SIZE = 20


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
    if expected == 1:
        for key in ("translation", "translated", "reply"):
            single = data.get(key)
            if isinstance(single, str) and single.strip():
                return [single.strip()]
    return None


def _parse_chat_json_array(reply: str, expected: int) -> list[str] | None:
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list) and len(parsed) == expected:
            out = [str(x or "").strip() for x in parsed]
            if all(out):
                return out
    except json.JSONDecodeError:
        pass
    if expected == 1:
        return [stripped]
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if len(lines) == expected:
        return lines
    return None


async def _translate_strings(
    ctx: PluginContext | None,
    texts: Sequence[str],
    *,
    target_lang: str = "zh-CN",
    context_title: str = "",
) -> list[str] | None:
    """Translate strings via `llm.translate.v1` (or fallback `llm.chat.v1`), never fabricating output."""
    if ctx is None or not texts:
        return None
    clean_texts = [str(t) for t in texts]
    results: list[str] = []

    if ctx.capabilities.has("llm.translate.v1"):
        for start in range(0, len(clean_texts), _BATCH_SIZE):
            chunk = clean_texts[start : start + _BATCH_SIZE]
            req: dict[str, Any] = {
                "action": "translate_batch" if len(chunk) > 1 else "translate",
                "domain": "text",
                "source_lang": "auto",
                "target_lang": target_lang,
                "context": context_title,
                "texts": chunk,
                "segments": [{"id": str(i), "text": txt} for i, txt in enumerate(chunk)],
            }
            if len(chunk) == 1:
                req["text"] = chunk[0]
            try:
                res = await ctx.capabilities.call("llm.translate.v1", req)
            except Exception:
                return None
            if not res.ok or not isinstance(res.data, Mapping):
                return None
            batch_out = _extract_batch_translations(res.data, len(chunk))
            if batch_out is None:
                return None
            results.extend(batch_out)
        return results

    if ctx.capabilities.has("llm.chat.v1"):
        for start in range(0, len(clean_texts), _BATCH_SIZE):
            chunk = clean_texts[start : start + _BATCH_SIZE]
            prompt = (
                f"Translate each item in this JSON array into {target_lang}. "
                "Preserve technical terms, inline code, and LaTeX math unchanged. "
                "Return ONLY a JSON array of translated strings in the exact same order.\n"
                f"Context: {context_title}\n"
                f"{json.dumps(chunk, ensure_ascii=False)}"
            )
            try:
                res = await ctx.capabilities.call(
                    "llm.chat.v1",
                    {"action": "chat", "domain": "text", "prompt": prompt},
                )
            except Exception:
                return None
            if not res.ok or not isinstance(res.data, Mapping):
                return None
            batch_out = _parse_chat_json_array(str(res.data.get("reply") or ""), len(chunk))
            if batch_out is None:
                return None
            results.extend(batch_out)
        return results

    return None


def _collect_comment_targets(nodes: Sequence[CommentNode], out: list[CommentNode]) -> None:
    for node in nodes:
        if node.body.strip() and not node.translated_body:
            out.append(node)
        if node.replies:
            _collect_comment_targets(node.replies, out)


async def translate_comments(
    ctx: PluginContext | None,
    comments: Sequence[CommentNode],
    *,
    target_lang: str = "zh-CN",
    context_title: str = "",
) -> list[CommentNode]:
    """Translate comment bodies recursively via LLM capability, degrading cleanly if unavailable."""
    nodes_copy = [c.model_copy(deep=True) for c in comments]
    targets: list[CommentNode] = []
    _collect_comment_targets(nodes_copy, targets)
    if not targets:
        return nodes_copy
    translated = await _translate_strings(
        ctx,
        [n.body for n in targets],
        target_lang=target_lang,
        context_title=context_title,
    )
    if translated is None or len(translated) != len(targets):
        return nodes_copy
    for node, tr in zip(targets, translated):
        node.translated_body = tr
    return nodes_copy


async def translate_article(
    ctx: PluginContext | None,
    article: TextArticle,
    *,
    target_lang: str = "zh-CN",
) -> TextArticle:
    """Translate article title, non-code/non-formula paragraphs, and comments gracefully."""
    if ctx is None:
        return article
    para_indices: list[int] = []
    texts: list[str] = []
    include_title = bool(article.title.strip() and not article.title_zh)
    if include_title:
        texts.append(article.title)
    for idx, p in enumerate(article.paragraphs):
        if p.kind in _SKIP_KINDS or p.id == "p_more_comments" or not p.original.strip() or p.translated:
            continue
        para_indices.append(idx)
        texts.append(p.original)

    new_title_zh = article.title_zh
    new_paragraphs = list(article.paragraphs)
    if texts:
        translated = await _translate_strings(
            ctx,
            texts,
            target_lang=target_lang,
            context_title=article.title,
        )
        if translated is not None and len(translated) == len(texts):
            cursor = 0
            if include_title:
                new_title_zh = translated[0]
                cursor = 1
            for pidx, tr_text in zip(para_indices, translated[cursor:]):
                p: TextParagraph = new_paragraphs[pidx]
                new_paragraphs[pidx] = p.model_copy(update={"translated": tr_text})

    new_comments = await translate_comments(
        ctx,
        article.comments,
        target_lang=target_lang,
        context_title=article.title,
    )
    bilingual_ok = bool(
        new_title_zh
        or any(p.translated for p in new_paragraphs)
        or any(c.translated_body for c in new_comments)
    )
    extra = dict(article.extra)
    extra["bilingual_translated"] = bilingual_ok
    return article.model_copy(
        update={
            "title_zh": new_title_zh,
            "paragraphs": new_paragraphs,
            "comments": new_comments,
            "extra": extra,
        }
    )


async def translate_feed_items(
    ctx: PluginContext | None,
    items: Sequence[TextFeedItem],
    *,
    target_lang: str = "zh-CN",
) -> list[TextFeedItem]:
    """Translate feed item titles into `title_zh` when translation capability is available."""
    if ctx is None or not items:
        return list(items)
    targets = [i for i, item in enumerate(items) if item.title.strip() and not item.title_zh]
    if not targets:
        return list(items)
    translated = await _translate_strings(
        ctx,
        [items[i].title for i in targets],
        target_lang=target_lang,
        context_title="交大水源社区",
    )
    if translated is None or len(translated) != len(targets):
        return list(items)
    copied = list(items)
    for idx, tr in zip(targets, translated):
        copied[idx] = copied[idx].model_copy(update={"title_zh": tr, "bilingual_cached": True})
    return copied
