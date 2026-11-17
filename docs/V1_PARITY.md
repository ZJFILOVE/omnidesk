# OmniDesk v1 → v2 迁移对照

> 只读对照：v1 C++17 Borealis 源码位于 `legacy-cpp` 分支（工作副本 `/tmp/omnidesk-legacy`）；v1 插件仓位于 `/home/zhu/Projects/omnidesk-plugin-*`。本文记录 **2026-11-17** 当前工作区的实际状态，不把规划项写成已完成。

## 一、宿主能力对照

| v1 能力 | v2 实现 | 状态 | 说明 |
| :--- | :--- | :--- | :--- |
| Borealis + C++ 窗口壳 | `shell-tauri/`（Rust + Tauri v2） | 已替换 | 不再依赖 NanoVG/C++ 运行时 |
| 插件 `.so` ABI / 头文件指纹 | `plugin.json` + `plugin.py` + Pydantic 契约 | 已替换 | 零 ABI；热重载无需编译 |
| `control.sock` 单请求 IPC | `ControlServer` + 长连接 `bus.subscribe` / `capability.stream` | 已实现 | 支持 `config.*`、`domain.snapshot`、`plugin.load/unload` |
| 插件安装 / 更新 / 回滚 | `omnidesk-pm install/update/rollback` | 已实现 | staging 验证、`upstreams.lock.json`、5 快照保留、并发锁 |
| 插件启停 / 配置 / 密钥 | `omnidesk-pm enable/disable/config` | 已实现 | 0600 ConfigStore / SecretVault；不掩码回显 secret |
| MCP stdio | `omnidesk-pm mcp-serve` | 已实现 | 仅能力调用，不暴露任意 shell |
| 全键盘空间导航 | `ui/` 2D 焦点引擎 | 已实现 | `hjkl`/方向键、`f` 跳点、`Ctrl+K`、`F8`、`?`、`Ctrl+Tab` |
| 原生硬解 / 媒体键 | `shell-tauri/` mpv IPC + MPRIS2 | 进行中 | 已有 IPC/诊断骨架，libmpv 画布与 MPRIS 仍未完成 |
| Chrome `--app` 阅读窗 | `plugin-chrome-web` | 已迁移 | Cookies SQLite、CDP 提取/同源代理；cookie 读取已移出事件循环 |
| 文本阅读 / 公式 / 评论树 | `ui/` TextWorkbench + 社区插件 | 部分完成 | UI 与解析已就绪；实时源仍依赖迁移中的社区插件 |
| 任务中心 / 全文检索 | `core/JobCenter` / `core/SearchIndex` | 底座已存在 | 尚未完整暴露为插件能力或 RPC |
| 双模策展 / 反写 | `plugin-ai-curator` + `plugin-magpie-agent` | 已实现 | `daily_immutable` / `interactive_ephemeral` 隔离与 Apple Music 门禁 |

## 二、插件迁移状态

v1 外部仓共 22 个（不含仅有 marketplace 条目的 `journals`）。

### 已迁入主仓并通过离线测试

| v1 仓库 | v2 目录 | 主要能力 |
| :--- | :--- | :--- |
| `annas-archive` | `plugins/plugin-annas-archive` | `book.search.v1` |
| `arr-imdb` | `plugins/plugin-arr-imdb` | `media.source.v1` |
| `asr-funasr` | `plugins/plugin-asr-funasr` | `speech.asr.v1` |
| `bilibili` | `plugins/plugin-bilibili` | `media.source.v1` / `bilibili.media.v1` |
| `chrome-web` | `plugins/plugin-chrome-web` | `web.embed/cookies/extract/fetch.v1` |
| `ebook` | `plugins/plugin-ebook` | `book.source.v1` / `vocab.sm2.v1` |
| `fnos-nas` | `plugins/plugin-fnos-nas` | `nas.connection.v1` / `media.source.v1` / `media.clip_jump.v1` |
| `magpie-agent` | `plugins/plugin-magpie-agent` | `llm.router.v1` / `llm.chat.v1` / `ai.curation.v1` |
| `music` | `plugins/plugin-music` | `media.playlist.v1` / `media.source.v1` |
| `pdf-babeldoc` | `plugins/plugin-pdf-babeldoc` | `book.source.v1` / `book.bilingual_pdf.v1` |
| `sponsorblock` | `plugins/plugin-sponsorblock` | `player.timeline.v1` |
| `sub-hunter` | `plugins/plugin-sub-hunter` | `subtitle.fetch.v1` / `subtitle.sync.v1` |
| `youtube` | `plugins/plugin-youtube` | `media.source.v1` / `media.youtube.v1` |
| `zlib` | `plugins/plugin-zlib` | `book.search.v1` |
| `zhihu` | `plugins/plugin-zhihu` | `text.source.v1` / `text.comments.v1` / `zhihu.companion.v1` |
| `v2ex` | `plugins/plugin-v2ex` | `text.source.v1` / `text.comments.v1` |

### v2 原生新建

| v2 目录 | 主要能力 |
| :--- | :--- |
| `plugins/plugin-ai-curator` | `recommend.feed.v1` 与三域 Source |
| `plugins/plugin-demo` | 最小 Protocol v2 参考插件 |

### 仍在迁移 / 待完成

| v1 仓库 | 当前状态 | 目标能力 |
| :--- | :--- | :--- |
| `linuxdo` | 迁移中 | `text.source.v1` / `text.comments.v1` |
| `shuiyuan` | 迁移中 | `text.source.v1` / `text.comments.v1` |
| `reddit` | 迁移中 | `text.source.v1` / `text.comments.v1` |
| `x` | 迁移中 | `text.source.v1` / `text.comments.v1` |
| `blog` | 迁移中 | `text.source.v1` / `blog.subscription.v1` |
| `feed-recipe` | 迁移中 | `text.source.v1` / `feed.recipe.v1` |
| `journals` | 无 v1 源码，按 v2 规范新写 | `text.source.v1` / `text.citation.v1` |

## 三、刻意不保持的 v1 行为

1. **不伪造未登录/抓取失败内容**：v1 在未登录、Cloudflare、超时或解析失败时会构造提示文章/缓存摘要；v2 一律 fail closed，返回 `auth_required` / `cloudflare_blocked` / `timeout` / `parse_error` 等明确错误码。
2. **不返回空 Cookie 冒充成功**：`plugin-chrome-web` 在数据库被占用、解密失败或文件缺失时返回错误，不返回空数组。
3. **不跨源转发网关密钥**：`plugin-magpie-agent` 的重定向只允许同源保留 `Authorization`，跨源自动剥离；URL 仍限制为 loopback。

## 四、剩余差距

- `shell-tauri/`：libmpv OpenGL 画布、MPRIS2 真实播放控制与端到端媒体键。
- `omnidesk-pm`：插件独立 `python_dependencies` 的自动隔离安装。
- `omnidesk_sdk/`：统一 `SafeHttpClient` / `CookieSessionResolver`，收敛各插件重复 HTTP 代码。
- 社区插件：`linuxdo`、`shuiyuan`、`reddit`、`x`、`blog`、`feed-recipe`、`journals`。
- `core/JobCenter` 与 `SearchIndex` 尚未完整接入插件上下文/RPC。
- 安装收尾：重建 v2 安装包，并把 v1 的 rofi 启动路径替换为 v2。
