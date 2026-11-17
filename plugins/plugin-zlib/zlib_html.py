"""HTML parsers for Z-Library search cards and book detail pages."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

_FILE_PROP_RE = re.compile(r"^\s*([A-Za-z0-9]{2,8})\s*[,|/·]\s*(.+?)\s*$")
_SIZE_FORMAT_RE = re.compile(r"\b(pdf|epub|mobi|azw3|txt)\b", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


def clean_html_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


class ZlibSearchHTMLParser(HTMLParser):
    """Parses Z-Library `<z-bookcard>` elements and classic `.resItemBox` cards."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self._card: dict[str, Any] | None = None
        self._card_depth = 0
        self._capture_slot = ""
        self._in_res_box = False
        self._res_depth = 0
        self._res_item: dict[str, Any] = {}
        self._field_tag = ""
        self._text_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls_tokens = set(attr_map.get("class", "").split())

        if tag == "z-bookcard":
            self._card = {
                "id": attr_map.get("id", ""),
                "hash": attr_map.get("hash", ""),
                "author": attr_map.get("author", ""),
                "year": attr_map.get("year", ""),
                "language": attr_map.get("language", ""),
                "extension": attr_map.get("extension", ""),
                "filesize": attr_map.get("filesize", ""),
                "href": attr_map.get("href", ""),
                "download": attr_map.get("download", ""),
                "title": attr_map.get("title", ""),
            }
            self._card_depth = 1
            return

        if self._card is not None:
            self._card_depth += 1
            slot = attr_map.get("slot", "").lower()
            if slot in {"title", "author"}:
                self._capture_slot = slot
                self._text_buf = []
            elif tag == "a" and not self._card.get("href") and "/book/" in attr_map.get("href", ""):
                self._card["href"] = attr_map["href"]
            return

        if not self._in_res_box and "resItemBox" in cls_tokens:
            self._in_res_box = True
            self._res_depth = 1
            self._res_item = {}
            self._field_tag = ""
            return

        if self._in_res_box:
            self._res_depth += 1
            if tag == "a":
                href = attr_map.get("href", "")
                if "/book/" in href and not self._res_item.get("href"):
                    self._res_item["href"] = href
                    self._field_tag = "title"
                    self._text_buf = []
                elif "/dl/" in href and not self._res_item.get("dl"):
                    self._res_item["dl"] = href
                elif "authors" in self._field_tag or attr_map.get("itemprop") == "author":
                    self._field_tag = "author"
                    self._text_buf = []
            elif "authors" in cls_tokens:
                self._field_tag = "authors_div"
                self._text_buf = []
            elif "property_year" in cls_tokens:
                self._field_tag = "year"
                self._text_buf = []
            elif "property_language" in cls_tokens:
                self._field_tag = "language"
                self._text_buf = []
            elif "property__file" in cls_tokens:
                self._field_tag = "file"
                self._text_buf = []

    def handle_data(self, data: str) -> None:
        if self._card is not None and self._capture_slot:
            self._text_buf.append(data)
        elif self._in_res_box and self._field_tag:
            self._text_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._card is not None:
            if self._capture_slot and tag in {"div", "span", "a"}:
                val = clean_html_text("".join(self._text_buf))
                if val and not self._card.get(self._capture_slot):
                    self._card[self._capture_slot] = val
                self._capture_slot = ""
                self._text_buf = []
            self._card_depth -= 1
            if self._card_depth <= 0:
                self.items.append(self._card)
                self._card = None
            return

        if self._in_res_box:
            if self._field_tag == "title" and tag == "a":
                self._res_item["title"] = clean_html_text("".join(self._text_buf))
                self._field_tag = ""
                self._text_buf = []
            elif self._field_tag in {"author", "authors_div"} and tag in {"a", "div"}:
                txt = clean_html_text("".join(self._text_buf))
                if txt:
                    existing = self._res_item.get("author", "")
                    self._res_item["author"] = (
                        f"{existing}, {txt}" if existing and txt not in existing else (existing or txt)
                    )
                self._field_tag = ""
                self._text_buf = []
            elif self._field_tag in {"year", "language", "file"} and tag == "div":
                txt = clean_html_text("".join(self._text_buf))
                if self._field_tag == "file":
                    m = _FILE_PROP_RE.match(txt)
                    if m:
                        self._res_item["format"] = m.group(1).lower()
                        self._res_item["size"] = m.group(2)
                elif txt:
                    self._res_item[self._field_tag] = txt
                self._field_tag = ""
                self._text_buf = []
            self._res_depth -= 1
            if self._res_depth <= 0:
                if self._res_item.get("title"):
                    self.items.append(self._res_item)
                self._in_res_box = False
                self._res_item = {}


class ZlibDetailHTMLParser(HTMLParser):
    """Parses Z-Library `/book/<id>/<hash>` detail metadata and download format links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, Any] = {"formats": []}
        self._capture = ""
        self._prop_name = ""
        self._buf: list[str] = []
        self._active_dl_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        cls_tokens = set(attr_map.get("class", "").split())
        if tag == "h1" and not self.meta.get("title"):
            self._capture = "title"
            self._buf = []
        elif tag == "meta" and attr_map.get("property") == "og:title" and not self.meta.get("title"):
            self.meta["title"] = clean_html_text(attr_map.get("content"))
        elif attr_map.get("itemprop") == "author" or "book-author" in cls_tokens:
            self._capture = "author"
            self._buf = []
        elif attr_map.get("id") == "bookDescriptionBox" or "book-description" in cls_tokens:
            self._capture = "description"
            self._buf = []
        elif any(k.startswith("property_") for k in cls_tokens):
            for tok in cls_tokens:
                if tok.startswith("property_"):
                    self._prop_name = tok.removeprefix("property_").lstrip("_")
                    break
        elif "property_value" in cls_tokens and self._prop_name:
            self._capture = f"prop:{self._prop_name}"
            self._buf = []
        elif tag == "a":
            href = attr_map.get("href", "")
            if "/dl/" in href or "addDownloadedBook" in cls_tokens or "dlButton" in cls_tokens:
                self._active_dl_href = href
                self._capture = "dl_link"
                self._buf = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._capture:
            return
        text = clean_html_text("".join(self._buf))
        if self._capture == "title" and tag == "h1":
            if text:
                self.meta["title"] = text
            self._capture = ""
        elif self._capture == "author" and tag in {"a", "span", "div"}:
            if text:
                self.meta["author"] = text
            self._capture = ""
        elif self._capture == "description" and tag in {"div", "p"}:
            if text:
                self.meta["description"] = text
            self._capture = ""
        elif self._capture.startswith("prop:") and tag in {"div", "span"}:
            prop = self._capture.split(":", 1)[1]
            if prop == "file":
                m = _FILE_PROP_RE.match(text)
                if m:
                    self.meta["format"] = m.group(1).lower()
                    self.meta["size"] = m.group(2)
            elif text:
                self.meta[prop] = text
            self._prop_name = ""
            self._capture = ""
        elif self._capture == "dl_link" and tag == "a":
            if self._active_dl_href:
                fmt_match = _SIZE_FORMAT_RE.search(text)
                fmt = fmt_match.group(1).lower() if fmt_match else clean_html_text(self.meta.get("format")).lower()
                self.meta["formats"].append(
                    {"format": fmt, "size": self.meta.get("size", ""), "download_token": self._active_dl_href, "label": text}
                )
            self._active_dl_href = ""
            self._capture = ""
