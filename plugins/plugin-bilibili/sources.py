"""BilibiliMediaSource implementing MediaSource (`media.source.v1`) for MediaWorkbench."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from omnidesk_sdk import ChapterSegment, LyricLine, MediaItem, MediaSource

from bilibili_impl.catalog import (
    fetch_favorites,
    fetch_history,
    fetch_popular,
    fetch_watch_later,
    normalize_favorite_row,
    normalize_history_row,
    normalize_watch_later_row,
)
from bilibili_impl.media_mappers import (
    map_bangumi_item,
    map_dynamic_item,
    map_live_item,
    map_recommend_item,
    map_search_item,
)
from bilibili_impl.parsers import cues_to_lyric_lines

if TYPE_CHECKING:
    from plugin import BilibiliPlugin

_CATEGORY_ALIASES: dict[str, str] = {
    "recommend": "recommend",
    "推荐": "recommend",
    "rcmd": "recommend",
    "popular": "popular",
    "热门": "popular",
    "hot": "popular",
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
    "mine": "history",
    "我的": "history",
    "history": "history",
    "历史": "history",
    "watch_later": "watch_later",
    "toview": "watch_later",
    "稍后再看": "watch_later",
    "favorites": "favorites",
    "favorite": "favorites",
    "fav": "favorites",
    "收藏": "favorites",
}


def _progress(row: Mapping[str, Any]) -> int:
    value = row.get("progress")
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


class BilibiliMediaSource(MediaSource):
    """Domain 1 MediaSource exposing Bilibili recommend/dynamic/bangumi/live/search."""

    source_id: str = "plugin-bilibili"
    display_name: str = "Bilibili"

    def __init__(self, plugin: BilibiliPlugin) -> None:
        self._plugin = plugin

    def categories(self) -> list[str]:
        """Return supported Bilibili workbench categories."""
        return [
            "recommend", "popular", "dynamic", "bangumi", "live",
            "history", "watch_later", "favorites", "search",
        ]

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
                if (item := map_recommend_item(
                    row, quality_label, api_base, source_id=self.source_id,
                    category="recommend", badge="Bilibili 推荐",
                )) is not None
            ]
        if resolved == "popular":
            raw_items = await fetch_popular(client, page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := map_recommend_item(
                    row, quality_label, api_base, source_id=self.source_id,
                    category="popular", badge="Bilibili 热门",
                )) is not None
            ]
        if resolved == "dynamic":
            raw_items = await client.fetch_dynamic(page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := map_dynamic_item(
                    row, quality_label, api_base, source_id=self.source_id
                )) is not None
            ]
        if resolved == "bangumi":
            raw_items = await client.fetch_bangumi(page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := map_bangumi_item(row, api_base, source_id=self.source_id)) is not None
            ]
        if resolved == "live":
            raw_items = await client.fetch_live(page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := map_live_item(row, api_base, source_id=self.source_id)) is not None
            ]
        if resolved == "history":
            raw_items = await fetch_history(client, page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := map_recommend_item(
                    normalize_history_row(row), quality_label, api_base, source_id=self.source_id,
                    category="history", badge="Bilibili 历史", progress=_progress(row),
                )) is not None
            ]
        if resolved == "watch_later":
            raw_items = await fetch_watch_later(client, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := map_recommend_item(
                    normalize_watch_later_row(row), quality_label, api_base,
                    source_id=self.source_id,
                    category="watch_later", badge="Bilibili 稍后再看",
                )) is not None
            ]
        if resolved == "favorites":
            raw_items = await fetch_favorites(client, page=page_num, cookie=cookie)
            return [
                item
                for row in raw_items
                if (item := map_recommend_item(
                    normalize_favorite_row(row), quality_label, api_base, source_id=self.source_id,
                    category="favorites", badge="Bilibili 收藏",
                )) is not None
            ]
        raw_items = await client.fetch_search(query=query, page=page_num, cookie=cookie)
        return [
            item
            for row in raw_items
            if (item := map_search_item(row, quality_label, api_base, source_id=self.source_id)) is not None
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
