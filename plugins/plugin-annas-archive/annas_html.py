"""HTML parsers for Anna's Archive search cards, MD5 detail pages, and slow download links."""

from __future__ import annotations

import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any

_MD5_HREF_RE = re.compile(r"/md5/([0-9a-fA-F]{32}|[A-Za-z0-9_-]{8,64})(?:[/?#]|$)")
_FORMAT_TOKEN_RE = re.compile(r"\b(pdf|epub|mobi|azw3|txt|djvu|fb2|cbr|cbz)\b", re.IGNORECASE)
_SIZE_TOKEN_RE = re.compile(r"\b(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|KiB|MiB|GiB))\b", re.IGNORECASE)
_YEAR_TOKEN_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_LANG_TAG_RE = re.compile(r"^([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff\s-]{1,24})\s*\[([a-z]{2,3})\]$", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


def clean_html_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def parse_meta_badges(text: str) -> dict[str, str]:
    """Parse Anna's Archive `English [en] · EPUB · 8.6MB · 2021` metadata strip."""
    out: dict[str, str] = {"language": "", "format": "", "size": "", "year": ""}
    segments = [clean_html_text(seg) for seg in re.split(r"[·•|,]", text) if clean_html_text(seg)]
    for seg in segments:
        if not out["language"]:
            m_lang = _LANG_TAG_RE.match(seg)
            if m_lang:
                out["language"] = clean_html_text(m_lang.group(1))
                continue
            if seg.lower() in {"english", "chinese", "中文", "japanese", "french", "german", "spanish", "russian"}:
                out["language"] = seg
                continue
        if not out["format"]:
            m_fmt = _FORMAT_TOKEN_RE.search(seg)
            if m_fmt and len(seg) <= 12:
                out["format"] = m_fmt.group(1).lower()
                continue
        if not out["size"]:
            m_size = _SIZE_TOKEN_RE.search(seg)
            if m_size:
                out["size"] = clean_html_text(m_size.group(1))
                continue
        if not out["year"]:
            m_year = _YEAR_TOKEN_RE.search(seg)
            if m_year:
                out["year"] = m_year.group(1)
    if not out["format"] and (m_fmt := _FORMAT_TOKEN_RE.search(text)):
        out["format"] = m_fmt.group(1).lower()
    if not out["size"] and (m_size := _SIZE_TOKEN_RE.search(text)):
        out["size"] = clean_html_text(m_size.group(1))
    if not out["year"] and (m_year := _YEAR_TOKEN_RE.search(text)):
        out["year"] = m_year.group(1)
    return out


class AnnasSearchHTMLParser(HTMLParser):
    """Parses Anna's Archive `<a href="/md5/<hash>">` result cards."""

    def __init__(self, *, parse_comment_cards: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self._parse_comment_cards = parse_comment_cards
        self._in_card = False
        self._card_depth = 0
        self._card: dict[str, Any] = {}
        self._capture = ""
        self._buf: list[str] = []
        self._all_lines: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls_str = attr_map.get("class", "")
        href = attr_map.get("href", "")

        if not self._in_card and tag == "a":
            m = _MD5_HREF_RE.search(urllib.parse.urlsplit(href).path)
            if m:
                self._in_card = True
                self._card_depth = 1
                self._card = {
                    "md5": m.group(1),
                    "href": href,
                    "title": clean_html_text(attr_map.get("data-title")),
                    "author": clean_html_text(attr_map.get("data-author")),
                    "format": clean_html_text(attr_map.get("data-format")),
                    "size": clean_html_text(attr_map.get("data-size")),
                    "year": clean_html_text(attr_map.get("data-year")),
                    "language": clean_html_text(attr_map.get("data-language")),
                    "download_token": clean_html_text(attr_map.get("data-download")),
                }
                self._capture = ""
                self._buf = []
                self._all_lines = []
                return

        if self._in_card:
            self._card_depth += 1
            if tag == "h3" or ("font-bold" in cls_str and not self._card.get("title")):
                self._capture = "title"
                self._buf = []
            elif "italic" in cls_str and not self._card.get("author"):
                self._capture = "author"
                self._buf = []
            elif any(tok in cls_str for tok in ("text-xs", "text-gray-500", "text-slate-500")):
                self._capture = "meta"
                self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_card:
            cleaned = clean_html_text(data)
            if cleaned:
                self._all_lines.append(cleaned)
            if self._capture:
                self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._in_card:
            return
        if self._capture and tag in {"h3", "div", "span", "p"}:
            txt = clean_html_text("".join(self._buf))
            if self._capture == "title" and txt and not self._card.get("title"):
                self._card["title"] = txt
            elif self._capture == "author" and txt and not self._card.get("author"):
                self._card["author"] = txt
            elif self._capture == "meta" and txt:
                for k, v in parse_meta_badges(txt).items():
                    if v and not self._card.get(k):
                        self._card[k] = v
            self._capture = ""
            self._buf = []

        self._card_depth -= 1
        if self._card_depth <= 0:
            if not self._card.get("title") and self._all_lines:
                self._card["title"] = self._all_lines[0]
            if not self._card.get("format") or not self._card.get("size"):
                for k, v in parse_meta_badges(" · ".join(self._all_lines)).items():
                    if v and not self._card.get(k):
                        self._card[k] = v
            self.items.append(self._card)
            self._in_card = False
            self._card = {}

    def handle_comment(self, data: str) -> None:
        if self._parse_comment_cards and "/md5/" in data:
            sub = AnnasSearchHTMLParser(parse_comment_cards=False)
            sub.feed(data)
            sub.close()
            self.items.extend(sub.items)


class AnnasDetailHTMLParser(HTMLParser):
    """Parses Anna's Archive `/md5/<hash>` detail metadata and download mirrors."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, Any] = {"formats": [], "meta_lines": []}
        self._capture = ""
        self._buf: list[str] = []
        self._active_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls_str = attr_map.get("class", "")
        if tag == "h1" or ("text-3xl" in cls_str and not self.meta.get("title")):
            self._capture = "title"
            self._buf = []
        elif tag == "meta" and attr_map.get("property") == "og:title" and not self.meta.get("title"):
            self.meta["title"] = clean_html_text(attr_map.get("content"))
        elif attr_map.get("itemprop") == "author" or ("italic" in cls_str and not self.meta.get("author")):
            self._capture = "author"
            self._buf = []
        elif "js-md5-top-box-description" in cls_str or "book-description" in cls_str:
            self._capture = "description"
            self._buf = []
        elif any(tok in cls_str for tok in ("text-sm", "text-xs", "text-gray-500")):
            self._capture = "meta_line"
            self._buf = []
        elif tag == "a":
            href = attr_map.get("href", "")
            if any(p in href for p in ("/fast_download/", "/slow_download/", "/dyn/api/fast_download", "/dl/")) or "js-download-link" in cls_str:
                self._active_href = href
                self._capture = "dl_link"
                self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._capture:
            return
        text = clean_html_text("".join(self._buf))
        if self._capture == "title" and tag in {"h1", "div"}:
            if text and not self.meta.get("title"):
                self.meta["title"] = text
            self._capture = ""
        elif self._capture == "author" and tag in {"div", "span", "a", "p"}:
            if text and not self.meta.get("author"):
                self.meta["author"] = text
            self._capture = ""
        elif self._capture == "description" and tag in {"div", "p"}:
            if text:
                self.meta["description"] = text
            self._capture = ""
        elif self._capture == "meta_line" and tag in {"div", "span", "p"}:
            if text:
                self.meta["meta_lines"].append(text)
            self._capture = ""
        elif self._capture == "dl_link" and tag == "a":
            if self._active_href:
                m_fmt = _FORMAT_TOKEN_RE.search(text)
                self.meta["formats"].append(
                    {"format": m_fmt.group(1).lower() if m_fmt else "", "size": "", "download_token": self._active_href, "label": text}
                )
            self._active_href = ""
            self._capture = ""


class SlowDownloadLinkParser(HTMLParser):
    """Extracts an explicit download link from a slow_download HTML confirmation page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        href = clean_html_text(attr_map.get("href"))
        cls_str = attr_map.get("class", "").lower()
        if not href:
            return
        if "download" in attr_map or "download" in cls_str or any(
            href.lower().endswith(f".{ext}") for ext in ("pdf", "epub", "mobi", "azw3", "txt")
        ):
            self.links.append(href)
