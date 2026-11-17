"""JSON & HTML response normalization for Anna's Archive search and MD5 detail payloads."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from annas_html import (
    AnnasDetailHTMLParser,
    AnnasSearchHTMLParser,
    SlowDownloadLinkParser,
    clean_html_text,
    parse_meta_badges,
)

_MD5_HREF_RE = re.compile(r"/md5/([0-9a-fA-F]{32}|[A-Za-z0-9_-]{8,64})(?:[/?#]|$)")
_MD5_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$|^[A-Za-z0-9_-]{6,64}$")
clean_text = clean_html_text


def normalize_format(raw: Any) -> str:
    fmt = clean_text(raw).lower().lstrip(".")
    if "," in fmt:
        fmt = fmt.split(",", 1)[0].strip()
    return fmt


def format_size(raw: Any) -> str:
    if raw is None or raw == "":
        return ""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        size = float(raw)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024.0 or unit == "GB":
                return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024.0
    return clean_text(raw)


def parse_annas_item_id(raw_id: str) -> str:
    """Validate and normalize an Anna's Archive MD5 or record identifier."""
    cleaned = clean_text(raw_id)
    if not cleaned or "\x00" in cleaned or ".." in cleaned or "\\" in cleaned or "/" in cleaned:
        return ""
    if cleaned.lower().startswith("md5:"):
        cleaned = cleaned[4:].strip()
    if not _MD5_ID_RE.match(cleaned):
        return ""
    return cleaned.lower() if re.fullmatch(r"[0-9a-fA-F]{32}", cleaned) else cleaned


def join_base_url(base_url: str, href: str) -> str:
    """Resolve a relative Anna's Archive path against `base_url` safely."""
    raw = clean_text(href)
    if not raw:
        return ""
    if raw.startswith("//"):
        base_scheme = urllib.parse.urlsplit(base_url).scheme or "https"
        return f"{base_scheme}:{raw}"
    if "://" in raw:
        return raw
    base = base_url.rstrip("/")
    parsed_base = urllib.parse.urlsplit(base)
    base_prefix = parsed_base.path.rstrip("/")
    if raw.startswith("/"):
        if base_prefix and raw.startswith(base_prefix + "/"):
            return f"{parsed_base.scheme}://{parsed_base.netloc}{raw}"
        return f"{parsed_base.scheme}://{parsed_base.netloc}{base_prefix}{raw}"
    return urllib.parse.urljoin(base + "/", raw)


def _normalize_search_entry(entry: Mapping[str, Any], base_url: str) -> dict[str, Any] | None:
    href = clean_text(entry.get("href") or entry.get("detail_url") or entry.get("url"))
    raw_id = clean_text(entry.get("md5") or entry.get("item_id") or entry.get("id"))
    if not raw_id and href:
        m = _MD5_HREF_RE.search(urllib.parse.urlsplit(href).path)
        if m:
            raw_id = m.group(1)
    item_id = parse_annas_item_id(raw_id)
    title = clean_text(entry.get("title"))
    if not item_id or not title:
        return None
    detail_url = join_base_url(base_url, href) if href else join_base_url(base_url, f"/md5/{item_id}")
    dl_token = clean_text(entry.get("download_token") or entry.get("download_url") or entry.get("dl"))
    if not dl_token:
        dl_token = f"/slow_download/{item_id}/0/0"
    return {
        "item_id": item_id,
        "id": item_id,
        "title": title,
        "author": clean_text(entry.get("author") or entry.get("authors")),
        "year": clean_text(entry.get("year")),
        "format": normalize_format(entry.get("format") or entry.get("extension")),
        "size": format_size(entry.get("size") or entry.get("filesize") or entry.get("filesizeString")),
        "language": clean_text(entry.get("language") or entry.get("lang")),
        "detail_url": detail_url,
        "download_token": dl_token,
        "source": "plugin-annas-archive",
    }


def parse_annas_search_response(body: bytes, content_type: str, base_url: str) -> list[dict[str, Any]]:
    """Parse Anna's Archive HTML search page or JSON mirror response."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    is_json = "json" in content_type.lower() or text.startswith(("{", "["))
    if is_json:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        raw_list = payload.get("results") or payload.get("books") or payload.get("items") or [] if isinstance(payload, Mapping) else payload
        if isinstance(raw_list, list):
            out = []
            for entry in raw_list:
                if isinstance(entry, Mapping):
                    norm = _normalize_search_entry(entry, base_url)
                    if norm is not None:
                        out.append(norm)
            return out
        return []

    parser = AnnasSearchHTMLParser()
    parser.feed(text)
    parser.close()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in parser.items:
        norm = _normalize_search_entry(raw, base_url)
        if norm is not None and norm["item_id"] not in seen:
            seen.add(norm["item_id"])
            out.append(norm)
    return out


def parse_annas_detail_response(
    body: bytes,
    content_type: str,
    base_url: str,
    *,
    requested_id: str,
    detail_url: str,
) -> dict[str, Any] | None:
    """Parse Anna's Archive `/md5/<hash>` HTML or JSON metadata and available formats."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    is_json = "json" in content_type.lower() or text.startswith("{")
    if is_json:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, Mapping):
            if payload.get("error") or payload.get("not_found"):
                return None
            book = payload.get("book") if isinstance(payload.get("book"), Mapping) else payload
            if not isinstance(book, Mapping) or not clean_text(book.get("title")):
                return None
            fmt = normalize_format(book.get("format") or book.get("extension"))
            size = format_size(book.get("size") or book.get("filesize"))
            dl_token = clean_text(book.get("download_token") or book.get("download_url") or f"/slow_download/{requested_id}/0/0")
            formats_out: list[dict[str, Any]] = []
            raw_formats = payload.get("formats") or book.get("formats") or []
            if isinstance(raw_formats, list):
                for entry in raw_formats:
                    if isinstance(entry, Mapping):
                        efmt = normalize_format(entry.get("format") or entry.get("extension") or fmt)
                        edl = clean_text(entry.get("download_token") or entry.get("download_url") or entry.get("href") or dl_token)
                        if efmt or edl:
                            formats_out.append({
                                "format": efmt,
                                "size": format_size(entry.get("size") or entry.get("filesize") or size),
                                "download_token": edl,
                            })
            if not formats_out and fmt:
                formats_out.append({"format": fmt, "size": size, "download_token": dl_token})
            return {
                "item_id": requested_id, "id": requested_id, "md5": requested_id,
                "title": clean_text(book.get("title")), "author": clean_text(book.get("author") or book.get("authors")),
                "year": clean_text(book.get("year")), "publisher": clean_text(book.get("publisher")),
                "isbn": clean_text(book.get("isbn")), "language": clean_text(book.get("language")),
                "description": clean_text(book.get("description")), "format": fmt, "size": size,
                "detail_url": join_base_url(base_url, str(book.get("detail_url") or detail_url)),
                "download_token": dl_token, "formats": formats_out,
                "available_formats": list(dict.fromkeys(f["format"] for f in formats_out if f.get("format"))),
                "source": "plugin-annas-archive",
            }

    parser = AnnasDetailHTMLParser()
    parser.feed(text)
    parser.close()
    meta = parser.meta
    if not clean_text(meta.get("title")):
        return None
    badges = parse_meta_badges(" · ".join(meta.get("meta_lines", [])))
    formats_out = [
        {
            "format": normalize_format(f.get("format") or badges["format"]),
            "size": badges["size"],
            "download_token": clean_text(f.get("download_token")),
            "label": clean_text(f.get("label")),
        }
        for f in meta.get("formats", [])
        if isinstance(f, Mapping)
    ]
    primary_fmt = badges["format"] or (formats_out[0]["format"] if formats_out else "")
    primary_dl = (formats_out[0]["download_token"] if formats_out else "") or f"/slow_download/{requested_id}/0/0"
    if not formats_out and primary_fmt:
        formats_out = [{"format": primary_fmt, "size": badges["size"], "download_token": primary_dl}]
    return {
        "item_id": requested_id, "id": requested_id, "md5": requested_id,
        "title": clean_text(meta.get("title")), "author": clean_text(meta.get("author")),
        "year": badges["year"], "publisher": "", "isbn": "", "language": badges["language"],
        "description": clean_text(meta.get("description")), "format": primary_fmt, "size": badges["size"],
        "detail_url": detail_url, "download_token": primary_dl, "formats": formats_out,
        "available_formats": list(dict.fromkeys(f["format"] for f in formats_out if f.get("format"))),
        "source": "plugin-annas-archive",
    }


def extract_slow_download_link(body: bytes) -> str:
    """Extract an explicit direct download link from a `/slow_download/` HTML page."""
    parser = SlowDownloadLinkParser()
    parser.feed(body.decode("utf-8", errors="replace"))
    parser.close()
    return parser.links[0] if parser.links else ""


def resolve_token_and_format(
    meta: Mapping[str, Any] | None,
    req_token: str,
    req_fmt: str,
    dest_raw: Any,
    supported_formats: frozenset[str],
) -> tuple[str, str]:
    """Select download token and inferred target format from explicit input, metadata, or destination."""
    dl_token, matched_fmt = req_token, req_fmt
    if not dl_token and meta is not None:
        for fmt_entry in meta.get("formats", []):
            if isinstance(fmt_entry, Mapping) and (not req_fmt or fmt_entry.get("format") == req_fmt):
                dl_token = clean_text(fmt_entry.get("download_token"))
                matched_fmt = matched_fmt or normalize_format(fmt_entry.get("format"))
                if dl_token:
                    break
        if not dl_token:
            dl_token = clean_text(meta.get("download_token"))
            matched_fmt = matched_fmt or normalize_format(meta.get("format"))
    if not matched_fmt and isinstance(dest_raw, str):
        dot_pos = dest_raw.rfind(".")
        ext = dest_raw[dot_pos + 1 :].strip().lower() if dot_pos >= 0 else ""
        if ext in supported_formats:
            matched_fmt = ext
    return dl_token, matched_fmt
