"""YouTube playback, settings, and local library actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnidesk_sdk import CapabilityResult, MediaItem

if __package__:
    from .yt_library import load_items, remove_item, store_item, stream_resolver
    from .yt_security import canonical_watch_url, hqdefault_cover_url
    from .yt_workspace_ui import build_ui_schema
else:
    from yt_library import load_items, remove_item, store_item, stream_resolver
    from yt_security import canonical_watch_url, hqdefault_cover_url
    from yt_workspace_ui import build_ui_schema

if TYPE_CHECKING:
    from plugin import YouTubePlugin

_QUALITY_OPTIONS = {"best", "2160p", "4k", "1440p", "1080p", "720p", "480p", "360p", "audio"}


async def open_media(plugin: YouTubePlugin, req: Mapping[str, Any]) -> CapabilityResult:
    """Resolve a stream, record history, and publish `entity.media.open`."""
    if plugin.ctx is None:
        return plugin._fail("插件尚未加载", error_code="not_configured")
    stream_res = await plugin.resolve_stream({**req, "record_history": False})
    if not stream_res.ok:
        return stream_res
    data = stream_res.data
    item = library_item(
        plugin,
        str(data.get("video_id") or req.get("video_id") or ""),
        data,
        title=str(req.get("title") or ""),
    )
    if item is not None:
        store_item(plugin, "history", item)
    await plugin.ctx.emit(
        "entity.media.open",
        {
            "item_id": item.id if item else data.get("video_id", ""),
            "source_id": plugin.media_source.source_id,
            "title": item.title if item else str(data.get("title") or ""),
            "kind": item.kind if item else "video",
            "play_url": str(data.get("play_url") or ""),
            "audio_url": str(data.get("audio_url") or ""),
            "http_headers": dict(data.get("headers") or {}),
            "start_sec": float(req.get("start_sec") or 0.0),
        },
    )
    return CapabilityResult.ok({**data, "opened": True})


def watch_later(plugin: YouTubePlugin, req: Mapping[str, Any]) -> CapabilityResult:
    """List, add, or remove local watch-later entries."""
    op = str(req.get("op") or "list").strip().lower()
    if op == "list":
        items = load_items(plugin, "watch_later")
        return CapabilityResult.ok(
            {"items": [item.model_dump() for item in items], "count": len(items)}
        )
    if op == "remove":
        removed = remove_item(
            plugin, "watch_later", str(req.get("video_id") or req.get("id") or "")
        )
        return CapabilityResult.ok(
            {"removed": removed, "ui_schema": build_ui_schema(plugin)}
        )
    if op != "add":
        return plugin._fail(
            "watch_later.op 必须是 list/add/remove", error_code="invalid_argument"
        )
    item = library_item(
        plugin,
        str(req.get("video_id") or req.get("id") or ""),
        req,
        title=str(req.get("title") or ""),
    )
    if item is None:
        return plugin._fail(
            "无法识别要加入稍后再看的视频", error_code="invalid_argument"
        )
    store_item(plugin, "watch_later", item)
    plugin._tab_status = f"已加入稍后再看：{item.title}"
    return CapabilityResult.ok(
        {"added": True, "item": item.model_dump(), "ui_schema": build_ui_schema(plugin)}
    )


def library_item(
    plugin: YouTubePlugin,
    video_id: str,
    data: Mapping[str, Any],
    *,
    title: str = "",
) -> MediaItem | None:
    """Build a MediaItem from cached feed metadata or a resolved stream payload."""
    raw_id = str(video_id or data.get("video_id") or "").strip()
    if not raw_id:
        return None
    cached = plugin.media_source._item_cache.get(raw_id)
    if cached is not None:
        return cached
    return MediaItem(
        id=raw_id,
        source_id=plugin.media_source.source_id,
        title=title or str(data.get("title") or raw_id),
        creator=str(data.get("channel") or ""),
        source_badge="YouTube",
        cover_url=hqdefault_cover_url(raw_id),
        play_url=canonical_watch_url(raw_id),
        quality_label=str(data.get("quality") or plugin.quality()),
        duration_sec=float(data.get("duration_sec") or 0.0),
        aspect="16:9",
        extra={
            "category": "history",
            "stream_resolver": stream_resolver(raw_id),
        },
    )


def set_settings(plugin: YouTubePlugin, req: Mapping[str, Any]) -> CapabilityResult:
    """Persist playback defaults, optional API credentials, and yt-dlp path."""
    if plugin.ctx is None:
        return plugin._fail("插件尚未加载", error_code="not_configured")
    quality = str(req.get("quality") or plugin.quality()).strip().lower()
    if quality not in _QUALITY_OPTIONS:
        return plugin._fail("不支持的画质选项", error_code="invalid_argument")
    patch: dict[str, Any] = {"quality": "2160p" if quality == "4k" else quality}
    if "region" in req:
        patch["region"] = str(req.get("region") or "US").strip().upper() or "US"
    if "target_lang" in req:
        patch["target_lang"] = (
            str(req.get("target_lang") or "zh-CN").strip() or "zh-CN"
        )
    if "ytdlp_bin" in req:
        patch["ytdlp_bin"] = (
            str(req.get("ytdlp_bin") or "yt-dlp").strip() or "yt-dlp"
        )
    if "proxy" in req:
        from urllib.parse import urlsplit
        proxy = str(req.get("proxy") or "").strip()
        if proxy and (urlsplit(proxy).scheme not in {"http", "https", "socks5", "socks5h"} or not urlsplit(proxy).hostname or any(ch in proxy for ch in "\r\n\t")):
            return plugin._fail("代理地址无效", error_code="invalid_argument")
        patch["proxy"] = proxy
    for key in ("api_key", "oauth_token", "cookie_header"):
        value = str(req.get(key) or "").strip()
        if value:
            plugin.ctx.set_secret(key, value)
    plugin.ctx.set_config(patch)
    plugin._last_action_status = "settings_saved"
    return CapabilityResult.ok(
        {
            "status": "设置已保存",
            "settings": {**plugin.ctx.get_config(), **patch},
            "ui_schema": build_ui_schema(plugin),
        }
    )
