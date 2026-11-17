# OmniDesk v1 → v2 迁移对照

> 只读对照：v1 C++17 Borealis 源码位于 `legacy-cpp` 分支；v1 插件仓位于 `/home/zhu/Projects/omnidesk-plugin-*`。本文记录 **2026-11-17** 当前工作区的实际状态，不把规划项写成已完成。

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
| 原生硬解 / 媒体键 | `shell-tauri/` libmpv 嵌入 + MPRIS2 | 已实现 | 真实 libmpv 出帧测试仍为 `#[ignore]`，需本机 libmpv/ffmpeg/音频设备手动跑 |
| Chrome `--app` 阅读窗 | `plugin-chrome-web` | 已迁移 | Cookies SQLite、CDP 提取/同源代理；cookie 读取已移出事件循环 |
| 文本阅读 / 公式 / 评论树 | `ui/` TextWorkbench + 社区插件 | 已实现 | 通过 `domain.invoke` 拉取 `fetch_feed` / `fetch_article`，离线时回退示例数据 |
| 书本 / PDF 工作台 | `ui/` BookWorkbench + book 插件 | 已实现 | 书架/书页接 `domain.invoke`，生词与 SM-2 写回 `vocab.sm2.v1` |
| 任务中心 / 全文检索 | `core/JobCenter` / `core/SearchIndex` | 已实现 | `job.submit/list/get/cancel` 与 `search.query/upsert/delete` RPC；`ctx.jobs` / `ctx.search` 注入插件上下文 |
| 双模策展 / 反写 | `plugin-ai-curator` + `plugin-magpie-agent` | 已实现 | `daily_immutable` / `interactive_ephemeral` 隔离与 Apple Music 门禁 |

## 二、插件迁移状态

v1 外部仓共 22 个（不含仅有 marketplace 条目的 `journals`）；v2 主仓当前共 **25 个插件目录**，全部通过 `omnidesk-pm verify`。

### 已迁入主仓并通过离线测试

| v1 仓库 | v2 目录 | 主要能力 |
| :--- | :--- | :--- |
| `annas-archive` | `plugins/plugin-annas-archive` | `book.search.v1` |
| `arr-imdb` | `plugins/plugin-arr-imdb` | `media.source.v1` / `media.local_match.v1` |
| `asr-funasr` | `plugins/plugin-asr-funasr` | `speech.asr.v1` |
| `bilibili` | `plugins/plugin-bilibili` | `media.source.v1` / `bilibili.media.v1` |
| `chrome-web` | `plugins/plugin-chrome-web` | `web.embed/cookies/extract/fetch.v1` |
| `ebook` | `plugins/plugin-ebook` | `book.source.v1` / `vocab.sm2.v1` |
| `fnos-nas` | `plugins/plugin-fnos-nas` | `nas.connection.v1` / `media.source.v1` / `media.clip_jump.v1` |
| `magpie-agent` | `plugins/plugin-magpie-agent` | `llm.router.v1` / `llm.chat.v1` / `ai.agent.v1` / `ai.curation.v1` |
| `music` | `plugins/plugin-music` | `media.playlist.v1` / `media.source.v1` |
| `pdf-babeldoc` | `plugins/plugin-pdf-babeldoc` | `book.source.v1` / `book.bilingual_pdf.v1` |
| `sponsorblock` | `plugins/plugin-sponsorblock` | `player.timeline.v1` |
| `sub-hunter` | `plugins/plugin-sub-hunter` | `subtitle.fetch.v1` / `subtitle.sync.v1` / `subtitle.translate.v1` |
| `youtube` | `plugins/plugin-youtube` | `media.source.v1` / `media.youtube.v1` |
| `zlib` | `plugins/plugin-zlib` | `book.search.v1` |
| `zhihu` | `plugins/plugin-zhihu` | `text.source.v1` / `text.comments.v1` / `zhihu.companion.v1` |
| `v2ex` | `plugins/plugin-v2ex` | `text.source.v1` / `text.comments.v1` |
| `linuxdo` | `plugins/plugin-linuxdo` | `text.source.v1` / `text.comments.v1` |
| `shuiyuan` | `plugins/plugin-shuiyuan` | `text.source.v1` / `text.comments.v1` |
| `reddit` | `plugins/plugin-reddit` | `text.source.v1` / `text.comments.v1` / `reddit.auth.v1` |
| `x` | `plugins/plugin-x` | `text.source.v1` / `text.comments.v1` |
| `blog` | `plugins/plugin-blog` | `text.source.v1` / `blog.subscription.v1` |
| `feed-recipe` | `plugins/plugin-feed-recipe` | `text.source.v1` / `text.comments.v1` / `feed.recipe.v1` |
| `journals`（v1 无源码） | `plugins/plugin-journals` | `text.source.v1` / `text.citation.v1` |

### v2 原生新建

| v2 目录 | 主要能力 |
| :--- | :--- |
| `plugins/plugin-ai-curator` | `recommend.feed.v1` 与三域 Source |
| `plugins/plugin-demo` | 最小 Protocol v2 参考插件 |

## 三、刻意不保持的 v1 行为

1. **不伪造未登录/抓取失败内容**：v1 在未登录、Cloudflare、超时或解析失败时会构造提示文章/缓存摘要；v2 一律 fail closed，返回 `auth_required` / `cloudflare_blocked` / `timeout` / `parse_error` 等明确错误码。
2. **不返回空 Cookie 冒充成功**：`plugin-chrome-web` 在数据库被占用、解密失败或文件缺失时返回错误，不返回空数组。
3. **不跨源转发网关密钥**：`plugin-magpie-agent` 的重定向只允许同源保留 `Authorization`，跨源自动剥离；URL 仍限制为 loopback。

## 四、Agent 能力暴露现状

MCP 侧已有统一入口：`omnidesk_list_plugins`、`omnidesk_list_capabilities`、`omnidesk_call_capability`、`omnidesk_emit_event`、`omnidesk_domain_snapshot`，以及新加的 `omnidesk_collect_context`、`omnidesk_recommend`、`omnidesk_download_book`。任何已注册能力也可经 `omnidesk-pm rpc <capability> '<payload>'` 或 MCP `omnidesk_call_capability` 调用。

| 上下文 / 动作 | 能力 ID | 当前状态 |
| :--- | :--- | :--- |
| 网页正文提取 | `web.extract.v1` / `web.fetch.v1` | 已注册，可调用 |
| 网页嵌入 / Cookie | `web.embed.v1` / `web.cookies.v1` | 已注册，可调用 |
| 电子书正文 / 生词 | `book.source.v1` / `vocab.sm2.v1` | 已注册，可调用 |
| PDF 双栏对照 | `book.bilingual_pdf.v1` | 已注册，可调用 |
| 字幕搜索 / 下载 / 同步 / 翻译 | `subtitle.fetch.v1` / `subtitle.sync.v1` / `subtitle.translate.v1` | 已注册，可调用；翻译复用 `llm.translate.v1`，支持双语 / 仅译文 / 仅原文三档输出、1-8 路并发与断点缓存 |
| 语音转写 | `speech.asr.v1` | 已注册，可调用 |
| 推荐流 / 策展 | `recommend.feed.v1` / `ai.curation.v1` | 已注册，可调用 |
| 书籍下载 / 送 NAS / 打开阅读器 | `book.search.v1` 的 `download` / `send_to_nas` / `open_in_babeldoc` / `open_in_ebook` 动作 | 已注册，可调用 |
| 统一上下文收集 | `context.collect.v1` | 已注册；`ai.agent.v1` 传 `context_refs` 时自动收集 |

**当前边界**：`context.collect.v1` 只接受白名单能力（web/book/subtitle/speech/text/media），会剥离 secret/token/cookie 等敏感字段，并限制总字符数与单项超时；多个上下文按最多 6 路并发收集，单项可传 `provider_plugin_id` 指定来源。`ai.agent.v1` 传 `context_refs` 时会自动收集；不传时仍只消费调用方给的 `context` / `selection`。UI 的 Text/Book/Media 伴读栏会按当前条目自动附带 `context_refs`。`subtitle.translate.v1` 目前直接解析 `.srt` / `.vtt`；`.ass` / `.ssa` 需先经 ffmpeg 转为 SRT。

推荐与下载已经有独立 MCP 工具，同时也保留通用 `omnidesk_call_capability`。所有副作用动作（下载、送 NAS、播放列表写入、Apple Music 写回、生词/同步写入等）都要求 `confirm=true` 且提供 `reason`，并通过 0600 JSONL 审计日志记录；日志位于 `<XDG_DATA_HOME>/omnidesk/audit/agent-actions.jsonl`。`omnidesk-pm rpc` 支持 `--provider <plugin_id>`，MCP 侧支持 `provider_plugin_id`。

## 五、剩余差距

- `ui/`：在线源不可用（浏览器直连 / daemon 未启动）时回退 `data/mock.ts` 示例数据，属预期行为。
- `ui/`：笔记暂存于 `localStorage`（键 `omnidesk.notes.v1`），尚未抽象为独立 capability。
- `shell-tauri/`：真实 libmpv 出帧测试需要 `OMNIDESK_MPV_E2E=1` + `libmpv` + `ffmpeg`，已在 `.github/workflows/ci.yml` 中强制执行。
- Agent 上下文：`context.collect.v1` 已落地，但 UI 伴读栏尚未自动传入当前网页/电子书/字幕引用。
- Agent 审批：当前是显式 `confirm` + `reason` + 审计日志，还没有桌面端人工确认弹窗或按 agent 的细粒度权限白名单。
