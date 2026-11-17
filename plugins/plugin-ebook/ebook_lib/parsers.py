"""Format dispatch, TXT UTF-8/GB18030 parser, and Calibre MOBI/AZW3 converter."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from omnidesk_sdk import BookFormat

from .epub_parser import parse_epub
from .errors import MAX_BOOK_FILE_BYTES, MAX_TXT_FILE_BYTES, BookParseError, FormatUnavailableError
from .models_internal import SUPPORTED_EXTENSIONS, ParsedBook, ParsedPage, collapse_ws, paginate_blocks, probe_file_format
from .security import read_bytes_nofollow, validate_book_file

_CHAPTER_HEAD_RE = re.compile(
    r"^(?:Chapter\s+\d+[\s:.-]*.*|PART\s+[IVXLC\d]+.*|第[0-9零一二三四五六七八九十百千万两]+[章回节卷集部].*)$",
    re.IGNORECASE,
)


def _decode_txt_bytes(raw: bytes) -> tuple[str, str]:
    if b"\x00" in raw:
        raise BookParseError("Binary NUL bytes detected in TXT file")
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            pass
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise BookParseError("Unable to decode TXT file as UTF-8 or GB18030")


def _split_long_paragraph(para: str, budget: int) -> list[str]:
    if len(para) <= budget:
        return [para]
    lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
    if len(lines) > 1:
        return lines
    chunks: list[str] = []
    remaining = para
    while len(remaining) > budget:
        cut = remaining.rfind(" ", 0, budget)
        if cut < budget // 2:
            cut = budget
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def parse_txt(path: Path, *, page_char_budget: int = 1800) -> ParsedBook:
    """Parse a TXT book with UTF-8 / GB18030 detection and paragraph pagination."""
    safe_path = validate_book_file(path, max_bytes=MAX_TXT_FILE_BYTES)
    raw = read_bytes_nofollow(safe_path, max_bytes=MAX_TXT_FILE_BYTES)
    text, encoding = _decode_txt_bytes(raw)
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")

    raw_paras = [p.strip() for p in re.split(r"\n\s*\n", normalized) if p.strip()]
    budget = max(200, int(page_char_budget))
    sections: list[tuple[str, list[tuple[str, str, str]]]] = []
    cur_title = safe_path.stem
    cur_blocks: list[tuple[str, str, str]] = []
    toc: list[dict[str, str]] = []

    for para in raw_paras:
        single_line = collapse_ws(para)
        if len(single_line) <= 80 and "\n" not in para and _CHAPTER_HEAD_RE.match(single_line):
            if cur_blocks:
                sections.append((cur_title, cur_blocks))
                cur_blocks = []
            cur_title = single_line
            toc.append({"title": single_line, "href": f"section-{len(toc) + 1}"})
            cur_blocks.append(("heading", single_line, ""))
        else:
            for chunk in _split_long_paragraph(para, budget):
                cleaned = collapse_ws(chunk)
                if cleaned:
                    cur_blocks.append(("paragraph", cleaned, ""))

    if cur_blocks:
        sections.append((cur_title, cur_blocks))

    pages: list[ParsedPage] = []
    next_page = 1
    for sec_title, sec_blocks in sections:
        sec_pages = paginate_blocks(sec_title, sec_blocks, start_page=next_page, char_budget=budget)
        pages.extend(sec_pages)
        next_page += len(sec_pages)

    if not pages:
        pages = [ParsedPage(page_number=1, chapter_title=safe_path.stem, blocks=[])]

    return ParsedBook(
        title=safe_path.stem,
        author="",
        language="",
        book_format="txt",
        toc=toc,
        pages=pages,
        extra={"encoding": encoding},
    )


def resolve_calibre_bin(calibre_bin: str) -> str | None:
    """Resolve user-configured `ebook-convert` binary without falling back to empty config."""
    cleaned = (calibre_bin or "").strip()
    if not cleaned or "\x00" in cleaned:
        return None
    if "/" in cleaned or "\\" in cleaned:
        cand = Path(cleaned).expanduser()
        return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None
    return shutil.which(cleaned)


async def convert_and_parse_mobi(
    path: Path,
    book_format: BookFormat,
    calibre_bin: str,
    exec_subprocess: Callable[..., Awaitable[Any]],
    *,
    page_char_budget: int = 1800,
) -> ParsedBook:
    """Convert MOBI/AZW3 using configured ebook-convert; fail closed if unavailable."""
    safe_path = validate_book_file(path, max_bytes=MAX_BOOK_FILE_BYTES)
    resolved = resolve_calibre_bin(calibre_bin)
    if resolved is None:
        raise FormatUnavailableError(
            f"Format {book_format!r} requires a valid calibre_bin (ebook-convert); refusing binary fallback"
        )
    with tempfile.TemporaryDirectory(prefix="omnidesk-calibre-") as tmpdir:
        out_epub = Path(tmpdir) / "converted.epub"
        res = await exec_subprocess([resolved, str(safe_path), str(out_epub)], timeout_sec=30.0)
        if not getattr(res, "ok", False) or not out_epub.is_file() or out_epub.is_symlink():
            raise FormatUnavailableError(
                f"Calibre conversion failed for {safe_path.name}; refusing binary fallback"
            )
        parsed = parse_epub(out_epub, page_char_budget=page_char_budget)
        parsed.book_format = book_format
        if parsed.title == "converted" or not parsed.title:
            parsed.title = safe_path.stem
        parsed.extra["converted_via"] = Path(resolved).name
        return parsed


def parse_book_file(path: Path, book_format: BookFormat, *, page_char_budget: int = 1800) -> ParsedBook:
    """Synchronously parse local EPUB or TXT; refuse raw MOBI/AZW3 binary parsing."""
    if book_format == "epub":
        return parse_epub(path, page_char_budget=page_char_budget)
    if book_format == "txt":
        return parse_txt(path, page_char_budget=page_char_budget)
    raise FormatUnavailableError(f"Format {book_format!r} requires configured calibre_bin")


__all__ = [
    "MAX_BOOK_FILE_BYTES",
    "MAX_TXT_FILE_BYTES",
    "SUPPORTED_EXTENSIONS",
    "ParsedBook",
    "ParsedPage",
    "convert_and_parse_mobi",
    "parse_book_file",
    "parse_epub",
    "parse_txt",
    "probe_file_format",
    "resolve_calibre_bin",
]
