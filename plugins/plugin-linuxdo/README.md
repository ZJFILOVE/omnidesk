# Linux.do Discourse Client (`plugin-linuxdo`)

OmniDesk Protocol v2 (`schema_version = 2`) pure Python plugin providing `text.source.v1`, `text.comments.v1`, and `TextSource`.

## 三层登录与抓取方案

1. **交互登录（`web.embed.v1`）**：呼起受管 Chrome `--app` 窗口打开 `https://linux.do/login` 完成账号登录与 Cloudflare Turnstile 人机验证。
2. **多源 Cookie 解析（`AuthManager`）**：按 `chrome_cookie_source` 策略依次尝试 `plugin-chrome-web` (`web.cookies.v1`) → `0600 SecretVault` (`cookie_header`) → 本地 Netscape/JSON 文件 (`cookie_file_path`)，自动将 `cf_clearance` 与 `_t` 合并并写入 SecretVault 快照。
3. **快慢双路径抓取（`DiscourseClient`）**：
   - 快路径：HTTPS 直连（强校验仅 `https://`、8 MiB 响应上限、最多 5 次重定向且跨源自动剥离 `Cookie` / `Authorization`）。
   - 慢路径：当 Cloudflare 返回 `403` / `429` / `503` 或拦截页时，自动降级至 `web.fetch.v1` → `web.extract.v1`（支持 `<pre>` JSON 与 `data-preloaded` 提取）。
   - 严格 fail-closed：未登录、Cloudflare 拦截、超时、上游 4xx/5xx、畸形响应均返回明确错误码，绝不伪造文章、评论或登录成功。

## 能力与动作

- `text.source.v1`：`channels`、`fetch_feed`、`fetch_article`、`search`、`login`、`open_login`、`sync_cookies`、`auth_status`、`set_cookie`、`logout`、`list_categories`、`pin_category`、`unpin_category`、`summarize`、`companion`、`ask`。
- `text.comments.v1`：`fetch_comments`（按 `offset` / `limit` 增量拉取 `/t/{id}/posts.json` 并构造嵌套 `CommentNode` 评论树）。
- 双语对照翻译：启用 `auto_translate` 时通过 `llm.translate.v1` 翻译正文与标题，自动跳过代码块（`code`）与数学公式（`formula`）。

## 离线测试与验证

```bash
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run pytest -q plugins/plugin-linuxdo/tests
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run omnidesk-pm verify plugins/plugin-linuxdo
```
