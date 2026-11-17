"""BilibiliMediaSource implementing MediaSource (`media.source.v1`) for MediaWorkbench."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from omnidesk_sdk import ChapterSegment, LyricLine, MediaItem, MediaSource

from bilibili_impl.parsers import (
    clean_bili_title,
    cues_to_lyric_lines,
    normalize_media_url,
    parse_bilibili_target,
    parse_duration_sec,
)

if TYPE_CHECKING:
    from plugin import BilibiliPlugin

_CATEGORY_ALIASES: dict[str, str] = {
    "recommend": "recommend",
    "推荐": "recommend",
    "rcmd": "recommend",
    "dynamic": "dynamic",
    "动态": "dynamic",
    "following": "dynamic",
    "bangumi": "bangumi",
    "追番": "bangumi",
    "pgc": "bangumi",
    "anime": "bangumi",
    "live": "live",
    "直播": "live",
    "search": "search",
    "搜索": "search",
}


class BilibiliMediaSource(MediaSource):
    """Domain 1 MediaSource exposing Bilibili recommend/dynamic/bangumi/live/search."""

    source_id: str = "plugin-bilibili"
    display_name: str = "Bilibili"

    def __init__(self, plugin: BilibiliPlugin) -> None:
        self._plugin = plugin

    def categories(self) -> list[str]:
        """Return supported Bilibili workbench categories."""
        return ["recommend", "dynamic", "bangumi", "live", "search"]

    async def fetch_items(
        self,
        category: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[MediaItem]:
        """Fetch real Bilibili feed/search entries mapped to MediaItem (fail-closed)."""
        del force_refresh
        page_num = max(1, int(page) if isinstance(page, int) and not isinstance(page, bool) else 1)
        clean_cat = category.strip().lower()
        if not clean_cat:
            resolved = "search" if query.strip() else "recommend"
        else:
            resolved = _CATEGORY_ALIASES.get(category.strip()) or _CATEGORY_ALIASES.get(clean_cat, "")
        if not resolved:
            return []

        client = self._plugin._build_client()
        cookie, _ = await self._plugin.resolve_cookie()
        quality_label = self._plugin._default_quality_label()
        api_base = client.api_base

        if resolved == "recommend":
            raw_items = await client.fetch_recommend(page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := self._map_recommend_item(row, quality_label, api_base)) is not None
            ]
        if resolved == "dynamic":
            raw_items = await client.fetch_dynamic(page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := self._map_dynamic_item(row, quality_label, api_base)) is not None
            ]
        if resolved == "bangumi":
            raw_items = await client.fetch_bangumi(page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := self._map_bangumi_item(row, api_base)) is not None
            ]
        if resolved == "live":
            raw_items = await client.fetch_live(page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := self._map_live_item(row, api_base)) is not None
            ]
        raw_items = await client.fetch_search(query=query, page=page_num, cookie=cookie)
        return [
            item
            for row in raw_items
            if (item := self._map_search_item(row, quality_label, api_base)) is not None
        ]

    async def fetch_chapters(self, item_id: str) -> list[ChapterSegment]:
        """Fetch real multi-part pages, view_points, and optional SponsorBlock segments."""
        return await self._plugin.fetch_chapters_for_item(item_id)

    async def fetch_lyrics(self, item_id: str) -> list[LyricLine]:
        """Fetch real Bilibili subtitles (with optional translation) as LyricLine cues."""
        res = await self._plugin.action_fetch_subtitles({"item_id": item_id, "use_asr": False})
        if not res.ok:
            return []
        cues = res.data.get("subtitles", [])
        return cues_to_lyric_lines(cues) if isinstance(cues, list) else []

    def _map_recommend_item(
        self, row: Mapping[str, Any], quality_label: str, api_base: str
    ) -> MediaItem | None:
        bvid = str(row.get("bvid") or "").strip()
        aid = row.get("aid") or row.get("id")
        aid_int = int(aid) if isinstance(aid, int) or (isinstance(aid, str) and aid.isdigit()) else None
        if not bvid and not aid_int:
            return None
        if str(row.get("goto") or "") == "ad":
            return None
        title = clean_bili_title(str(row.get("title") or ""))
        if not title:
            return None
        owner = row.get("owner") if isinstance(row.get("owner"), Mapping) else {}
        stat = row.get("stat") if isinstance(row.get("stat"), Mapping) else {}
        creator = str(owner.get("name") or "")
        item_id = bvid or f"av{aid_int}"
        play_url = f"https://www.bilibili.com/video/{bvid}" if bvid else f"https://www.bilibili.com/video/av{aid_int}"
        return MediaItem(
            id=item_id,
            source_id=self.source_id,
            title=title,
            subtitle=f"{creator} · 推荐" if creator else "Bilibili 推荐",
            creator=creator,
            kind="video",
            source_badge="Bilibili 推荐",
            cover_url=normalize_media_url(str(row.get("pic") or ""), api_base=api_base),
            play_url=play_url,
            quality_label=quality_label,
            duration_sec=parse_duration_sec(row.get("duration")),
            view_count=max(0, int(stat.get("view") or 0)) if str(stat.get("view") or "").isdigit() else 0,
            aspect="16:9",
            summary=str(row.get("rcmd_reason", {}).get("content", "") if isinstance(row.get("rcmd_reason"), Mapping) else ""),
            extra={
                "category": "recommend",
                "bvid": bvid,
                "aid": aid_int,
                "avid": f"av{aid_int}" if aid_int else "",
                "cid": row.get("cid"),
                "mid": owner.get("mid"),
            },
        )

    def _map_dynamic_item(
        self, row: Mapping[str, Any], quality_label: str, api_base: str
    ) -> MediaItem | None:
        modules = row.get("modules") if isinstance(row.get("modules"), Mapping) else {}
        author = modules.get("module_author") if isinstance(modules.get("module_author"), Mapping) else {}
        dyn = modules.get("module_dynamic") if isinstance(modules.get("module_dynamic"), Mapping) else {}
        major = dyn.get("major") if isinstance(dyn.get("major"), Mapping) else {}
        archive = major.get("archive") if isinstance(major.get("archive"), Mapping) else None
        if archive is None:
            return None
        bvid = str(archive.get("bvid") or "").strip()
        aid_raw = archive.get("aid")
        aid_int = int(aid_raw) if isinstance(aid_raw, int) or (isinstance(aid_raw, str) and aid_raw.isdigit()) else None
        title = clean_bili_title(str(archive.get("title") or ""))
        if not title or (not bvid and not aid_int):
            return None
        creator = str(author.get("name") or "")
        stat = archive.get("stat") if isinstance(archive.get("stat"), Mapping) else {}
        item_id = bvid or f"av{aid_int}"
        play_url = f"https://www.bilibili.com/video/{bvid}" if bvid else f"https://www.bilibili.com/video/av{aid_int}"
        return MediaItem(
            id=item_id,
            source_id=self.source_id,
            title=title,
            subtitle=f"{creator} · 动态" if creator else "Bilibili 动态",
            creator=creator,
            kind="video",
            source_badge="Bilibili 动态",
            cover_url=normalize_media_url(str(archive.get("cover") or ""), api_base=api_base),
            play_url=play_url,
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
            },
        )

    def _map_bangumi_item(self, row: Mapping[str, Any], api_base: str) -> MediaItem | None:
        season_id = row.get("season_id")
        title = clean_bili_title(str(row.get("title") or ""))
        if not season_id or not title:
            return None
        new_ep = row.get("new_ep") if isinstance(row.get("new_ep"), Mapping) else {}
        ep_id = new_ep.get("id")
        badge = str(row.get("badge") or "").strip()
        index_show = str(new_ep.get("index_show") or "").strip()
        raw_url = str(row.get("url") or f"https://www.bilibili.com/bangumi/play/ss{season_id}")
        try:
            target = parse_bilibili_target(raw_url)
            play_url = target["url"]
        except ValueError:
            play_url = f"https://www.bilibili.com/bangumi/play/ss{season_id}"
        return MediaItem(
            id=f"ss{season_id}",
            source_id=self.source_id,
            title=title,
            subtitle=index_show or str(row.get("progress") or "追番"),
            creator=str(row.get("season_type_name") or "Bilibili 番剧"),
            kind="video",
            source_badge="Bilibili 追番",
            cover_url=normalize_media_url(str(row.get("cover") or ""), api_base=api_base),
            play_url=play_url,
            quality_label=badge or "番剧",
            duration_sec=parse_duration_sec(new_ep.get("duration")),
            aspect="16:9",
            summary=str(row.get("evaluate") or row.get("summary") or ""),
            extra={
                "category": "bangumi",
                "season_id": int(season_id) if str(season_id).isdigit() else season_id,
                "ep_id": int(ep_id) if str(ep_id).isdigit() else ep_id,
                "media_id": row.get("media_id"),
            },
        )

    def _map_live_item(self, row: Mapping[str, Any], api_base: str) -> MediaItem | None:
        room_id = row.get("roomid") or row.get("room_id")
        title = clean_bili_title(str(row.get("title") or ""))
        if not room_id or not title:
            return None
        uname = str(row.get("uname") or "")
        area = str(row.get("area_name") or row.get("parent_name") or "")
        cover = str(row.get("cover") or row.get("user_cover") or row.get("system_cover") or "")
        online = row.get("online")
        return MediaItem(
            id=f"live:{room_id}",
            source_id=self.source_id,
            title=title,
            subtitle=f"{uname} · {area}".strip(" ·"),
            creator=uname,
            kind="live",
            source_badge="Bilibili 直播",
            cover_url=normalize_media_url(cover, api_base=api_base),
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
            },
        )

    def _map_search_item(
        self, row: Mapping[str, Any], quality_label: str, api_base: str
    ) -> MediaItem | None:
        bvid = str(row.get("bvid") or "").strip()
        aid_raw = row.get("aid") or row.get("id")
        aid_int = int(aid_raw) if isinstance(aid_raw, int) or (isinstance(aid_raw, str) and aid_raw.isdigit()) else None
        title = clean_bili_title(str(row.get("title") or ""))
        if not title or (not bvid and not aid_int):
            return None
        creator = str(row.get("author") or "")
        item_id = bvid or f"av{aid_int}"
        play_url = f"https://www.bilibili.com/video/{bvid}" if bvid else f"https://www.bilibili.com/video/av{aid_int}"
        return MediaItem(
            id=item_id,
            source_id=self.source_id,
            title=title,
            subtitle=f"{creator} · 搜索" if creator else "Bilibili 搜索",
            creator=creator,
            kind="video",
            source_badge="Bilibili 搜索",
            cover_url=normalize_media_url(str(row.get("pic") or ""), api_base=api_base),
            play_url=play_url,
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
            },
        )
