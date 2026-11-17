"""Untrusted HTML extraction and optional llm.translate.v1 bilingual segmentation."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from omnidesk_sdk import PluginContext
from omnidesk_sdk.media_markup import image_markdown

if __package__:
    from .security import redact_sensitive
else:  # pragma: no cover - direct module import fallback
    from security import redact_sensitive

_IGNORED_TAGS = frozenset({"script", "style", "noscript", "template", "iframe", "svg", "object", "embed"})
_BLOCK_TAGS = frozenset({"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "dt", "dd", "figcaption", "pre"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_MAX_HTML_CHARS = 8 * 1024 * 1024


def _clean_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class _UntrustedHtmlExtractor(HTMLParser):
    """Stdlib HTMLParser treating all external HTML strictly as untrusted data."""

    def __init__(self, *, base_url: str, max_links: int = 500) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._max_links = max_links
        self._ignored_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._block_stack: list[tuple[str, list[str]]] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self.paragraphs: list[dict[str, str]] = []
        self.links: list[dict[str, str]] = []
        self._seen_links: set[tuple[str, str]] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t in _IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth > 0:
            return
        if t == "img":
            image_attrs = {k.lower(): (v or "") for k, v in attrs}
            image = image_markdown(image_attrs.get("src") or image_attrs.get("data-src") or "", image_attrs.get("alt", ""), base_url=self._base_url)
            self._text_parts.append(image)
            if self._block_stack:
                self._block_stack[-1][1].append(image)
            else:
                self.paragraphs.append({"id": f"p{len(self.paragraphs) + 1}", "kind": "paragraph", "original": image})
        if t == "title":
            self._in_title = True
        if t == "a" and self._anchor_href is None:
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            self._anchor_href = attr_map.get("href", "").strip()
            self._anchor_parts = []
        if t in _BLOCK_TAGS:
            self._block_stack.append((t, []))
        elif t == "br":
            self._text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _IGNORED_TAGS:
            if self._ignored_depth > 0:
                self._ignored_depth -= 1
            return
        if self._ignored_depth > 0:
            return
        if t == "title":
            self._in_title = False
        if t == "a" and self._anchor_href is not None:
            self._record_link(self._anchor_href, _clean_ws("".join(self._anchor_parts)))
            self._anchor_href = None
            self._anchor_parts = []
        if t in _BLOCK_TAGS and self._block_stack:
            for idx in range(len(self._block_stack) - 1, -1, -1):
                if self._block_stack[idx][0] == t:
                    _, parts = self._block_stack.pop(idx)
                    content = _clean_ws("".join(parts))
                    if content:
                        kind = "heading" if t in _HEADING_TAGS else ("quote" if t == "blockquote" else "paragraph")
                        self.paragraphs.append(
                            {"id": f"p{len(self.paragraphs) + 1}", "kind": kind, "original": content}
                        )
                    self._text_parts.append("\n")
                    break

    def handle_data(self, data: str) -> None:
        if self._ignored_depth > 0:
            return
        if self._in_title:
            self._title_parts.append(data)
            return
        self._text_parts.append(data)
        if self._anchor_href is not None:
            self._anchor_parts.append(data)
        if self._block_stack:
            self._block_stack[-1][1].append(data)

    def _record_link(self, raw_href: str, text: str) -> None:
        if len(self.links) >= self._max_links or not raw_href or raw_href.startswith("#"):
            return
        resolved = urljoin(self._base_url, raw_href)
        try:
            parsed = urlsplit(resolved)
        except ValueError:
            return
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return
        key = (resolved, text)
        if key in self._seen_links:
            return
        self._seen_links.add(key)
        self.links.append({"text": text, "href": resolved})

    @property
    def title(self) -> str:
        return _clean_ws("".join(self._title_parts))

    @property
    def text(self) -> str:
        lines = [_clean_ws(line) for line in "".join(self._text_parts).splitlines()]
        return "\n".join(line for line in lines if line)


def extract_from_html(html: str, *, base_url: str, fallback_title: str = "") -> dict[str, Any]:
    """Parse untrusted HTML into `title`, `text`, `links`, and paragraph segments."""
    if len(html) > _MAX_HTML_CHARS:
        raise ValueError("网页 HTML 超过 8 MiB 解析上限，未返回截断正文")
    bounded = html
    parser = _UntrustedHtmlExtractor(base_url=base_url)
    parser.feed(bounded)
    parser.close()

    text = parser.text
    paragraphs = parser.paragraphs
    if not paragraphs and text:
        paragraphs = [
            {"id": f"p{idx}", "kind": "paragraph", "original": line}
            for idx, line in enumerate(text.splitlines(), start=1)
            if line.strip()
        ]
    return {
        "title": parser.title or _clean_ws(fallback_title),
        "html": bounded,
        "text": text,
        "links": parser.links,
        "paragraphs": paragraphs,
    }


def _extract_translations(data: Mapping[str, Any], count: int) -> list[str] | None:
    for key in ("translations", "translated", "items", "segments"):
        val = data.get(key)
        if isinstance(val, list) and len(val) >= count:
            out: list[str] = []
            for item in val[:count]:
                if isinstance(item, Mapping):
                    txt = str(item.get("translated") or item.get("translation") or item.get("text") or "").strip()
                else:
                    txt = str(item or "").strip()
                if not txt:
                    return None
                out.append(txt)
            return out
    if count == 1:
        for key in ("translation", "translated", "reply"):
            single = data.get(key)
            if isinstance(single, str) and single.strip():
                return [single.strip()]
    return None


async def build_bilingual_segments(
    ctx: PluginContext,
    paragraphs: Sequence[Mapping[str, str]],
    *,
    title: str = "",
    translate: bool = False,
    target_lang: str = "zh-CN",
) -> dict[str, Any]:
    """Optionally translate extracted paragraphs via `llm.translate.v1`, never fabricating translations."""
    base_segments = [
        {
            "id": str(seg.get("id") or f"p{idx}"),
            "kind": str(seg.get("kind") or "paragraph"),
            "original": str(seg.get("original") or ""),
            "translated": None,
        }
        for idx, seg in enumerate(paragraphs, start=1)
        if str(seg.get("original") or "").strip()
    ]
    if not translate or not base_segments:
        return {
            "text_only": True,
            "translated": False,
            "target_lang": target_lang,
            "bilingual_segments": base_segments,
        }

    if not ctx.capabilities.has("llm.translate.v1"):
        return {
            "text_only": True,
            "translated": False,
            "target_lang": target_lang,
            "bilingual_segments": base_segments,
        }

    texts = [seg["original"] for seg in base_segments]
    tr_res = await ctx.capabilities.call(
        "llm.translate.v1",
        {
            "action": "translate_batch",
            "domain": "text",
            "target_lang": target_lang,
            "title": title,
            "texts": texts,
            "segments": [{"id": seg["id"], "text": seg["original"]} for seg in base_segments],
        },
    )
    if not tr_res.ok:
        return {
            "text_only": True,
            "translated": False,
            "target_lang": target_lang,
            "translation_error": redact_sensitive(tr_res.error),
            "bilingual_segments": base_segments,
        }

    translated_items = _extract_translations(tr_res.data, len(base_segments))
    if translated_items is None:
        return {
            "text_only": True,
            "translated": False,
            "target_lang": target_lang,
            "bilingual_segments": base_segments,
        }

    bilingual = [
        {**seg, "translated": tr_text}
        for seg, tr_text in zip(base_segments, translated_items)
    ]
    return {
        "text_only": False,
        "translated": True,
        "target_lang": target_lang,
        "bilingual_segments": bilingual,
    }
