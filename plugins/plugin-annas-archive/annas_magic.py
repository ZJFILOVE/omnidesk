"""Magic-byte verification, TXT encoding validation, and EPUB zip-bomb inspection for plugin-annas-archive."""

from __future__ import annotations

import codecs
import os
import zipfile
from pathlib import Path

SUPPORTED_BOOK_FORMATS: frozenset[str] = frozenset({"pdf", "epub", "mobi", "azw3", "txt"})
_MAX_ZIP_ENTRIES = 10_000
_MAX_ZIP_RATIO = 50.0
_ZIP_RATIO_MIN_BYTES = 512 * 1024
_BINARY_HEAD_PREFIXES: tuple[bytes, ...] = (
    b"%PDF-",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x7fELF",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"MZ",
)
_HTML_SNIFF_PREFIXES: tuple[bytes, ...] = (b"<!doctype html", b"<html", b"<head", b"<body")


class BookValidationError(ValueError):
    """Raised when downloaded content violates size, magic-byte, or archive safety rules."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


def _validate_epub_zip_safety(path: Path, *, compressed_size: int, max_bytes: int) -> None:
    max_uncompressed = max(16 * 1024 * 1024, min(512 * 1024 * 1024, max_bytes * 10))
    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise BookValidationError("zip_bomb", f"EPUB ZIP 条目过多 ({len(infos)})")
            total_uncompressed = 0
            mimetype_entry: zipfile.ZipInfo | None = None
            has_epub_structure = False
            has_office_structure = False
            for info in infos:
                name = info.filename
                if "\x00" in name or name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
                    raise BookValidationError("path_safety", f"EPUB 内含非法路径条目: {name!r}")
                if info.file_size < 0 or info.file_size > max_uncompressed:
                    raise BookValidationError("zip_bomb", "EPUB 解压体积超出安全限制")
                total_uncompressed += info.file_size
                if total_uncompressed > max_uncompressed:
                    raise BookValidationError("zip_bomb", "EPUB 总解压体积超出安全上限 (疑似 zip 炸弹)")
                if (
                    total_uncompressed > _ZIP_RATIO_MIN_BYTES
                    and (total_uncompressed / max(1, compressed_size)) > _MAX_ZIP_RATIO
                ):
                    raise BookValidationError("zip_bomb", "EPUB 压缩比异常过高 (疑似 zip 炸弹)")
                low = name.lower()
                if low == "mimetype":
                    mimetype_entry = info
                elif low == "meta-inf/container.xml" or low.endswith((".opf", ".xhtml", ".ncx")):
                    has_epub_structure = True
                elif low in {"word/document.xml", "xl/workbook.xml", "ppt/presentation.xml"}:
                    has_office_structure = True
            if mimetype_entry is not None:
                with zf.open(mimetype_entry, "r") as mhandle:
                    raw_mime = mhandle.read(128).strip()
                if not raw_mime.startswith(b"application/epub+zip"):
                    raise BookValidationError("invalid_magic", "ZIP 容器 mimetype 不是 application/epub+zip")
            elif has_office_structure or not has_epub_structure:
                raise BookValidationError("invalid_magic", "ZIP 容器缺少 EPUB 标准结构 (mimetype / container.xml)")
    except zipfile.BadZipFile as exc:
        raise BookValidationError("invalid_magic", f"无效的 EPUB/ZIP 文件结构: {exc}") from exc


def _validate_txt_file(path: Path, head: bytes, *, max_bytes: int) -> None:
    if b"\x00" in head:
        raise BookValidationError("invalid_magic", "TXT 文件包含二进制空字节")
    stripped_head = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if any(stripped_head.startswith(sig) for sig in _BINARY_HEAD_PREFIXES):
        raise BookValidationError("invalid_magic", "文件头为二进制格式而非纯文本 TXT")
    if len(head) >= 68 and head[60:68] in (b"BOOKMOBI", b"TEXtREAd"):
        raise BookValidationError("invalid_magic", "文件头为 MOBI/PalmDOC 而非 TXT")
    if stripped_head.lower().startswith(_HTML_SNIFF_PREFIXES):
        raise BookValidationError("invalid_magic", "响应内容为 HTML 页面而非纯文本 TXT")

    for encoding in ("utf-8", "gb18030"):
        decoder = codecs.getincrementaldecoder(encoding)("strict")
        read_total = 0
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        decoder.decode(b"", final=True)
                        return
                    read_total += len(chunk)
                    if read_total > max_bytes:
                        raise BookValidationError("file_too_large", "TXT 文件大小超过上限")
                    if b"\x00" in chunk:
                        raise BookValidationError("invalid_magic", "TXT 文件包含二进制空字节")
                    decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            continue
    raise BookValidationError("invalid_magic", "TXT 文件编码无效 (非 UTF-8 / GB18030 文本)")


def validate_downloaded_file(path: Path, expected_format: str | None, max_bytes: int) -> str:
    """Verify size cap, format magic (`pdf`/`epub`/`mobi`/`azw3`/`txt`), and zip-bomb safety."""
    size = path.stat().st_size
    if size <= 0:
        raise BookValidationError("invalid_magic", "下载文件为空 (0 字节)")
    if size > max_bytes:
        raise BookValidationError("file_too_large", f"下载文件体积 ({size} B) 超过上限 ({max_bytes} B)")

    with path.open("rb") as handle:
        head = handle.read(4096)

    fmt = (expected_format or "").strip().lower().lstrip(".")
    if fmt and fmt not in SUPPORTED_BOOK_FORMATS:
        raise BookValidationError("invalid_argument", f"不支持的电子书格式: {fmt!r}")

    if not fmt:
        stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
        if stripped.startswith(b"%PDF-"):
            fmt = "pdf"
        elif head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            fmt = "epub"
        elif len(head) >= 68 and head[60:68] in (b"BOOKMOBI", b"TEXtREAd"):
            fmt = "mobi"
        else:
            fmt = "txt"

    if fmt == "pdf":
        if not head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"%PDF-"):
            raise BookValidationError("invalid_magic", "文件魔数不匹配 PDF (%PDF-)")
        return "pdf"

    if fmt == "epub":
        if not head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            raise BookValidationError("invalid_magic", "文件魔数不匹配 EPUB (PK ZIP)")
        _validate_epub_zip_safety(path, compressed_size=size, max_bytes=max_bytes)
        return "epub"

    if fmt in {"mobi", "azw3"}:
        if len(head) < 68 or head[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
            raise BookValidationError("invalid_magic", f"文件魔数不匹配 {fmt.upper()} (BOOKMOBI/TEXtREAd)")
        return fmt

    _validate_txt_file(path, head, max_bytes=max_bytes)
    return "txt"


def finalize_downloaded_file(tmp_path: Path, target_path: Path, expected_format: str | None, max_bytes: int) -> str:
    """Validate downloaded temp file and atomically replace `target_path`, cleaning temp on error."""
    try:
        final_fmt = validate_downloaded_file(tmp_path, expected_format, max_bytes)
        if target_path.is_symlink():
            raise BookValidationError("path_safety", "拒绝覆盖符号链接目标路径")
        os.replace(tmp_path, target_path)
        return final_fmt
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
