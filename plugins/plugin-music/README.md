# plugin-music (OmniDesk v2 Stage 2)

`plugin-music` is a hybrid media plugin (`domain=media`) that provides:
- `media.charts.v1`: live Apple Music Marketing Tools most-played charts (default `cn`), exposed as verified `interactive_ephemeral` media candidates with `chart_rank`; requests fail closed instead of fabricating a chart.
- `media.playlist.v1`: Apple Music CN catalog preflight, fail-closed `ChatGPT Daily 5` (`p.YJXV7dEIeb3GzZX`) sync gated by committed dual-repo `PUBLISHED` transactions + QA verification + readback checks, and isolated `interactive_ephemeral` playlists / `libmpv` playback queueing.
- `media.source.v1`: `AppleMusicMediaSource` serving verified Daily 5 archives (`每日 5 首·最新` / `每日 5 首·往期` / `每日 5 首·全量` / `交互灵感歌单`) with `canonical_id`, `apple_music_song_id`, `business_date`, `curation_mode`, `listening_cue`, and `analysis_summary` in `extra`, plus bilingual lyrics parsed via `/home/zhu/Projects/qwen-translator` (`music_lyrics` / `music_adam`).

## Guardrails & Security
- Secrets (`apple_music_developer_token`, `apple_music_user_token`, `apple_music_cookie`) are `type: "secret"` in `plugin.json` and read exclusively via `PluginContext.get_secret(...)` without logging or leaking in errors/status.
- All unknown actions/parameters, missing credentials, and offline upstreams fail closed (`invalid_argument`, `not_configured`, `service_offline`, `needs_verified_bundle`, `protected_daily_playlist`, `readback_verification_failed`).
- State checkpoints (`~/.local/state/daily5/apple-music-playlist.json` or test-isolated XDG/data paths) and `fcntl.flock` lock files are written atomically with `0600` permissions outside curation git repositories.
- The `2026-11-06` 4-track historical archive anomaly is reported in `history_anomalies` without fabricating a 5th track.

## Adam playback and browsing

`media.adam.v1` connects to the existing v1 Adam engine at `https://127.0.0.1:20025`, checks listener ownership, and pins the local TLS leaf for API requests. `adam_engine_bin` and `adam_data_dir` select the engine and existing configuration when startup is needed.

- Account actions: `status`, `authenticate`, `challenge`, `resend`, `logout`.
- Browsing: recommendations, heavy rotation, recently played, library playlists, catalog search, albums, playlists, and artist collections. Playlist tracks paginate with catalog IDs and artwork preserved.
- `resolve_stream` creates an engine playback session with hires/lossless/AAC/Atmos preferences and returns the actual codec, sample rate, and bit depth. Availability depends on the track and account. HTTP 500/502/503 session failures receive one bounded retry; authentication errors and ambiguous timeouts do not.
- `sync_library` reads the Apple Music library and updates the engine cache through ingest. It does not mutate remote playlists. Daily 5 curation and bilingual lyrics remain available.

The workbench provides account controls, square cover grids, collection navigation, and the existing mpv player. Account passwords are submitted only to the local engine and cleared from the form after the request.
