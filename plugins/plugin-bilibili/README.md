# plugin-bilibili

OmniDesk v2 Stage 4 `workspace` plugin (`domain=media`, `provides=media.source.v1`).

## Features
- **MediaSource (`media.source.v1`)**: `categories()` (`recommend`, `dynamic`, `bangumi`, `live`, `search`), `fetch_items()`, `fetch_chapters()`, `fetch_lyrics()`.
- **Stream Resolution (`resolve_stream`)**: Resolves Bilibili DASH/progressive streams via `yt-dlp` (`ctx.exec_subprocess`, `shell=False`) or `/x/player/wbi/playurl`, strictly reporting `quality_unavailable` when 4K/VIP tiers are unavailable.
- **QR Login & Vault Auth**: `qr_login_start`, `qr_login_poll`, `logout`, `status` with pure-Python QR SVG/PNG generation, `0600` `SecretVault` storage (or `web.cookies.v1`), and cookie masking/redaction.
- **Danmaku & Subtitles**: `fetch_danmaku` (XML), `fetch_subtitles` (JSON), optional `speech.asr.v1` / `llm.translate.v1` / `player.timeline.v1` integration with graceful degradation, and `entity.media.open` EventBus emission.

## Verification
```bash
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run pytest -q plugins/plugin-bilibili/tests
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run omnidesk-pm verify plugin-bilibili
```
