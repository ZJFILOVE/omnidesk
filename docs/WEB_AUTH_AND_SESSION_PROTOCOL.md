# Web 社区插件统一登录、Cookie 同步与反爬代理规范

本文档作为 [`PLUGIN_INTERFACE_PROTOCOL.md`](PLUGIN_INTERFACE_PROTOCOL.md) 的配套设计规范，规定 OmniDesk 中所有 Web / 社区与博客类插件（`plugin-zhihu`、`plugin-linuxdo`、`plugin-shuiyuan`、`plugin-v2ex`、`plugin-reddit`、`plugin-x`、`plugin-blog`、`plugin-feed-recipe` 等）如何统一解决**登录鉴权、会话持久化、防风控抓取与多源降级**问题，避免为单个网站维护多套语言运行时或重复编写逆向签名器。

---

## 一、核心架构原则：C++17 `.so` + `plugin-chrome-web` 统一会话底座

1. **主体形态统一**：所有社区与阅读插件均以 **C++17 `.so`（ABI v1）** 交付；不按站点单独引入 Go/Node/Python 常驻爬虫服务（注：`plugin-music` 采用 Go 引擎属于 Apple Music FairPlay/DRM 历史专有层特例，不作为社区网站开发范式）。
2. **浏览器登录集中托管**：凡涉及扫码、滑块、Cloudflare Turnstile、jAccount SSO、OAuth、2FA 或前端 JS 动态签名的网站，**统一复用 `plugin-chrome-web`**（`../omnidesk-plugin-chrome-web`）的隔离 Chrome `--app` 窗口与 MV3 扩展桥接（`127.0.0.1:8766`），不在各插件内重新造嵌入式 WebView 或无头框架。
3. **数据获取按风控等级分层**：
   - **L0 免密 / 公开 API**：优先直连官方 JSON / RSS / Algolia 接口（零浏览器开销）。
   - **L1 Cookie + `libcurl` 直连**：通过 `web.cookies.v1` 从 Chrome 同步真实登录 Cookie 并转存 `SecretVault`（`0600`），由 C++ `libcurl` 高速拉取页面或 JSON（百毫秒级）。
   - **L2 受控页面上下文代理（`web.extract.v1` / `web.fetch.v1`）**：对于校验 TLS 指纹（Cloudflare）、前端动态签名（如知乎 `x-zse-96`）或 GraphQL 动态请求头（如 X）的接口，委托 `plugin-chrome-web` 在真实 Chrome 上下文中完成请求或提取。
4. **安全与隔离红线**：
   - Cookie、Bearer Token、API Secret 一律存入 `SecretVault`（`ctx.setSecret` / `0600`），绝不写入普通配置 JSON、日志或错误字符串。
   - 跨主机重定向时**必须剥离** `Cookie` 与 `Authorization` 头（参考 `omnidesk-plugin-feed-recipe/src/cookie_bridge.cpp`）。
   - 若社区插件不注册自定义 Borealis UI，或解析链路较重，推荐在 `plugin.json` 中设置 `"isolation": "subprocess"`。

---

## 二、既有登录范式参考与取舍

OmniDesk 现有三大登录范式分别适用于不同接口稳定度的平台，Web 社区插件优先采用 **YouTube 模式 + Apple Music 模式** 的组合，仅在官方接口无验证码且长期稳定时补充原生扫码：

| 参考插件 | 已有实现方式 | 适用场景与对社区插件的借鉴 |
| :--- | :--- | :--- |
| **`plugin-bilibili`** (`src/auth.cpp`) | 插件内 NanoVG 绘制二维码 + 轮询官网扫码接口 + `SecretVault` 持久化 | 仅适合接口公开、较少风控拦截的平台；对知乎等易触发 `40362`/滑块风控的站点不建议作主路径 |
| **`plugin-youtube`** (`src/auth.cpp`) | `SecretVault` Cookie + 一键调用 `web.cookies.v1` 同步 Chrome 会话 + Netscape `cookies.txt` 回退 | **标准会话读取模板**：所有社区插件都应照此实现多源优先级解析链与生成号刷新 |
| **`apple-music-online`** (`electron` 登录) | 首次由真实浏览器打开官网登录页（过 2FA / 验证码），完成后由后端接管会话 | **标准交互登录模板**：在 OmniDesk 中对应调用 `plugin-chrome-web` 的 `web.embed.v1` 打开 `--app` 登录窗 |

---

## 三、三层统一登录与抓取契约

### 3.1 第一层：交互式登录（`web.embed.v1`）

当用户未登录或接口返回 `401` / `403` / 登录墙 / Cloudflare 验证页时，插件向 `CapabilityRegistry` 发起：

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
- `plugin-chrome-web` 随附的 MV3 扩展自动读取该域全部 Cookie（含 `HttpOnly`）并通过本地 `127.0.0.1:8766` Bearer Token 桥接同步至本地 `CookieStore`。

### 3.2 第二层：多源 Cookie 解析优先级（`web.cookies.v1` + `SecretVault`）

参考 `omnidesk-plugin-youtube/src/auth.cpp` 与 `omnidesk-plugin-feed-recipe/src/cookie_bridge.cpp`，每个社区插件应提供统一 `AuthManager`，按以下优先级解析会话凭证（支持通过配置 `chrome_cookie_source` 控制）：

1. **`plugin-chrome-web` 实时桥接（`web.cookies.v1`）**：
   ```json
   {
     "action": "get_cookies",
     "url": "https://<target-host>/",
     "domain": "<root-domain>",
     "include_subdomains": true
   }
   ```
   同步成功后可将关键 Cookie 快照写入 `SecretVault`（如 `cookie_header`），以便离线重启或暂未拉起 Chrome 时直接恢复。
2. **`SecretVault` 持久化凭据**：通过 `ctx.getSecret("cookie_header")` 或站点专用 Secret Key（如 `api_token`）读取，支持用户在插件设置表单手动粘贴。
3. **Netscape / JSON Cookie 文件回退（`cookie_file_path`）**：按 RFC 6265 域名、路径与过期时间严格过滤匹配后拼装 `Cookie` 头。
4. **游客降级模式（Guest）**：未配置登录态时允许访问公开热榜/公开板块，并在 UI 清晰提示「需要登录以解锁关注流/搜索/完整评论」。

### 3.3 第三层：高风控 / 动态签名请求代理（`web.extract.v1` 与 `web.fetch.v1`）

针对仅靠 `libcurl` + Cookie 仍会被拦截的场景，分两级回退：

1. **现成页面提取回退（`web.extract.v1`）**：
   - 适用于服务端渲染或需要执行轻度 JS 挑战才能拿到 HTML/DOM 的页面：
   - 请求：`{"url": "...", "mode": "auto", "use_cookies": true, "render_js": true, "timeout_ms": 20000}`。
2. **规划扩展：同源浏览器上下文代理（`web.fetch.v1`，由 `plugin-chrome-web` 提供）**：
   - 针对 **Cloudflare TLS 指纹校验（Linux.do）**、**前端动态签名（知乎 `x-zse-96` 搜索/推荐流）** 与 **前端 GraphQL 动态头（X / Twitter）**，由 `plugin-chrome-web` 的 MV3 扩展在受控标签页/页面上下文执行同源 `fetch()` 并返回 JSON/状态码：
   - 建议请求契约：
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
   - 建议响应契约：
     ```json
     {
       "status": 200,
       "url": "https://www.zhihu.com/api/v4/search_v3?...",
       "headers": { "content-type": "application/json" },
       "body": "{...}"
     }
     ```
   - **安全约束**：仅允许 `https://` 目标域名；禁止跨域转发内部凭据；限制单次响应体上限（如 `<= 8 MiB`，对齐宿主 Worker 帧限制）；严禁在日志中打印请求 `Cookie` 与响应敏感字段。

---

## 四、各社区站点技术适配蓝图

| 插件 ID | 目录 | 引擎 / 协议 | 登录入口与关键凭证 | 常规抓取（快路径） | 反爬 / 高风控兜底（慢路径） |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `plugin-zhihu` | `../omnidesk-plugin-zhihu` | 知乎 Web + `api/v4` | `https://www.zhihu.com/signin`；同步 `z_c0`、`d_c0`、`_xsrf`、`__zse_ck` | `libcurl` + Cookie 解析问答/专栏 HTML 内嵌 `<script id="js-initialData">` 及热榜 API（保留 `data-formula` LaTeX） | 搜索、个性化推荐流与部分评论分页走 `web.fetch.v1` / `web.extract.v1`（规避手写 `x-zse-96` 逆向） |
| `plugin-shuiyuan` | `../omnidesk-plugin-shuiyuan` | Discourse JSON API | `https://shuiyuan.sjtu.edu.cn/login`（jAccount SSO 扫码/密码/2FA）；同步 `_t`（HttpOnly）、`_forum_session` | `libcurl` + Cookie 直连 `/latest.json`、`/top.json`、`/c/*.json`、`/t/{id}.json`、`/search.json` | 会话过期返回 403/登录页时一键呼起 `web.embed.v1` 重登 jAccount |
| `plugin-linuxdo` | `../omnidesk-plugin-linuxdo` | Discourse JSON API + Cloudflare | `https://linux.do/login`；同步 `_t`、`cf_clearance` | 先试 `libcurl` + Cookie；若无盾或白名单 IP 可直接走 `/latest.json` 与 `/t/{id}.json` | 被 Cloudflare TLS 指纹拦截 `403` 时自动切换 `web.fetch.v1` / `web.extract.v1` |
| `plugin-v2ex` | `../omnidesk-plugin-v2ex` | V2EX API v2 (PAT) + Web HTML | 方式 A：设置页填写 Personal Access Token（`SecretVault`）；方式 B：`https://www.v2ex.com/signin` 同步 `A2` Cookie | PAT 走 `/api/v2/*` JSON；或 `libcurl` + `A2` 抓节点/主题 HTML 与全部回复（补充 `plugin-feed-recipe` 内置 `v2ex.json`） | 遇到 Google OAuth / Turnstile 或登录校验时走 `web.embed.v1` |
| `plugin-reddit` | `../omnidesk-plugin-reddit` | Reddit `.json` / OAuth API | 公开板块可免登；个人订阅通过 `https://www.reddit.com/login` 同步 `reddit_session`/`token_v2` 或配置 OAuth App | `libcurl` 请求 `old.reddit.com` / `oauth.reddit.com` JSON 与深层递归评论树（注意设置合规 `User-Agent`） | 触发限流/人机验证时回退 `web.fetch.v1` |
| `plugin-x` | `../omnidesk-plugin-x` | X Web GraphQL | `https://x.com/i/flow/login`（过密码/2FA/Arkose）；同步 `auth_token`、`ct0` | 不建议手写 `x-client-transaction-id` 与轮换 `queryId` | 主路径直接通过 `web.fetch.v1`（Chrome 页面上下文 `fetch` / 网络响应拦截）读取时间线、推文与回复树 |
| `plugin-blog` | `../omnidesk-plugin-blog` | SPA / 动态渲染博客 + 会员站 | 按博客域名呼起 `web.embed.v1` 登录并同步 Cookie（Substack / Medium / 独立会员站） | 先试 `/feed.json`、`/sitemap.xml`、RSS/Atom 或静态 HTML；缺省优先 Chrome 渲染抽取 | 优先走 `web.extract.v1` / `web.fetch.v1` 渲染后抽取文章列表与正文（保留代码块/公式） |
| `plugin-feed-recipe` | `../omnidesk-plugin-feed-recipe` | JSON / HTML / XML / Lua 声明式配方 | 已内置 `CookieBridge`（`web.cookies.v1` + 文件 + `SecretVault`） | 已内置 `hackernews.json`、`v2ex.json` 等 20 行配方 | 已内置 `web.extract.v1` JS 挑战回退 |

### 4.1 独立博客订阅与「博主式」浏览设计（`plugin-blog`）

针对不提供标准 RSS、采用 SPA 动态渲染（Next.js / React / Vue）、带 Cloudflare 防护或需要会员登录（Substack / Medium）的个人/工程博客，`plugin-blog` 采用 **方案 5（Chrome 会话与渲染抓取优先）+ 原生订阅管理工作台（`WorkspaceTabSpec`）**：

1. **UI 与隔离边界**：因需提供「添加博客 / 收藏博主 / 点进单博主文章流」原生 Borealis 页面（`WorkspaceTabSpec`），`plugin-blog` 的主 `.so` 声明 `"isolation": "in_process"`（遵守 `PLUGIN_SPEC.md`：整包 `subprocess` 模式会拒绝 UI 注册；若后续需隔离重解析任务，可通过 `PluginContext::registerWorker` 挂独立 worker）。
2. **用户自助添加与博主式视图**：
   - 用户在页面输入任意博客主页 / 归档页 URL、博主名与标签即可入库（持久化至 `ctx.dataDir()/blogs.json`），无需手写 JSON 配方。
   - 界面按 **「收藏的博客/博主卡片 → 点进查看该博主文章列表 → 在 `TextWorkbench` 双语伴读」** 组织，体验与 B 站 UP 主 / YouTube 订阅频道一致。
   - 暴露 `blog.subscription.v1` 能力（`list_blogs` / `add_blog` / `remove_blog` / `update_blog` / `fetch_blog_posts` / `refresh_all`），方便跨插件或 AI 伴读联动。
3. **四级抽取优先级**：`web.extract.v1`（Chrome 渲染 DOM / Readability，默认优先以兼容 SPA）⇄ `feed.json` / `sitemap.xml` / RSS ⇄ `web.fetch.v1`（需交互会话的会员站）⇄ `web.embed.v1`（弹窗登录或浏览器直读）。

---

## 五、标准化 `plugin.json` 配置项约定

新建的 Web 社区插件应在 `plugin.json` 的 `config_schema` 中统一使用一致的字段命名，以便插件市场自动生成表单并复用同一套排查习惯：

1. `chrome_cookie_source`（`select`: `["auto", "plugin-chrome-web", "vault", "file", "disabled"]`，默认 `"auto"`）：登录态来源优先级策略。
2. `cookie_header`（`secret`，默认 `""`）：手动/同步保存的 `Cookie` Header（加密存放于 `SecretVault` `0600`）。
3. `cookie_file_path`（`string`，默认 `""`）：可选 Netscape `cookies.txt` 或 Chrome JSON 导出文件路径。
4. `prefer_chrome_fetch`（`bool`，站点需要时声明，默认 `true` / `false`）：当直连遭遇风控或签名校验时自动改走 `plugin-chrome-web`。
5. `auto_translate`（`bool`，默认 `false` 或按站点语言设定）：消费 `llm.translate.v1` / `llm.chat.v1` 做双语对照并跳过代码块/公式。
6. `request_timeout_sec`（`int`，默认 `15`）：单次网络请求超时时间。

---

## 六、文本工作台数据映射统一建议

所有社区插件通过 `ITextSource`（`text.source.v1`）向 `TextWorkbench` 供数时，应遵守以下内容保真规则：

1. **两级栏目接口（`channels()` + `fetchFeedInChannel()`）**：
   - 各站点插件应在 `ITextSource::channels()` 返回各自的一级子栏目 `TextChannel`（支持登录后动态解锁或按用户订阅变化）：
     - `plugin-zhihu`：`hot`（热榜）、`recommend`（推荐，需登录）、`follow`（关注，需登录）、`collections`（收藏夹，需登录）
     - `plugin-blog`：每个收藏的博主本身生成一个 `TextChannel`（如 `blog:lilianweng`），同时在专属 `WorkspaceTab` 提供「+ 添加博客 / 博主卡片」管理
     - `plugin-linuxdo` / `plugin-shuiyuan`：`latest`（最新）、`top`（热门）、`unread`（未读，需登录）及站点置顶/动态版面分类
     - `plugin-v2ex`：`hot`（最热）、`all`（全部）、`tech`（技术）、`creative`（创意）、`play`（好玩）、`apple`（Apple）、`jobs`（酷工作）及关注节点
     - `plugin-reddit`：`home`、`popular` 与用户配置的各个 `r/<subreddit>`
     - `plugin-x`：`following`、`for_you`、`lists`、`bookmarks`
   - 各插件的自定义订阅列表（如置顶版面、Subreddit、收藏博主）统一持久化在 `ctx.dataDir()/subscriptions.json`，并在 `config_schema` 提供初始默认列表。
2. **数学公式保真**：将 LaTeX 源码（如知乎 `img[data-formula]`、Discourse `$...$` / `$$...$$`）直接映射为 `TextParagraph{kind = "formula"}`，严禁转为纯图片或丢弃源码，确保 AI 翻译流程自动跳过公式块。
3. **代码块与引用保真**：`<pre><code>` 映射为 `kind = "code"`，引用块映射为 `kind = "quote"`，保留原始换行与缩进。
4. **评论树统一映射**：将根评论与楼中楼回复递归构造为 `CommentNode`（含 `author`、`role_badge` 如 `OP` / `答主` / `版主`、`score`、`created_at`、`replies[]`），供 `CommentTreeView` 折叠渲染。
5. **按需增量分页**：正文及首屏高赞评论一次加载，长贴评论（如知乎上千条评论、V2EX 多页回复）支持按页或游标按需拉取，避免单次请求阻塞或触及风控阈值。
