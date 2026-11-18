"""Declarative YouTube workspace tab schema and action routing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if __package__:
    from .yt_auth import session_cookie_present
else:
    from yt_auth import session_cookie_present

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
    "open_login",
    "open_api_console",
    "auth_status",
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
    vault_cookie = str(plugin.ctx.get_secret("cookie_header") or "")
    vault_logged_in = session_cookie_present(vault_cookie)
    cookie_logged_in = bool(getattr(plugin, "_cookie_logged_in", False)) or vault_logged_in
    cookie_source = str(getattr(plugin, "_cookie_source", "none") or "none")
    if cookie_logged_in:
        cookie_label = f"已登录 ({cookie_source if cookie_source != 'none' else 'vault'})"
    else:
        cookie_label = "未登录（请先在 Chrome 登录并刷新）"
    children: list[dict[str, Any]] = [
        {"type": "ActionRow", "actions": [
            {"id": "open_login", "label": "在 Chrome 登录 YouTube", "variant": "primary"},
            {"id": "auth_status", "label": "已登录，刷新登录状态", "variant": "secondary"},
            {"id": "open_api_console", "label": "获取 API Key / OAuth 凭证", "variant": "secondary"},
        ]},
        {
            "type": "Markdown",
            "id": "youtube-intro",
            "content": (
                "YouTube 使用 **yt-dlp** 配合 Chrome 登录 Cookie 解析音视频流、个人订阅、稍后再看与推荐；"
                "`Data API Key` / `OAuth Token` 仅用于官方 Google Data API v3 配额（Chrome Cookie 中不包含这两项，"
                "如需使用可点击上方「获取 API Key / OAuth 凭证」前往 Google Cloud 控制台创建）。"
            ),
        },
        {
            "type": "KeyValue",
            "id": "youtube-status",
            "items": [
                {"key": "cookie_login", "label": "Chrome / Cookie 登录", "value": cookie_label},
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
        if action_id == "auth_status" and res.ok:
            plugin._last_action_status = (
                f"已登录 ({res.data.get('cookie_source') or 'cookie'})"
                if res.data.get("session_cookie_present")
                else "guest"
            )
        else:
            plugin._last_action_status = res.error if not res.ok else "操作完成"
    plugin._tab_status = res.error if not res.ok else str(res.data.get("message") or res.data.get("status") or "")
    return {
        "ok": res.ok,
        "data": res.data,
        "error": res.error,
        "ui_schema": build_ui_schema(plugin),
    }
