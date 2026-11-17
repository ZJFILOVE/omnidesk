"""Shared internal book parsing models, format probe, and paragraph pagination."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnidesk_sdk import BookFormat, BookPageBlock

CALIBRE_FORMATS = frozenset(['mobi', 'azw3', 'azw', 'azw4', 'fb2', 'fbz', 'rtf', 'html', 'htm', 'htmlz', 'docx', 'odt', 'cbz', 'cbr', 'cb7', 'chm', 'djvu', 'lit', 'lrf', 'prc', 'pdb', 'pml', 'rb', 'snb', 'tcr', 'txtz', 'kepub'])
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".epub", ".txt"} | {f".{fmt}" for fmt in CALIBRE_FORMATS})
_SPACE_RE = re.compile(r"\s+")
_MAX_BLOCKS_PER_PAGE = 12


@dataclass(slots=True)
class ParsedPage:
    """Single paginated spread of extracted blocks."""

    page_number: int
    chapter_title: str
    blocks: list[BookPageBlock]


@dataclass(slots=True)
class ParsedBook:
    """Normalized book metadata, TOC, and paginated spreads."""

    title: str
    author: str
    language: str
    book_format: BookFormat
    toc: list[dict[str, str]] = field(default_factory=list)
    pages: list[ParsedPage] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def collapse_ws(text: str) -> str:
    return _SPACE_RE.sub(" ", text).strip()


def paginate_blocks(
    chapter_title: str,
    raw_blocks: Sequence[tuple[str, str, str]],
    *,
    start_page: int,
    char_budget: int,
) -> list[ParsedPage]:
    """Group paragraph/heading/image blocks into pages without splitting paragraphs."""
    budget = max(200, int(char_budget))
    pages: list[ParsedPage] = []
    cur_blocks: list[BookPageBlock] = []
    cur_chars = 0
    page_num = start_page

    def _flush() -> None:
        nonlocal cur_blocks, cur_chars, page_num
        if not cur_blocks:
            return
        pages.append(ParsedPage(page_number=page_num, chapter_title=chapter_title, blocks=cur_blocks))
        page_num += 1
        cur_blocks = []
        cur_chars = 0

    for kind, orig, trans in raw_blocks:
        est = len(orig) + len(trans)
        if cur_blocks and (cur_chars + est > budget or len(cur_blocks) >= _MAX_BLOCKS_PER_PAGE):
            _flush()
        idx = len(cur_blocks) + 1
        cur_blocks.append(
            BookPageBlock(
                id=f"p{page_num}-b{idx}",
                kind=kind,  # type: ignore[arg-type]
                original=orig,
                translated=trans,
            )
        )
        cur_chars += est

    _flush()
    return pages


def probe_file_format(path: Path) -> dict[str, Any]:
    """Inspect extension and leading magic bytes safely without following symlinks."""
    suffix = path.suffix.lower()
    fmt_map = {ext: ext.lstrip(".") for ext in SUPPORTED_EXTENSIONS}
    declared = fmt_map.get(suffix, "")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            header = handle.read(128)
    finally:
        if fd >= 0:
            os.close(fd)

    is_zip = header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    mobi_magic = len(header) >= 68 and header[60:68] in (b"BOOKMOBI", b"TEXtREAd")
    has_nul = b"\x00" in header
    detected = declared
    if is_zip and suffix == ".epub":
        detected = "epub"
    elif mobi_magic and suffix in {".mobi", ".azw3"}:
        detected = suffix.lstrip(".")
    elif declared == "txt" and (is_zip or mobi_magic or has_nul):
        detected = "binary"
    return {
        "format": detected,
        "declared_format": declared,
        "suffix": suffix,
        "is_zip": is_zip,
        "is_mobi_magic": mobi_magic,
        "has_nul": has_nul,
    }
