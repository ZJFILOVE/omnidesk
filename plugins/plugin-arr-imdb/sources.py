"""MediaSource implementation and metadata normalization for `plugin-arr-imdb`."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from omnidesk_sdk import MediaItem, MediaSource

try:
    from .http_client import (
        UpstreamClientError,
        as_float,
        as_int,
        extract_genres,
        extract_poster_url,
        extract_year,
        normalize_category,
    )
    from .imdb_client import ImdbTmdbClient
except ImportError:
    _HERE = str(Path(__file__).resolve().parent)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from http_client import (
        UpstreamClientError,
        as_float,
        as_int,
        extract_genres,
        extract_poster_url,
        extract_year,
        normalize_category,
    )
    from imdb_client import ImdbTmdbClient

_IMDB_ID_RE = re.compile(r"^tt\d{5,12}\Z")
_RUNTIME_MIN_RE = re.compile(r"(\d{1,4})")
_CATEGORIES: tuple[str, ...] = ("popular", "top_rated", "upcoming", "tv_popular", "tv_top_rated", "search")


def _extract_imdb_id(raw: Mapping[str, Any]) -> str:
    ext = raw.get("external_ids") if isinstance(raw.get("external_ids"), Mapping) else {}
    for cand in (raw.get("imdb_id"), raw.get("imdbId"), raw.get("tconst"), ext.get("imdb_id"), raw.get("id")):
        val = str(cand or "").strip()
        if _IMDB_ID_RE.match(val):
            return val
    return ""


def _extract_tmdb_id(raw: Mapping[str, Any]) -> int | None:
    for key in ("tmdb_id", "tmdbId"):
        if (val := as_int(raw.get(key))) is not None and val > 0:
            return val
    raw_id = raw.get("id")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
        return raw_id
    if isinstance(raw_id, str) and raw_id.strip().isdigit():
        return int(raw_id.strip())
    return None


def _extract_rating(raw: Mapping[str, Any]) -> float | None:
    agg = raw.get("aggregateRating") if isinstance(raw.get("aggregateRating"), Mapping) else {}
    for cand in (raw.get("rating"), raw.get("imDbRating"), raw.get("imdb_rating"), raw.get("vote_average"), agg.get("ratingValue")):
        if (val := as_float(cand)) is not None and 0.0 <= val <= 10.0:
            return round(val, 2)
    return None


def _extract_runtime_minutes(raw: Mapping[str, Any]) -> int | None:
    for key in ("runtime_minutes", "runtimeMinutes", "runtime"):
        val = raw.get(key)
        if (num := as_int(val)) is not None and num > 0:
            return num
        if isinstance(val, str) and (match := _RUNTIME_MIN_RE.search(val)):
            return int(match.group(1))
    ep_rt = raw.get("episode_run_time")
    if isinstance(ep_rt, list) and ep_rt and (num := as_int(ep_rt[0])) is not None and num > 0:
        return num
    sec = as_int(raw.get("runtimeSeconds"))
    if sec is not None and sec > 0:
        return max(1, round(sec / 60))
    return None


def _extract_names(entries: Any, *, limit: int = 15) -> list[str]:
    names: list[str] = []
    if isinstance(entries, str):
        for part in entries.split(","):
            if cleaned := part.strip():
                names.append(cleaned)
        return names[:limit]
    if not isinstance(entries, list):
        return names
    for item in entries:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, Mapping):
            name = str(item.get("name") or item.get("fullName") or "").strip()
            if name:
                names.append(name)
        if len(names) >= limit:
            break
    return names


def _extract_directors(raw: Mapping[str, Any]) -> list[str]:
    for key in ("directors", "director"):
        if names := _extract_names(raw.get(key), limit=10):
            return names
    credits = raw.get("credits") if isinstance(raw.get("credits"), Mapping) else {}
    crew = credits.get("crew") if isinstance(credits.get("crew"), list) else []
    directors = [
        str(member.get("name") or "").strip()
        for member in crew
        if isinstance(member, Mapping) and str(member.get("job") or "").strip().lower() == "director" and str(member.get("name") or "").strip()
    ]
    if directors:
        return directors[:10]
    return _extract_names(raw.get("created_by"), limit=10)


def _extract_cast(raw: Mapping[str, Any]) -> list[str]:
    for key in ("cast", "actors", "stars"):
        if names := _extract_names(raw.get(key), limit=15):
            return names
    credits = raw.get("credits") if isinstance(raw.get("credits"), Mapping) else {}
    if names := _extract_names(credits.get("cast"), limit=15):
        return names
    principals = raw.get("principals") if isinstance(raw.get("principals"), list) else []
    return _extract_names(principals, limit=15)


def _infer_media_type(raw: Mapping[str, Any], category: str = "") -> str:
    raw_type = str(raw.get("media_type") or raw.get("mediaType") or raw.get("type") or "").strip().lower()
    if raw_type in {"tv", "series", "tvseries", "tv_series", "tvminiseries"} or category in {"tv_popular", "tv_top_rated"}:
        return "series"
    if not raw_type and ("first_air_date" in raw or ("name" in raw and "title" not in raw)):
        return "series"
    return "movie"


def normalize_detail_payload(raw: Mapping[str, Any], *, requested_id: str = "", media_type: str = "") -> dict[str, Any]:
    """Normalize an IMDb or TMDB detail payload into canonical OmniDesk fields."""
    imdb_id = _extract_imdb_id(raw) or (requested_id if _IMDB_ID_RE.match(requested_id) else "")
    tmdb_id = _extract_tmdb_id(raw)
    ext = raw.get("external_ids") if isinstance(raw.get("external_ids"), Mapping) else {}
    tvdb_id = as_int(raw.get("tvdb_id") or raw.get("tvdbId") or ext.get("tvdb_id"))
    resolved_type = media_type.lower() if media_type.lower() in {"movie", "series"} else _infer_media_type(raw)
    title = str(raw.get("title") or raw.get("primaryTitle") or raw.get("name") or "").strip()
    if not title:
        raise UpstreamClientError("not_found", f"媒体条目 {requested_id or '(unknown)'} 缺少标题元数据")
    original_title = str(raw.get("original_title") or raw.get("originalTitle") or raw.get("original_name") or title).strip()
    directors = _extract_directors(raw)
    cast = _extract_cast(raw)
    runtime_min = _extract_runtime_minutes(raw)
    genres = extract_genres(raw)
    rating = _extract_rating(raw)
    overview = str(raw.get("overview") or raw.get("plot") or raw.get("summary") or raw.get("description") or "").strip()
    year = extract_year(raw)
    poster_url = extract_poster_url(raw)
    canonical_id = imdb_id or (f"tmdb:{resolved_type}:{tmdb_id}" if tmdb_id else (requested_id or title))
    return {
        "item_id": canonical_id,
        "imdb_id": imdb_id,
        "tmdb_id": tmdb_id,
        "tvdb_id": tvdb_id,
        "media_type": resolved_type,
        "title": title,
        "original_title": original_title,
        "year": year,
        "directors": directors,
        "director": ", ".join(directors),
        "cast": cast,
        "runtime_minutes": runtime_min,
        "duration_sec": (runtime_min * 60) if runtime_min else 0,
        "genres": genres,
        "rating": rating,
        "overview": overview,
        "poster_url": poster_url,
        "imdb_url": f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "",
    }


def _map_raw_to_media_item(raw: Mapping[str, Any], *, source_id: str, norm_category: str) -> MediaItem | None:
    title = str(raw.get("title") or raw.get("primaryTitle") or raw.get("name") or raw.get("originalTitle") or "").strip()
    if not title:
        return None
    imdb_id = _extract_imdb_id(raw)
    tmdb_id = _extract_tmdb_id(raw)
    tvdb_id = as_int(raw.get("tvdb_id") or raw.get("tvdbId"))
    media_type = _infer_media_type(raw, norm_category)
    raw_str_id = str(raw.get("id") or "").strip()
    item_id = imdb_id or (f"tmdb:{media_type}:{tmdb_id}" if tmdb_id else raw_str_id)
    if not item_id:
        return None

    year = extract_year(raw)
    rating = _extract_rating(raw)
    genres = extract_genres(raw)
    poster_url = extract_poster_url(raw)
    overview = str(raw.get("overview") or raw.get("plot") or raw.get("summary") or raw.get("description") or "").strip()
    runtime_min = _extract_runtime_minutes(raw)
    directors = _extract_directors(raw)

    parts = [str(year)] if year else []
    if rating is not None:
        parts.append(f"★ {rating:.1f}")
    if genres:
        parts.append(", ".join(genres[:3]))

    return MediaItem(
        id=item_id,
        source_id=source_id,
        title=title,
        subtitle=" · ".join(parts),
        creator=", ".join(directors),
        kind="video",
        source_badge="IMDb · 剧集" if media_type == "series" else "IMDb · 电影",
        cover_url=poster_url,
        play_url=f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else "",
        quality_label=f"★ {rating:.1f}" if rating is not None else "",
        duration_sec=float(runtime_min * 60) if runtime_min else 0.0,
        aspect="16:9",
        summary=overview,
        extra={
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "tvdb_id": tvdb_id,
            "media_type": media_type,
            "year": year,
            "rating": rating,
            "genres": genres,
            "poster_url": poster_url,
            "overview": overview,
            "category": norm_category,
        },
    )


class ArrImdbMediaSource(MediaSource):
    """Domain 1 MediaSource exposing IMDb/TMDB charts, search, and metadata details."""

    source_id: str = "arr-imdb"
    display_name: str = "IMDb · Radarr / Sonarr"

    def __init__(self, client_factory: Callable[[], ImdbTmdbClient]) -> None:
        self._client_factory = client_factory

    def categories(self) -> list[str]:
        """Return supported IMDb/TMDB chart and search categories."""
        return list(_CATEGORIES)

    async def fetch_items(
        self,
        category: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[MediaItem]:
        """Fetch chart or search items from configured IMDb/TMDB endpoint (fail closed on errors)."""
        _ = force_refresh
        norm_cat = normalize_category(category, query)
        client = self._client_factory()
        rows = await client.fetch_items(category=norm_cat, query=query, page=page)
        items: list[MediaItem] = []
        for row in rows:
            mapped = _map_raw_to_media_item(row, source_id=self.source_id, norm_category=norm_cat)
            if mapped is not None:
                items.append(mapped)
        return items

    async def fetch_detail(self, item_id: str, *, media_type: str = "") -> dict[str, Any]:
        """Fetch full movie/series metadata (directors, cast, runtime, genres, rating, overview)."""
        client = self._client_factory()
        raw = await client.fetch_detail(item_id, media_type=media_type)
        return normalize_detail_payload(raw, requested_id=item_id.strip(), media_type=media_type)
