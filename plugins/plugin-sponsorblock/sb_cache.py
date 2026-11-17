"""Atomic TTL JSON cache for SponsorBlock timeline segments."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_CACHE_FILE_NAME = "timeline_cache.json"
_MAX_CACHE_ENTRIES = 500


class CacheSafetyError(RuntimeError):
    """Raised when cache storage path violates symlink/safety rules."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


def make_cache_key(platform: str, video_id: str, categories: Sequence[str], api_base: str = "") -> str:
    """Build deterministic cache key from platform, video ID, categories, and API base."""
    cats = ",".join(sorted({c.strip().lower() for c in categories if c.strip()}))
    return f"{platform}:{video_id}:{cats}:{api_base.rstrip('/')}"


def _cache_path(data_dir: Path) -> Path:
    if data_dir.is_symlink():
        raise CacheSafetyError("path_safety", f"拒绝使用符号链接缓存目录: {data_dir}")
    target = data_dir / _CACHE_FILE_NAME
    if target.is_symlink():
        raise CacheSafetyError("path_safety", f"拒绝访问符号链接缓存文件: {target}")
    return target


def _atomic_write_json(target: Path, payload: Mapping[str, Any]) -> None:
    if target.is_symlink():
        raise CacheSafetyError("path_safety", f"拒绝覆盖符号链接缓存文件: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
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
        os.fchmod(handle.fileno(), 0o600)
        handle.write(serialized)
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


def _load_entries(cache_file: Path) -> dict[str, dict[str, Any]]:
    if not cache_file.is_file():
        return {}
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, Mapping):
        return {}
    return {
        str(k): dict(v)
        for k, v in entries.items()
        if isinstance(k, str) and isinstance(v, Mapping) and isinstance(v.get("segments"), list)
    }


def read_cached_segments(data_dir: Path, key: str, ttl_sec: int) -> list[dict[str, Any]] | None:
    """Return cached segments if present and unexpired, or None on miss/expiry."""
    if ttl_sec <= 0:
        return None
    cache_file = _cache_path(data_dir)
    entries = _load_entries(cache_file)
    entry = entries.get(key)
    if entry is None:
        return None
    now = time.time()
    try:
        expires_at = float(entry.get("expires_at", 0.0))
    except (TypeError, ValueError):
        return None
    if now >= expires_at:
        return None
    return [dict(seg) for seg in entry["segments"] if isinstance(seg, Mapping)]


def write_cached_segments(
    data_dir: Path,
    key: str,
    segments: Sequence[Mapping[str, Any]],
    ttl_sec: int,
) -> None:
    """Persist segments to TTL cache atomically; skip writing when ttl_sec <= 0."""
    if ttl_sec <= 0:
        return
    cache_file = _cache_path(data_dir)
    now = time.time()
    entries = _load_entries(cache_file)
    live: dict[str, dict[str, Any]] = {}
    for k, entry in entries.items():
        try:
            if float(entry.get("expires_at", 0.0)) > now:
                live[k] = entry
        except (TypeError, ValueError):
            continue
    live[key] = {
        "fetched_at": now,
        "expires_at": now + float(ttl_sec),
        "segments": [dict(seg) for seg in segments],
    }
    if len(live) > _MAX_CACHE_ENTRIES:
        ordered = sorted(live.items(), key=lambda item: float(item[1].get("fetched_at", 0.0)), reverse=True)
        live = dict(ordered[:_MAX_CACHE_ENTRIES])
    _atomic_write_json(cache_file, {"version": 1, "entries": live})


def cache_stats(data_dir: Path) -> int:
    """Count currently valid (unexpired) entries in cache."""
    cache_file = _cache_path(data_dir)
    now = time.time()
    entries = _load_entries(cache_file)
    count = 0
    for entry in entries.values():
        try:
            if float(entry.get("expires_at", 0.0)) > now:
                count += 1
        except (TypeError, ValueError):
            continue
    return count


def clear_cache(data_dir: Path) -> int:
    """Atomically clear all cached timeline entries and return previous entry count."""
    cache_file = _cache_path(data_dir)
    prev = len(_load_entries(cache_file))
    _atomic_write_json(cache_file, {"version": 1, "entries": {}})
    return prev
