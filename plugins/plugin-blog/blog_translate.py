"""Optional bilingual translation for blog titles and article paragraphs."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from omnidesk_sdk import PluginContext, TextArticle, TextFeedItem, TextParagraph

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
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return [value.strip()]
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
                "Preserve technical terms, inline code, links, and LaTeX math unchanged. "
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
    if not items:
        return list(items)
    translated = await translate_texts(
        ctx,
        [item.title for item in items],
        target_lang=target_lang,
        context="personal blog post titles",
    )
    if translated is None:
        return list(items)
    return [
        item.model_copy(update={"title_zh": title_zh})
        for item, title_zh in zip(items, translated)
    ]


async def translate_article(
    ctx: PluginContext | None,
    article: TextArticle,
    *,
    target_lang: str = "zh-CN",
) -> TextArticle:
    originals = [paragraph.original for paragraph in article.paragraphs if paragraph.original.strip()]
    translated: list[str] = []
    title_zh = ""
    title_result = await translate_texts(
        ctx,
        [article.title],
        target_lang=target_lang,
        context="personal blog post title",
    )
    if title_result:
        title_zh = title_result[0]
    if originals:
        result = await translate_texts(
            ctx,
            originals,
            target_lang=target_lang,
            context=f"personal blog post: {article.title}",
        )
        if result is not None:
            translated = result
    if not title_zh and not translated:
        return article
    iterator = iter(translated)
    paragraphs: list[TextParagraph] = []
    for paragraph in article.paragraphs:
        paragraph_translation = next(iterator, "") if paragraph.original.strip() else ""
        paragraphs.append(paragraph.model_copy(update={"translated": paragraph_translation}))
    return article.model_copy(
        update={
            "title_zh": title_zh or article.title_zh,
            "paragraphs": paragraphs,
            "summary_zh": translated[0] if translated else article.summary_zh,
        }
    )
