"""YouTube Data API v3 client plus ISO 8601 duration and chapter description parsers."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from omnidesk_sdk import ChapterSegment

if __package__:
    from .yt_http import http_get_json
    from .yt_security import extract_video_id, url_origin
else:
    from yt_http import http_get_json
    from yt_security import extract_video_id, url_origin

YOUTUBE_API_BASE_URL = "https://www.googleapis.com/youtube/v3"

_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
_DESC_CHAPTER_RE = re.compile(r"^\s*(?:[-*•]\s*)?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\s+(.+?)\s*$")


def parse_iso8601_duration(raw: str | None) -> float:
    """Parse an ISO 8601 YouTube duration string (e.g. PT1H2M10S) into seconds."""
    if not isinstance(raw, str) or not raw.strip():
        return 0.0
    m = _ISO_DURATION_RE.match(raw.strip())
    if not m:
        return 0.0
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = float(m.group("seconds") or 0.0)
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


def parse_description_chapters(description: str, duration_sec: float = 0.0) -> list[ChapterSegment]:
    """Extract timestamped chapter markers from a video description."""
    if not isinstance(description, str) or not description.strip():
        return []
    raw_markers: list[tuple[float, str]] = []
    for line in description.splitlines():
        m = _DESC_CHAPTER_RE.match(line)
        if not m:
            continue
        h, mins, secs = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
        if mins >= 60 or secs >= 60:
            continue
        title = m.group(4).strip(" -–—|")
        if title:
            raw_markers.append((float(h * 3600 + mins * 60 + secs), title))

    if not raw_markers:
        return []
    raw_markers.sort(key=lambda item: item[0])
    chapters: list[ChapterSegment] = []
    for idx, (start, label) in enumerate(raw_markers):
        end: float | None = None
        if idx + 1 < len(raw_markers):
            end = max(start, raw_markers[idx + 1][0])
        elif duration_sec > start:
            end = float(duration_sec)
        chapters.append(ChapterSegment(id=f"ch-{idx + 1}", start_sec=start, end_sec=end, label=label, kind="content"))
    return chapters


class YouTubeApiClient:
    """Thin YouTube Data API v3 client backed by safe HTTPS/loopback requests."""

    def __init__(self, *, api_base_url: str = YOUTUBE_API_BASE_URL, allow_loopback_http: bool = False) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.allow_loopback_http = allow_loopback_http

    def _allowed_origins(self) -> set[str]:
        return {url_origin(self.api_base_url)} if self.api_base_url.startswith("http://") else set()

    async def _call_api(
        self, endpoint: str, params: Mapping[str, Any], *, api_key: str = "", oauth_token: str = "", timeout_sec: float = 15.0
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        query = {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
        if api_key:
            query["key"] = api_key
        url = f"{self.api_base_url}/{endpoint.lstrip('/')}?{urllib.parse.urlencode(query)}"
        hdrs: dict[str, str] = {}
        if oauth_token:
            hdrs["Authorization"] = f"Bearer {oauth_token}"
        if api_key:
            hdrs["X-Goog-Api-Key"] = api_key
        secrets = [s for s in (api_key, oauth_token) if s]
        return await http_get_json(
            url,
            headers=hdrs,
            timeout_sec=timeout_sec,
            extra_secrets=secrets,
            allow_loopback_http=self.allow_loopback_http,
            allowed_loopback_origins=self._allowed_origins(),
        )

    async def enrich_videos(
        self, video_ids: list[str], *, api_key: str = "", oauth_token: str = "", timeout_sec: float = 15.0
    ) -> dict[str, dict[str, Any]]:
        """Best-effort fetch of duration/statistics/description from `/videos`."""
        valid = [vid for vid in video_ids if len(vid) == 11]
        if not valid:
            return {}
        data, err = await self._call_api(
            "videos",
            {"part": "snippet,contentDetails,statistics", "id": ",".join(valid[:50])},
            api_key=api_key,
            oauth_token=oauth_token,
            timeout_sec=timeout_sec,
        )
        if err is not None or not data:
            return {}
        return {item["id"]: dict(item) for item in (data.get("items") or []) if isinstance(item, Mapping) and isinstance(item.get("id"), str)}

    async def _enrich_items(
        self, items: list[dict[str, Any]], id_extractor: Any, *, api_key: str, oauth_token: str, timeout_sec: float
    ) -> list[dict[str, Any]]:
        vids: list[str] = []
        for item in items:
            raw_id = id_extractor(item)
            if isinstance(raw_id, str):
                try:
                    vids.append(extract_video_id(raw_id))
                except ValueError:
                    pass
        details = await self.enrich_videos(vids, api_key=api_key, oauth_token=oauth_token, timeout_sec=timeout_sec)
        merged: list[dict[str, Any]] = []
        for item in items:
            raw_id = id_extractor(item)
            if isinstance(raw_id, str) and raw_id in details:
                combined = {**item, **details[raw_id], "id": raw_id}
                merged.append(combined)
            else:
                merged.append(item)
        return merged

    async def search_videos(
        self,
        query: str,
        *,
        api_key: str = "",
        oauth_token: str = "",
        region: str = "US",
        max_results: int = 20,
        page_token: str = "",
        channel_id: str = "",
        timeout_sec: float = 15.0,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
        """Search videos via `/search` and enrich durations when `/videos` responds."""
        params: dict[str, Any] = {"part": "snippet", "type": "video", "maxResults": max(1, min(50, max_results)), "regionCode": region or "US"}
        if query.strip():
            params["q"] = query.strip()
        if channel_id:
            params["channelId"] = channel_id
            params["order"] = "date"
        if page_token:
            params["pageToken"] = page_token
        data, err = await self._call_api("search", params, api_key=api_key, oauth_token=oauth_token, timeout_sec=timeout_sec)
        if err is not None or data is None:
            return [], "", err
        items = [dict(i) for i in (data.get("items") or []) if isinstance(i, Mapping)]
        enriched = await self._enrich_items(
            items,
            lambda it: it.get("id", {}).get("videoId") if isinstance(it.get("id"), Mapping) else it.get("id"),
            api_key=api_key,
            oauth_token=oauth_token,
            timeout_sec=timeout_sec,
        )
        return enriched, str(data.get("nextPageToken") or ""), None

    async def fetch_trending(
        self,
        *,
        api_key: str = "",
        oauth_token: str = "",
        region: str = "US",
        max_results: int = 20,
        page_token: str = "",
        timeout_sec: float = 15.0,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
        """Fetch the regional most-popular chart via `/videos?chart=mostPopular`."""
        params: dict[str, Any] = {
            "part": "snippet,contentDetails,statistics",
            "chart": "mostPopular",
            "regionCode": region or "US",
            "maxResults": max(1, min(50, max_results)),
        }
        if page_token:
            params["pageToken"] = page_token
        data, err = await self._call_api(
            "videos",
            params,
            api_key=api_key,
            oauth_token=oauth_token,
            timeout_sec=timeout_sec,
        )
        if err is not None or data is None:
            return [], "", err
        items = [dict(item) for item in (data.get("items") or []) if isinstance(item, Mapping)]
        return items, str(data.get("nextPageToken") or ""), None

    async def fetch_playlist_videos(
        self,
        playlist_id: str,
        *,
        api_key: str = "",
        oauth_token: str = "",
        max_results: int = 20,
        page_token: str = "",
        timeout_sec: float = 15.0,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None]:
        """Fetch playlist entries via `/playlistItems` and enrich durations."""
        params: dict[str, Any] = {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": max(1, min(50, max_results))}
        if page_token:
            params["pageToken"] = page_token
        data, err = await self._call_api("playlistItems", params, api_key=api_key, oauth_token=oauth_token, timeout_sec=timeout_sec)
        if err is not None or data is None:
            return [], "", err
        items = [dict(i) for i in (data.get("items") or []) if isinstance(i, Mapping)]

        def _pl_vid(it: Mapping[str, Any]) -> Any:
            sn = it.get("snippet") if isinstance(it.get("snippet"), Mapping) else {}
            rid = sn.get("resourceId") if isinstance(sn.get("resourceId"), Mapping) else {}
            cd = it.get("contentDetails") if isinstance(it.get("contentDetails"), Mapping) else {}
            return cd.get("videoId") or rid.get("videoId")

        enriched = await self._enrich_items(items, _pl_vid, api_key=api_key, oauth_token=oauth_token, timeout_sec=timeout_sec)
        return enriched, str(data.get("nextPageToken") or ""), None

    async def fetch_subscriptions(
        self, *, oauth_token: str, api_key: str = "", max_results: int = 25, timeout_sec: float = 15.0
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        """Fetch personal YouTube channel subscriptions using OAuth Bearer token."""
        if not oauth_token:
            return [], {"error_code": "not_configured", "error": "需要配置 oauth_token 以获取个人订阅列表"}
        data, err = await self._call_api(
            "subscriptions",
            {"part": "snippet,contentDetails", "mine": "true", "maxResults": max(1, min(50, max_results))},
            api_key=api_key,
            oauth_token=oauth_token,
            timeout_sec=timeout_sec,
        )
        if err is not None or data is None:
            return [], err
        subs: list[dict[str, Any]] = []
        for item in data.get("items") or []:
            if not isinstance(item, Mapping):
                continue
            snippet = item.get("snippet") if isinstance(item.get("snippet"), Mapping) else {}
            res_id = snippet.get("resourceId") if isinstance(snippet.get("resourceId"), Mapping) else {}
            channel_id = str(res_id.get("channelId") or snippet.get("channelId") or "").strip()
            if channel_id:
                subs.append(
                    {
                        "channel_id": channel_id,
                        "title": str(snippet.get("title") or channel_id),
                        "description": str(snippet.get("description") or ""),
                        "source": "oauth",
                    }
                )
        return subs, None
