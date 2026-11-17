"""Local YouTube history and watch-later persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnidesk_sdk import MediaItem

if __package__:
    from .yt_http import atomic_write_json
else:
    from yt_http import atomic_write_json

if TYPE_CHECKING:
    from plugin import YouTubePlugin

_MAX_ITEMS = 100


def library_path(plugin: YouTubePlugin, kind: str) -> Path:
    """Return the persisted JSON path for `history` or `watch_later`."""
    assert plugin.ctx is not None
    name = "watch_later.json" if kind == "watch_later" else "history.json"
    return plugin.ctx.data_dir / name


def load_items(plugin: YouTubePlugin, kind: str) -> list[MediaItem]:
    """Read a local media list, tolerating v1 arrays and v2 `{items: []}` wrappers."""
    if plugin.ctx is None:
        return []
    path = library_path(plugin, kind)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    raw_items = payload.get("items") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_items, list):
        return []
    items: list[MediaItem] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        candidate = dict(raw)
        candidate.setdefault("source_id", plugin.media_source.source_id)
        candidate.setdefault("category", kind)
        extra = candidate.get("extra")
        candidate["extra"] = {
            **(dict(extra) if isinstance(extra, Mapping) else {}),
            "category": kind,
            "stream_resolver": stream_resolver(str(candidate.get("id") or "")),
        }
        try:
            items.append(MediaItem.model_validate(candidate))
        except ValueError:
            continue
    return items


def save_items(plugin: YouTubePlugin, kind: str, items: list[MediaItem]) -> None:
    """Atomically persist a capped local media list."""
    if plugin.ctx is None:
        return
    payload = [item.model_dump(mode="json") for item in items[:_MAX_ITEMS]]
    atomic_write_json(library_path(plugin, kind), {"items": payload})


def store_item(plugin: YouTubePlugin, kind: str, item: MediaItem | Mapping[str, Any]) -> bool:
    """Insert or promote one item in history/watch-later."""
    try:
        normalized = item if isinstance(item, MediaItem) else MediaItem.model_validate(dict(item))
    except ValueError:
        return False
    existing = [entry for entry in load_items(plugin, kind) if entry.id != normalized.id]
    save_items(plugin, kind, [normalized, *existing])
    return True


def remove_item(plugin: YouTubePlugin, kind: str, item_id: str) -> bool:
    """Remove one item by video id."""
    items = load_items(plugin, kind)
    kept = [entry for entry in items if entry.id != item_id]
    if len(kept) == len(items):
        return False
    save_items(plugin, kind, kept)
    return True


def clear_items(plugin: YouTubePlugin, kind: str) -> int:
    """Clear one local list and return the removed count."""
    count = len(load_items(plugin, kind))
    save_items(plugin, kind, [])
    return count


def stream_resolver(video_id: str) -> dict[str, Any]:
    """Descriptor consumed by MediaWorkbench to resolve a real yt-dlp stream."""
    return {
        "capability": "media.youtube.v1",
        "payload": {"action": "resolve_stream", "video_id": video_id},
        "quality_options": ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "audio"],
        "settings_action": "set_settings",
    }
