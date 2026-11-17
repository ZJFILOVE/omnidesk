# YouTube Media & Bilingual Subtitles (`plugin-youtube`)

OmniDesk v2 Stage 4 `workspace` plugin (`domain = "media"`, `provides = ["media.source.v1"]`).

## Provided Interfaces

- **Domain Source (`MediaSource` / `media.source.v1`)**:
  - `categories()` -> `subscriptions`, `search`, `channels`, `watch_later` (plus `playlists` when `playlist_ids` is configured).
  - `fetch_items(category, query, page)` -> maps YouTube Data API v3 or `yt-dlp` entries to `MediaItem` (`11`-char `video_id`, creator, `hqdefault.jpg` cover, duration, HTTPS watch URL). Fails closed (`[]`) on error or empty input.
  - `fetch_chapters(item_id)` -> merges native video chapters with optional `player.timeline.v1` SponsorBlock segments.
  - `fetch_lyrics(item_id)` -> maps fetched/cached subtitles into `LyricLine` cues.
- **Capability (`media.youtube.v1`)**:
  - `status`, `categories`, `fetch_items`, `search`
  - `resolve_stream`: invokes `yt-dlp -J` via `PluginContext.exec_subprocess`, filters DRM/storyboard/non-HTTPS formats, selects video/audio for configured `quality`, and maps missing binary (`not_configured`), timeouts (`timeout`), HTTP 403/members-only/DRM (`permission_denied`).
  - `list_subtitles` / `fetch_subtitles`: prioritizes official subtitles over automatic captions, parses VTT & SRT, optionally translates via `llm.translate.v1` with atomic cache under `cache_dir`, and optionally falls back to `speech.asr.v1` when no subtitles exist.
  - `list_subscriptions` / `sync_subscriptions`: requires SecretVault `oauth_token` or user-provided `channel_ids` / `playlist_ids` (returns `not_configured` otherwise).
  - `fetch_chapters`: merges native chapters and optional `player.timeline.v1`.

## Security Guarantees

- Enforces `https://` for external YouTube media/API/subtitle URLs.
- Rejects path traversal (`..`), relative escapes, and symlinks on `cache_dir` and `ytdlp_bin`.
- Stores `api_key` and `oauth_token` exclusively in `0600` `SecretVault`; redacts tokens, API keys, query credentials, and cookies from logs, errors, and returned playback headers.
- Never bypasses DRM, paywalls, or membership gates.
