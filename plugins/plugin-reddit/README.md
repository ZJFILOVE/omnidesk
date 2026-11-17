# Reddit Technical Subreddits (`plugin-reddit`)

OmniDesk v2 zero-ABI pure Python plugin providing Domain 2 (`TextWorkbench`) Reddit feeds, deep recursive comment trees, multi-source Chrome/Vault/OAuth authentication, and optional bilingual translation & AI companion reading.

## 桌面阅读

- 与 Linux.do 一样，插件页默认打开应用内网页，不自动请求帖子接口；需要结构化内容时再切换「帖子列表」。
- 精选流的「打开原帖与讨论」、正文中的 Reddit / Linux.do 链接，以及社区帖子列表的条目，会定位到对应插件内的原链接。「返回来源」回到之前的工作台，保留精选列表和选中条目。
- Reddit 与 Linux.do 的内嵌网页使用各自的持久化会话；网页登录与插件的 Chrome Cookie 同步是独立的。网页仍可能要求登录或验证，可用「在 Chrome 打开原链接」继续访问。
- HTTP 404 / 410 保留原始错误码，但不再据此断言原帖已删除。

## Capabilities

- `text.source.v1`: `channels`, `fetch_feed`, `fetch_article`, `search`, `list_subscriptions` / `list_subreddits`, `subscribe` / `add_subreddit`, `unsubscribe` / `remove_subreddit`, `sync_subscriptions`, `login` / `open_login`, `sync_cookies`, `auth_status`, `set_cookie_header` / `set_cookie`, `set_oauth_token`, `logout`, `companion` / `summarize` / `ask`
- `text.comments.v1`: `fetch_comments`, `fetch_more_comments` / `expand_more`
- `reddit.auth.v1`: `status`, `login`, `sync_cookies`, `set_cookie_header`, `set_oauth_token`, `logout`

## Verification

```bash
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run pytest -q plugins/plugin-reddit/tests
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run omnidesk-pm verify plugins/plugin-reddit
```
