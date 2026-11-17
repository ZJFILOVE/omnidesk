# 知乎深度问答阅读器 (`plugin-zhihu`, Protocol v2)

OmniDesk v2 零 ABI 纯 Python 知乎插件，移植自 v1 C++ `omnidesk-plugin-zhihu`。

## 提供能力 (`provides`)

- `text.source.v1`: `channels`, `fetch_feed`, `fetch_article`, `search`, `login`, `sync_cookies`, `auth_status`, `companion`, `summarize`, `ask`
- `text.comments.v1`: `fetch_comments`, `fetch_child_comments`
- `zhihu.auth.v1`: `status`, `login`, `sync_cookies`, `set_cookie_header`, `logout`
- `zhihu.companion.v1`: `summarize`, `key_points`, `explain_formulas`, `ask`

## 认证与抓取架构

- 复用 `omnidesk_sdk.http.SafeHttpClient`（强制 HTTPS、SSRF/DNS 重绑定防护、8 MiB 响应上限、最多 5 次重定向、跨源自动剥离敏感头）。
- 会话解析优先级：`web.cookies.v1` / `web.embed.v1` -> `0600 SecretVault` (`cookie_header`) -> Netscape/JSON `cookie_file_path`（拒绝 `..` 与符号链接）-> 游客模式（仅热榜免登，个性化栏目 fail closed）。
- 遇到知乎签名校验 (`40362`/`x-zse-96`) 或 Cloudflare 验证时，自动通过 `web.fetch.v1` / `web.extract.v1` 降级代理。
- 保留 `img[data-formula]` / `span.ztext-math` LaTeX 公式、`pre>code` 代码块缩进与语言标记，以及 `auto_translate` 双语翻译（自动跳过代码块与公式）。

## 离线校验

```bash
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run pytest -q plugins/plugin-zhihu/tests
UV_CACHE_DIR=/tmp/omnidesk-uv-cache uv run omnidesk-pm verify plugins/plugin-zhihu
```
