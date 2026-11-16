# Web 社区插件统一登录、Cookie 同步与反爬代理规范（Protocol v2 · Python）

本文档作为 [`PLUGIN_INTERFACE_PROTOCOL.md`](PLUGIN_INTERFACE_PROTOCOL.md) 的配套规范，规定 OmniDesk v2 中所有 Web / 社区与博客类 Python 插件（`plugin-zhihu`、`plugin-linuxdo`、`plugin-shuiyuan`、`plugin-v2ex`、`plugin-reddit`、`plugin-x`、`plugin-blog`、`plugin-feed-recipe` 等）如何通过 **`omnidesk_sdk` 公共会话底座 + `plugin-chrome-web` 托管 Chrome 桥接** 统一解决**登录鉴权、会话持久化、防风控抓取与多源降级**问题，彻底消除过去每个插件各自手写几百行 C++ `http_client.cpp` / `auth.cpp` 的重复劳动。

---

## 一、核心架构原则：`omnidesk_sdk.auth` + `plugin-chrome-web` 统一会话底座

1. **零重复轮子**：所有社区与阅读插件均以精简 **Python 模块（`150~300` 行）** 实现，统一继承/调用 `omnidesk_sdk.http.SafeHttpClient`（基于 `httpx.AsyncClient`）与 `omnidesk_sdk.auth.CookieSessionResolver`，严禁在插件内各自重造 HTTP 客户端或 Cookie 解析器。
2. **浏览器登录集中托管**：凡涉及扫码、滑块、Cloudflare Turnstile、jAccount SSO、OAuth、2FA 或前端 JS 动态签名的网站，**统一复用 `plugin-chrome-web`** 的隔离 Chrome `--app` 窗口与 MV3 扩展桥接（`127.0.0.1:8766`），不在各插件内塞 Playwright/Puppeteer 重型爬虫依赖。
3. **数据获取按风控等级分三层**：
   - **L0 免密 / 公开 API**：优先直连官方 JSON / RSS / Algolia 接口（零浏览器开销）。
   - **L1 Cookie + `httpx` 直连**：通过 `web.cookies.v1` 从 Chrome 同步真实登录 Cookie 并转存 `SecretVault`（`0600`），由 `SafeHttpClient` 高速拉取 JSON 或 HTML（配合 `selectolax` 解析，几十毫秒级）。
   - **L2 受控页面上下文代理（`web.extract.v1` / `web.fetch.v1`）**：对于校验 TLS 指纹（Cloudflare）、前端动态签名（如知乎 `x-zse-96`）或 GraphQL 动态请求头（如 X）的接口，委托 `plugin-chrome-web` 在真实 Chrome 上下文中完成请求或提取。
4. **安全与隔离红线**：
   - Cookie、Bearer Token、API Secret 一律存入 `SecretVault`（`ctx.set_secret` / `0600`），绝不写入普通配置 JSON、日志或错误字符串。
   - `SafeHttpClient` 在跨主机（Cross-Origin）重定向钩子中**强制自动剥离** `Cookie` 与 `Authorization` 头。

---

## 二、三层统一登录与抓取契约

### 2.1 第一层：交互式登录（`web.embed.v1`）

当用户未登录或接口返回 `401` / `403` / 登录墙 / Cloudflare 验证页时，插件向 `ctx.capabilities` 发起：

```json
{
  "capability": "web.embed.v1",
  "request": {
    "action": "open",
    "url": "https://<site-login-url>",
    "title": "<Site> 登录（完成后关闭窗口即可）",
    "auto_translate": false
  }
}
```

- 用户在真实 Chrome `--app` 窗口中完成扫码、密码、jAccount SSO、Google OAuth、2FA 或 Cloudflare 人机验证。
- `plugin-chrome-web` 随附的 MV3 扩展自动读取该域全部 Cookie（含 `HttpOnly`）并通过本地 `127.0.0.1:8766` Bearer Token 桥接同步至本地会话仓。

### 2.2 第二层：多源 Cookie 解析优先级（`CookieSessionResolver`）

`omnidesk_sdk.auth.CookieSessionResolver` 为所有社区插件统一按以下优先级解析会话凭证（由标准配置字段 `chrome_cookie_source` 控制）：

1. **`plugin-chrome-web` 实时桥接（`web.cookies.v1`）**：
   ```json
   {
     "action": "get_cookies",
     "url": "https://<target-host>/",
     "domain": "<root-domain>",
     "include_subdomains": true
   }
   ```
   同步成功后自动将关键 Cookie 快照写入 `SecretVault`（`cookie_header`），以便离线重启或暂未拉起 Chrome 时直接恢复。
2. **`SecretVault` 持久化凭据**：读取 `ctx.get_secret("cookie_header")` 或站点专用 Secret Key（如 `api_token`），支持用户在设置表单手动粘贴。
3. **Netscape / JSON Cookie 文件回退（`cookie_file_path`）**：使用标准库 `http.cookiejar.MozillaCookieJar` / JSON 解析器按 RFC 6265 域名、路径与过期时间严格过滤匹配。
4. **游客降级模式（Guest）**：未配置登录态时允许访问公开热榜/公开板块，并在界面清晰提示「需要登录以解锁关注流/搜索/完整评论」。

### 2.3 第三层：高风控 / 动态签名请求代理（`web.extract.v1` 与 `web.fetch.v1`）

针对仅靠 `httpx` + Cookie 仍会被拦截的场景，分两级回退：

1. **渲染后页面提取回退（`web.extract.v1`）**：
   - 请求：`{"url": "...", "mode": "auto", "use_cookies": true, "render_js": true, "timeout_ms": 20000}`。
2. **同源浏览器上下文代理（`web.fetch.v1`，由 `plugin-chrome-web` 提供）**：
   - 针对 **Cloudflare TLS 指纹校验（Linux.do）**、**前端动态签名（知乎 `x-zse-96` 搜索/推荐流）** 与 **前端 GraphQL 动态头（X / Twitter）**，由 MV3 扩展在受控页面上下文执行同源 `fetch()` 并返回 JSON/状态码：
   - 请求契约：
     ```json
     {
       "url": "https://www.zhihu.com/api/v4/search_v3?...",
       "method": "GET",
       "headers": { "Accept": "application/json" },
       "body": "",
       "credentials": "include",
       "timeout_ms": 15000
     }
     ```
   - **安全约束**：仅允许 `https://` 目标域名；禁止跨域转发内部凭据；单次响应体上限 `<= 8 MiB`；严禁在日志中打印请求 `Cookie` 与响应敏感字段。

---

## 三、各社区站点技术适配蓝图（`plugins/`）

| 插件 ID | 路径 | 引擎 / 协议 | 登录入口与关键凭证 | 常规抓取（快路径） | 反爬 / 高风控兜底（慢路径） |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plugin-zhihu` | `plugins/plugin-zhihu` | 知乎 Web + `api/v4` | `https://www.zhihu.com/signin`；同步 `z_c0`、`d_c0`、`_xsrf`、`__zse_ck` | `httpx` + Cookie 解析问答/专栏 HTML 内嵌 `<script id="js-initialData">` 及热榜 API（保留 `data-formula` LaTeX） | 搜索、个性化推荐流与部分评论分页走 `web.fetch.v1` / `web.extract.v1`（无需手写 `x-zse-96` 逆向） |
| `plugin-shuiyuan` | `plugins/plugin-shuiyuan` | Discourse JSON API | `https://shuiyuan.sjtu.edu.cn/login`（jAccount SSO）；同步 `_t`（HttpOnly）、`_forum_session` | `httpx` + Cookie 直连 `/latest.json`、`/top.json`、`/c/*.json`、`/t/{id}.json`、`/search.json` | 会话过期返回 403/登录页时一键呼起 `web.embed.v1` 重登 jAccount |
| `plugin-linuxdo` | `plugins/plugin-linuxdo` | Discourse JSON API + Cloudflare | `https://linux.do/login`；同步 `_t`、`cf_clearance` | 先试 `httpx` + Cookie 访问 `/latest.json` 与 `/t/{id}.json` | 被 Cloudflare TLS 指纹拦截 `403` 时自动切换 `web.fetch.v1` / `web.extract.v1` |
| `plugin-v2ex` | `plugins/plugin-v2ex` | V2EX API v2 (PAT) + Web HTML | 设置页填写 Personal Access Token（`SecretVault`）或同步 `A2` Cookie | PAT 走 `/api/v2/*` JSON；或 `httpx` + `A2` 解析节点/主题与回复 | 遇到 Google OAuth / Turnstile 时走 `web.embed.v1` |
| `plugin-reddit` | `plugins/plugin-reddit` | Reddit `.json` / OAuth API | 公开板块可免登；个人订阅通过登录窗同步会话或配置 OAuth | `httpx` 请求 `old.reddit.com` / `oauth.reddit.com` JSON 与递归评论树 | 触发限流/人机验证时回退 `web.fetch.v1` |
| `plugin-x` | `plugins/plugin-x` | X Web GraphQL | `https://x.com/i/flow/login`；同步 `auth_token`、`ct0` | 不手写易碎签名器 | 主路径直接通过 `web.fetch.v1`（Chrome 页面上下文）读取时间线、推文与回复树 |
| `plugin-blog` | `plugins/plugin-blog` | SPA / 动态博客 + 会员站 | 按博客域名呼起 `web.embed.v1` 登录并同步 Cookie（Substack / Medium 等） | 先试 `/feed.json`、RSS/Atom 或静态 HTML；SPA 站点优先 Chrome 渲染抽取 | 走 `web.extract.v1` / `web.fetch.v1` 抽取文章列表与正文（保留代码块/公式） |
| `plugin-feed-recipe` | `plugins/plugin-feed-recipe` | 20 行声明式 JSON 配方 | 复用 `CookieSessionResolver` | 内置 `hackernews.json` 等轻量配方 | 自动回退 `web.extract.v1` |

### 3.1 独立博客订阅与「博主式」浏览设计（`plugin-blog`）

1. **声明式管理面板**：通过 `ctx.register_workspace_tab(...)` 提供声明式「收藏博主 / + 添加博客」视图（无需任何 C++ 进程内组件），用户填入任意博客主页 URL、名称与标签即可持久化至 `ctx.data_dir / "blogs.json"`。
2. **博主即栏目**：每位收藏的博主动态生成对应的 `TextChannel`（如 `blog:lilianweng`），点进即可在 `TextWorkbench` 双语伴读，同时暴露 `blog.subscription.v1`（`list_blogs` / `add_blog` / `remove_blog` / `update_blog` / `fetch_blog_posts` / `refresh_all`）。

---

## 四、标准化 `plugin.json` 配置项与 `TextSource` 保真规则

### 4.1 统一 `config_schema` 字段名
`omnidesk_sdk.auth.STANDARD_WEB_AUTH_SCHEMA` 提供标准字段模板，各社区插件直接引用即可：
1. `chrome_cookie_source`（`select`: `["auto", "plugin-chrome-web", "vault", "file", "disabled"]`，默认 `"auto"`）
2. `cookie_header`（`secret`，默认 `""`，存放于 `0600` `SecretVault`）
3. `cookie_file_path`（`string`，默认 `""`）
4. `prefer_chrome_fetch`（`bool`）
5. `auto_translate`（`bool`）
6. `request_timeout_sec`（`int`，默认 `15`）

### 4.2 `TextSource` 内容保真规范
1. **两级栏目接口（`channels()` + `fetch_feed(channel_id, query, page)`）**：按站点动态暴露子栏目（如知乎 `hot/recommend/follow/collections`、Discourse `latest/top/unread`、Reddit `home/popular/r/*`）。
2. **数学公式保真**：将 LaTeX 源码（如知乎 `img[data-formula]`、Discourse `$...$` / `$$...$$`）映射为 `TextParagraph(kind="formula")`，前端使用 KaTeX 原生渲染且翻译管线自动跳过公式块。
3. **代码块与引用保真**：`<pre><code>` 映射为 `kind="code"`，`<blockquote>` 映射为 `kind="quote"`，完整保留原始换行与缩进。
4. **递归评论树与按需分页**：根评论与楼中楼回复映射为 `CommentNode`（含 `author`、`role_badge`、`score`、`created_at`、`replies`），长帖支持按页/游标增量加载。
