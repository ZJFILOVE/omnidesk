"""MediaItem and ChapterSegment normalization helpers for YouTube API, yt-dlp, and SponsorBlock."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from omnidesk_sdk import ChapterKind, ChapterSegment, MediaItem

if __package__:
    from .yt_api import parse_iso8601_duration
    from .yt_security import canonical_watch_url, extract_video_id, hqdefault_cover_url
else:
    from yt_api import parse_iso8601_duration
    from yt_security import canonical_watch_url, extract_video_id, hqdefault_cover_url

_DELETED_TITLES = frozenset({"[deleted video]", "[private video]", "deleted video", "private video"})


def safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return num if math.isfinite(num) and num >= 0.0 else default


def _build_media_item(
    *,
    raw_id: str,
    title: str,
    channel: str,
    subtitle: str,
    is_live: bool,
    quality_label: str,
    duration_sec: float,
    view_count: int,
    description: str,
    chapters: list[ChapterSegment] | None = None,
    extra: dict[str, Any] | None = None,
    source_id: str,
) -> MediaItem | None:
    try:
        video_id = extract_video_id(raw_id)
    except ValueError:
        return None
    cleaned_title = title.strip()
    if not cleaned_title or cleaned_title.lower() in _DELETED_TITLES:
        return None
    merged_extra = {"video_id": video_id, "description": description, **(extra or {})}
    return MediaItem(
        id=video_id,
        source_id=source_id,
        title=cleaned_title,
        subtitle=subtitle,
        creator=channel,
        kind="live" if is_live else "video",
        source_badge="YouTube",
        cover_url=hqdefault_cover_url(video_id),
        play_url=canonical_watch_url(video_id),
        quality_label=quality_label,
        duration_sec=duration_sec,
        view_count=view_count,
        aspect="16:9",
        summary=description[:240],
        chapters=chapters or [],
        extra=merged_extra,
    )


def map_api_item_to_media_item(raw: Mapping[str, Any], *, source_id: str, quality_label: str, category: str) -> MediaItem | None:
    """Convert a YouTube Data API v3 search/playlist/video dict to MediaItem."""
    snippet = raw.get("snippet") if isinstance(raw.get("snippet"), Mapping) else {}
    res_id = snippet.get("resourceId") if isinstance(snippet.get("resourceId"), Mapping) else {}
    cd = raw.get("contentDetails") if isinstance(raw.get("contentDetails"), Mapping) else {}
    stats = raw.get("statistics") if isinstance(raw.get("statistics"), Mapping) else {}
    raw_id = raw.get("id")
    candidate = raw_id.get("videoId") if isinstance(raw_id, Mapping) else (cd.get("videoId") or res_id.get("videoId") or raw_id)
    if not isinstance(candidate, str):
        return None
    channel = str(snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle") or raw.get("channelTitle") or "").strip()
    published_at = str(snippet.get("publishedAt") or "").strip()
    return _build_media_item(
        raw_id=candidate,
        title=str(snippet.get("title") or raw.get("title") or ""),
        channel=channel,
        subtitle=" · ".join(p for p in (channel, published_at[:10] if published_at else "") if p),
        is_live=str(snippet.get("liveBroadcastContent") or "").lower() == "live",
        quality_label=quality_label,
        duration_sec=parse_iso8601_duration(cd.get("duration")),
        view_count=safe_int(stats.get("viewCount")),
        description=str(snippet.get("description") or "").strip(),
        extra={"channel_id": str(snippet.get("videoOwnerChannelId") or snippet.get("channelId") or "").strip(), "published_at": published_at, "category": category},
        source_id=source_id,
    )


def map_ytdlp_entry_to_media_item(raw: Mapping[str, Any], *, source_id: str, quality_label: str, category: str) -> MediaItem | None:
    """Convert a yt-dlp flat-playlist or single-video JSON dict to MediaItem."""
    candidate = str(raw.get("id") or raw.get("url") or raw.get("webpage_url") or "").strip()
    if not candidate:
        return None
    channel = str(raw.get("channel") or raw.get("uploader") or raw.get("playlist_uploader") or "").strip()
    return _build_media_item(
        raw_id=candidate,
        title=str(raw.get("title") or raw.get("fulltitle") or ""),
        channel=channel,
        subtitle=channel,
        is_live=bool(raw.get("is_live")) or str(raw.get("live_status") or "").lower() == "is_live",
        quality_label=quality_label,
        duration_sec=safe_float(raw.get("duration")),
        view_count=safe_int(raw.get("view_count")),
        description=str(raw.get("description") or "").strip(),
        chapters=parse_ytdlp_chapters(raw.get("chapters")),
        extra={"channel_id": str(raw.get("channel_id") or raw.get("uploader_id") or ""), "category": category},
        source_id=source_id,
    )


def parse_ytdlp_chapters(raw_chapters: Any) -> list[ChapterSegment]:
    """Parse native chapters from yt-dlp -J metadata into ChapterSegment models."""
    if not isinstance(raw_chapters, list):
        return []
    out: list[ChapterSegment] = []
    for idx, ch in enumerate(raw_chapters, 1):
        if not isinstance(ch, Mapping):
            continue
        start = safe_float(ch.get("start_time") if "start_time" in ch else ch.get("start_sec"))
        raw_end = ch.get("end_time") if "end_time" in ch else ch.get("end_sec")
        end = max(start, safe_float(raw_end)) if raw_end is not None else None
        label = str(ch.get("title") or ch.get("label") or f"Chapter {idx}").strip()
        out.append(ChapterSegment(id=f"ch-{idx}", start_sec=start, end_sec=end, label=label, kind="content"))
    return out


def _coerce_chapter_kind(raw_kind: Any, raw_category: Any = "") -> ChapterKind:
    val = str(raw_kind or "").strip().lower()
    if val in {"content", "ad", "sponsor"}:
        return val  # type: ignore[return-value]
    cat = str(raw_category or "").strip().lower()
    if cat in {"sponsor", "selfpromo", "interaction", "intro", "outro", "preview", "music_offtopic"}:
        return "sponsor"
    return "ad" if cat in {"ad", "promotion"} else "content"


def merge_chapters_and_sponsorblock(
    native_chapters: list[ChapterSegment], timeline_payload: Mapping[str, Any] | None
) -> list[ChapterSegment]:
    """Merge native chapters with optional SponsorBlock segments, deduplicating and sorting."""
    combined: list[ChapterSegment] = list(native_chapters)
    if isinstance(timeline_payload, Mapping):
        raw_items = timeline_payload.get("chapters") if isinstance(timeline_payload.get("chapters"), list) else timeline_payload.get("segments")
        if isinstance(raw_items, list):
            for idx, entry in enumerate(raw_items, 1):
                if not isinstance(entry, Mapping):
                    continue
                start = safe_float(entry.get("start_sec") if "start_sec" in entry else entry.get("startSec") if "startSec" in entry else entry.get("start"))
                raw_end = entry.get("end_sec") if "end_sec" in entry else entry.get("endSec") if "endSec" in entry else entry.get("end")
                end = max(start, safe_float(raw_end)) if raw_end is not None else None
                kind = _coerce_chapter_kind(entry.get("kind"), entry.get("category"))
                label = str(entry.get("label") or entry.get("title") or entry.get("category") or f"Segment {idx}").strip()
                combined.append(ChapterSegment(id=str(entry.get("id") or f"sb-{idx}"), start_sec=start, end_sec=end, label=label, kind=kind))
    deduped: dict[tuple[float, float | None, str], ChapterSegment] = {}
    for seg in combined:
        key = (round(seg.start_sec, 2), round(seg.end_sec, 2) if seg.end_sec is not None else None, seg.kind)
        if key not in deduped:
            deduped[key] = seg
    return sorted(deduped.values(), key=lambda c: (c.start_sec, c.end_sec if c.end_sec is not None else c.start_sec))
