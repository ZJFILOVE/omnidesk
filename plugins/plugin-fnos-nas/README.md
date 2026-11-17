# Feiniu fnOS NAS Connector (`plugin-fnos-nas`)

OmniDesk v2 `hybrid` media-domain plugin providing progressive L1/L2/L3 connectivity to user-configured fnOS NAS libraries without hardcoded paths, IPs, or credentials.

## Capabilities

- `nas.connection.v1`: `status`, `list_roots` (`list_mounts`), `test_mount` (`probe`), `scan_now`, and protected atomic `write_subtitle` (`safe_write_subtitle`) using `.<name>.omnidesk.tmp` + `os.replace`.
- `media.source.v1` / `MediaSource`: scans real files into `电影` / `剧集` / `音乐` / `书籍` categories (only returning categories actually present), returning real `MediaItem` entries with `file://` URLs, byte sizes, optional duration, and episode info.
- `media.local_match.v1`: matches local high-bitrate media (plus optional L3 Radarr/Sonarr v3) by `title`, `imdb_id`, `tmdb_id`, `season`, and `episode` with strict mismatch rejection.
- `media.clip_jump.v1`: searches local bilingual `.srt`/`.vtt`/`.ass` subtitles (plus optional L2 `media_service_url`) and optionally emits `entity.media.open` on `EntityBus`.
- `book.sync.v1`: local-first reading progress & vocabulary sync with LWW merge and optional L2 `companion_sync_url` sync (`not_configured` when unset).
