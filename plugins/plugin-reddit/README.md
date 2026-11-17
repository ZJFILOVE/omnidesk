# Reddit Technical Subreddits (`plugin-reddit`)

OmniDesk v2 zero-ABI pure Python plugin providing Domain 2 (`TextWorkbench`) Reddit feeds, deep recursive comment trees, multi-source Chrome/Vault/OAuth authentication, and optional bilingual translation & AI companion reading.

## Capabilities

- `text.source.v1`: `channels`, `fetch_feed`, `fetch_article`, `search`, `list_subscriptions` / `list_subreddits`, `subscribe` / `add_subreddit`, `unsubscribe` / `remove_subreddit`, `sync_subscriptions`, `login` / `open_login`, `sync_cookies`, `auth_status`, `set_cookie_header` / `set_cookie`, `set_oauth_token`, `logout`, `companion` / `summarize` / `ask`
- `text.comments.v1`: `fetch_comments`, `fetch_more_comments` / `expand_more`
- `reddit.auth.v1`: `status`, `login`, `sync_cookies`, `set_cookie_header`, `set_oauth_token`, `logout`

## Verification

```bash
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run pytest -q plugins/plugin-reddit/tests
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run omnidesk-pm verify plugins/plugin-reddit
```
