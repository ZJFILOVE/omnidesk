"""JSON & HTML response normalization for Z-Library search and book detail payloads."""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from zlib_html import ZlibDetailHTMLParser, ZlibSearchHTMLParser, clean_html_text

_BOOK_HREF_RE = re.compile(r"^/book/([A-Za-z0-9_-]+)(?:/([A-Za-z0-9_-]+))?(?:/|$)")
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


def parse_item_id(raw_id: str) -> tuple[str, str]:
    """Parse `book_id` and optional `book_hash` from `id`, `id:hash`, or `id/hash`."""
    cleaned = clean_text(raw_id)
    if not cleaned or "\x00" in cleaned or ".." in cleaned or "\\" in cleaned:
        return "", ""
    if ":" in cleaned:
        left, right = cleaned.split(":", 1)
        if re.fullmatch(r"[A-Za-z0-9_-]+", left) and re.fullmatch(r"[A-Za-z0-9_-]*", right):
            return left, right
        return "", ""
    if "/" in cleaned:
        parts = [p for p in cleaned.split("/") if p]
        if len(parts) == 2 and all(re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in parts):
            return parts[0], parts[1]
        return "", ""
    if re.fullmatch(r"[A-Za-z0-9_-]+", cleaned):
        return cleaned, ""
    return "", ""


def join_base_url(base_url: str, href: str) -> str:
    """Resolve a relative site path against `base_url` without altering scheme/host unintentionally."""
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


def _extract_id_hash_from_href(href: str) -> tuple[str, str]:
    path = urllib.parse.urlsplit(href).path
    match = _BOOK_HREF_RE.search(path)
    if not match:
        return "", ""
    return match.group(1) or "", match.group(2) or ""


def _normalize_book_entry(entry: Mapping[str, Any], base_url: str) -> dict[str, Any] | None:
    book_id = clean_text(entry.get("id") or entry.get("item_id"))
    book_hash = clean_text(entry.get("hash") or entry.get("book_hash"))
    href = clean_text(entry.get("href") or entry.get("detail_url") or entry.get("url"))
    if (not book_id or not book_hash) and href:
        parsed_id, parsed_hash = _extract_id_hash_from_href(href)
        book_id = book_id or parsed_id
        book_hash = book_hash or parsed_hash
    title = clean_text(entry.get("title"))
    if not title or not book_id:
        return None
    item_id = f"{book_id}:{book_hash}" if book_hash else book_id
    detail_url = join_base_url(base_url, href) if href else join_base_url(
        base_url, f"/book/{book_id}/{book_hash}" if book_hash else f"/book/{book_id}"
    )
    dl_raw = clean_text(entry.get("download_token") or entry.get("dl") or entry.get("download_url") or entry.get("download"))
    download_token = dl_raw or (f"/dl/{book_id}/{book_hash}" if book_hash else f"/dl/{book_id}")
    return {
        "item_id": item_id,
        "id": item_id,
        "title": title,
        "author": clean_text(entry.get("author") or entry.get("authors")),
        "year": clean_text(entry.get("year")),
        "format": normalize_format(entry.get("format") or entry.get("extension")),
        "size": format_size(entry.get("filesizeString") or entry.get("size") or entry.get("filesize")),
        "language": clean_text(entry.get("language") or entry.get("lang")),
        "detail_url": detail_url,
        "download_token": download_token,
        "source": "plugin-zlib",
    }


def parse_zlib_search_response(body: bytes, content_type: str, base_url: str) -> list[dict[str, Any]]:
    """Parse Z-Library EAPI JSON or HTML search page into normalized result entries."""
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    is_json = "json" in content_type.lower() or text.startswith(("{", "["))
    if is_json:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        raw_list = payload.get("books") or payload.get("results") or payload.get("items") or [] if isinstance(payload, Mapping) else payload
        if isinstance(raw_list, list):
            out = []
            for entry in raw_list:
                if isinstance(entry, Mapping):
                    norm = _normalize_book_entry(entry, base_url)
                    if norm is not None:
                        out.append(norm)
            return out
        return []

    parser = ZlibSearchHTMLParser()
    parser.feed(text)
    parser.close()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in parser.items:
        norm = _normalize_book_entry(raw, base_url)
        if norm is not None and norm["item_id"] not in seen:
            seen.add(norm["item_id"])
            out.append(norm)
    return out


def parse_zlib_detail_response(
    body: bytes,
    content_type: str,
    base_url: str,
    *,
    requested_id: str,
    detail_url: str,
) -> dict[str, Any] | None:
    """Parse Z-Library book detail JSON/HTML into normalized metadata & available formats."""
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
            if payload.get("success") == 0:
                return None
            book = payload.get("book") if isinstance(payload.get("book"), Mapping) else payload
            if not isinstance(book, Mapping) or not clean_text(book.get("title")):
                return None
            book_id = clean_text(book.get("id") or requested_id)
            book_hash = clean_text(book.get("hash"))
            item_id = f"{book_id}:{book_hash}" if book_hash and ":" not in book_id else book_id
            fmt = normalize_format(book.get("extension") or book.get("format"))
            size = format_size(book.get("filesizeString") or book.get("size") or book.get("filesize"))
            dl_token = clean_text(book.get("download_token") or book.get("dl") or book.get("download_url"))
            if not dl_token:
                dl_token = f"/dl/{book_id}/{book_hash}" if book_hash else f"/dl/{book_id}"
            formats_out: list[dict[str, Any]] = []
            raw_formats = payload.get("formats") or book.get("formats") or payload.get("otherFormats") or []
            if isinstance(raw_formats, list):
                for fmt_entry in raw_formats:
                    if isinstance(fmt_entry, Mapping):
                        efmt = normalize_format(fmt_entry.get("format") or fmt_entry.get("extension"))
                        edl = clean_text(fmt_entry.get("download_token") or fmt_entry.get("dl") or fmt_entry.get("href"))
                        if efmt or edl:
                            formats_out.append({
                                "format": efmt or fmt,
                                "size": format_size(fmt_entry.get("size") or fmt_entry.get("filesizeString") or fmt_entry.get("filesize")),
                                "download_token": edl or dl_token,
                            })
            if not formats_out and fmt:
                formats_out.append({"format": fmt, "size": size, "download_token": dl_token})
            return {
                "item_id": item_id,
                "id": item_id,
                "title": clean_text(book.get("title")),
                "author": clean_text(book.get("author") or book.get("authors")),
                "year": clean_text(book.get("year")),
                "publisher": clean_text(book.get("publisher")),
                "isbn": clean_text(book.get("isbn") or book.get("isbn13") or book.get("isbn10")),
                "language": clean_text(book.get("language")),
                "pages": clean_text(book.get("pages")),
                "description": clean_text(book.get("description")),
                "format": fmt,
                "size": size,
                "detail_url": join_base_url(base_url, str(book.get("href") or detail_url)),
                "download_token": dl_token,
                "formats": formats_out,
                "available_formats": list(dict.fromkeys(f["format"] for f in formats_out if f.get("format"))),
                "source": "plugin-zlib",
            }

    parser = ZlibDetailHTMLParser()
    parser.feed(text)
    parser.close()
    meta = parser.meta
    if not clean_text(meta.get("title")):
        return None
    bid, bhash = parse_item_id(requested_id)
    default_dl = f"/dl/{bid}/{bhash}" if bhash else (f"/dl/{bid}" if bid else "")
    formats_out = [f for f in meta.get("formats", []) if isinstance(f, Mapping)]
    primary_fmt = normalize_format(meta.get("format") or (formats_out[0]["format"] if formats_out else ""))
    primary_dl = clean_text((formats_out[0]["download_token"] if formats_out else "") or default_dl)
    if not formats_out and primary_fmt and primary_dl:
        formats_out = [{"format": primary_fmt, "size": clean_text(meta.get("size")), "download_token": primary_dl}]
    return {
        "item_id": requested_id,
        "id": requested_id,
        "title": clean_text(meta.get("title")),
        "author": clean_text(meta.get("author")),
        "year": clean_text(meta.get("year")),
        "publisher": clean_text(meta.get("publisher")),
        "isbn": clean_text(meta.get("isbn") or meta.get("isbn 13") or meta.get("isbn 10")),
        "language": clean_text(meta.get("language")),
        "pages": clean_text(meta.get("pages")),
        "description": clean_text(meta.get("description")),
        "format": primary_fmt,
        "size": clean_text(meta.get("size")),
        "detail_url": detail_url,
        "download_token": primary_dl,
        "formats": formats_out,
        "available_formats": list(dict.fromkeys(f["format"] for f in formats_out if f.get("format"))),
        "source": "plugin-zlib",
    }


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
