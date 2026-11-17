"""Additional Bilibili feeds: popular, history, watch-later, and favorites."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .client import BilibiliApiError, BilibiliClient
from .security import cookie_value


def _items(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    items = data.get(key)
    return [dict(item) for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


async def fetch_popular(client: BilibiliClient, page: int = 1, *, cookie: str = "") -> list[dict[str, Any]]:
    """Fetch the public popular ranking feed."""
    payload = await client.get_json(
        "/x/web-interface/popular",
        params={"pn": max(1, page), "ps": 20, "web_location": "bilibili-electron"},
        cookie=cookie,
    )
    return _items(payload, "list")


async def fetch_history(client: BilibiliClient, page: int = 1, *, cookie: str = "") -> list[dict[str, Any]]:
    """Fetch watch history for the logged-in web session."""
    if not cookie:
        raise BilibiliApiError("查看历史记录需要登录 Cookie", error_code="not_configured")
    payload = await client.get_json(
        "/x/web-interface/history/cursor",
        params={"max": 0, "view_at": 0, "business": "", "ps": 20, "page": max(1, page)},
        cookie=cookie,
    )
    return _items(payload, "list")


async def fetch_watch_later(client: BilibiliClient, *, cookie: str = "") -> list[dict[str, Any]]:
    """Fetch the account watch-later list."""
    if not cookie:
        raise BilibiliApiError("查看稍后再看需要登录 Cookie", error_code="not_configured")
    payload = await client.get_json("/x/v2/history/toview/web", cookie=cookie)
    return _items(payload, "list")


async def fetch_favorites(
    client: BilibiliClient,
    page: int = 1,
    *,
    cookie: str = "",
    folder_id: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch resources from an explicit or first-created favorites folder."""
    if not cookie:
        raise BilibiliApiError("查看收藏夹需要登录 Cookie", error_code="not_configured")
    media_id = int(folder_id or 0)
    if media_id <= 0:
        mid = cookie_value(cookie, "DedeUserID")
        if not mid:
            nav = await client.check_nav(cookie)
            mid = str(nav.get("mid") or "")
        if not mid:
            raise BilibiliApiError("无法识别收藏夹用户 MID，请重新登录", error_code="unauthorized")
        folders = await client.get_json(
            "/x/v3/fav/folder/created/list-all",
            params={"up_mid": mid},
            cookie=cookie,
        )
        rows = _items(folders, "list")
        media_id = int(rows[0].get("id") or 0) if rows else 0
        if media_id <= 0:
            return []
    payload = await client.get_json(
        "/x/v3/fav/resource/list",
        params={"media_id": media_id, "pn": max(1, page), "ps": 20, "platform": "web"},
        cookie=cookie,
    )
    rows = _items(payload, "medias")
    return [row for row in rows if int(row.get("attr") or 0) != 9]


def stream_resolver(item_id: str) -> dict[str, Any]:
    """Return the declarative MediaItem playback resolver descriptor."""
    return {
        "capability": "bilibili.media.v1",
        "payload": {"action": "resolve_stream", "item_id": item_id, "emit_open": False},
        "quality_options": ["auto", "4k", "1080p60", "1080p+", "1080p", "720p", "480p", "360p"],
        "settings_action": "set_settings",
    }


def normalize_history_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a history cursor row into the shared video card shape."""
    history = row.get("history") if isinstance(row.get("history"), Mapping) else {}
    covers = row.get("covers")
    return {
        "bvid": history.get("bvid") or row.get("bvid"),
        "aid": history.get("oid") or row.get("aid") or row.get("oid"),
        "cid": history.get("cid") or row.get("cid"),
        "title": row.get("title"),
        "pic": covers[0] if isinstance(covers, list) and covers else row.get("cover"),
        "duration": row.get("duration"),
        "owner": {"name": row.get("author_name"), "mid": row.get("author_mid")},
        "stat": row.get("stat"),
    }


def normalize_watch_later_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a watch-later row into the shared video card shape."""
    return {**row, "pic": row.get("pic") or row.get("cover")}


def normalize_favorite_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a favorites-resource row into the shared video card shape."""
    upper = row.get("upper") if isinstance(row.get("upper"), Mapping) else {}
    stats = row.get("cnt_info") if isinstance(row.get("cnt_info"), Mapping) else {}
    return {
        "bvid": row.get("bvid"),
        "aid": row.get("id") or row.get("aid"),
        "title": row.get("title"),
        "pic": row.get("cover"),
        "duration": row.get("duration"),
        "owner": upper,
        "stat": {"view": stats.get("play")},
    }
