"""Sibling-repo import bridge and availability probes for qwen-translator music modules."""

from __future__ import annotations

import importlib
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnidesk_sdk import LyricLine

DEFAULT_QWEN_TRANSLATOR_DIR = Path("/home/zhu/Projects/qwen-translator")
_MODULE_NAMES = ("music_adam", "music_lyrics", "music_service", "music_playback")
_IMPORT_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class QwenModules:
    """Loaded qwen-translator music modules and availability status."""

    source_dir: Path | None
    available: bool
    modules: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def module(self, name: str) -> Any | None:
        return self.modules.get(name)

    def status_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "source_dir": str(self.source_dir) if self.source_dir is not None else "",
            "modules": {name: (name in self.modules) for name in _MODULE_NAMES},
            "errors": dict(self.errors),
        }


def resolve_qwen_dir(configured_dir: str | None = None) -> Path | None:
    """Resolve the qwen-translator directory without touching unrelated home files."""
    raw = (configured_dir or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_dir() else None
    env_dir = os.environ.get("OMNIDESK_QWEN_TRANSLATOR_DIR", "").strip()
    if env_dir:
        candidate = Path(env_dir).expanduser()
        return candidate if candidate.is_dir() else None
    return DEFAULT_QWEN_TRANSLATOR_DIR if DEFAULT_QWEN_TRANSLATOR_DIR.is_dir() else None


def load_qwen_modules(configured_dir: str | None = None) -> QwenModules:
    """Probe and import qwen-translator music modules from sibling checkout."""
    target_dir = resolve_qwen_dir(configured_dir)
    if target_dir is None:
        return QwenModules(
            source_dir=None,
            available=False,
            errors={name: "qwen_translator_dir_unavailable" for name in _MODULE_NAMES},
        )

    resolved_str = str(target_dir.resolve())
    loaded: dict[str, Any] = {}
    errors: dict[str, str] = {}

    with _IMPORT_LOCK:
        # Ensure we bind to the requested sibling directory when switching paths in tests.
        for name in _MODULE_NAMES:
            existing = sys.modules.get(name)
            existing_file = getattr(existing, "__file__", "") or ""
            if existing is not None and not existing_file.startswith(resolved_str):
                sys.modules.pop(name, None)

        inserted = resolved_str not in sys.path
        if inserted:
            sys.path.insert(0, resolved_str)
        try:
            for name in _MODULE_NAMES:
                mod_path = target_dir / f"{name}.py"
                if not mod_path.is_file():
                    errors[name] = f"missing:{name}.py"
                    continue
                try:
                    loaded[name] = importlib.import_module(name)
                except Exception as exc:
                    errors[name] = f"{type(exc).__name__}: {exc}"
        finally:
            if inserted and resolved_str in sys.path:
                sys.path.remove(resolved_str)

    return QwenModules(
        source_dir=target_dir,
        available=("music_lyrics" in loaded and "music_adam" in loaded),
        modules=loaded,
        errors=errors,
    )


def parse_lrc_to_lyric_lines(raw_text: str, qwen: QwenModules) -> list[LyricLine]:
    """Parse raw LRC or alternating bilingual text using qwen-translator music_lyrics."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        return []
    lyrics_mod = qwen.module("music_lyrics")
    if lyrics_mod is None or not callable(getattr(lyrics_mod, "parse_lyrics", None)):
        return []
    parsed = lyrics_mod.parse_lyrics(raw_text)
    lines: list[LyricLine] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        start = entry.get("start")
        time_sec = float(start) if isinstance(start, (int, float)) and start >= 0 else 0.0
        lines.append(
            LyricLine(
                time_sec=time_sec,
                original=str(entry.get("text") or ""),
                translation=str(entry.get("translation") or ""),
                note="",
            )
        )
    return lines


def probe_adam_listener(qwen: QwenModules) -> bool:
    """Safely check whether the local same-user Adam engine port (20025) is listening."""
    adam_mod = qwen.module("music_adam")
    if adam_mod is None:
        return False
    fn = getattr(adam_mod, "_listener_owned_by_user", None)
    if not callable(fn):
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def fetch_adam_lrc(song_id: str, storefront: str, qwen: QwenModules) -> str | None:
    """Fetch native LRC from local Adam engine when listening; returns None when absent."""
    adam_mod = qwen.module("music_adam")
    if adam_mod is None or not probe_adam_listener(qwen):
        return None
    fetch_fn = getattr(adam_mod, "_fetch_lrc", None)
    if not callable(fetch_fn):
        return None
    try:
        return str(fetch_fn(song_id, storefront or "cn"))
    except Exception:
        return None


def read_cached_lyrics(
    *,
    song_id: str,
    title: str,
    artist: str,
    album: str,
    duration_sec: float,
    lyrics_cache_dir: Path,
    qwen: QwenModules,
) -> list[LyricLine]:
    """Read local cached lyrics strictly inside plugin data_dir via music_lyrics."""
    lyrics_mod = qwen.module("music_lyrics")
    if lyrics_mod is None:
        return []

    for ext in (".bilingual.lrc", ".lrc", ".txt"):
        candidate = lyrics_cache_dir / f"{song_id}{ext}"
        if candidate.is_file() and not candidate.is_symlink():
            try:
                raw = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            parsed = parse_lrc_to_lyric_lines(raw, qwen)
            if parsed:
                return parsed

    repo_cls = getattr(lyrics_mod, "LyricsRepository", None)
    if callable(repo_cls) and (title or artist):
        try:
            repo = repo_cls(cache_dir=lyrics_cache_dir)
            meta = repo._track_metadata({
                "title": title,
                "artist": artist,
                "album": album,
                "duration": duration_sec,
            }) if hasattr(repo, "_track_metadata") else None
            # Use LyricsRepository's exact signature hash without triggering online fetch.
            import hashlib, json
            if meta is None:
                normal = getattr(lyrics_mod, "_normal", lambda v: str(v or "").strip().casefold())
                sig = [normal(title), normal(artist), normal(album), round(float(duration_sec or 0), 1)]
            else:
                normal = getattr(lyrics_mod, "_normal", lambda v: str(v or "").strip().casefold())
                sig = [normal(meta[k]) for k in ("title", "artist", "album")] + [round(meta["duration"], 1)]
            ident = hashlib.sha256(json.dumps(sig, ensure_ascii=False).encode("utf-8")).hexdigest()
            cached = repo._cached(lyrics_cache_dir / ident)
            raw_lines = cached.get("lines") if isinstance(cached, dict) else None
            if isinstance(raw_lines, list) and raw_lines:
                return [
                    LyricLine(
                        time_sec=float(line["start"]) if isinstance(line.get("start"), (int, float)) and line["start"] >= 0 else 0.0,
                        original=str(line.get("text") or ""),
                        translation=str(line.get("translation") or ""),
                    )
                    for line in raw_lines
                    if isinstance(line, dict)
                ]
        except Exception:
            return []
    return []
