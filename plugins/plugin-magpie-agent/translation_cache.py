"""Persistent content-addressed LRU translation cache for `llm.translate.v1`."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def cache_key(target_lang: str, source_lang: str, text: str) -> str:
    """Compute a deterministic SHA-256 key for a translated segment."""
    raw = json.dumps(
        [target_lang.strip(), (source_lang or "auto").strip() or "auto", text],
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class TranslationCache:
    """Bounded LRU JSON cache stored under plugin data_dir."""

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = 4000,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self._path = path
        self._max_entries = max_entries
        self._max_bytes = max_bytes

    def _load(self) -> list[dict[str, str]]:
        if not self._path.is_file() or self._path.is_symlink():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(raw, Mapping) or raw.get("version") != 1:
            return []
        entries = raw.get("entries")
        if not isinstance(entries, list):
            return []
        valid: list[dict[str, str]] = []
        for entry in entries:
            if (
                isinstance(entry, Mapping)
                and isinstance(entry.get("key"), str)
                and isinstance(entry.get("translation"), str)
                and entry["translation"].strip()
            ):
                valid.append({"key": entry["key"], "translation": entry["translation"]})
        return valid

    def _trim(self, entries: list[dict[str, str]]) -> list[dict[str, str]]:
        if len(entries) > self._max_entries:
            entries = entries[-self._max_entries :]
        encoded_size = len(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False).encode("utf-8"))
        while entries and encoded_size > self._max_bytes:
            removed = entries.pop(0)
            encoded_size -= len(json.dumps(removed, ensure_ascii=False).encode("utf-8")) + 2
        return entries

    def _save(self, entries: list[dict[str, str]]) -> None:
        trimmed = self._trim(entries)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "entries": trimmed}, ensure_ascii=False)
        fd, tmp_name = tempfile.mkstemp(prefix=".tr-cache-", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, self._path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def lookup(
        self,
        *,
        target_lang: str,
        source_lang: str,
        texts: Sequence[str],
    ) -> dict[int, str]:
        """Return `{index: translation}` for all cached texts."""
        entries = self._load()
        if not entries:
            return {}
        by_key = {entry["key"]: entry["translation"] for entry in entries}
        hits: dict[int, str] = {}
        touched: set[str] = set()
        for idx, text in enumerate(texts):
            key = cache_key(target_lang, source_lang, text)
            val = by_key.get(key)
            if val:
                hits[idx] = val
                touched.add(key)
        if touched:
            reordered = [e for e in entries if e["key"] not in touched] + [
                e for e in entries if e["key"] in touched
            ]
            try:
                self._save(reordered)
            except OSError:
                pass
        return hits

    def store(
        self,
        *,
        target_lang: str,
        source_lang: str,
        pairs: Sequence[tuple[str, str]],
    ) -> None:
        """Persist newly translated `(source_text, translation)` pairs."""
        additions: dict[str, dict[str, str]] = {}
        for src, dst in pairs:
            if src and dst and dst.strip():
                key = cache_key(target_lang, source_lang, src)
                additions[key] = {"key": key, "translation": dst.strip()}
        if not additions:
            return
        entries = self._load()
        merged = [e for e in entries if e["key"] not in additions] + list(additions.values())
        try:
            self._save(merged)
        except OSError:
            pass
