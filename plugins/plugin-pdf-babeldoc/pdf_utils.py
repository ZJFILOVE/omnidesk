"""PDF inspection, path safety containment, config parsing, and log redaction helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnidesk_sdk.stores import redact_secrets

_PDF_MAGIC = b"%PDF-"
_MAX_INSPECT_BYTES = 8 * 1024 * 1024
_PAGES_COUNT_RE = re.compile(rb"/Type\s*/Pages\b[^>]{0,512}?/Count\s+(\d+)|/Count\s+(\d+)[^>]{0,512}?/Type\s*/Pages\b", re.DOTALL)
_PAGE_LEAF_RE = re.compile(rb"/Type\s*/Page\b(?!s)")
_INFO_LIT_RE = {
    "title": re.compile(rb"/Title\s*\(([^()\\]*(?:\\.[^()\\]*)*)\)"),
    "author": re.compile(rb"/Author\s*\(([^()\\]*(?:\\.[^()\\]*)*)\)"),
}
_INFO_HEX_RE = {
    "title": re.compile(rb"/Title\s*<([0-9A-Fa-f\s]+)>"),
    "author": re.compile(rb"/Author\s*<([0-9A-Fa-f\s]+)>"),
}
_XMP_TITLE_RE = re.compile(rb"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.DOTALL)
_XMP_AUTHOR_RE = re.compile(rb"<dc:creator>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.DOTALL)
_CLI_SECRET_FLAG_RE = re.compile(
    r"(--(?:openai-api-key|openai-term-extraction-api-key|api-key|token)|-k)(?:\s+|=)([^\s\"']+|\"[^\"]*\"|'[^']*')"
)
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_LANG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


class PathContainmentError(ValueError):
    """Raised when a path contains traversal segments, unsafe symlinks, or escapes allowed roots."""


@dataclass(frozen=True, slots=True)
class PdfMetadata:
    """Lightweight metadata extracted from a verified local PDF file."""

    path: Path
    title: str
    author: str
    total_pages: int
    file_size: int
    page_count_estimated: bool = False


def validate_safe_identifier(value: Any, name: str = "identifier") -> str:
    """Validate an identifier (job_id / operation_id / book_id) against traversal characters."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a non-empty string")
    cleaned = value.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned or "\x00" in cleaned:
        raise ValueError(f"Unsafe {name}: {value!r}")
    if not _SAFE_TOKEN_RE.match(cleaned):
        raise ValueError(f"Invalid {name} format: {value!r}")
    return cleaned


def validate_language_code(value: Any, *, default: str = "zh") -> str:
    """Validate a BabelDOC target/source language code without leading dashes or whitespace."""
    raw = default if value is None or str(value).strip() == "" else str(value).strip()
    if not _SAFE_LANG_RE.match(raw):
        raise ValueError(f"Invalid language code {raw!r}")
    return raw


def redact_diagnostic(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Scrub secret values, bearer tokens, and sensitive CLI flags from diagnostics."""
    if not text:
        return ""
    scrubbed = redact_secrets(text, extra_secrets)
    return _CLI_SECRET_FLAG_RE.sub(r"\1 [REDACTED]", scrubbed)


def redact_command(args: Sequence[str], extra_secrets: Iterable[str] = ()) -> list[str]:
    """Return a copy of command-line args with secret flag values masked."""
    secret_flags = {"--openai-api-key", "--openai-term-extraction-api-key", "--api-key", "-k"}
    redacted: list[str] = []
    mask_next = False
    for part in args:
        if mask_next:
            redacted.append("[REDACTED]")
            mask_next = False
            continue
        if part in secret_flags:
            redacted.append(part)
            mask_next = True
            continue
        matched_prefix = next((f"{flag}=" for flag in secret_flags if part.startswith(f"{flag}=")), None)
        if matched_prefix:
            redacted.append(f"{matched_prefix}[REDACTED]")
            continue
        redacted.append(redact_diagnostic(part, extra_secrets))
    return redacted


def parse_roots_config(raw: Any) -> list[Path]:
    """Parse `pdf_library_roots` from string (JSON array, lines, comma/colon/semicolon) or sequence."""
    if raw is None:
        return []
    entries: list[str] = []
    if isinstance(raw, (list, tuple)):
        entries = [str(item).strip() for item in raw if str(item).strip()]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                entries = [str(item).strip() for item in parsed if str(item).strip()]
        if not entries:
            if "\n" in text:
                parts = text.splitlines()
            elif ";" in text:
                parts = text.split(";")
            elif "," in text:
                parts = text.split(",")
            else:
                parts = text.split(os.pathsep)
            entries = [part.strip() for part in parts if part.strip()]
    else:
        return []

    roots: list[Path] = []
    seen: set[Path] = set()
    for entry in entries:
        if "\x00" in entry:
            continue
        candidate = Path(entry).expanduser()
        if ".." in candidate.parts:
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            roots.append(resolved)
    return roots


def parse_single_root(raw: Any) -> Path | None:
    """Parse `output_root` into a single validated resolved Path or None when unset."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or "\x00" in text:
        return None
    candidate = Path(text).expanduser()
    if ".." in candidate.parts:
        raise PathContainmentError(f"Unsafe root path contains '..': {raw!r}")
    if candidate.is_symlink():
        raise PathContainmentError(f"Refusing symlinked root directory: {candidate}")
    return candidate.resolve()


def _check_no_traversal_syntax(raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        raise PathContainmentError("Path must be a non-empty string without null bytes")
    candidate = Path(raw_path.strip()).expanduser()
    if ".." in candidate.parts:
        raise PathContainmentError(f"Path traversal '..' is forbidden: {raw_path!r}")
    return candidate


def _assert_no_symlink_under_root(root: Path, candidate: Path) -> None:
    """Verify no path segment inside `root` down to `candidate` is a symlink."""
    cur = candidate
    seen: set[Path] = set()
    while cur not in seen:
        seen.add(cur)
        try:
            if cur.resolve() == root:
                return
        except OSError:
            pass
        if cur.is_symlink():
            raise PathContainmentError(f"Refusing symlink inside allowed root: {cur}")
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


def resolve_contained_path(
    raw_path: str,
    allowed_roots: Sequence[Path],
    *,
    allow_relative: bool = False,
) -> Path:
    """Resolve `raw_path` and verify it stays strictly inside one of `allowed_roots`."""
    if not allowed_roots:
        raise PathContainmentError("No allowed roots configured")
    candidate = _check_no_traversal_syntax(raw_path)
    resolved_roots = [r.resolve() for r in allowed_roots]

    if not candidate.is_absolute():
        if not allow_relative:
            raise PathContainmentError(f"Relative path {raw_path!r} is not allowed")
        for root in resolved_roots:
            joined = root / candidate
            if joined.exists() or len(resolved_roots) == 1:
                candidate = joined
                break
        else:
            candidate = resolved_roots[0] / candidate

    if candidate.is_symlink():
        raise PathContainmentError(f"Refusing symlink path: {candidate}")
    resolved = candidate.resolve()
    for root in resolved_roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        _assert_no_symlink_under_root(root, candidate)
        return resolved
    raise PathContainmentError(f"Path {resolved} escapes allowed roots")


def resolve_babeldoc_bin(raw_bin: Any) -> str | None:
    """Resolve configured `babeldoc_bin` command or executable file path (fail closed if missing)."""
    if not isinstance(raw_bin, str):
        return None
    cleaned = raw_bin.strip()
    if not cleaned or "\x00" in cleaned:
        return None
    if "/" in cleaned or "\\" in cleaned:
        candidate = Path(cleaned).expanduser()
        if ".." in candidate.parts:
            return None
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
        except OSError:
            return None
        return None
    found = shutil.which(cleaned)
    if found and os.access(found, os.X_OK):
        return found
    return None


def verify_pdf_magic(pdf_path: Path) -> int:
    """Verify file exists, is a regular non-symlink file, starts with `%PDF-`, and return byte size."""
    if pdf_path.is_symlink():
        raise PathContainmentError(f"Refusing symlinked PDF: {pdf_path}")
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(pdf_path, flags)
    try:
        st = os.fstat(fd)
        head = os.read(fd, 16)
    finally:
        os.close(fd)
    if not head.startswith(_PDF_MAGIC):
        raise ValueError(f"Invalid PDF magic header in {pdf_path.name}: expected %PDF-")
    return st.st_size


def _unescape_pdf_literal(raw: bytes) -> str:
    if raw.startswith((b"\xfe\xff", b"\xff\xfe")):
        return raw.decode("utf-16", errors="replace").strip("\x00 \t\r\n")
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == 0x5C and i + 1 < len(raw):
            nxt = raw[i + 1]
            escapes = {ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12}
            if nxt in escapes:
                out.append(escapes[nxt])
                i += 2
                continue
            if nxt in (ord("\\"), ord("("), ord(")")):
                out.append(nxt)
                i += 2
                continue
            if 48 <= nxt <= 55:
                oct_digits = bytes(raw[i + 1 : i + 4])
                m = re.match(rb"[0-7]{1,3}", oct_digits)
                if m:
                    out.append(int(m.group(0), 8) & 0xFF)
                    i += 1 + len(m.group(0))
                    continue
        out.append(ch)
        i += 1
    data = bytes(out)
    if data.startswith((b"\xfe\xff", b"\xff\xfe")):
        return data.decode("utf-16", errors="replace").strip("\x00 \t\r\n")
    return data.decode("utf-8", errors="replace").strip("\x00 \t\r\n")


def _decode_pdf_hex(raw_hex: bytes) -> str:
    cleaned = re.sub(rb"\s+", b"", raw_hex)
    if len(cleaned) % 2 == 1:
        cleaned += b"0"
    try:
        data = bytes.fromhex(cleaned.decode("ascii"))
    except ValueError:
        return ""
    if data.startswith((b"\xfe\xff", b"\xff\xfe")):
        return data.decode("utf-16", errors="replace").strip("\x00 \t\r\n")
    return data.decode("utf-8", errors="replace").strip("\x00 \t\r\n")


def _extract_info_field(blob: bytes, field: str) -> str:
    m_lit = _INFO_LIT_RE[field].search(blob)
    if m_lit:
        val = _unescape_pdf_literal(m_lit.group(1))
        if val:
            return val
    m_hex = _INFO_HEX_RE[field].search(blob)
    if m_hex:
        val = _decode_pdf_hex(m_hex.group(1))
        if val:
            return val
    xmp_re = _XMP_TITLE_RE if field == "title" else _XMP_AUTHOR_RE
    m_xmp = xmp_re.search(blob)
    if m_xmp:
        return m_xmp.group(1).decode("utf-8", errors="replace").strip()
    return ""


def inspect_pdf_metadata(pdf_path: Path) -> PdfMetadata:
    """Validate PDF magic and extract title, author, page count, and file size in pure Python."""
    file_size = verify_pdf_magic(pdf_path)
    with pdf_path.open("rb") as handle:
        head_blob = handle.read(min(file_size, _MAX_INSPECT_BYTES))
        tail_blob = b""
        if file_size > _MAX_INSPECT_BYTES:
            handle.seek(max(0, file_size - 2 * 1024 * 1024))
            tail_blob = handle.read(2 * 1024 * 1024)
    blob = head_blob + b"\n" + tail_blob

    counts: list[int] = []
    for match in _PAGES_COUNT_RE.finditer(blob):
        raw_num = match.group(1) or match.group(2)
        if raw_num:
            counts.append(int(raw_num))
    estimated = False
    if counts:
        total_pages = max(1, max(counts))
    else:
        leaves = len(_PAGE_LEAF_RE.findall(blob))
        if leaves > 0:
            total_pages = leaves
        else:
            total_pages = 1
            estimated = True

    title = _extract_info_field(blob, "title") or pdf_path.stem
    author = _extract_info_field(blob, "author")
    return PdfMetadata(
        path=pdf_path,
        title=title,
        author=author,
        total_pages=total_pages,
        file_size=file_size,
        page_count_estimated=estimated,
    )


def make_book_id(pdf_path: Path) -> str:
    """Compute a deterministic BookItem ID for a canonical PDF path."""
    digest = hashlib.sha256(str(pdf_path.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"pdf-{digest}"


def is_generated_artifact_name(path: Path) -> bool:
    """Return True for BabelDOC generated mono/dual PDFs so library scans avoid duplicate entries."""
    lower = path.name.lower()
    return lower.endswith((".dual.pdf", "_dual.pdf", ".mono.pdf", "_mono.pdf", ".bilingual.pdf"))
