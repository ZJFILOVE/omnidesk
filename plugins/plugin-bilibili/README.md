# plugin-bilibili

OmniDesk v2 Stage 4 `workspace` plugin (`domain=media`, `provides=media.source.v1`).

## Features
- **MediaSource (`media.source.v1`)**: `categories()` (`recommend`, `dynamic`, `bangumi`, `live`, `search`), `fetch_items()`, `fetch_chapters()`, `fetch_lyrics()`.
- **Stream Resolution (`resolve_stream`)**: Automatic mode tries native web/Android playview first, then `yt-dlp`. BV-only inputs resolve their aid before Android fallback, including when web playurl fails. Explicit quality requests report `quality_unavailable` when unavailable; automatic mode uses the highest returned tier. The response reports the actual selected quality.
- **Account browsing**: `followings`, `dynamics`, and `space` expose followed UPs, mixed text/image/video dynamics, and UP spaces with cursor pagination. The media workbench provides cover grids and creator navigation. Bilibili CDN covers are upgraded to HTTPS for the desktop CSP.
- **Ad skipping**: The workbench can automatically skip community-marked ad/sponsor segments from `player.timeline.v1`; unmarked segments are not detected automatically.
- **QR Login & Vault Auth**: `qr_login_start`, `qr_login_poll`, `logout`, `status` with pure-Python QR SVG/PNG generation, `0600` `SecretVault` storage (or `web.cookies.v1`), and cookie masking/redaction.
- **Danmaku & Subtitles**: `fetch_danmaku` (XML), `fetch_subtitles` (JSON), optional `speech.asr.v1` / `llm.translate.v1` / `player.timeline.v1` integration with graceful degradation, and `entity.media.open` EventBus emission.

## Verification
```bash
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run pytest -q plugins/plugin-bilibili/tests
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run omnidesk-pm verify plugin-bilibili
```
