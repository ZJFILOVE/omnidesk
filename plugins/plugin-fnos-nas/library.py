"""L1 local NAS media library scanner and MediaSource implementation for plugin-fnos-nas."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnidesk_sdk import ChapterSegment, LyricLine, MediaItem, MediaKind, MediaSource

if __package__:
    from .safety import MAX_METADATA_BYTES, SUBTITLE_EXTENSIONS, MountValidationError, is_safe_scan_entry, read_text_bounded, validate_mount_root
    from .subtitles import extract_episode_info, is_bilingual_subtitle_file, parse_lrc, strip_subtitle_lang_suffix
else:
    from safety import MAX_METADATA_BYTES, SUBTITLE_EXTENSIONS, MountValidationError, is_safe_scan_entry, read_text_bounded, validate_mount_root
    from subtitles import extract_episode_info, is_bilingual_subtitle_file, parse_lrc, strip_subtitle_lang_suffix

CATEGORY_ORDER: tuple[str, ...] = ("电影", "剧集", "音乐", "书籍")
VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mkv", ".mp4", ".m4v", ".mov", ".avi", ".ts", ".m2ts", ".webm", ".wmv", ".iso"})
AUDIO_EXTENSIONS: frozenset[str] = frozenset({".flac", ".alac", ".m4a", ".wav", ".dsf", ".dff", ".ape", ".ogg", ".opus", ".mp3", ".aac", ".wv"})
BOOK_STRICT_EXTENSIONS: frozenset[str] = frozenset({".epub", ".mobi", ".azw3"})
BOOK_FOLDER_EXTENSIONS: frozenset[str] = frozenset({".epub", ".mobi", ".azw3", ".pdf", ".txt"})

_MOVIE_DIR_NAMES = frozenset({"movie", "movies", "film", "films", "电影"})
_TV_DIR_NAMES = frozenset({"tv", "series", "show", "shows", "剧集", "电视剧", "美剧", "anime", "番剧"})
_MUSIC_DIR_NAMES = frozenset({"music", "audio", "lossless", "音乐"})
_BOOK_DIR_NAMES = frozenset({"book", "books", "ebook", "ebooks", "书籍", "电子书"})

_IMDB_RE = re.compile(r"\b(tt\d{6,10})\b", re.IGNORECASE)
_TMDB_RE = re.compile(r"(?:\{|\[|\btmdb(?:id)?[=:-]\s*)(\d{2,10})(?:\}|\])?", re.IGNORECASE)
_TVDB_RE = re.compile(r"(?:\btvdb(?:id)?[=:-]\s*)(\d{2,10})", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<![0-9])(19\d{2}|20\d{2})(?![0-9])")
_QUALITY_RE = re.compile(r"\b(2160p|4k|1080p|720p|remux|bluray|blu-ray|web-dl|webrip|dovi|hdr10\+?|hdr|hevc|x265|x264)\b", re.IGNORECASE)
_SEASON_DIR_RE = re.compile(r"^(?:season|s)\s*0*(\d{1,2})$", re.IGNORECASE)
_MAX_SCAN_ITEMS = 20000


_MAX_COVER_BYTES = 8 * 1024 * 1024


def _detect_image_mime(raw: bytes) -> str:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _encode_image_data_url(raw: bytes) -> str:
    if not raw or len(raw) > _MAX_COVER_BYTES:
        return ""
    mime = _detect_image_mime(raw)
    if not mime:
        return ""
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _read_adjacent_cover(mount_root: Path, media: Path) -> str:
    roots = [media.parent]
    if _SEASON_DIR_RE.match(media.parent.name):
        roots.append(media.parent.parent)
    candidates = [
        media.with_name(media.stem + suffix)
        for suffix in (
            "-poster.jpg", "-poster.jpeg", "-poster.png", "-poster.webp",
            "-thumb.jpg", "-thumb.png", "-fanart.jpg",
            ".jpg", ".jpeg", ".png", ".webp",
        )
    ]
    candidates += [
        root / (name + ext)
        for root in roots
        for name in ("poster", "cover", "folder", "thumb", "fanart", "backdrop")
        for ext in (".jpg", ".jpeg", ".png", ".webp")
    ]
    for path in candidates:
        if not is_safe_scan_entry(mount_root, path) or path.is_symlink() or not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_COVER_BYTES + 1)
            encoded = _encode_image_data_url(raw)
            if encoded:
                return encoded
        except OSError:
            continue
    return ""


def _probe_media_for_cover(media: Path, ffprobe_bin: str) -> tuple[int | None, float | None]:
    """Return (attached_cover_stream_index, duration_sec) via ffprobe."""
    try:
        res = subprocess.run(
            [
                ffprobe_bin, "-v", "error",
                "-show_entries", "stream=index,codec_type:stream_tags=mimetype:stream_disposition=attached_pic",
                "-show_entries", "format=duration",
                "-of", "json", str(media),
            ],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if res.returncode != 0 or not res.stdout:
        return None, None
    try:
        parsed = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None, None
    cover_idx: int | None = None
    for stream in parsed.get("streams") or []:
        if not isinstance(stream, Mapping):
            continue
        disp = stream.get("disposition") or {}
        tags = stream.get("tags") or {}
        mimetype = str(tags.get("mimetype") or "").lower()
        is_attached = bool(disp.get("attached_pic") == 1) or (
            stream.get("codec_type") == "video" and mimetype.startswith("image/")
        )
        if is_attached and isinstance(stream.get("index"), int):
            cover_idx = stream["index"]
            break
    duration_sec: float | None = None
    raw_dur = (parsed.get("format") or {}).get("duration")
    if raw_dur is not None:
        try:
            dur = float(raw_dur)
            if dur > 0:
                duration_sec = dur
        except (TypeError, ValueError):
            pass
    return cover_idx, duration_sec


def _run_ffmpeg_cover(ffmpeg_bin: str, args: list[str]) -> bytes:
    try:
        res = subprocess.run(
            [ffmpeg_bin, "-v", "error", "-nostdin", *args],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return b""
    if res.returncode != 0 or not res.stdout.startswith(b"\xff\xd8\xff"):
        return b""
    return res.stdout


def _extract_embedded_or_frame_cover(media: Path) -> bytes:
    """Extract embedded artwork or a representative video frame as JPEG bytes."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        return b""
    ext = media.suffix.lower()
    out_args = [
        "-frames:v", "1",
        "-vf", "scale='min(600,iw)':-2,format=yuvj420p",
        "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "pipe:1",
    ]
    if ext in AUDIO_EXTENSIONS:
        return _run_ffmpeg_cover(ffmpeg_bin, ["-i", str(media), "-map", "0:v:0", *out_args])
    if ext in VIDEO_EXTENSIONS and ext != ".iso":
        ffprobe_bin = shutil.which("ffprobe")
        cover_idx, duration_sec = _probe_media_for_cover(media, ffprobe_bin) if ffprobe_bin else (None, None)
        if cover_idx is not None:
            raw = _run_ffmpeg_cover(ffmpeg_bin, ["-i", str(media), "-map", f"0:{cover_idx}", *out_args])
            if raw:
                return raw
        if duration_sec is not None and duration_sec > 0:
            seek_sec = min(max(duration_sec * 0.1, 5.0), 120.0) if duration_sec > 10.0 else duration_sec * 0.5
        else:
            seek_sec = 30.0
        raw = _run_ffmpeg_cover(ffmpeg_bin, ["-ss", f"{seek_sec:.3f}", "-i", str(media), "-map", "0:v:0", *out_args])
        if raw:
            return raw
        return _run_ffmpeg_cover(ffmpeg_bin, ["-i", str(media), "-map", "0:v:0", *out_args])
    return b""


def read_cover(snapshot: LibrarySnapshot, item_id: str, *, cache_dir: Path | None = None) -> str:
    """Read adjacent artwork or extract embedded/frame cover for an indexed item inside the configured mount."""
    record = snapshot.records_by_id.get(item_id)
    if not record or snapshot.mount_root is None:
        return ""
    media = Path(record["path"])
    adjacent = _read_adjacent_cover(snapshot.mount_root, media)
    if adjacent:
        return adjacent
    if not is_safe_scan_entry(snapshot.mount_root, media) or media.is_symlink() or not media.is_file():
        return ""
    cache_file: Path | None = None
    if cache_dir is not None:
        cache_key = hashlib.sha1(
            f"{record['path']}|{record.get('mtime', 0)}|{record.get('size_bytes', 0)}".encode("utf-8")
        ).hexdigest()
        cache_file = cache_dir / f"{cache_key}.jpg"
        if cache_file.is_file() and not cache_file.is_symlink():
            try:
                with cache_file.open("rb") as stream:
                    cached_url = _encode_image_data_url(stream.read(_MAX_COVER_BYTES + 1))
                if cached_url:
                    return cached_url
            except OSError:
                pass
    raw = _extract_embedded_or_frame_cover(media)
    encoded = _encode_image_data_url(raw)
    if encoded and cache_file is not None:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{cache_file.stem}.", suffix=".tmp", dir=str(cache_file.parent))
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            os.replace(tmp_name, cache_file)
        except OSError:
            pass
    return encoded


@dataclass
class LibrarySnapshot:
    """In-memory snapshot of scanned L1 NAS files."""

    mount_root: Path | None = None
    items: list[MediaItem] = field(default_factory=list)
    item_records: list[dict[str, Any]] = field(default_factory=list)
    items_by_id: dict[str, MediaItem] = field(default_factory=dict)
    records_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)
    roots: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    error_code: str = ""


def _split_csv_names(raw: str) -> set[str]:
    return {seg.strip().strip("/\\").lower() for seg in raw.split(",") if seg.strip()}


def _clean_title(stem: str) -> str:
    text = re.split(r"(?:^|[^a-z0-9])(?:S\d{1,2}[ ._-]*E\d{1,3}|\d{1,2}x\d{1,3})(?=$|[^a-z0-9])", stem, maxsplit=1, flags=re.IGNORECASE)[0]
    text = re.sub(r"[\[(](?:tmdb|imdb|tvdb)[^\]\)]*[\])]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\{(?:tmdb|imdb|tvdb)-[^}]*\}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:2160p|4k|1080p|720p|remux|bluray|blu-ray|web-dl|webrip|dovi|hdr10\+?|hdr|hevc|x265|x264|dts-hd|dts|aac|flac)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", re.sub(r"[._]+", " ", text)).strip(" -_")
    return text or stem


def _format_size(size_bytes: int) -> str:
    for unit, thresh in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size_bytes >= thresh:
            return f"{size_bytes / thresh:.1f} {unit}"
    return f"{size_bytes} B"


def _read_sidecar_meta(media_path: Path, mount_root: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    for cand in (media_path.with_suffix(".meta.json"), media_path.with_suffix(".nfo"), media_path.parent / "movie.nfo", media_path.parent / "tvshow.nfo"):
        if not cand.exists() or cand.is_symlink():
            continue
        try:
            raw = read_text_bounded(cand, MAX_METADATA_BYTES, root_guard=mount_root)
        except MountValidationError:
            continue
        if cand.suffix == ".json":
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                if isinstance(parsed.get("title"), str) and parsed["title"].strip():
                    meta.setdefault("title", parsed["title"].strip())
                if isinstance(parsed.get("imdb_id"), str):
                    meta.setdefault("imdb_id", parsed["imdb_id"].strip().lower())
                if parsed.get("tmdb_id") is not None:
                    meta.setdefault("tmdb_id", str(parsed["tmdb_id"]).strip())
                if isinstance(parsed.get("duration_sec"), (int, float)) and parsed["duration_sec"] > 0:
                    meta.setdefault("duration_sec", float(parsed["duration_sec"]))
            continue
        if m := (re.search(r"<imdbid>\s*(tt\d+)\s*</imdbid>", raw, re.IGNORECASE) or _IMDB_RE.search(raw)):
            meta.setdefault("imdb_id", m.group(1).lower())
        if m := re.search(r"<tmdbid>\s*(\d+)\s*</tmdbid>", raw, re.IGNORECASE):
            meta.setdefault("tmdb_id", m.group(1))
        if m := re.search(r"<durationinseconds>\s*(\d+)\s*</durationinseconds>", raw, re.IGNORECASE):
            meta.setdefault("duration_sec", float(m.group(1)))
        elif m := re.search(r"<runtime>\s*(\d+)\s*</runtime>", raw, re.IGNORECASE):
            meta.setdefault("duration_sec", float(int(m.group(1)) * 60))
        if m := re.search(r"<title>\s*([^<]+?)\s*</title>", raw, re.IGNORECASE):
            meta.setdefault("title", m.group(1).strip())
    return meta


def _classify_entry(rel_parts: tuple[str, ...], ext: str, season: int | None, cfg: Mapping[str, Any]) -> str | None:
    dir_tokens = [p.lower() for p in rel_parts[:-1]]
    book_dirs = _BOOK_DIR_NAMES | _split_csv_names(str(cfg.get("book_subdir", "") or ""))
    if ext in AUDIO_EXTENSIONS:
        return "音乐"
    if ext in BOOK_STRICT_EXTENSIONS or (ext in BOOK_FOLDER_EXTENSIONS and any(d in book_dirs for d in dir_tokens)):
        return "书籍"
    if ext in VIDEO_EXTENSIONS:
        if any(d in _TV_DIR_NAMES for d in dir_tokens) or season is not None or any(_SEASON_DIR_RE.match(d) for d in dir_tokens):
            return "剧集"
        return "电影"
    return None


def _extract_ids_and_year(rel_str: str) -> tuple[str, str, str, int | None]:
    imdb_m, tmdb_m, tvdb_m = _IMDB_RE.search(rel_str), _TMDB_RE.search(rel_str), _TVDB_RE.search(rel_str)
    years = [int(y) for y in _YEAR_RE.findall(rel_str) if 1900 <= int(y) <= 2099]
    return (imdb_m.group(1).lower() if imdb_m else "", tmdb_m.group(1) if tmdb_m else "", tvdb_m.group(1) if tvdb_m else "", years[-1] if years else None)


def _discover_roots(mount_root: Path, cat_counts: Mapping[str, int]) -> list[dict[str, Any]]:
    roots: list[dict[str, Any]] = [
        {"id": "mount_root", "name": mount_root.name or str(mount_root), "category": "root", "path": str(mount_root), "file_url": mount_root.as_uri(), "exists": True, "readable": True, "item_count": sum(cat_counts.values())}
    ]
    resolved_root = mount_root.resolve()
    for child in sorted(mount_root.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith(".") or not child.is_dir() or child.is_symlink() or not is_safe_scan_entry(resolved_root, child):
            continue
        lname = child.name.lower()
        cat = "电影" if lname in _MOVIE_DIR_NAMES else "剧集" if lname in _TV_DIR_NAMES else "音乐" if lname in _MUSIC_DIR_NAMES else "书籍" if lname in _BOOK_DIR_NAMES else "other"
        roots.append({"id": child.name, "name": child.name, "category": cat, "path": str(child.resolve()), "file_url": child.resolve().as_uri(), "exists": True, "readable": os.access(child, os.R_OK | os.X_OK)})
    return roots


def scan_library(mount_root_raw: str | None, cfg: Mapping[str, Any]) -> LibrarySnapshot:
    """Safely scan user-configured mount_root into real MediaItem records."""
    if not isinstance(mount_root_raw, str) or not mount_root_raw.strip():
        return LibrarySnapshot(error="尚未配置 NAS 挂载根目录 (mount_root)", error_code="not_configured")
    try:
        mount_root = validate_mount_root(mount_root_raw)
    except MountValidationError as exc:
        return LibrarySnapshot(error=str(exc), error_code=exc.error_code)

    resolved_root = mount_root.resolve()
    media_files: list[tuple[Path, os.stat_result]] = []
    subs_by_dir_stem: dict[tuple[Path, str], list[Path]] = {}
    lrc_by_dir_stem: dict[tuple[Path, str], Path] = {}

    for dirpath, dirnames, filenames in os.walk(mount_root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        cur_dir = Path(dirpath)
        if not is_safe_scan_entry(resolved_root, cur_dir):
            dirnames.clear()
            continue
        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            cand = cur_dir / fname
            if cand.is_symlink() or not is_safe_scan_entry(resolved_root, cand):
                continue
            ext = cand.suffix.lower()
            if ext in SUBTITLE_EXTENSIONS:
                base_stem = strip_subtitle_lang_suffix(cand.stem).lower()
                for target_dir in (cur_dir, cur_dir.parent):
                    subs_by_dir_stem.setdefault((target_dir, base_stem), []).append(cand)
                continue
            if ext == ".lrc":
                lrc_by_dir_stem[(cur_dir, cand.stem.lower())] = cand
                continue
            if ext in VIDEO_EXTENSIONS or ext in AUDIO_EXTENSIONS or ext in BOOK_FOLDER_EXTENSIONS:
                try:
                    media_files.append((cand, cand.stat()))
                except OSError:
                    continue
                if len(media_files) >= _MAX_SCAN_ITEMS:
                    dirnames.clear()
                    break
        if len(media_files) >= _MAX_SCAN_ITEMS:
            break

    items: list[MediaItem] = []
    records: list[dict[str, Any]] = []
    items_by_id: dict[str, MediaItem] = {}
    records_by_id: dict[str, dict[str, Any]] = {}
    cat_counts: dict[str, int] = {c: 0 for c in CATEGORY_ORDER}

    for path, st in media_files:
        rel_path = path.relative_to(mount_root)
        rel_str, ext = rel_path.as_posix(), path.suffix.lower()
        season, episode, ep_code = extract_episode_info(rel_str)
        category = _classify_entry(rel_path.parts, ext, season, cfg)
        if category is None:
            continue

        sidecar = _read_sidecar_meta(path, mount_root)
        imdb_id, tmdb_id, tvdb_id, year = _extract_ids_and_year(rel_str)
        imdb_id, tmdb_id = sidecar.get("imdb_id") or imdb_id, sidecar.get("tmdb_id") or tmdb_id
        duration_opt: float | None = sidecar.get("duration_sec")

        base_title = sidecar.get("title") or _clean_title(path.stem)
        if category == "剧集" and len(rel_path.parts) >= 2:
            parent_name = rel_path.parts[-3] if (_SEASON_DIR_RE.match(rel_path.parts[-2]) and len(rel_path.parts) >= 3) else rel_path.parts[-2]
            if parent_name.lower() not in _TV_DIR_NAMES:
                show_clean = _clean_title(parent_name)
                if show_clean and show_clean.lower() not in base_title.lower():
                    base_title = f"{show_clean} · {ep_code}" if ep_code else show_clean
                elif ep_code and ep_code.lower() not in base_title.lower():
                    base_title = f"{base_title} · {ep_code}"

        sub_paths = subs_by_dir_stem.get((path.parent, path.stem.lower()), [])
        lrc_path = lrc_by_dir_stem.get((path.parent, path.stem.lower()))
        has_bilingual = any(is_bilingual_subtitle_file(sp, mount_root) for sp in sub_paths) or (lrc_path is not None)
        if category == "音乐" and lrc_path is not None and duration_opt is None:
            try:
                lyrics = parse_lrc(read_text_bounded(lrc_path, MAX_METADATA_BYTES, root_guard=mount_root))
                if lyrics:
                    duration_opt = lyrics[-1].time_sec
            except MountValidationError:
                pass

        q_tokens = _QUALITY_RE.findall(path.stem)
        quality_label = " ".join(dict.fromkeys(t.upper() if len(t) <= 4 else t for t in q_tokens)) or ext.lstrip(".").upper()
        item_id = f"fnos:{hashlib.sha1(rel_str.encode('utf-8')).hexdigest()[:16]}"
        media_kind: MediaKind = "music" if category == "音乐" else "nas"
        play_url = path.resolve().as_uri()
        sub_parts = [category, *([ep_code] if ep_code else []), ext.lstrip(".").upper(), _format_size(st.st_size)]

        extra = {
            "category": category, "path": str(path.resolve()), "relative_path": rel_str, "file_url": play_url,
            "size_bytes": st.st_size, "size": st.st_size, "format": ext.lstrip("."), "mtime": st.st_mtime,
            "season": season, "episode": episode, "episode_code": ep_code, "year": year,
            "imdb_id": imdb_id, "tmdb_id": tmdb_id, "tvdb_id": tvdb_id,
            "subtitles": [str(sp.resolve()) for sp in sub_paths],
            "lrc_path": str(lrc_path.resolve()) if lrc_path else "",
            "duration_sec": duration_opt,
        }
        item = MediaItem(
            id=item_id, source_id="plugin-fnos-nas", title=base_title, subtitle=" · ".join(sub_parts),
            creator="fnOS NAS", kind=media_kind, source_badge=f"飞牛 NAS · {category}", play_url=play_url,
            quality_label=quality_label, duration_sec=float(duration_opt) if duration_opt is not None else 0.0,
            has_bilingual_sub=has_bilingual, aspect="1:1" if media_kind == "music" else "16:9", extra=extra,
        )
        rec = {**extra, "id": item_id, "title": base_title, "play_url": play_url, "quality_label": quality_label}
        items.append(item)
        records.append(rec)
        items_by_id[item_id] = item
        records_by_id[item_id] = rec
        cat_counts[category] += 1

    categories = [cat for cat in CATEGORY_ORDER if cat_counts[cat] > 0]
    return LibrarySnapshot(
        mount_root=mount_root, items=items, item_records=records, items_by_id=items_by_id,
        records_by_id=records_by_id, categories=categories, roots=_discover_roots(mount_root, cat_counts),
    )


class FnosMediaSource(MediaSource):
    """Domain 1 MediaSource exposing real files under user-configured fnOS mount_root."""

    source_id: str = "plugin-fnos-nas"
    display_name: str = "飞牛 fnOS NAS"

    def __init__(self, snapshot_provider: Callable[[bool], LibrarySnapshot]) -> None:
        self._snapshot_provider = snapshot_provider

    def categories(self) -> list[str]:
        return list(self._snapshot_provider(False).categories)

    async def fetch_items(
        self,
        category: str = "",
        query: str = "",
        page: int = 1,
        format: str = "",
        *,
        page_size: int = 50,
        force_refresh: bool = False,
    ) -> list[MediaItem]:
        snap = self._snapshot_provider(force_refresh)
        if not snap.items or page < 1:
            return []
        cat_filter, q, fmt = category.strip(), query.strip().lower(), format.strip().lstrip(".").lower()
        filtered: list[MediaItem] = []
        for item in snap.items:
            if cat_filter and item.extra.get("category") != cat_filter:
                continue
            if fmt and item.extra.get("format", "").lower() != fmt:
                continue
            if q:
                hay = " ".join([
                    item.title, item.subtitle, str(item.extra.get("relative_path", "")),
                    str(item.extra.get("episode_code", "")), str(item.extra.get("imdb_id", "")),
                    str(item.extra.get("tmdb_id", "")),
                ]).lower()
                if q not in hay:
                    continue
            filtered.append(item)
        size = max(1, min(int(page_size), 200))
        start = (int(page) - 1) * size
        return filtered[start : start + size]

    async def fetch_chapters(self, item_id: str) -> list[ChapterSegment]:
        snap = self._snapshot_provider(False)
        rec = snap.records_by_id.get(item_id)
        if not rec or snap.mount_root is None:
            return []
        ch_path = Path(rec["path"]).with_suffix(".chapters.json")
        if not ch_path.exists() or ch_path.is_symlink():
            return []
        try:
            raw = json.loads(read_text_bounded(ch_path, MAX_METADATA_BYTES, root_guard=snap.mount_root))
        except (MountValidationError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        return [ChapterSegment.model_validate({"id": e.get("id") or f"ch-{i+1}", **e}) for i, e in enumerate(raw) if isinstance(e, Mapping)]

    async def fetch_lyrics(self, item_id: str) -> list[LyricLine]:
        snap = self._snapshot_provider(False)
        rec = snap.records_by_id.get(item_id)
        if not rec or snap.mount_root is None or not rec.get("lrc_path"):
            return []
        try:
            raw = read_text_bounded(Path(rec["lrc_path"]), MAX_METADATA_BYTES, root_guard=snap.mount_root)
        except MountValidationError:
            return []
        return parse_lrc(raw)
