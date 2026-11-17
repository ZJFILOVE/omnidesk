"""MediaSource implementation backed by real Daily 5 archives and qwen-translator lyrics."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from collections.abc import Awaitable, Sequence
from pathlib import Path
from typing import Any

from omnidesk_sdk import LyricLine, MediaItem, MediaSource

if __package__:
    from .adam import CATEGORIES as ADAM_CATEGORIES, resolver as adam_resolver, artwork
    from .state_store import (
        atomic_write_json,
        is_valid_iso_date,
        read_private_json,
        resolve_data_root,
        resolve_interactive_store_path,
        resolve_repo_dir,
    )
    from .qwen_bridge import (
        QwenModules,
        fetch_adam_lrc,
        load_qwen_modules,
        parse_lrc_to_lyric_lines,
        read_cached_lyrics,
    )
else:
    from adam import CATEGORIES as ADAM_CATEGORIES, resolver as adam_resolver, artwork
    from state_store import (
        atomic_write_json,
        is_valid_iso_date,
        read_private_json,
        resolve_data_root,
        resolve_interactive_store_path,
        resolve_repo_dir,
    )
    from qwen_bridge import (
        QwenModules,
        fetch_adam_lrc,
        load_qwen_modules,
        parse_lrc_to_lyric_lines,
        read_cached_lyrics,
    )

_CATEGORIES = ["每日 5 首·最新", "每日 5 首·往期", "每日 5 首·全量", "交互灵感歌单"]
_PAGE_SIZE = 50
_NOTES_RE = re.compile(r"<!--\s*AUTO:END\s*-->\s*(.*)$", re.DOTALL)
_AM_PREFIX_RE = re.compile(r"^(?:am-[a-z]{2}-|ix-am-)(.+)$")
_URL_SONG_RE = re.compile(r"https?://music\.apple\.com/[a-z]{2}/song/(?:[^/]+/)?([0-9]+)")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

TranslateLines = Callable[[Sequence[str], str], Awaitable[tuple[list[str] | None, str]]]


def normalize_song_id(raw_id: str) -> str:
    cleaned = (raw_id or "").strip()
    if cleaned.isdigit():
        return cleaned
    if m_pref := _AM_PREFIX_RE.match(cleaned):
        tail = m_pref.group(1)
        if tail.isdigit():
            return tail
        if "-" in tail and tail.rsplit("-", 1)[-1].isdigit():
            return tail.rsplit("-", 1)[-1]
    if m_url := _URL_SONG_RE.search(cleaned):
        return m_url.group(1)
    return cleaned


def _extract_handwritten_notes(md_path: Path) -> str:
    if not md_path.is_file() or md_path.is_symlink():
        return ""
    try:
        text = md_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _NOTES_RE.search(text)
    return match.group(1).strip() if match else ""


class AppleMusicMediaSource(MediaSource):
    """Domain 1 MediaSource serving verified Daily 5 archives and bilingual lyrics."""

    source_id: str = "plugin-music"
    display_name: str = "Apple Music · Daily 5 & 双语歌词"

    def __init__(
        self,
        *,
        get_config: Callable[[], Mapping[str, Any]],
        get_data_dir: Callable[[], Path],
        get_qwen: Callable[[], QwenModules] | None = None,
        translate_lines: TranslateLines | None = None,
        fetch_metadata: Callable[[list[str]], Awaitable[list[dict[str, Any]]]] | None = None,
        adam_feed: Callable[..., Awaitable[list[MediaItem]]] | None = None,
    ) -> None:
        self._get_config = get_config
        self._get_data_dir = get_data_dir
        self._get_qwen = get_qwen
        self._translate_lines = translate_lines
        self._fetch_metadata = fetch_metadata
        self._adam_feed = adam_feed
        self.last_translation_error = ""
        self.last_translated_count = 0

    def categories(self) -> list[str]:
        return list(_CATEGORIES) + (ADAM_CATEGORIES if self._adam_feed else [])

    def _qwen(self) -> QwenModules:
        if self._get_qwen is not None:
            return self._get_qwen()
        cfg = self._get_config()
        return load_qwen_modules(str(cfg.get("qwen_translator_dir") or ""))

    def _lyrics_dir(self) -> Path:
        return self._get_data_dir().expanduser().resolve() / "lyrics"

    def _translation_cache_path(self) -> Path:
        return self._get_data_dir().expanduser().resolve() / "lyrics_translations.json"

    def _load_translation_cache(self) -> dict[str, dict[str, str]]:
        path = self._translation_cache_path()
        if not path.is_file() or path.is_symlink():
            return {}
        try:
            raw = read_private_json(path)
        except Exception:
            return {}
        songs = raw.get("songs")
        if not isinstance(songs, Mapping):
            return {}
        clean: dict[str, dict[str, str]] = {}
        for song_id, entries in songs.items():
            if not isinstance(entries, Mapping):
                continue
            clean[str(song_id)] = {
                str(key): str(value)
                for key, value in entries.items()
                if isinstance(value, str) and value.strip()
            }
        return clean

    @staticmethod
    def _text_key(text: str) -> str:
        return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()

    def _apply_translation_cache(self, song_id: str, lines: list[LyricLine]) -> None:
        cached = self._load_translation_cache().get(song_id, {})
        if not cached:
            return
        for line in lines:
            if not line.translation:
                line.translation = cached.get(self._text_key(line.original), "")

    def _save_translation_cache(self, song_id: str, lines: list[LyricLine]) -> None:
        entries = {
            self._text_key(line.original): line.translation
            for line in lines
            if line.translation and line.original.strip()
        }
        if not entries:
            return
        cache = self._load_translation_cache()
        cache.setdefault(song_id, {}).update(entries)
        try:
            atomic_write_json(
                self._translation_cache_path(),
                {"version": 1, "songs": cache},
            )
        except Exception:
            return

    @staticmethod
    def _needs_translation(text: str) -> bool:
        stripped = text.strip()
        if not stripped or not _LATIN_RE.search(stripped):
            return False
        return not _CJK_RE.search(stripped)

    async def _finalize_lyrics(
        self, song_id: str, lines: list[LyricLine], title: str
    ) -> list[LyricLine]:
        self.last_translation_error = ""
        self.last_translated_count = 0
        if not lines:
            return lines
        self._apply_translation_cache(song_id, lines)
        cfg = self._get_config()
        if cfg.get("auto_translate_lyrics", True) is not True or self._translate_lines is None:
            return lines
        missing = [
            index
            for index, line in enumerate(lines)
            if not line.translation and self._needs_translation(line.original)
        ]
        if not missing:
            return lines
        texts = [lines[index].original for index in missing]
        try:
            translated, error = await self._translate_lines(texts, title)
        except Exception as exc:
            self.last_translation_error = f"translation_failed:{type(exc).__name__}"
            return lines
        if error:
            self.last_translation_error = error
            return lines
        if translated is None or len(translated) != len(missing):
            self.last_translation_error = "llm.translate.v1_invalid_response"
            return lines
        for index, value in zip(missing, translated):
            lines[index].translation = value.strip()
        self.last_translated_count = len(missing)
        self._save_translation_cache(song_id, lines)
        return lines

    def _catalog_cache(self) -> dict[str, dict[str, Any]]:
        path = self._get_data_dir().expanduser().resolve() / "catalog_cache.json"
        if not path.is_file() or path.is_symlink():
            return {}
        try:
            raw = read_private_json(path)
        except Exception:
            return {}
        tracks = raw.get("tracks")
        return dict(tracks) if isinstance(tracks, Mapping) else {}

    def _metadata_from_track(
        self, track: Mapping[str, Any], song_id: str
    ) -> tuple[str, str, str, float] | None:
        am = track.get("apple_music") if isinstance(track.get("apple_music"), Mapping) else {}
        ids = {
            str(am.get("song_id") or ""),
            str(track.get("apple_music_song_id") or ""),
            str(track.get("song_id") or ""),
            normalize_song_id(str(track.get("id") or "")),
            normalize_song_id(str(track.get("play_url") or "")),
        }
        if song_id not in ids:
            return None
        title = str(track.get("title") or "").strip()
        artist = str(track.get("artist") or track.get("creator") or "").strip()
        if not title and not artist:
            return None
        album = str(track.get("album") or track.get("subtitle") or "").strip()
        try:
            duration = float(track.get("duration_sec") or am.get("duration_sec") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        return title, artist, album, duration

    def _load_play_history(self) -> list[dict[str, Any]]:
        path = self._get_data_dir().expanduser().resolve() / "play_history.json"
        if not path.is_file() or path.is_symlink():
            return []
        try:
            raw = read_private_json(path)
        except Exception:
            return []
        tracks = raw.get("tracks") if isinstance(raw, Mapping) else None
        return [dict(item) for item in tracks if isinstance(item, Mapping)] if isinstance(tracks, list) else []

    def _load_interactive_tracks(self) -> list[dict[str, Any]]:
        store_path = resolve_interactive_store_path(self._get_data_dir())
        sessions_dir = store_path.parent / "sessions"
        if not sessions_dir.is_dir():
            return []
        tracks: list[dict[str, Any]] = []
        for path in sorted(sessions_dir.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            items = raw.get("media_items") if isinstance(raw, Mapping) else None
            if isinstance(items, list):
                tracks.extend(dict(item) for item in items if isinstance(item, Mapping))
        return tracks

    def _resolve_track_metadata(self, song_id: str) -> tuple[str, str, str, float]:
        for _, tracks in self._load_daily_records():
            for track in tracks:
                found = self._metadata_from_track(track, song_id)
                if found is not None:
                    return found
        for track in self._load_play_history():
            found = self._metadata_from_track(track, song_id)
            if found is not None:
                return found
        for track in self._load_interactive_tracks():
            found = self._metadata_from_track(track, song_id)
            if found is not None:
                return found
        cached = self._catalog_cache().get(song_id, {})
        title = str(cached.get("title") or "")
        artist = str(cached.get("artistName") or cached.get("artist") or "")
        album = str(cached.get("albumName") or cached.get("album") or "")
        try:
            duration = float(cached.get("duration_sec") or cached.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        return title, artist, album, duration

    async def _fetch_online_lyrics(
        self, title: str, artist: str, album: str, duration_sec: float
    ) -> list[LyricLine]:
        if not title or not artist or not 1 <= duration_sec <= 3600:
            return []
        qwen = self._qwen()
        lyrics_mod = qwen.module("music_lyrics")
        repo_cls = getattr(lyrics_mod, "LyricsRepository", None) if lyrics_mod is not None else None
        if not callable(repo_cls):
            return []
        cfg = self._get_config()
        if cfg.get("enable_lrclib", True) is not True:
            return []
        try:
            repo = repo_cls(cache_dir=self._lyrics_dir())
            result = await asyncio.wait_for(
                repo.get(
                    {
                        "title": title,
                        "artist": artist,
                        "album": album,
                        "duration": duration_sec,
                    },
                    {
                        "music_lyrics_online": True,
                        "music_translation_enabled": False,
                        "text_provider": "none",
                    },
                ),
                timeout=20.0,
            )
        except Exception:
            return []
        raw_lines = result.get("lines") if isinstance(result, Mapping) else None
        if not isinstance(raw_lines, list):
            return []
        return [
            LyricLine(
                time_sec=(
                    float(line["start"])
                    if isinstance(line.get("start"), (int, float)) and line["start"] >= 0
                    else 0.0
                ),
                original=str(line.get("text") or ""),
                translation=str(line.get("translation") or ""),
            )
            for line in raw_lines
            if isinstance(line, dict) and str(line.get("text") or "").strip()
        ]

    def _has_cached_lyrics(self, song_id: str) -> bool:
        ldir = self._lyrics_dir()
        return any((ldir / f"{song_id}{ext}").is_file() for ext in (".bilingual.lrc", ".lrc", ".txt"))

    def _load_daily_records(self) -> list[tuple[str, list[dict[str, Any]]]]:
        cfg = self._get_config()
        data_dir = self._get_data_dir()
        music_repo = resolve_repo_dir(str(cfg.get("music_daily_dir") or ""), data_dir, "music-daily")
        vault_repo = resolve_repo_dir(str(cfg.get("obsidian_vault_dir") or ""), data_dir, "obsidian-vault")

        by_date: dict[str, list[dict[str, Any]]] = {}
        manifest_paths: list[Path] = []
        if music_repo is not None and (music_repo / "recommendations").is_dir():
            manifest_paths.extend(sorted((music_repo / "recommendations").glob("*/*/*.json")))
        elif vault_repo is not None and (vault_repo / "Music" / "Daily" / "manifests").is_dir():
            manifest_paths.extend(sorted((vault_repo / "Music" / "Daily" / "manifests").glob("*.json")))

        for path in manifest_paths:
            if path.is_symlink() or not path.is_file() or not is_valid_iso_date(path.stem):
                continue
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(doc, Mapping) or doc.get("date") != path.stem:
                continue
            tracks = doc.get("tracks")
            if not isinstance(tracks, list):
                continue
            notes = (
                _extract_handwritten_notes(vault_repo / "Music" / "Daily" / f"{path.stem}.md")
                if vault_repo is not None
                else ""
            )
            cleaned: list[dict[str, Any]] = []
            for item in tracks:
                if not isinstance(item, Mapping):
                    continue
                am = item.get("apple_music") if isinstance(item.get("apple_music"), Mapping) else {}
                sid = str(am.get("song_id") or "")
                title = str(item.get("title") or "").strip()
                if not sid.isdigit() or not title:
                    continue
                cleaned.append({**dict(item), "_notes": notes})
            if cleaned:
                cleaned.sort(key=lambda t: int(t.get("position") or 0))
                by_date[path.stem] = cleaned

        # Merge rehydrated local daily cache if present and not already loaded from git repos.
        rehydrated_root = resolve_data_root(data_dir) / "curation" / "daily"
        if rehydrated_root.is_dir():
            for day_dir in sorted(rehydrated_root.iterdir()):
                if not day_dir.is_dir() or not is_valid_iso_date(day_dir.name) or day_dir.name in by_date:
                    continue
                music_file = day_dir / "music.json"
                if not music_file.is_file() or music_file.is_symlink():
                    continue
                try:
                    raw = json.loads(music_file.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    continue
                items = raw.get("tracks") if isinstance(raw, Mapping) else (raw if isinstance(raw, list) else [])
                if isinstance(items, list) and items:
                    by_date[day_dir.name] = [dict(x) for x in items if isinstance(x, Mapping)]

        return sorted(by_date.items(), key=lambda pair: pair[0], reverse=True)

    def _build_daily_media_item(
        self, day: str, raw: Mapping[str, Any], catalog_cache: Mapping[str, Mapping[str, Any]]
    ) -> MediaItem | None:
        am = raw.get("apple_music") if isinstance(raw.get("apple_music"), Mapping) else {}
        song_id = str(am.get("song_id") or raw.get("song_id") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not song_id or not title:
            return None
        artist = str(raw.get("artist") or "").strip()
        album = str(raw.get("album") or "").strip()
        storefront = str(am.get("storefront") or "cn").strip().lower() or "cn"
        play_url = str(am.get("url") or f"https://music.apple.com/{storefront}/song/{song_id}")
        canonical_id = str(raw.get("canonical_id") or (f"{artist}::{title}" if artist else title))
        analysis_summary = str(raw.get("analysis_summary") or "")
        listening_cue = str(raw.get("listening_cue") or "")
        position = raw.get("position")
        cached = catalog_cache.get(song_id, {})
        subtitle = f"{artist} · {album}" if artist and album else (artist or album)
        return MediaItem(
            id=f"am-{storefront}-{song_id}",
            source_id=self.source_id,
            title=title,
            subtitle=subtitle,
            creator=artist,
            kind="music",
            source_badge=f"每日 5 首 · {day}",
            cover_url=str(cached.get("cover_url") or raw.get("cover_url") or am.get("cover_url") or ""),
            play_url=play_url,
            quality_label=str(cached.get("quality_label") or "Apple Music CN · ALAC"),
            duration_sec=float(cached.get("duration_sec") or 0.0),
            has_bilingual_sub=self._has_cached_lyrics(song_id),
            aspect="1:1",
            summary=analysis_summary,
            listening_cue=listening_cue,
            curation_mode="daily_immutable",
            business_date=day,
            extra={
                "canonical_id": canonical_id,
                "apple_music": {"song_id": song_id, "storefront": storefront, "url": play_url},
                "apple_music_song_id": song_id,
                "song_id": song_id,
                "album": album,
                "position": position,
                "storefront": storefront,
                "business_date": day,
                "curation_mode": "daily_immutable",
                "listening_cue": listening_cue,
                "analysis_summary": analysis_summary,
                "handwritten_notes": str(raw.get("_notes") or ""),
            },
        )

    def _load_interactive_items(self, catalog_cache: Mapping[str, Mapping[str, Any]]) -> list[MediaItem]:
        store_path = resolve_interactive_store_path(self._get_data_dir())
        try:
            store = read_private_json(store_path)
        except Exception:
            return []
        items: list[MediaItem] = []
        for playlist in store.get("playlists", []) if isinstance(store.get("playlists"), list) else []:
            if not isinstance(playlist, Mapping):
                continue
            pname = str(playlist.get("name") or "OmniDesk AI 灵感电台")
            for sid in playlist.get("track_ids", []) if isinstance(playlist.get("track_ids"), list) else []:
                song_id = str(sid).strip()
                if not song_id.isdigit():
                    continue
                cached = catalog_cache.get(song_id, {})
                title = str(cached.get("title") or "").strip()
                if not title:
                    continue
                artist = str(cached.get("artist") or "")
                album = str(cached.get("album") or "")
                items.append(
                    MediaItem(
                        id=f"ix-am-{song_id}",
                        source_id=self.source_id,
                        title=title,
                        subtitle=f"{artist} · {album}" if artist and album else artist,
                        creator=artist,
                        kind="music",
                        source_badge=pname,
                        cover_url=str(cached.get("cover_url") or ""),
                        play_url=str(cached.get("url") or f"https://music.apple.com/cn/song/{song_id}"),
                        quality_label=str(cached.get("quality_label") or "Apple Music CN · ALAC"),
                        duration_sec=float(cached.get("duration_sec") or 0.0),
                        has_bilingual_sub=self._has_cached_lyrics(song_id),
                        aspect="1:1",
                        curation_mode="interactive_ephemeral",
                        extra={
                            "canonical_id": f"{artist}::{title}" if artist else title,
                            "apple_music_song_id": song_id,
                            "song_id": song_id,
                            "playlist_name": pname,
                            "curation_mode": "interactive_ephemeral",
                            "storefront": "cn",
                        },
                    )
                )
        return items

    async def fetch_items(
        self,
        category: str = "",
        query: str = "",
        page: int = 1,
        *,
        force_refresh: bool = False,
    ) -> list[MediaItem]:
        if page < 1:
            return []
        cat = (category or "").strip()
        if cat in ADAM_CATEGORIES and self._adam_feed:
            return await self._adam_feed(cat, query, page)
        catalog_cache = self._catalog_cache()
        items: list[MediaItem] = []

        if cat in {"交互灵感歌单", "interactive", "interactive_ephemeral"}:
            items = self._load_interactive_items(catalog_cache)
        else:
            daily_days = self._load_daily_records()
            if not daily_days:
                return []
            latest_date = daily_days[0][0]
            if cat in {"", "每日 5 首·最新", "latest", "daily_latest"}:
                selected_days = [daily_days[0]]
            elif cat in {"每日 5 首·往期", "past", "archive", "daily_past"}:
                selected_days = [(d, ts) for d, ts in daily_days if d != latest_date]
            elif cat in {"每日 5 首·全量", "all", "music", "daily_all"}:
                selected_days = daily_days
            elif is_valid_iso_date(cat):
                selected_days = [(d, ts) for d, ts in daily_days if d == cat]
            else:
                return []

            for day, tracks in selected_days:
                for tr in tracks:
                    if built := self._build_daily_media_item(day, tr, catalog_cache):
                        items.append(built)

        q = (query or "").strip().casefold()
        if q:
            items = [
                item
                for item in items
                if q in " ".join(
                    [
                        item.id,
                        item.title,
                        item.subtitle,
                        item.creator,
                        item.business_date or "",
                        str(item.extra.get("canonical_id") or ""),
                        str(item.extra.get("apple_music_song_id") or ""),
                        str(item.extra.get("analysis_summary") or ""),
                        str(item.extra.get("listening_cue") or ""),
                    ]
                ).casefold()
            ]
        start = (page - 1) * _PAGE_SIZE
        page_items = items[start : start + _PAGE_SIZE]
        missing = [normalize_song_id(item.id) for item in page_items if not item.cover_url]
        if missing and self._fetch_metadata:
            try:
                metadata = await asyncio.wait_for(self._fetch_metadata(missing), timeout=12)
                by_id = {str(track.get("id")): track for track in metadata}
                for item in page_items:
                    track = by_id.get(normalize_song_id(item.id), {})
                    item.cover_url = item.cover_url or str(track.get("cover_url") or "")
                    item.duration_sec = item.duration_sec or float(track.get("duration_sec") or 0)
            except Exception:
                # A metadata outage must not hide the user's local collection.
                pass
        for item in page_items:
            song_id = str(item.extra.get("apple_music_song_id") or normalize_song_id(item.id))
            item.extra["stream_resolver"] = adam_resolver(song_id)
            item.cover_url = artwork(item.cover_url)
        return page_items

    async def fetch_lyrics(self, item_id: str) -> list[LyricLine]:
        song_id = normalize_song_id(item_id)
        if not song_id:
            return []
        qwen = self._qwen()
        if not qwen.available:
            return []

        title, artist, album, duration_sec = self._resolve_track_metadata(song_id)

        lines = read_cached_lyrics(
            song_id=song_id,
            title=title,
            artist=artist,
            album=album,
            duration_sec=duration_sec,
            lyrics_cache_dir=self._lyrics_dir(),
            qwen=qwen,
        )
        if not lines and song_id.isdigit():
            raw_lrc = fetch_adam_lrc(song_id, str(self._get_config().get("storefront") or "cn"), qwen)
            if raw_lrc:
                lines = parse_lrc_to_lyric_lines(raw_lrc, qwen)
        if not lines:
            lines = await self._fetch_online_lyrics(title, artist, album, duration_sec)
        return await self._finalize_lyrics(song_id, lines, title)
