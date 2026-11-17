"""Local reading progress store and safe sidecar reader for plugin-ebook."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import MAX_SIDECAR_BYTES, BookParseError, EbookPathSafetyError
from .security import (
    atomic_write_json_0600,
    read_json_nofollow,
    resolve_progress_path,
    resolve_safe_under_root,
)

if TYPE_CHECKING:
    from omnidesk_sdk import PluginContext


class ProgressStore:
    """Thread-safe local-first reading progress store with optional book sidecar fallback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._memory: dict[str, dict[str, Any]] = {}

    def clear(self) -> None:
        with self._lock:
            self._memory.clear()

    def _read_all(self, ctx: PluginContext | None) -> dict[str, dict[str, Any]]:
        pid = ctx.plugin_id if ctx is not None else "plugin-ebook"
        path = resolve_progress_path(ctx, pid)
        if path is None:
            return dict(self._memory)
        data = read_json_nofollow(path)
        if not data:
            return dict(self._memory)
        books = data.get("books", data if "version" not in data else {})
        if not isinstance(books, Mapping):
            return dict(self._memory)
        return {str(k): dict(v) for k, v in books.items() if isinstance(v, Mapping)}

    @staticmethod
    def _read_sidecar(root_dir: Path, book_path: Path) -> dict[str, Any] | None:
        for cand_name in (f"{book_path.name}.progress.json", f"{book_path.stem}.progress.json"):
            cand = book_path.parent / cand_name
            if not cand.exists() and not cand.is_symlink():
                continue
            try:
                safe = resolve_safe_under_root(root_dir, cand)
                parsed = read_json_nofollow(safe, max_bytes=MAX_SIDECAR_BYTES)
                if parsed:
                    return parsed
            except (EbookPathSafetyError, BookParseError):
                continue
        return None

    def get(self, ctx: PluginContext | None, book_id: str, root_dir: Path, book_path: Path) -> dict[str, Any]:
        with self._lock:
            all_prog = self._read_all(ctx)
        if book_id in all_prog:
            return all_prog[book_id]
        return self._read_sidecar(root_dir, book_path) or {}

    def save(
        self,
        ctx: PluginContext | None,
        book_id: str,
        *,
        current_page: int,
        total_pages: int,
        progress_percent: float,
    ) -> dict[str, Any]:
        record = {
            "book_id": book_id,
            "current_page": current_page,
            "total_pages": total_pages,
            "progress_percent": progress_percent,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        pid = ctx.plugin_id if ctx is not None else "plugin-ebook"
        with self._lock:
            self._memory[book_id] = dict(record)
            path = resolve_progress_path(ctx, pid)
            if path is not None:
                all_prog = self._read_all(ctx)
                all_prog[book_id] = record
                atomic_write_json_0600(path, {"version": 1, "books": all_prog})
        return record
