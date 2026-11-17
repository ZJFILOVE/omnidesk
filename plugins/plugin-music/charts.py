"""Apple Music Marketing RSS charts with host validation, proxy support, and strict parsing."""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

_CHART_HOST = "rss.marketingtools.apple.com"
_STOREFRONT_RE = re.compile(r"^[a-z]{2}$")
_SONG_ID_RE = re.compile(r"^[0-9]{1,24}$")
_ARTWORK_SIZE_RE = re.compile(r"/\d+x\d+bb\.(jpg|png)$", re.IGNORECASE)


class ChartFetchError(RuntimeError):
    """Structured chart fetch failure that never carries query secrets."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def build_chart_url(storefront: str = "cn", limit: int = 10) -> str:
    """Build and validate a Marketing Tools most-played songs URL."""
    sf = (storefront or "").strip().lower()
    if not _STOREFRONT_RE.match(sf):
        raise ChartFetchError("invalid_argument", "storefront 必须是两位小写地区码")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
        raise ChartFetchError("invalid_argument", "limit 必须是 1..25 的整数")
    return f"https://{_CHART_HOST}/api/v2/{sf}/music/most-played/{limit}/songs.json"


def _normalize_artwork(raw: Any) -> str:
    url = str(raw or "").strip()
    if not url.startswith("https://"):
        return ""
    return _ARTWORK_SIZE_RE.sub("/600x600bb.\\1", url)


def parse_chart_payload(payload: Mapping[str, Any], *, storefront: str, limit: int) -> list[dict[str, Any]]:
    """Convert a Marketing Tools feed into OmniDesk MediaItem-compatible dicts."""
    sf = (storefront or "").strip().lower()
    feed = payload.get("feed")
    if not isinstance(feed, Mapping):
        raise ChartFetchError("service_offline", "Apple Music 榜单响应缺少 feed")
    results = feed.get("results")
    if not isinstance(results, list):
        raise ChartFetchError("service_offline", "Apple Music 榜单响应缺少 results")
    feed_updated = str(feed.get("updated") or "").strip()
    items: list[dict[str, Any]] = []
    for rank, row in enumerate(results, start=1):
        if len(items) >= limit:
            break
        if not isinstance(row, Mapping):
            continue
        song_id = str(row.get("id") or "").strip()
        title = str(row.get("name") or "").strip()
        if not _SONG_ID_RE.match(song_id) or not title:
            continue
        artist = str(row.get("artistName") or "").strip()
        album = str(row.get("collectionName") or "").strip()
        url = str(row.get("url") or "").strip()
        if not url.startswith("https://music.apple.com/"):
            url = f"https://music.apple.com/{sf}/song/{song_id}"
        subtitle = " · ".join(part for part in (artist, f"国区实时榜 #{rank}") if part)
        items.append(
            {
                "id": f"am-{sf}-{song_id}",
                "source_id": "plugin-music",
                "title": title,
                "subtitle": subtitle,
                "creator": artist,
                "kind": "music",
                "source_badge": f"Apple Music {sf.upper()} 实时榜 #{rank}",
                "cover_url": _normalize_artwork(row.get("artworkUrl100")),
                "play_url": url,
                "duration_sec": 0.0,
                "aspect": "1:1",
                "summary": f"Apple Music 国区热门歌曲排行第 {rank} 位",
                "listening_cue": "想快速了解当下流行风向时播放",
                "curation_mode": "interactive_ephemeral",
                "extra": {
                    "apple_music_song_id": song_id,
                    "song_id": song_id,
                    "storefront": sf,
                    "chart": "most-played",
                    "chart_rank": rank,
                    "chart_updated": feed_updated,
                    "album": album,
                    "release_date": str(row.get("releaseDate") or ""),
                    "curation_mode": "interactive_ephemeral",
                },
            }
        )
    return items


def _fetch_sync(url: str, *, proxy: str, timeout_sec: float) -> dict[str, Any]:
    proxy_clean = (proxy or "").strip()
    handler = (
        urllib.request.ProxyHandler({"http": proxy_clean, "https": proxy_clean})
        if proxy_clean
        else urllib.request.ProxyHandler({})
    )
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "OmniDesk/2"})
    try:
        with opener.open(req, timeout=max(1.0, timeout_sec)) as resp:
            final_host = (urllib.parse.urlparse(resp.geturl()).hostname or "").lower()
            if final_host != _CHART_HOST:
                raise ChartFetchError("service_offline", f"榜单请求被重定向到非白名单主机: {final_host or 'unknown'}")
            raw = resp.read(512 * 1024)
    except urllib.error.HTTPError as exc:
        raise ChartFetchError("service_offline", f"Apple Music 榜单上游错误 (HTTP {exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ChartFetchError("service_offline", f"Apple Music 榜单不可达: {str(exc) or type(exc).__name__}") from None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChartFetchError("service_offline", f"Apple Music 榜单响应无法解析: {str(exc)}") from None
    if not isinstance(parsed, dict):
        raise ChartFetchError("service_offline", "Apple Music 榜单响应必须是 JSON 对象")
    return parsed


async def fetch_apple_music_chart(
    *,
    storefront: str = "cn",
    limit: int = 10,
    proxy: str = "",
    timeout_sec: float = 15.0,
) -> list[dict[str, Any]]:
    """Fetch and parse a live Apple Music chart without credentials or fallback fabrication."""
    url = build_chart_url(storefront, limit)
    payload = await asyncio.to_thread(_fetch_sync, url, proxy=proxy, timeout_sec=timeout_sec)
    return parse_chart_payload(payload, storefront=storefront, limit=limit)
