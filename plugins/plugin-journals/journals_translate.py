"""Optional bilingual translation for journal titles, abstracts, and paragraphs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from omnidesk_sdk import PluginContext, TextArticle, TextFeedItem

_BATCH_SIZE = 16


def _extract_batch(data: Mapping[str, Any], expected: int) -> list[str] | None:
    for key in ("translations", "translated_texts", "results", "segments"):
        raw = data.get(key)
        if isinstance(raw, list) and len(raw) == expected:
            values: list[str] = []
            for item in raw:
                text = (
                    str(item.get("translation") or item.get("translated") or item.get("text") or "").strip()
                    if isinstance(item, Mapping)
                    else str(item or "").strip()
                )
                if not text:
                    return None
                values.append(text)
            return values
    if expected == 1:
        for key in ("translation", "translated", "reply"):
            text = data.get(key)
            if isinstance(text, str) and text.strip():
                return [text.strip()]
    return None


def _parse_chat_array(reply: str, expected: int) -> list[str] | None:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (reply or "").strip())
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list) and len(parsed) == expected:
        values = [str(item or "").strip() for item in parsed]
        return values if all(values) else None
    if expected == 1:
        return [cleaned]
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return lines if len(lines) == expected and all(lines) else None


async def translate_texts(
    ctx: PluginContext | None,
    texts: Sequence[str],
    *,
    target_lang: str = "zh-CN",
    context: str = "",
) -> list[str] | None:
    """Translate strings through `llm.translate.v1` or `llm.chat.v1`; never fabricate."""
    if ctx is None or not texts:
        return None
    clean = [str(text) for text in texts]
    output: list[str] = []
    if ctx.capabilities.has("llm.translate.v1"):
        for start in range(0, len(clean), _BATCH_SIZE):
            chunk = clean[start : start + _BATCH_SIZE]
            request: dict[str, Any] = {
                "action": "translate_batch" if len(chunk) > 1 else "translate",
                "domain": "text",
                "source_lang": "auto",
                "target_lang": target_lang,
                "context": context,
                "texts": chunk,
                "segments": [{"id": str(index), "text": text} for index, text in enumerate(chunk)],
            }
            if len(chunk) == 1:
                request["text"] = chunk[0]
            try:
                result = await ctx.capabilities.call("llm.translate.v1", request)
            except Exception:
                return None
            if not result.ok or not isinstance(result.data, Mapping):
                return None
            batch = _extract_batch(result.data, len(chunk))
            if batch is None:
                return None
            output.extend(batch)
        return output

    if ctx.capabilities.has("llm.chat.v1"):
        for start in range(0, len(clean), _BATCH_SIZE):
            chunk = clean[start : start + _BATCH_SIZE]
            prompt = (
                f"Translate each item in this JSON array into {target_lang}. "
                "Preserve technical terms, inline code, citations, and LaTeX math unchanged. "
                "Return ONLY a JSON array of translated strings in the exact same order.\n"
                f"Context: {context}\n{json.dumps(chunk, ensure_ascii=False)}"
            )
            try:
                result = await ctx.capabilities.call(
                    "llm.chat.v1",
                    {"action": "chat", "domain": "text", "prompt": prompt},
                )
            except Exception:
                return None
            if not result.ok or not isinstance(result.data, Mapping):
                return None
            batch = _parse_chat_array(str(result.data.get("reply") or ""), len(chunk))
            if batch is None:
                return None
            output.extend(batch)
        return output
    return None


async def translate_feed_items(
    ctx: PluginContext | None,
    items: Sequence[TextFeedItem],
    *,
    target_lang: str = "zh-CN",
) -> list[TextFeedItem]:
    """Fill `title_zh` for feed items when a translation capability is available."""
    if not items:
        return list(items)
    translated = await translate_texts(
        ctx,
        [item.title for item in items],
        target_lang=target_lang,
        context="academic paper titles",
    )
    if translated is None:
        return list(items)
    out: list[TextFeedItem] = []
    for item, title_zh in zip(items, translated):
        out.append(item.model_copy(update={"title_zh": title_zh}))
    return out


async def translate_article(
    ctx: PluginContext | None,
    article: TextArticle,
    *,
    target_lang: str = "zh-CN",
) -> TextArticle:
    """Translate title and paragraphs, preserving paragraph kinds and ids."""
    paragraphs = [paragraph.original for paragraph in article.paragraphs if paragraph.original.strip()]
    title_zh = ""
    translated_paragraphs: list[str] = []
    title_result = await translate_texts(
        ctx,
        [article.title],
        target_lang=target_lang,
        context="academic paper title",
    )
    if title_result:
        title_zh = title_result[0]
    if paragraphs:
        result = await translate_texts(
            ctx,
            paragraphs,
            target_lang=target_lang,
            context=f"academic paper: {article.title}",
        )
        if result is not None:
            translated_paragraphs = result
    if not title_zh and not translated_paragraphs:
        return article
    iterator = iter(translated_paragraphs)
    updated = []
    for paragraph in article.paragraphs:
        translated = next(iterator, "") if paragraph.original.strip() else ""
        updated.append(paragraph.model_copy(update={"translated": translated}))
    return article.model_copy(
        update={
            "title_zh": title_zh or article.title_zh,
            "paragraphs": updated,
            "summary_zh": translated_paragraphs[0] if translated_paragraphs else article.summary_zh,
        }
    )
