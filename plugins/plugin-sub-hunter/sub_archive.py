"""Secure archive extraction, encoding normalization (UTF-8/GB18030/Big5), and atomic file writes."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

HARD_MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_ZIP_ENTRIES = 200
MAX_ZIP_RATIO = 100
CHUNK_SIZE = 64 * 1024
SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".vtt"})
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")
_MANAGED_DB_NAME = "managed_subtitles.json"


class ArchiveSecurityError(ValueError):
    """Raised when archive/path validation or encoding fails."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ExtractedSubtitle:
    """Validated and UTF-8 decoded subtitle entry."""

    filename: str
    text: str
    encoding: str
    size_bytes: int


def effective_max_bytes(max_download_mb: Any) -> int:
    """Return effective byte budget strictly clamped to [1 MiB, 16 MiB]."""
    try:
        mb = int(max_download_mb)
    except (TypeError, ValueError):
        mb = 16
    clamped = max(1, min(mb, 16))
    return clamped * 1024 * 1024


def validate_safe_fs_path(raw: str | Path, *, allow_nonexistent: bool = True) -> Path:
    """Validate a filesystem path against NUL bytes, '..' segments, and symlinks."""
    raw_str = os.fspath(raw)
    if not isinstance(raw_str, str) or not raw_str.strip() or "\x00" in raw_str:
        raise ArchiveSecurityError("invalid_argument", "文件路径无效或为空")
    p = Path(raw_str).expanduser()
    if ".." in p.parts:
        raise ArchiveSecurityError("path_safety", f"拒绝包含路径穿越 '..' 的路径: {raw_str}")
    if p.is_symlink() or p.parent.is_symlink():
        raise ArchiveSecurityError("path_safety", f"拒绝访问符号链接路径: {p}")
    if not allow_nonexistent and not p.exists():
        raise ArchiveSecurityError("not_found", f"路径不存在: {p}")
    return p.resolve()


def resolve_download_dir(
    output_dir: str | None,
    download_root: str | None,
    fallback_dir: Path,
) -> Path:
    """Resolve and validate the target directory for downloaded subtitles."""
    root_resolved: Path | None = None
    if isinstance(download_root, str) and download_root.strip():
        root_resolved = validate_safe_fs_path(download_root.strip(), allow_nonexistent=True)

    if isinstance(output_dir, str) and output_dir.strip():
        candidate = validate_safe_fs_path(output_dir.strip(), allow_nonexistent=True)
        if root_resolved is not None:
            try:
                rel = candidate.relative_to(root_resolved)
            except ValueError as exc:
                raise ArchiveSecurityError(
                    "path_safety",
                    f"目标目录 {candidate} 超出配置的 download_root {root_resolved}",
                ) from exc
            cur = root_resolved
            for part in rel.parts:
                cur = cur / part
                if cur.is_symlink():
                    raise ArchiveSecurityError("path_safety", f"拒绝穿过符号链接目录: {cur}")
        return candidate

    return root_resolved if root_resolved is not None else validate_safe_fs_path(fallback_dir, allow_nonexistent=True)


def sanitize_subtitle_filename(name: str, fallback: str = "subtitle.srt") -> str:
    """Normalize an archive/URL filename into a safe single-level filename."""
    cleaned = name.replace("\\", "/").split("/")[-1].strip()
    cleaned = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", cleaned)
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned


def _validate_zip_member(info: zipfile.ZipInfo, max_bytes: int) -> None:
    name = info.filename
    if not name or "\x00" in name or "\\" in name or name.startswith("/") or _DRIVE_PREFIX_RE.match(name):
        raise ArchiveSecurityError("path_safety", f"压缩包条目路径不安全，已拒绝: {name!r}")
    parts = PurePosixPath(name).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveSecurityError("path_safety", f"压缩包包含路径穿越条目，已拒绝: {name!r}")

    unix_mode = info.external_attr >> 16
    fmt = stat.S_IFMT(unix_mode)
    if stat.S_ISLNK(unix_mode) or (unix_mode != 0 and fmt not in {0, stat.S_IFREG, stat.S_IFDIR}):
        raise ArchiveSecurityError("path_safety", f"压缩包包含符号链接或特殊文件，已拒绝: {name!r}")

    if info.file_size < 0 or info.file_size > max_bytes:
        raise ArchiveSecurityError("validation_error", f"压缩包条目 {name!r} 超过大小限制 ({max_bytes} bytes)")
    if info.file_size > 1024 * 1024 and (
        info.compress_size <= 0 or info.file_size > MAX_ZIP_RATIO * max(1, info.compress_size)
    ):
        raise ArchiveSecurityError("validation_error", f"检测到异常压缩比 (zip bomb): {name!r}")


def _read_bounded_stream(stream: io.BufferedIOBase | Any, remaining_budget: int, label: str) -> bytes:
    buf = bytearray()
    while True:
        chunk = stream.read(CHUNK_SIZE)
        if not chunk:
            break
        if len(buf) + len(chunk) > remaining_budget:
            raise ArchiveSecurityError("validation_error", f"{label} 解压后超过体积上限，已中止")
        buf.extend(chunk)
    return bytes(buf)


def _score_cjk_decode(text: str, raw: bytes, encoding: str, lang_hint: str) -> tuple[int, int, int]:
    """Return a quality tuple (higher is better) to disambiguate GB18030 vs Big5."""
    pua_count = sum(1 for ch in text if "\ue000" <= ch <= "\uf8ff" or ch == "\ufffd")
    cjk_basic = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    cjk_ext = sum(1 for ch in text if "\u3400" <= ch <= "\u4dbf")
    rare_big5_pairs = 0
    if encoding == "big5":
        i = 0
        while i < len(raw):
            b = raw[i]
            if b >= 0x81 and i + 1 < len(raw):
                if b >= 0xC9:
                    rare_big5_pairs += 1
                i += 2
            else:
                i += 1
    hint_bonus = 0
    low_hint = lang_hint.lower()
    if encoding == "big5" and any(tag in low_hint for tag in ("cht", "zh-tw", "zh-hk", "hant", "big5")):
        hint_bonus = 5
    elif encoding == "gb18030" and any(tag in low_hint for tag in ("chs", "zh-cn", "hans", "gb")):
        hint_bonus = 5
    gb2312_bonus = 0
    if encoding == "gb18030":
        try:
            raw.decode("gb2312")
            gb2312_bonus = 3
        except UnicodeError:
            pass
    return (-pua_count * 100 - rare_big5_pairs * 2 - cjk_ext, hint_bonus + gb2312_bonus, cjk_basic)


def decode_subtitle_bytes(
    data: bytes,
    *,
    charset_hint: str = "",
    lang_hint: str = "",
) -> tuple[str, str]:
    """Decode UTF-16, UTF-8/UTF-8-SIG, GB18030, or Big5 subtitle bytes into UTF-8 text."""
    if not data:
        raise ArchiveSecurityError("validation_error", "字幕文件内容为空")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeError as exc:
            raise ArchiveSecurityError("validation_error", f"UTF-16 字幕解码失败: {exc}") from exc

    hint = charset_hint.strip().lower()
    if hint in {"utf-8", "utf8", "utf-8-sig"}:
        try:
            return data.decode("utf-8-sig"), "utf-8"
        except UnicodeError:
            pass

    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeError:
        pass

    candidates: list[tuple[tuple[int, int, int], str, str]] = []
    preferred_order = ("big5", "gb18030") if hint in {"big5", "big5hkscs"} else ("gb18030", "big5")
    for enc in preferred_order:
        try:
            decoded = data.decode(enc)
        except UnicodeError:
            continue
        if hint and (hint == enc or (hint in {"gbk", "gb2312"} and enc == "gb18030")):
            return decoded, enc
        candidates.append((_score_cjk_decode(decoded, data, enc, lang_hint), enc, decoded))

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        _, best_enc, best_text = candidates[0]
        return best_text, best_enc

    raise ArchiveSecurityError("validation_error", "字幕编码无法识别（仅支持 UTF-8 / UTF-16 / GB18030 / Big5）")


def _looks_like_text_subtitle(data: bytes) -> str | None:
    sample = data[:4096].lstrip(b"\xef\xbb\xbf")
    if sample.startswith(b"WEBVTT"):
        return ".vtt"
    if b"[Script Info]" in sample or b"Dialogue:" in sample:
        return ".ass"
    if re.search(rb"\d{1,3}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,3}:\d{2}:\d{2}[,.]\d{3}", sample):
        return ".srt"
    return None


def extract_subtitle_payload(
    data: bytes,
    filename: str,
    *,
    max_bytes: int = HARD_MAX_DOWNLOAD_BYTES,
    charset_hint: str = "",
    lang_hint: str = "",
) -> list[ExtractedSubtitle]:
    """Safely extract and decode subtitle file(s) from raw text, gzip, or zip archives."""
    limit = min(max_bytes, HARD_MAX_DOWNLOAD_BYTES)
    if len(data) > limit:
        raise ArchiveSecurityError("validation_error", f"下载内容超过最大限制 ({limit} bytes)")

    if data.startswith(b"Rar!"):
        raise ArchiveSecurityError("validation_error", "不支持 RAR 压缩包，请选择 ZIP 或单文件字幕")

    stream = io.BytesIO(data)
    if zipfile.is_zipfile(stream):
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream) as zf:
                members = zf.infolist()
                if len(members) > MAX_ZIP_ENTRIES:
                    raise ArchiveSecurityError("validation_error", f"压缩包条目数过多 ({len(members)} > {MAX_ZIP_ENTRIES})")
                total_declared = 0
                for info in members:
                    _validate_zip_member(info, limit)
                    if not info.is_dir():
                        total_declared += info.file_size
                if total_declared > limit:
                    raise ArchiveSecurityError("validation_error", f"压缩包声明总解压大小超过限制 ({limit} bytes)")

                extracted: list[ExtractedSubtitle] = []
                remaining = limit
                for info in members:
                    if info.is_dir():
                        continue
                    suffix = PurePosixPath(info.filename).suffix.lower()
                    if suffix not in SUBTITLE_EXTENSIONS:
                        continue
                    with zf.open(info, "r") as member_fp:
                        raw_member = _read_bounded_stream(member_fp, remaining, info.filename)
                    remaining -= len(raw_member)
                    text, enc = decode_subtitle_bytes(raw_member, charset_hint=charset_hint, lang_hint=lang_hint)
                    safe_name = sanitize_subtitle_filename(info.filename, fallback=f"subtitle{suffix}")
                    extracted.append(ExtractedSubtitle(safe_name, text, enc, len(raw_member)))
        except ArchiveSecurityError:
            raise
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError) as exc:
            raise ArchiveSecurityError("validation_error", f"ZIP 压缩包损坏或校验失败: {exc}") from None
        if not extracted:
            raise ArchiveSecurityError("validation_error", "压缩包中未找到受支持的字幕文件 (.srt/.ass/.ssa/.vtt)")
        return extracted

    if data[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
                data = _read_bounded_stream(gz, limit, filename or "subtitle.gz")
        except ArchiveSecurityError:
            raise
        except (zlib.error, EOFError, OSError) as exc:
            raise ArchiveSecurityError("validation_error", f"GZIP 压缩包损坏或校验失败: {exc}") from None
        filename = filename[:-3] if filename.lower().endswith(".gz") else filename

    ext = Path(filename).suffix.lower()
    if ext not in SUBTITLE_EXTENSIONS:
        sniffed = _looks_like_text_subtitle(data)
        if sniffed is None:
            raise ArchiveSecurityError("validation_error", f"不支持的字幕文件格式: {filename or '(unknown)'}")
        filename = f"{Path(filename).stem or 'subtitle'}{sniffed}"
        ext = sniffed

    text, enc = decode_subtitle_bytes(data, charset_hint=charset_hint, lang_hint=lang_hint)
    safe_name = sanitize_subtitle_filename(filename, fallback=f"subtitle{ext}")
    return [ExtractedSubtitle(safe_name, text, enc, len(data))]


def atomic_write_text(target: Path, content: str, *, mode: int = 0o644) -> None:
    """Atomically write UTF-8 text using a sibling temp file, fsync, and os.replace."""
    if target.is_symlink():
        raise ArchiveSecurityError("path_safety", f"拒绝覆盖符号链接目标: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".omnidesk.tmp",
        delete=False,
    )
    try:
        tmp_path = Path(handle.name)
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(tmp_path, target)
    except BaseException:
        try:
            if not handle.closed:
                handle.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


def _load_managed_index(data_dir: Path) -> dict[str, str]:
    idx_path = data_dir / _MANAGED_DB_NAME
    if idx_path.is_symlink() or not idx_path.is_file():
        return {}
    try:
        raw = json.loads(idx_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def record_managed_subtitle(data_dir: Path, target: Path, utf8_content: str) -> None:
    """Record SHA-256 of an OmniDesk-written subtitle so manual edits are still protected."""
    data_dir.mkdir(parents=True, exist_ok=True)
    idx_path = data_dir / _MANAGED_DB_NAME
    index = _load_managed_index(data_dir)
    digest = hashlib.sha256(utf8_content.encode("utf-8")).hexdigest()
    index[str(target.resolve())] = digest
    atomic_write_text(idx_path, json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n", mode=0o600)


def check_overwrite_allowed(target: Path, data_dir: Path, policy: str) -> str:
    """Validate whether `target` can be written under `policy`; returns 'write' or 'skip'."""
    if target.is_symlink():
        raise ArchiveSecurityError("path_safety", f"拒绝写入符号链接目标: {target}")
    if not target.exists():
        return "write"
    if policy == "overwrite":
        return "write"
    if policy == "skip_existing":
        return "skip"

    # Default policy: protect_manual
    index = _load_managed_index(data_dir)
    recorded_hash = index.get(str(target.resolve()))
    if recorded_hash:
        try:
            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            current_hash = ""
        if current_hash == recorded_hash:
            return "write"
    raise ArchiveSecurityError(
        "conflict",
        f"目标字幕已存在且受人工精校保护，默认拒绝覆盖: {target.name}",
    )
