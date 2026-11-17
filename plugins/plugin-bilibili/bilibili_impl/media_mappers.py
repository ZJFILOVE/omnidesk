"""Normalize Bilibili feed rows into MediaItem records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnidesk_sdk import MediaItem

from .catalog import stream_resolver
from .parsers import clean_bili_title, normalize_media_url, parse_bilibili_target, parse_duration_sec


def _video_url(bvid: str, aid: int | None) -> str:
    return f"https://www.bilibili.com/video/{bvid}" if bvid else f"https://www.bilibili.com/video/av{aid}"


def _aid(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def map_recommend_item(
    row: Mapping[str, Any],
    quality_label: str,
    api_base: str,
    *,
    source_id: str,
    category: str = "recommend",
    badge: str = "Bilibili 推荐",
    progress: int = 0,
) -> MediaItem | None:
    """Map a recommend/popular/history/watch-later/favorite row."""
    bvid = str(row.get("bvid") or "").strip()
    aid_int = _aid(row.get("aid") or row.get("id"))
    if (not bvid and not aid_int) or str(row.get("goto") or "") == "ad":
        return None
    title = clean_bili_title(str(row.get("title") or ""))
    if not title:
        return None
    owner = row.get("owner") if isinstance(row.get("owner"), Mapping) else {}
    stat = row.get("stat") if isinstance(row.get("stat"), Mapping) else {}
    creator = str(owner.get("name") or "")
    item_id = bvid or f"av{aid_int}"
    summary = str(row.get("summary") or row.get("desc") or "")
    if progress:
        summary = f"观看进度：{progress}s" if progress > 0 else "已看完"
    if not summary:
        reason = row.get("rcmd_reason")
        summary = str(reason.get("content") or "") if isinstance(reason, Mapping) else ""
    return MediaItem(
        id=item_id,
        source_id=source_id,
        title=title,
        subtitle=f"{creator} · {badge.removeprefix('Bilibili ')}" if creator else badge,
        creator=creator,
        kind="video",
        source_badge=badge,
        cover_url=normalize_media_url(str(row.get("pic") or ""), api_base=api_base),
        play_url=_video_url(bvid, aid_int),
        quality_label=quality_label,
        duration_sec=parse_duration_sec(row.get("duration")),
        view_count=max(0, int(stat.get("view") or 0)) if str(stat.get("view") or "").isdigit() else 0,
        aspect="16:9",
        summary=summary,
        extra={
            "category": category,
            "bvid": bvid,
            "aid": aid_int,
            "avid": f"av{aid_int}" if aid_int else "",
            "cid": row.get("cid"),
            "mid": owner.get("mid"),
            "stream_resolver": stream_resolver(item_id),
        },
    )


def map_dynamic_item(
    row: Mapping[str, Any], quality_label: str, api_base: str, *, source_id: str
) -> MediaItem | None:
    """Map a dynamic-feed archive card."""
    modules = row.get("modules") if isinstance(row.get("modules"), Mapping) else {}
    author = modules.get("module_author") if isinstance(modules.get("module_author"), Mapping) else {}
    dyn = modules.get("module_dynamic") if isinstance(modules.get("module_dynamic"), Mapping) else {}
    major = dyn.get("major") if isinstance(dyn.get("major"), Mapping) else {}
    archive = major.get("archive") if isinstance(major.get("archive"), Mapping) else None
    if archive is None:
        return None
    bvid = str(archive.get("bvid") or "").strip()
    aid_int = _aid(archive.get("aid"))
    title = clean_bili_title(str(archive.get("title") or ""))
    if not title or (not bvid and not aid_int):
        return None
    creator = str(author.get("name") or "")
    stat = archive.get("stat") if isinstance(archive.get("stat"), Mapping) else {}
    item_id = bvid or f"av{aid_int}"
    return MediaItem(
        id=item_id,
        source_id=source_id,
        title=title,
        subtitle=f"{creator} · 动态" if creator else "Bilibili 动态",
        creator=creator,
        kind="video",
        source_badge="Bilibili 动态",
        cover_url=normalize_media_url(str(archive.get("cover") or ""), api_base=api_base),
        play_url=_video_url(bvid, aid_int),
        quality_label=quality_label,
        duration_sec=parse_duration_sec(archive.get("duration_text") or archive.get("duration")),
        view_count=int(stat["play"]) if str(stat.get("play") or "").isdigit() else 0,
        aspect="16:9",
        summary=str(archive.get("desc") or ""),
        extra={
            "category": "dynamic",
            "bvid": bvid,
            "aid": aid_int,
            "avid": f"av{aid_int}" if aid_int else "",
            "mid": author.get("mid"),
            "dynamic_id": str(row.get("id_str") or ""),
            "stream_resolver": stream_resolver(item_id),
        },
    )


def map_bangumi_item(
    row: Mapping[str, Any], api_base: str, *, source_id: str
) -> MediaItem | None:
    """Map a bangumi follow-card."""
    season_id = row.get("season_id")
    title = clean_bili_title(str(row.get("title") or ""))
    if not season_id or not title:
        return None
    new_ep = row.get("new_ep") if isinstance(row.get("new_ep"), Mapping) else {}
    ep_id = new_ep.get("id")
    try:
        play_url = parse_bilibili_target(
            str(row.get("url") or f"https://www.bilibili.com/bangumi/play/ss{season_id}")
        )["url"]
    except ValueError:
        play_url = f"https://www.bilibili.com/bangumi/play/ss{season_id}"
    return MediaItem(
        id=f"ss{season_id}",
        source_id=source_id,
        title=title,
        subtitle=str(new_ep.get("index_show") or row.get("progress") or "追番"),
        creator=str(row.get("season_type_name") or "Bilibili 番剧"),
        kind="video",
        source_badge="Bilibili 追番",
        cover_url=normalize_media_url(str(row.get("cover") or ""), api_base=api_base),
        play_url=play_url,
        quality_label=str(row.get("badge") or "番剧"),
        duration_sec=parse_duration_sec(new_ep.get("duration")),
        aspect="16:9",
        summary=str(row.get("evaluate") or row.get("summary") or ""),
        extra={
            "category": "bangumi",
            "season_id": int(season_id) if str(season_id).isdigit() else season_id,
            "ep_id": int(ep_id) if str(ep_id).isdigit() else ep_id,
            "media_id": row.get("media_id"),
            "stream_resolver": stream_resolver(f"ss{season_id}"),
        },
    )


def map_live_item(
    row: Mapping[str, Any], api_base: str, *, source_id: str
) -> MediaItem | None:
    """Map a live-room card."""
    room_id = row.get("roomid") or row.get("room_id")
    title = clean_bili_title(str(row.get("title") or ""))
    if not room_id or not title:
        return None
    uname = str(row.get("uname") or "")
    area = str(row.get("area_name") or row.get("parent_name") or "")
    online = row.get("online")
    return MediaItem(
        id=f"live:{room_id}",
        source_id=source_id,
        title=title,
        subtitle=f"{uname} · {area}".strip(" ·"),
        creator=uname,
        kind="live",
        source_badge="Bilibili 直播",
        cover_url=normalize_media_url(
            str(row.get("cover") or row.get("user_cover") or row.get("system_cover") or ""),
            api_base=api_base,
        ),
        play_url=f"https://live.bilibili.com/{room_id}",
        quality_label="直播",
        duration_sec=0.0,
        view_count=max(0, int(online)) if str(online or "").isdigit() else 0,
        aspect="16:9",
        extra={
            "category": "live",
            "room_id": int(room_id) if str(room_id).isdigit() else room_id,
            "uid": row.get("uid"),
            "area_name": area,
            "stream_resolver": stream_resolver(f"live:{room_id}"),
        },
    )


def map_search_item(
    row: Mapping[str, Any], quality_label: str, api_base: str, *, source_id: str
) -> MediaItem | None:
    """Map a search-result row."""
    bvid = str(row.get("bvid") or "").strip()
    aid_int = _aid(row.get("aid") or row.get("id"))
    title = clean_bili_title(str(row.get("title") or ""))
    if not title or (not bvid and not aid_int):
        return None
    creator = str(row.get("author") or "")
    item_id = bvid or f"av{aid_int}"
    return MediaItem(
        id=item_id,
        source_id=source_id,
        title=title,
        subtitle=f"{creator} · 搜索" if creator else "Bilibili 搜索",
        creator=creator,
        kind="video",
        source_badge="Bilibili 搜索",
        cover_url=normalize_media_url(str(row.get("pic") or ""), api_base=api_base),
        play_url=_video_url(bvid, aid_int),
        quality_label=quality_label,
        duration_sec=parse_duration_sec(row.get("duration")),
        view_count=int(row["play"]) if str(row.get("play") or "").isdigit() else 0,
        aspect="16:9",
        summary=str(row.get("description") or ""),
        extra={
            "category": "search",
            "bvid": bvid,
            "aid": aid_int,
            "avid": f"av{aid_int}" if aid_int else "",
            "mid": row.get("mid"),
            "stream_resolver": stream_resolver(item_id),
        },
    )
