# X (Twitter) Timeline & Threads (`plugin-x`)

OmniDesk v2 zero-ABI pure Python plugin (`schema_version = 2`, `kind = "workspace"`, `domain = "text"`).

## Capabilities

- **Provides**: `text.source.v1`, `text.comments.v1` + `TextSource` (`category = "x"`)
- **Consumes (graceful degradation)**: `web.embed.v1`, `web.cookies.v1`, `web.extract.v1`, `web.fetch.v1`, `llm.translate.v1`, `llm.chat.v1`

## Channels & Features

- **Channels**: `following` (`HomeLatestTimeline`), `for_you` (`HomeTimeline`), `lists` (`ListsManagementPageTimeline` + `ListLatestTweetsTimeline` + pinned `list:<id>`), `bookmarks` (`Bookmarks`).
- **Session Auth**: Resolves `auth_token` + `ct0` via `web.cookies.v1`, `0600` `SecretVault`, or safe Netscape/JSON `cookie_file_path`, and opens `https://x.com/i/flow/login` via `web.embed.v1`.
- **Fail-Closed Security**: Enforces `https://`, bounded timeouts, redirect cap with cross-origin credential stripping, 8 MiB response size cap, path safety, secret redaction, and explicit error codes (`auth_required`, `cloudflare_blocked`, `rate_limited`, `timeout`, `parse_error`, `not_found`, `service_offline`).
- **Thread & Reply Tree**: Unrolls OP self-threads (`[1/N]..[N/N]`), `NoteTweet`, X `Article`, quote tweets, code blocks, and LaTeX formulas; builds nested `CommentNode` trees with cursor pagination.
- **Bilingual Translation**: Uses `llm.translate.v1` / `llm.chat.v1` with SHA-256 memory + `0600` disk cache, skipping `code` and `formula` blocks.

## Verification

```bash
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run pytest -q plugins/plugin-x/tests
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run omnidesk-pm verify plugins/plugin-x
```
