"""Declarative YouTube workspace tab schema and action routing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from plugin import YouTubePlugin

_CATEGORY_LABELS = {
    "subscriptions": "订阅",
    "trending": "热门",
    "podcast": "播客",
    "search": "搜索",
    "channels": "频道",
    "watch_later": "稍后再看",
    "history": "历史",
    "playlists": "播放列表",
}
_QUALITY_OPTIONS = ["best", "2160p", "1440p", "1080p", "720p", "480p", "360p", "audio"]
SUPPORTED_ACTIONS = (
    "status",
    "categories",
    "fetch_items",
    "search",
    "resolve_stream",
    "cover",
    "list_subtitles",
    "fetch_subtitles",
    "list_subscriptions",
    "sync_subscriptions",
    "fetch_chapters",
    "set_settings",
    "ui_schema",
    "history_clear",
    "watch_later",
    "open_media",
)


def build_ui_schema(plugin: YouTubePlugin) -> dict[str, Any]:
    """Build a live schema for account state, feed categories, and playback defaults."""
    assert plugin.ctx is not None
    categories = plugin.media_source.categories()
    history = plugin.library_items("history")[:8]
    watch_later = plugin.library_items("watch_later")[:8]
    children: list[dict[str, Any]] = [
        {
            "type": "Markdown",
            "id": "youtube-intro",
            "content": (
                "YouTube 使用 **yt-dlp** 解析真实音视频流，Data API/OAuth 用于订阅、"
                "热门与搜索。历史与稍后再看保存在插件数据目录，播放仍在桌面 libmpv。"
            ),
        },
        {
            "type": "KeyValue",
            "id": "youtube-status",
            "items": [
                {"key": "api_key", "label": "Data API Key", "value": "已配置" if plugin.api_key() else "未配置"},
                {"key": "oauth", "label": "OAuth Token", "value": "已配置" if plugin.oauth_token() else "未配置"},
                {"key": "ytdlp", "label": "yt-dlp", "value": plugin.ytdlp_bin() or "未配置"},
                {
                    "key": "cache",
                    "label": "音频/视频落盘缓存",
                    "value": "本地历史已启用" if history or watch_later else "暂无记录",
                },
                {"key": "region", "label": "地区", "value": plugin.region()},
                {"key": "quality", "label": "默认画质", "value": plugin.quality()},
                {"key": "target_lang", "label": "双语字幕目标", "value": plugin.target_lang()},
                {"key": "last_status", "label": "最近状态", "value": plugin._last_action_status},
            ],
        },
        {
            "type": "Form",
            "id": "youtube-settings-form",
            "fields": [
                {"name": "proxy", "label": "YouTube 独立代理（留空使用系统代理）", "type": "string", "default": str(plugin.ctx.get_config("proxy") or "")},
                {"name": "cookie_header", "label": "YouTube Cookie（留空保持不变，也可复用浏览器登录）", "type": "secret", "default": ""},
                {
                    "name": "quality",
                    "label": "默认播放画质",
                    "type": "select",
                    "default": plugin.quality(),
                    "options": _QUALITY_OPTIONS,
                },
                {"name": "region", "label": "地区代码", "type": "string", "default": plugin.region()},
                {
                    "name": "target_lang",
                    "label": "双语字幕目标语言",
                    "type": "string",
                    "default": plugin.target_lang(),
                },
                {
                    "name": "ytdlp_bin",
                    "label": "yt-dlp 可执行路径",
                    "type": "string",
                    "default": plugin.ytdlp_bin(),
                },
                {
                    "name": "api_key",
                    "label": "YouTube Data API Key（留空保持不变）",
                    "type": "secret",
                    "default": "",
                },
                {
                    "name": "oauth_token",
                    "label": "YouTube OAuth Token（留空保持不变）",
                    "type": "secret",
                    "default": "",
                },
            ],
        },
        {
            "type": "ActionRow",
            "id": "youtube-settings-actions",
            "actions": [
                {"id": "set_settings", "label": "保存设置", "variant": "primary"},
                {"id": "status", "label": "校验状态", "variant": "secondary"},
                {"id": "sync_subscriptions", "label": "同步订阅", "variant": "secondary"},
            ],
        },
        {
            "type": "ActionRow",
            "id": "youtube-library-actions",
            "actions": [
                {"id": "history_clear", "label": "清空本地历史", "variant": "secondary"},
                {"id": "watch_later_clear", "label": "清空稍后再看", "variant": "secondary"},
            ],
        },
        {
            "type": "List",
            "id": "youtube-categories",
            "title": "分类入口",
            "items": [
                {
                    "id": category,
                    "title": _CATEGORY_LABELS.get(category, category),
                    "subtitle": category,
                }
                for category in categories
            ],
        },
    ]
    if history:
        children.append(
            {
                "type": "List",
                "id": "youtube-history",
                "title": "最近播放",
                "items": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "subtitle": item.creator or item.extra.get("category", "history"),
                    }
                    for item in history
                ],
            }
        )
    if watch_later:
        children.append(
            {
                "type": "List",
                "id": "youtube-watch-later",
                "title": "稍后再看",
                "items": [
                    {"id": item.id, "title": item.title, "subtitle": item.creator or item.id}
                    for item in watch_later
                ],
            }
        )
    if plugin._tab_status:
        children.append(
            {"type": "Markdown", "id": "youtube-action-status", "content": plugin._tab_status}
        )
    return {
        "type": "Section",
        "id": "youtube-workspace-root",
        "title": "YouTube 登录与播放",
        "description": "订阅/热门/播客/历史、yt-dlp 取流与画质切换",
        "children": children,
    }


async def handle_tab_action(
    plugin: YouTubePlugin, action_id: str, state: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Route declarative tab actions through the capability dispatcher."""
    payload = {**(dict(state) if state is not None else {}), "action": action_id}
    res = await plugin.handle_action(payload)
    if action_id not in {"status", "history_clear", "watch_later_clear"}:
        plugin._last_action_status = res.error if not res.ok else "操作完成"
    plugin._tab_status = res.error if not res.ok else str(res.data.get("status") or "")
    return {
        "ok": res.ok,
        "data": res.data,
        "error": res.error,
        "ui_schema": build_ui_schema(plugin),
    }
