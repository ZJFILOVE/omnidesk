# V2EX Tech Community (`plugin-v2ex`)

OmniDesk v2 zero-ABI pure Python plugin (`schema_version = 2`), ported from the v1 C++ plugin while preserving all capabilities, actions, config schema keys, fail-closed auth/error semantics, reply-tree parsing, and rich content fidelity.

## Capabilities

- **`text.source.v1`** & Domain `TextSource`:
  - `channels` / `list_channels`: built-in tabs (`hot`, `all`, `tech`, `creative`, `play`, `apple`, `jobs`, `qna`), `following` (`requires_auth=true`), and persisted subscribed nodes (`node:<slug>`).
  - `fetch_feed` / `search`: multi-page tab/node/following feeds (`page >= 1`), API v2 PAT fast path, HTML parsing, public API v1 fallback on Cloudflare block.
  - `fetch_article`: API v2 PAT + paginated `/replies` or HTML topic + paginated `?p=N` replies + supplements (`附言`), preserving `heading`, `paragraph`, `quote`, `code` (with language), `formula` (`$$...$$`, `data-formula`, `math/tex`, `.katex-display`), and optional `llm.translate.v1` / `llm.chat.v1` bilingual translation (skipping code & formula blocks).
  - `list_nodes` / `subscribe_node` / `unsubscribe_node` / `sync_nodes`: persisted to `<plugin_data_dir>/subscriptions.json`.
  - `status` / `auth_status`, `auth_sync_chrome` / `sync_chrome`, `auth_open_login` / `open_login`, `auth_set_pat` / `set_pat`, `auth_set_cookie` / `set_cookie`, `auth_logout` / `logout`.
  - `summarize` / `companion` / `ask`: optional `llm.chat.v1` thread summary & Q&A.
- **`text.comments.v1`**:
  - `fetch_comments`: paginated reply fetching & nested `CommentNode` tree assembly via explicit `#floor` and `@mention` references with OP badges and depth clamping.

## Tests & Verification

```bash
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run pytest -q plugins/plugin-v2ex/tests
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run omnidesk-pm verify plugins/plugin-v2ex
```
