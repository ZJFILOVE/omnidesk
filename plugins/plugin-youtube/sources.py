"""MediaSource implementation for YouTube subscriptions, search, channels, playlists, and chapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from omnidesk_sdk import ChapterSegment, LyricLine, MediaItem, MediaSource, PluginContext

if __package__:
    from .yt_api import YouTubeApiClient, parse_description_chapters
    from .yt_auth import resolve_cookie, session_cookie_present
    from .yt_dlp import fetch_video_info, run_ytdlp_json
    from .yt_library import load_items
    from .yt_mappers import (
        map_api_item_to_media_item,
        map_ytdlp_entry_to_media_item,
        merge_chapters_and_sponsorblock,
        parse_ytdlp_chapters,
        safe_float,
    )
    from .yt_security import extract_video_id, validate_channel_id, validate_playlist_id
else:
    from yt_api import YouTubeApiClient, parse_description_chapters
    from yt_auth import resolve_cookie, session_cookie_present
    from yt_dlp import fetch_video_info, run_ytdlp_json
    from yt_library import load_items
    from yt_mappers import (
        map_api_item_to_media_item,
        map_ytdlp_entry_to_media_item,
        merge_chapters_and_sponsorblock,
        parse_ytdlp_chapters,
        safe_float,
    )
    from yt_security import extract_video_id, validate_channel_id, validate_playlist_id

_MAX_CACHE_ENTRIES = 200
_CATEGORY_ALIASES = {
    "订阅": "subscriptions", "subs": "subscriptions", "subscriptions": "subscriptions",
    "搜索": "search", "search": "search",
    "频道": "channels", "channel": "channels", "channels": "channels",
    "稍后观看": "watch_later", "watch_later": "watch_later", "wl": "watch_later",
    "playlist": "playlists", "playlists": "playlists",
    "热门": "trending", "trending": "trending", "hot": "trending",
    "播客": "podcast", "podcast": "podcast", "podcasts": "podcast",
    "历史": "history", "history": "history",
}

_PODCAST_QUERIES = (
    "technology engineering podcast full episode",
    "science AI interview podcast",
    "computer science systems podcast",
    "software architecture founder interview podcast",
)


def split_csv(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _normalize_category(category: str, query: str, *, has_subs_cfg: bool) -> str:
    raw = (category or "").strip().lower()
    if raw in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[raw]
    if not raw:
        return "search" if query.strip() else ("subscriptions" if has_subs_cfg else "search")
    return raw


class YouTubeMediaSource(MediaSource):
    """Domain 1 MediaSource providing YouTube subscriptions, search, channels, playlists, and chapters."""

    source_id = "plugin-youtube"
    display_name = "YouTube"

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._item_cache: dict[str, MediaItem] = {}
        self._chapter_cache: dict[str, list[ChapterSegment]] = {}
        self._page_tokens: dict[tuple[str, str, int], str] = {}
        self.last_error: dict[str, Any] | None = None

    def clear_caches(self) -> None:
        """Clear transient in-memory caches on unload."""
        self._item_cache.clear()
        self._chapter_cache.clear()
        self._page_tokens.clear()
        self.last_error = None

    def categories(self) -> list[str]:
        """Return actual supported YouTube media categories."""
        cats = [
            "subscriptions", "trending", "podcast", "search",
            "channels", "watch_later", "history",
        ]
        if self._plugin.configured_playlist_ids():
            cats.append("playlists")
        return cats

    def remember_items(self, items: Iterable[MediaItem]) -> None:
        for item in items:
            if len(self._item_cache) >= _MAX_CACHE_ENTRIES:
                self._item_cache.pop(next(iter(self._item_cache)), None)
            self._item_cache[item.id] = item
            if item.chapters:
                self._chapter_cache[item.id] = list(item.chapters)

    def _map_api_items(self, raw_items: Iterable[Mapping[str, Any]], *, category: str) -> list[MediaItem]:
        quality = self._plugin.quality()
        return [
            m for r in raw_items
            if (m := map_api_item_to_media_item(r, source_id=self.source_id, quality_label=quality, category=category)) is not None
        ]

    async def fetch_from_target(self, target_type: str, target_id: str, *, category: str, per_page: int, page: int = 1) -> list[MediaItem]:
        api: YouTubeApiClient = self._plugin.api_client
        api_key, oauth, region = self._plugin.api_key(), self._plugin.oauth_token(), self._plugin.region()
        timeout_sec = self._plugin.timeout_sec()

        token_key = (f"{category}:{target_type}", f"{target_id}:{per_page}")
        if page == 1:
            self._page_tokens = {k: v for k, v in self._page_tokens.items() if k[:2] != token_key}
        token = self._page_tokens.get((*token_key, page), "")
        if (api_key or oauth) and page > 1 and not token:
            return []

        if target_type == "playlist":
            try:
                pid = validate_playlist_id(target_id)
            except ValueError:
                return []
            if api_key or oauth:
                raw_items, next_token, err = await api.fetch_playlist_videos(pid, api_key=api_key, oauth_token=oauth, max_results=per_page, page_token=token, timeout_sec=timeout_sec)
                if err is not None:
                    self.last_error = err
                    return []
                if next_token:
                    self._page_tokens[(*token_key, page + 1)] = next_token
                return self._map_api_items(raw_items, category=category)
            return await self._fetch_via_ytdlp_list(f"https://www.youtube.com/playlist?list={pid}", category=category, start=(page - 1) * per_page + 1, end=page * per_page)

        if target_type == "channel":
            try:
                cid = validate_channel_id(target_id)
            except ValueError:
                return []
            if api_key or oauth:
                if cid.startswith("UC"):
                    raw_items, next_token, err = await api.fetch_playlist_videos("UU" + cid[2:], api_key=api_key, oauth_token=oauth, max_results=per_page, page_token=token, timeout_sec=timeout_sec)
                    if err is not None:
                        raw_items, next_token, err = await api.search_videos("", api_key=api_key, oauth_token=oauth, region=region, max_results=per_page, page_token=token, channel_id=cid, timeout_sec=timeout_sec)
                else:
                    raw_items, next_token, err = await api.search_videos(cid, api_key=api_key, oauth_token=oauth, region=region, max_results=per_page, page_token=token, timeout_sec=timeout_sec)
                if err is not None:
                    self.last_error = err
                    return []
                if next_token:
                    self._page_tokens[(*token_key, page + 1)] = next_token
                return self._map_api_items(raw_items, category=category)
            url = f"https://www.youtube.com/{cid}/videos" if cid.startswith("@") else f"https://www.youtube.com/channel/{cid}/videos"
            return await self._fetch_via_ytdlp_list(url, category=category, start=(page - 1) * per_page + 1, end=page * per_page)
        return []

    async def _has_session_cookie(self) -> bool:
        if self._plugin.ctx is None:
            return False
        cookie, _ = await resolve_cookie(self._plugin.ctx)
        return session_cookie_present(cookie)

    async def _fetch_via_ytdlp_list(self, target: str, *, category: str, start: int, end: int) -> list[MediaItem]:
        if self._plugin.ctx is None:
            return []
        payload, err = await run_ytdlp_json(
            self._plugin.ctx,
            self._plugin.ytdlp_bin(),
            ["-J", "--flat-playlist", "--no-warnings", "--skip-download", "--playlist-start", str(start), "--playlist-end", str(end), target],
            timeout_sec=self._plugin.timeout_sec(),
            extra_secrets=self._plugin.secret_values(),
        )
        if err is not None or payload is None:
            self.last_error = err
            return []
        raw_entries = payload.get("entries") if isinstance(payload, Mapping) and isinstance(payload.get("entries"), list) else (
            payload if isinstance(payload, list) else ([payload] if isinstance(payload, Mapping) else [])
        )
        quality = self._plugin.quality()
        return [
            m for entry in raw_entries if isinstance(entry, Mapping)
            and (m := map_ytdlp_entry_to_media_item(entry, source_id=self.source_id, quality_label=quality, category=category)) is not None
        ]

    async def fetch_items(self, category: str = "", query: str = "", page: int = 1, *, force_refresh: bool = False) -> list[MediaItem]:
        """Fetch real YouTube MediaItems fail-closed (returning [] on empty or error)."""
        self.last_error = None
        if page < 1:
            return []
        has_subs = bool(self._plugin.oauth_token() or self._plugin.configured_channel_ids() or self._plugin.configured_playlist_ids())
        cat = _normalize_category(category, query, has_subs_cfg=has_subs)
        per_page = 20
        items: list[MediaItem] = []

        if cat == "search":
            q = query.strip()
            if not q:
                return []
            if api_key := self._plugin.api_key():
                if page == 1 or force_refresh:
                    self._page_tokens = {k: v for k, v in self._page_tokens.items() if k[:2] != (cat, q)}
                token = self._page_tokens.get((cat, q, page), "")
                if page > 1 and not token:
                    return []
                raw_items, next_tok, err = await self._plugin.api_client.search_videos(
                    q, api_key=api_key, oauth_token=self._plugin.oauth_token(), region=self._plugin.region(),
                    max_results=per_page, page_token=token, timeout_sec=self._plugin.timeout_sec(),
                )
                if err is not None:
                    self.last_error = err
                    return []
                if next_tok:
                    self._page_tokens[(cat, q, page + 1)] = next_tok
                items = self._map_api_items(raw_items, category=cat)
            else:
                items = await self._fetch_via_ytdlp_list(f"ytsearch{page * per_page}:{q}", category=cat, start=(page - 1) * per_page + 1, end=page * per_page)
        elif cat == "subscriptions":
            oauth = self._plugin.oauth_token()
            channel_ids, playlist_ids = list(self._plugin.configured_channel_ids()), list(self._plugin.configured_playlist_ids())
            if not oauth and not channel_ids and not playlist_ids:
                if await self._has_session_cookie():
                    items = await self._fetch_via_ytdlp_list(
                        "https://www.youtube.com/feed/subscriptions",
                        category=cat,
                        start=(page - 1) * per_page + 1,
                        end=page * per_page,
                    )
                    if items:
                        self.remember_items(items)
                    return items
                return []
            if oauth:
                subs, err = await self._plugin.api_client.fetch_subscriptions(oauth_token=oauth, api_key=self._plugin.api_key(), timeout_sec=self._plugin.timeout_sec())
                if err is not None:
                    self.last_error = err
                    return []
                channel_ids.extend(s["channel_id"] for s in subs[:5] if s.get("channel_id"))
            for cid in dict.fromkeys(channel_ids):
                items.extend(await self.fetch_from_target("channel", cid, category=cat, per_page=max(5, per_page // max(1, len(channel_ids))), page=page))
            for pid in dict.fromkeys(playlist_ids):
                items.extend(await self.fetch_from_target("playlist", pid, category=cat, per_page=max(5, per_page // max(1, len(playlist_ids))), page=page))
        elif cat == "channels":
            for cid in ([query.strip()] if query.strip() else self._plugin.configured_channel_ids()):
                items.extend(await self.fetch_from_target("channel", cid, category=cat, per_page=per_page, page=page))
        elif cat == "history":
            local_items = load_items(self._plugin, "history")
            if query.strip():
                needle = query.strip().lower()
                local_items = [
                    item for item in local_items
                    if needle in item.title.lower() or needle in item.creator.lower()
                ]
            items.extend(local_items[(page - 1) * per_page : page * per_page])
        elif cat == "watch_later":
            local_items = load_items(self._plugin, "watch_later")
            if query.strip():
                needle = query.strip().lower()
                local_items = [
                    item for item in local_items
                    if needle in item.title.lower() or needle in item.creator.lower()
                ]
            items.extend(local_items[(page - 1) * per_page : page * per_page])
            targets = [query.strip()] if query.strip() else self._plugin.configured_playlist_ids()
            if not targets and (self._plugin.oauth_token() or await self._has_session_cookie()):
                targets = ["WL"]
            for pid in targets:
                items.extend(
                    await self.fetch_from_target("playlist", pid, category=cat, per_page=per_page, page=page)
                )
        elif cat == "playlists":
            targets = [query.strip()] if query.strip() else self._plugin.configured_playlist_ids()
            for pid in targets:
                items.extend(
                    await self.fetch_from_target("playlist", pid, category=cat, per_page=per_page, page=page)
                )
        elif cat in {"trending", "podcast"}:
            if cat == "trending" and (self._plugin.api_key() or self._plugin.oauth_token()):
                if page == 1 or force_refresh:
                    self._page_tokens = {k: v for k, v in self._page_tokens.items() if k[:2] != (cat, "")}
                token = self._page_tokens.get((cat, "", page), "")
                if page > 1 and not token:
                    return []
                raw_items, next_tok, err = await self._plugin.api_client.fetch_trending(
                    api_key=self._plugin.api_key(),
                    oauth_token=self._plugin.oauth_token(),
                    region=self._plugin.region(),
                    max_results=per_page,
                    page_token=token,
                    timeout_sec=self._plugin.timeout_sec(),
                )
                if err is not None:
                    self.last_error = err
                    return []
                if next_tok:
                    self._page_tokens[(cat, "", page + 1)] = next_tok
                items = self._map_api_items(raw_items, category=cat)
            else:
                if cat == "trending" and await self._has_session_cookie():
                    items = await self._fetch_via_ytdlp_list(
                        "https://www.youtube.com/",
                        category=cat,
                        start=(page - 1) * per_page + 1,
                        end=page * per_page,
                    )
                if not items:
                    fallback = (
                        _PODCAST_QUERIES[(page - 1) % len(_PODCAST_QUERIES)]
                        if cat == "podcast"
                        else f"science technology documentary {self._plugin.region()}"
                    )
                    items = await self._fetch_via_ytdlp_list(
                        f"ytsearch{page * per_page}:{fallback}",
                        category=cat,
                        start=(page - 1) * per_page + 1,
                        end=page * per_page,
                    )

        if items:
            self.remember_items(items)
        return items

    async def fetch_chapters(self, item_id: str) -> list[ChapterSegment]:
        """Return native chapters merged with optional SponsorBlock `player.timeline.v1` segments."""
        ctx: PluginContext | None = self._plugin.ctx
        if ctx is None:
            return []
        try:
            vid = extract_video_id(item_id)
        except ValueError:
            return []

        native = list(self._chapter_cache.get(vid, ()))
        if not native and vid in self._item_cache:
            cached = self._item_cache[vid]
            native = parse_description_chapters(str(cached.extra.get("description") or ""), cached.duration_sec)
        if not native:
            info, _ = await fetch_video_info(
                ctx, self._plugin.ytdlp_bin(), vid, timeout_sec=self._plugin.timeout_sec(),
                extra_secrets=self._plugin.secret_values(), allow_loopback_http=self._plugin.allow_loopback_http,
                allowed_loopback_origins=self._plugin.allowed_loopback_origins(),
            )
            if info is not None:
                native = parse_ytdlp_chapters(info.get("chapters")) or parse_description_chapters(str(info.get("description") or ""), safe_float(info.get("duration")))

        sb_payload: Mapping[str, Any] | None = None
        if ctx.capabilities.has("player.timeline.v1"):
            res = await ctx.capabilities.call(
                "player.timeline.v1",
                {"action": "query", "platform": "youtube", "video_id": vid, "item_id": vid, "chapters": [c.model_dump() for c in native]},
            )
            if res.ok and isinstance(res.data, Mapping):
                sb_payload = res.data

        merged = merge_chapters_and_sponsorblock(native, sb_payload)
        if merged:
            self._chapter_cache[vid] = merged
        return merged

    async def fetch_lyrics(self, item_id: str) -> list[LyricLine]:
        """Return synchronized bilingual subtitle cues mapped as LyricLine items."""
        return await self._plugin.fetch_lyrics_for_item(item_id)
