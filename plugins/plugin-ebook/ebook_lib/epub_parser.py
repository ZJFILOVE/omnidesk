"""Stdlib zipfile + XML + HTMLParser implementation for EPUB 2/3 archives."""

from __future__ import annotations

import posixpath
import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from .errors import MAX_BOOK_FILE_BYTES, MAX_EPUB_ENTRIES, MAX_EPUB_ENTRY_BYTES, BookParseError
from .models_internal import ParsedBook, ParsedPage, collapse_ws, paginate_blocks, probe_file_format
from .security import read_zip_member_bounded, safe_zip_member_path, validate_book_file

_ENTITY_DECL_RE = re.compile(rb"<!ENTITY\b", re.IGNORECASE)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _safe_parse_xml(raw: bytes) -> ET.Element:
    if _ENTITY_DECL_RE.search(raw):
        raise BookParseError("XML entity declarations are forbidden for security")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise BookParseError(f"Invalid XML payload: {exc}") from exc


def _resolve_zip_rel(base_file: str, href: str) -> str:
    clean_href = urllib.parse.unquote(href.split("#", 1)[0]).strip()
    if not clean_href:
        return ""
    base_dir = posixpath.dirname(base_file)
    joined = posixpath.join(base_dir, clean_href) if base_dir else clean_href
    return safe_zip_member_path(posixpath.normpath(joined))


class _NavHtmlParser(HTMLParser):
    """Extract ordered (href, label) TOC pairs from EPUB 3 nav XHTML."""

    def __init__(self, nav_member: str) -> None:
        super().__init__(convert_charrefs=True)
        self._nav_member = nav_member
        self._in_toc_nav = False
        self._nav_depth = 0
        self._current_href: str | None = None
        self._buf: list[str] = []
        self.entries: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "nav":
            epub_type = attr_map.get("epub:type", "") or attr_map.get("role", "")
            if not self._in_toc_nav and ("toc" in epub_type.split() or not epub_type):
                self._in_toc_nav = True
                self._nav_depth = 1
            elif self._in_toc_nav:
                self._nav_depth += 1
        elif self._in_toc_nav and t == "a":
            href = attr_map.get("href", "").strip()
            if href:
                try:
                    self._current_href = _resolve_zip_rel(self._nav_member, href)
                except Exception:
                    self._current_href = None
                self._buf.clear()

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "a" and self._current_href is not None:
            title = collapse_ws("".join(self._buf))
            if title:
                self.entries.append({"href": self._current_href, "title": title})
            self._current_href = None
            self._buf.clear()
        elif t == "nav" and self._in_toc_nav:
            self._nav_depth -= 1
            if self._nav_depth <= 0:
                self._in_toc_nav = False


class _XhtmlBlockExtractor(HTMLParser):
    """Extract heading, paragraph, caption, and image placeholder blocks from XHTML."""

    _HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
    _PARAS = frozenset({"p", "blockquote", "li", "pre"})
    _SKIP = frozenset({"head", "script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page_title = ""
        self.blocks: list[tuple[str, str, str]] = []
        self.image_sources: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._title_buf: list[str] = []
        self._active_kind: str | None = None
        self._active_translated = False
        self._buf: list[str] = []

    def _flush_active(self) -> None:
        if self._active_kind is None:
            self._buf.clear()
            return
        text = collapse_ws("".join(self._buf))
        self._buf.clear()
        kind, is_trans = self._active_kind, self._active_translated
        self._active_kind = None
        self._active_translated = False
        if not text:
            return
        if is_trans and self.blocks and not self.blocks[-1][2]:
            prev_kind, prev_orig, _ = self.blocks[-1]
            self.blocks[-1] = (prev_kind, prev_orig, text)
        elif not is_trans:
            self.blocks.append((kind, text, ""))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        if t == "title":
            self._in_title = True
            self._title_buf.clear()
            return
        if t in self._SKIP:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t in {"img", "image"}:
            self._flush_active()
            alt = collapse_ws(attr_map.get("alt", ""))
            src = attr_map.get("src", "") or attr_map.get("xlink:href", "") or attr_map.get("href", "")
            label = alt or posixpath.basename(src.split("?", 1)[0]) or "figure"
            self.blocks.append(("image", f"[Image: {label}]", ""))
            self.image_sources.append(src)
            return
        classes = set(attr_map.get("class", "").split())
        is_trans = attr_map.get("data-bilingual") == "translation" or "omnidesk-translation" in classes
        if t in self._HEADINGS:
            self._flush_active()
            self._active_kind, self._active_translated = "heading", is_trans
        elif t in self._PARAS:
            self._flush_active()
            self._active_kind, self._active_translated = "paragraph", is_trans
        elif t == "figcaption":
            self._flush_active()
            self._active_kind, self._active_translated = "caption", is_trans
        elif t == "br" and self._active_kind is not None:
            self._buf.append(" ")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
        elif self._skip_depth > 0:
            return
        elif self._active_kind is not None:
            self._buf.append(data)
        elif data.strip():
            self._active_kind = "paragraph"
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "title":
            self._in_title = False
            if not self.page_title:
                self.page_title = collapse_ws("".join(self._title_buf))
            return
        if t in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if t in self._HEADINGS or t in self._PARAS or t in {"figcaption", "div", "section", "body"}:
            self._flush_active()

    def close(self) -> None:
        super().close()
        self._flush_active()


def _build_zip_index(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > MAX_EPUB_ENTRIES:
        raise BookParseError(f"EPUB entry count exceeds limit ({len(infos)} > {MAX_EPUB_ENTRIES})")
    index: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        norm = safe_zip_member_path(info.filename)
        if not info.is_dir():
            index[norm] = info
    return index


def _lookup_zip_info(index: dict[str, zipfile.ZipInfo], target: str) -> zipfile.ZipInfo | None:
    if target in index:
        return index[target]
    lowered = target.lower()
    return next((info for key, info in index.items() if key.lower() == lowered), None)


def _parse_ncx_toc(ncx_bytes: bytes, ncx_member: str) -> list[dict[str, str]]:
    root = _safe_parse_xml(ncx_bytes)
    entries: list[dict[str, str]] = []

    def _walk(node: ET.Element) -> None:
        for child in node:
            if _strip_ns(child.tag) != "navPoint":
                continue
            label, src = "", ""
            for sub in child:
                sname = _strip_ns(sub.tag)
                if sname == "navLabel":
                    for txt in sub:
                        if _strip_ns(txt.tag) == "text" and txt.text:
                            label = collapse_ws(txt.text)
                elif sname == "content":
                    src = sub.attrib.get("src", "")
            if label and src:
                try:
                    resolved = _resolve_zip_rel(ncx_member, src)
                except Exception:
                    resolved = ""
                if resolved:
                    entries.append({"href": resolved, "title": label})
            _walk(child)

    for elem in root:
        if _strip_ns(elem.tag) == "navMap":
            _walk(elem)
            break
    return entries


def _parse_opf_document(
    opf_xml: ET.Element, opf_member: str
) -> tuple[list[str], list[str], str, dict[str, dict[str, str]], list[str], str]:
    titles: list[str] = []
    creators: list[str] = []
    language = ""
    manifest: dict[str, dict[str, str]] = {}
    spine_ids: list[str] = []
    ncx_id = ""
    for section in opf_xml:
        sname = _strip_ns(section.tag)
        if sname == "metadata":
            for meta in section.iter():
                mname = _strip_ns(meta.tag)
                txt = collapse_ws(meta.text or "")
                if not txt:
                    continue
                if mname == "title":
                    titles.append(txt)
                elif mname == "creator":
                    creators.append(txt)
                elif mname == "language" and not language:
                    language = txt
        elif sname == "manifest":
            for item in section:
                if _strip_ns(item.tag) == "item":
                    iid, href = item.attrib.get("id", "").strip(), item.attrib.get("href", "").strip()
                    if iid and href:
                        manifest[iid] = {
                            "href": _resolve_zip_rel(opf_member, href),
                            "media_type": item.attrib.get("media-type", "").strip(),
                            "properties": item.attrib.get("properties", "").strip(),
                        }
        elif sname == "spine":
            ncx_id = section.attrib.get("toc", "").strip()
            for ref in section:
                if _strip_ns(ref.tag) == "itemref" and (idref := ref.attrib.get("idref", "").strip()):
                    spine_ids.append(idref)
    return titles, creators, language, manifest, spine_ids, ncx_id


def parse_epub(path: Path, *, page_char_budget: int = 1800) -> ParsedBook:
    """Parse an EPUB archive using stdlib zipfile + XML/HTML parsers."""
    safe_path = validate_book_file(path, max_bytes=MAX_BOOK_FILE_BYTES)
    if not probe_file_format(safe_path)["is_zip"]:
        raise BookParseError(f"Invalid EPUB archive (not a ZIP file): {safe_path.name}")

    tracker = [0]
    try:
        zf_ctx = zipfile.ZipFile(safe_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise BookParseError(f"Corrupted EPUB archive {safe_path.name}: {exc}") from exc

    with zf_ctx as zf:
        index = _build_zip_index(zf)
        container_info = _lookup_zip_info(index, "META-INF/container.xml")
        if container_info is None:
            raise BookParseError(f"Missing META-INF/container.xml in {safe_path.name}")
        container_xml = _safe_parse_xml(read_zip_member_bounded(zf, container_info, tracker=tracker))

        opf_member = next(
            (
                safe_zip_member_path(elem.attrib["full-path"].strip())
                for elem in container_xml.iter()
                if _strip_ns(elem.tag) == "rootfile" and elem.attrib.get("full-path", "").strip()
            ),
            "",
        )
        if not opf_member:
            raise BookParseError(f"No rootfile declared in container.xml for {safe_path.name}")
        opf_info = _lookup_zip_info(index, opf_member)
        if opf_info is None:
            raise BookParseError(f"OPF manifest file missing: {opf_member}")
        opf_xml = _safe_parse_xml(read_zip_member_bounded(zf, opf_info, tracker=tracker))
        titles, creators, language, manifest, spine_ids, ncx_id = _parse_opf_document(opf_xml, opf_member)

        image_cache: dict[str, str] = {}

        def image_url(member: str) -> str:
            if member in image_cache:
                return image_cache[member]
            info = _lookup_zip_info(index, member)
            if info is None or info.file_size > 1024 * 1024:
                return ""
            raw = read_zip_member_bounded(zf, info, max_bytes=1024 * 1024, tracker=tracker)
            mime = (
                "image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else
                "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else
                "image/gif" if raw.startswith((b"GIF87a", b"GIF89a")) else
                "image/webp" if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP" else ""
            )
            value = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}" if mime else ""
            image_cache[member] = value
            return value

        cover_id = next((node.attrib.get("content", "") for node in opf_xml.iter()
                         if _strip_ns(node.tag) == "meta" and node.attrib.get("name") == "cover"), "")
        cover_item = manifest.get(cover_id) or next((item for item in manifest.values()
            if "cover-image" in item["properties"].split()), None)
        cover_url = image_url(cover_item["href"]) if cover_item else ""

        toc_entries: list[dict[str, str]] = []
        nav_item = next((m for m in manifest.values() if "nav" in m["properties"].split()), None)
        if nav_item and (nav_info := _lookup_zip_info(index, nav_item["href"])) is not None:
            nav_parser = _NavHtmlParser(nav_item["href"])
            nav_parser.feed(read_zip_member_bounded(zf, nav_info, tracker=tracker).decode("utf-8", errors="replace"))
            nav_parser.close()
            toc_entries.extend(nav_parser.entries)
        if not toc_entries:
            ncx_item = manifest.get(ncx_id) if ncx_id else next(
                (m for m in manifest.values() if m["media_type"] == "application/x-dtbncx+xml"), None
            )
            if ncx_item and (ncx_info := _lookup_zip_info(index, ncx_item["href"])) is not None:
                toc_entries.extend(_parse_ncx_toc(read_zip_member_bounded(zf, ncx_info, tracker=tracker), ncx_item["href"]))

        toc_title_by_href = {entry["href"]: entry["title"] for entry in toc_entries}
        pages: list[ParsedPage] = []
        next_page = 1
        for ch_idx, idref in enumerate(spine_ids, start=1):
            item = manifest.get(idref)
            if not item:
                continue
            mtype = item["media_type"].lower()
            if mtype and "html" not in mtype and "xml" not in mtype:
                continue
            member_info = _lookup_zip_info(index, item["href"])
            if member_info is None:
                continue
            raw_html = read_zip_member_bounded(zf, member_info, max_bytes=MAX_EPUB_ENTRY_BYTES, tracker=tracker)
            extractor = _XhtmlBlockExtractor()
            extractor.feed(raw_html.decode("utf-8", errors="replace"))
            extractor.close()
            if not extractor.blocks:
                continue
            first_heading = next((orig for kind, orig, _ in extractor.blocks if kind == "heading"), "")
            ch_title = toc_title_by_href.get(item["href"]) or first_heading or extractor.page_title or f"Chapter {ch_idx}"
            ch_pages = paginate_blocks(ch_title, extractor.blocks, start_page=next_page, char_budget=page_char_budget)
            image_sources = iter(extractor.image_sources)
            for parsed_page in ch_pages:
                for block in parsed_page.blocks:
                    if block.kind == "image":
                        src = next(image_sources, "")
                        if src and not urllib.parse.urlsplit(src).scheme and not src.startswith("//"):
                            block.image_url = image_url(_resolve_zip_rel(item["href"], src))
                            if not cover_url:
                                cover_url = block.image_url
            pages.extend(ch_pages)
            next_page += len(ch_pages)

        if not pages:
            pages = [ParsedPage(page_number=1, chapter_title=titles[0] if titles else safe_path.stem, blocks=[])]

        return ParsedBook(
            title=titles[0] if titles else safe_path.stem,
            author=", ".join(creators),
            language=language,
            book_format="epub",
            toc=toc_entries,
            pages=pages,
            extra={"spine_count": len(spine_ids), "toc_count": len(toc_entries), "cover_data_url": cover_url},
        )
