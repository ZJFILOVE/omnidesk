"""Path containment, symlink rejection, zip-bomb bounds, and atomic 0600 IO."""

from __future__ import annotations

import errno
import json
import os
import posixpath
import re
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import (
    MAX_BOOK_FILE_BYTES,
    MAX_EPUB_COMPRESSION_RATIO,
    MAX_EPUB_ENTRY_BYTES,
    MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES,
    BookNotFoundError,
    BookParseError,
    EbookPathSafetyError,
    InvalidEbookArgError,
)

if TYPE_CHECKING:
    from omnidesk_sdk import PluginContext

_ZIP_SYMLINK_MODE = 0o120000
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _is_shared_system_root(path: Path) -> bool:
    resolved = path.resolve()
    protected = {Path("/"), Path("/tmp"), Path("/var/tmp"), Path(tempfile.gettempdir()).resolve()}
    home_raw = os.environ.get("HOME", "").strip()
    if home_raw:
        protected.add(Path(home_raw).expanduser().resolve())
    for env_name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "TMPDIR"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            protected.add(Path(raw).expanduser().resolve())
    return resolved in protected


def ensure_private_dir(directory: Path) -> Path:
    """Create directory safely without altering shared roots or following symlinks."""
    if _is_shared_system_root(directory):
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    if directory.is_symlink():
        raise EbookPathSafetyError(f"Refusing symlinked directory: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = directory.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise EbookPathSafetyError(f"Path is not a directory: {directory}")
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def atomic_write_json_0600(target: Path, payload: Mapping[str, Any]) -> None:
    """Atomically write JSON to `target` with 0600 permissions, refusing symlinks."""
    if target.is_symlink():
        raise EbookPathSafetyError(f"Refusing to overwrite symlinked file: {target}")
    ensure_private_dir(target.parent)
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp_path: Path | None = None
    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        tmp_path = Path(tmp_file.name)
        os.fchmod(tmp_file.fileno(), 0o600)
        tmp_file.write(serialized)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
        tmp_file.close()
        os.replace(tmp_path, target)
        os.chmod(target, 0o600)
    except BaseException:
        try:
            if not tmp_file.closed:
                tmp_file.close()
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        raise


def read_bytes_nofollow(path: Path, *, max_bytes: int) -> bytes:
    """Read a regular non-symlinked file bounded by `max_bytes` using O_NOFOLLOW."""
    if "\x00" in str(path):
        raise EbookPathSafetyError("Null byte in path is forbidden")
    if path.is_symlink():
        raise EbookPathSafetyError(f"Refusing symlinked file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        raise BookNotFoundError(f"File not found: {path}") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise EbookPathSafetyError(f"Refusing symlinked file: {path}") from None
        raise BookParseError(f"Failed to open {path}: {exc}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise EbookPathSafetyError(f"Path is not a regular file: {path}")
        if st.st_size > max_bytes:
            raise BookParseError(f"File exceeds size limit ({st.st_size} > {max_bytes} bytes)")
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > max_bytes:
        raise BookParseError(f"File exceeds size limit (> {max_bytes} bytes)")
    return data


def read_json_nofollow(target: Path, *, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any] | None:
    """Safely read a JSON object file without following symlinks."""
    if not target.exists() and not target.is_symlink():
        return None
    raw = read_bytes_nofollow(target, max_bytes=max_bytes)
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BookParseError(f"Invalid JSON in {target}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise BookParseError(f"Expected JSON object in {target}")
    return parsed


def parse_library_roots(raw: Any) -> list[str]:
    """Parse user-configured library_roots from list or delimited string."""
    if raw is None:
        return []
    candidates: list[str] = []
    if isinstance(raw, (list, tuple)):
        candidates = [str(item).strip() for item in raw if item is not None and str(item).strip()]
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    candidates = [str(item).strip() for item in parsed if item is not None and str(item).strip()]
            except json.JSONDecodeError:
                pass
        if not candidates:
            seps = ["\n", ";", ","]
            if os.pathsep not in seps:
                seps.append(os.pathsep)
            parts = [stripped]
            for sep in seps:
                next_parts: list[str] = []
                for chunk in parts:
                    next_parts.extend(chunk.split(sep))
                parts = next_parts
            candidates = [p.strip() for p in parts if p.strip()]
    else:
        raise InvalidEbookArgError("library_roots must be a string or list of directory strings")

    for cand in candidates:
        if "\x00" in cand:
            raise EbookPathSafetyError("Null byte in library_roots entry is forbidden")
    return candidates


def validate_library_root(raw_root: str | Path) -> Path | None:
    """Validate and resolve a configured library root, returning None if absent."""
    text = os.fspath(raw_root)
    if not text or "\x00" in text:
        raise EbookPathSafetyError("Unsafe library root path")
    candidate = Path(text).expanduser()
    if ".." in candidate.parts:
        raise EbookPathSafetyError(f"Path traversal '..' forbidden in library root: {raw_root}")
    if candidate.is_symlink():
        raise EbookPathSafetyError(f"Refusing symlinked library root: {raw_root}")
    if not candidate.exists() or not candidate.is_dir():
        return None
    return candidate.resolve(strict=True)


def _check_no_symlinks_between(root_resolved: Path, target: Path) -> None:
    if target.is_symlink():
        raise EbookPathSafetyError(f"Refusing symlinked path: {target}")
    try:
        rel = target.relative_to(root_resolved)
    except ValueError:
        try:
            rel = target.resolve(strict=False).relative_to(root_resolved)
        except ValueError as exc:
            raise EbookPathSafetyError(f"Path {target} escapes root {root_resolved}") from exc
    cur = root_resolved
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise EbookPathSafetyError(f"Refusing symlink component {cur}")


def resolve_safe_under_root(root: Path, relative_or_child: str | Path) -> Path:
    """Resolve a path strictly inside `root`, rejecting traversal and symlinks."""
    root_resolved = root.resolve(strict=True)
    raw_str = os.fspath(relative_or_child)
    if not raw_str or "\x00" in raw_str:
        raise EbookPathSafetyError("Unsafe empty or null-byte path")
    rel_path = Path(raw_str)
    if ".." in rel_path.parts:
        raise EbookPathSafetyError(f"Path traversal '..' forbidden: {raw_str}")
    candidate = rel_path if rel_path.is_absolute() else (root_resolved / rel_path)
    _check_no_symlinks_between(root_resolved, candidate)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise EbookPathSafetyError(f"Path {resolved} escapes root {root_resolved}") from exc
    return resolved


def validate_book_file(
    path: Path,
    *,
    roots: Sequence[Path] | None = None,
    max_bytes: int = MAX_BOOK_FILE_BYTES,
) -> Path:
    """Ensure `path` is a regular non-symlink file within allowed size and optional roots."""
    if "\x00" in str(path):
        raise EbookPathSafetyError("Null byte in book path is forbidden")
    if ".." in path.parts:
        raise EbookPathSafetyError(f"Path traversal '..' forbidden in book path: {path}")
    if path.is_symlink():
        raise EbookPathSafetyError(f"Refusing symlinked book file: {path}")
    if not path.exists():
        raise BookNotFoundError(f"Book file not found: {path}")
    if not path.is_file():
        raise EbookPathSafetyError(f"Book path is not a regular file: {path}")
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise EbookPathSafetyError(f"Refusing non-regular book path: {path}")
    if st.st_size > max_bytes:
        raise BookParseError(f"Book file exceeds maximum size ({st.st_size} > {max_bytes} bytes)")
    resolved = path.resolve(strict=True)
    if roots is not None:
        matched_root = next((r.resolve(strict=True) for r in roots if resolved.is_relative_to(r.resolve(strict=True))), None)
        if matched_root is None:
            raise EbookPathSafetyError(f"Book path {resolved} is outside configured library_roots")
        _check_no_symlinks_between(matched_root, path)
    return resolved


def iter_safe_files(roots: Iterable[Path], allowed_suffixes: frozenset[str]) -> list[tuple[Path, Path]]:
    """Walk `roots` without following symlinks and return deduplicated `(root, file_path)` pairs."""
    seen: set[Path] = set()
    results: list[tuple[Path, Path]] = []
    for raw_root in roots:
        if raw_root.is_symlink() or not raw_root.is_dir():
            continue
        root_res = raw_root.resolve(strict=True)
        stack: list[Path] = [root_res]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                entry_path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry_path)
                elif entry.is_file(follow_symlinks=False) and entry_path.suffix.lower() in allowed_suffixes:
                    try:
                        resolved = entry_path.resolve(strict=True)
                    except OSError:
                        continue
                    if resolved.is_relative_to(root_res) and resolved not in seen:
                        seen.add(resolved)
                        results.append((root_res, resolved))
    return results


def safe_zip_member_path(member_name: str) -> str:
    """Validate and normalize a ZIP member path against Zip Slip traversal."""
    if not isinstance(member_name, str) or not member_name or "\x00" in member_name:
        raise EbookPathSafetyError("Invalid empty or null-byte zip member name")
    if "\\" in member_name or member_name.startswith("/") or _DRIVE_PREFIX_RE.match(member_name):
        raise EbookPathSafetyError(f"Unsafe zip member path: {member_name!r}")
    parts = [p for p in member_name.split("/") if p]
    if not parts or any(part in {".", ".."} for part in parts):
        raise EbookPathSafetyError(f"Zip Slip traversal forbidden in member: {member_name!r}")
    normalized = posixpath.normpath("/".join(parts))
    if normalized.startswith("../") or normalized in {".", ".."}:
        raise EbookPathSafetyError(f"Zip Slip traversal forbidden in member: {member_name!r}")
    return normalized


def read_zip_member_bounded(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_bytes: int = MAX_EPUB_ENTRY_BYTES,
    tracker: list[int] | None = None,
    total_limit: int = MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES,
) -> bytes:
    """Read a ZipInfo entry safely with symlink, ratio, and streaming byte limits."""
    safe_zip_member_path(info.filename)
    if ((info.external_attr >> 16) & 0o170000) == _ZIP_SYMLINK_MODE:
        raise EbookPathSafetyError(f"Refusing symlink inside EPUB archive: {info.filename}")
    if info.file_size > max_bytes:
        raise BookParseError(f"EPUB entry {info.filename!r} exceeds size limit ({info.file_size} > {max_bytes})")
    if info.file_size > 1024 * 1024 and (
        info.compress_size <= 0 or (info.file_size / info.compress_size) > MAX_EPUB_COMPRESSION_RATIO
    ):
        raise BookParseError(f"EPUB entry {info.filename!r} exceeds safe compression ratio")
    if tracker is not None and tracker[0] + info.file_size > total_limit:
        raise BookParseError("EPUB archive exceeds total uncompressed size limit")

    with zf.open(info, "r") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise BookParseError(f"EPUB entry {info.filename!r} actual size exceeds {max_bytes} bytes")
    if info.compress_size > 0 and len(data) > 1024 * 1024 and (len(data) / info.compress_size) > MAX_EPUB_COMPRESSION_RATIO:
        raise BookParseError(f"EPUB entry {info.filename!r} actual size violates compression ratio limit")
    if tracker is not None:
        if tracker[0] + len(data) > total_limit:
            raise BookParseError("EPUB archive total read bytes exceed uncompressed size limit")
        tracker[0] += len(data)
    return data


def resolve_omnidesk_vocab_path(ctx: PluginContext | None, plugin_id: str = "plugin-ebook") -> Path:
    """Resolve `<XDG_DATA_HOME>/omnidesk/vocab/<plugin_id>.json` without touching real HOME in tests."""
    pid = ctx.plugin_id if ctx is not None else plugin_id
    if ctx is not None and ctx.data_dir.name == pid and ctx.data_dir.parent.name == "plugin-data":
        return ctx.data_dir.parent.parent / "vocab" / f"{pid}.json"
    xdg_raw = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_raw:
        xdg_path = Path(xdg_raw).expanduser()
        if xdg_path.is_absolute():
            return xdg_path / "omnidesk" / "vocab" / f"{pid}.json"
    if ctx is not None and ctx.data_dir != ctx.plugin_dir:
        return ctx.data_dir / "vocab" / f"{pid}.json"
    home_raw = os.environ.get("HOME", "").strip()
    home_dir = Path(home_raw).expanduser() if home_raw else Path.home()
    return home_dir / ".local" / "share" / "omnidesk" / "vocab" / f"{pid}.json"


def resolve_progress_path(ctx: PluginContext | None, plugin_id: str = "plugin-ebook") -> Path | None:
    """Resolve persistent reading progress JSON path isolated from `plugin_dir`."""
    pid = ctx.plugin_id if ctx is not None else plugin_id
    if ctx is not None and ctx.data_dir != ctx.plugin_dir:
        return ctx.data_dir / "progress.json"
    xdg_raw = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_raw:
        xdg_path = Path(xdg_raw).expanduser()
        if xdg_path.is_absolute():
            return xdg_path / "omnidesk" / "plugin-data" / pid / "progress.json"
    home_raw = os.environ.get("HOME", "").strip()
    if home_raw:
        return Path(home_raw).expanduser() / ".local" / "share" / "omnidesk" / "plugin-data" / pid / "progress.json"
    return None
