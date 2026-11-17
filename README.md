# OmniDesk · 万象

**OmniDesk** 是一个采用 **极薄原生桌面壳（`Tauri v2`；`libmpv` 软件渲染嵌入画布 + MPRIS2 已接入）+ 全键盘空间导航前端（`React 19 / TS / Tailwind v4`）+ 零 ABI 纯 Python 微内核与插件生态（`Python 3.14 + uv`）** 的桌面内容工作台与 AI 协作中心。

视觉与交互完整延续 **Paper `#F6F5F3`（纸感亮色）& Graphite `#181718`（石墨深色）** 设计体系与 **「键盘控制一切」** 的操作手感（稳定 ID 绑定的 2D 空间焦点环、`h/j/k/l` 与方向键几何跳转、显式键帽角标、`f` 全屏字母闪跳、`Ctrl+K` 全局命令面板、`F8` 无遮罩响应式伴读栏），并以模块化拆分为工程准则（预算与实际规模见 [ARCHITECTURE.md](ARCHITECTURE.md)），彻底告别 C++ `.so` 二进制 ABI。

---

## 五大核心板块

1. **零编译插件中心 (Plugin Store & `omnidesk-pm`)**：废除 CMake 编译与 `.so` ABI 指纹锁死；内置纯 Python 插件以 `plugin.json + plugin.py` 形态运行，支持 `uv` 环境管理、热重载、`0600` 配置/密钥表单与冒烟验证。当前 `omnidesk-pm` CLI 已支持 `list` / `init` / `verify` / `install` / `update` / `rollback` / `enable` / `disable` / `config` / `mcp-serve` / `reload` / `rpc` / `emit` / `ping` / `curate` / `rehydrate`；插件 `python_dependencies` 会在安装/更新时自动装入 `<plugin_data>/python-env` 独立 venv，并在加载时临时注入 `sys.path`。
2. **影视与音乐 (Media Workbench)**：16:9 视频卡片流、1:1 封面与双语 LRC 歌词对照舞台、飞牛 fnOS 私有影库视图与空降章节导航；已迁入 `plugin-music`、`plugin-bilibili`、`plugin-youtube`、`plugin-fnos-nas`、`plugin-arr-imdb`、`plugin-sub-hunter`、`plugin-sponsorblock`（`libmpv` 软件渲染画布、MPRIS2 媒体键、真实播放状态与 UI 双向同步已接入）。
3. **文字 · 社区 · 科学期刊 (Text Workbench)**：高密度阅读排版，原生支持 Markdown、KaTeX 公式、代码块高亮、三态双语对照与可折叠嵌套评论树；`zhihu`、`v2ex`、`linuxdo`、`shuiyuan`、`reddit`、`x`、`blog`、`feed-recipe` 均已迁入/完成并通过离线测试，`journals` 按 v2 规范完成 arXiv / Nature / bioRxiv / PubMed / Crossref 链路。
4. **书本与 PDF (Book Workbench)**：2:3 书架、BabelDOC PDF 左右双栏对照、EPUB/MOBI 原版书划词伴读（联动 `media.clip_jump.v1` 跳转本地台词切片）及 SM-2 间隔重复生词闪卡；已迁入 `plugin-pdf-babeldoc`、`plugin-ebook`、`plugin-zlib`、`plugin-annas-archive`，前端书架、书页与生词卡均已接 `domain.invoke` / `vocab.sm2.v1` 实时源。
5. **AI Agent 进驻与双模策展反写中心 (AI Workbench & Curation)**：内驻 Magpie Agent（复用本机 `127.0.0.1:3425` 网关与专属组 `group/omnidesk-active`）与外部入站 Agent（`control.sock` / `omnidesk-pm rpc` / MCP）共享唯一渐进式 Skill（[`.agents/skills/omnidesk-curation-writeback/SKILL.md`](.agents/skills/omnidesk-curation-writeback/SKILL.md)），自动克隆 [`ZJFILOVE/obsidian-vault`](https://github.com/ZJFILOVE/obsidian-vault) 与 [`ZJFILOVE/music-daily`](https://github.com/ZJFILOVE/music-daily) 回灌全量历史推荐，并用硬防火墙隔离 **「每日不重复精选（`daily_immutable`）」** 与 **「按需可重复推荐（`interactive_ephemeral`）」** 反写工作台与 Apple Music 歌单（严格失败关闭：禁止合成占位推荐，Apple Music 须经国区实时 `verified_ids` 预检，每日发布须具备匹配的 `PUBLISHED` 事务收据）。`context.collect.v1` 可把网页、电子书、字幕、语音转写和媒体源的白名单上下文收敛成有界 Markdown，供 `ai.agent.v1` 的 `context_refs` 自动引用；MCP 侧另有 `omnidesk_collect_context` / `omnidesk_recommend` / `omnidesk_download_book`。

> 架构总览见 [ARCHITECTURE.md](ARCHITECTURE.md)；插件规范与标准 Capability 协议见 [PLUGIN_SPEC.md](PLUGIN_SPEC.md) 与 [docs/PLUGIN_INTERFACE_PROTOCOL.md](docs/PLUGIN_INTERFACE_PROTOCOL.md)；Web 社区统一登录与会话桥接见 [docs/WEB_AUTH_AND_SESSION_PROTOCOL.md](docs/WEB_AUTH_AND_SESSION_PROTOCOL.md)；AI 策展回灌与反写协议见 [docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md](docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md)。

---

## 目录结构（v2 实际布局）

- `core/`：`omnidesk_core` Python 微内核（`CapabilityRegistry`、`EntityBus`、`DomainRegistry`、`JobCenter`、`0600` `ConfigStore`/`SecretVault`、SQLite FTS5 `SearchIndex`、`ControlServer` `control.sock` 与 `omnidesk-pm` CLI）
- `omnidesk_sdk/`：纯 Python 插件 SDK（`BasePlugin`、`PluginContext`、`MediaSource`/`TextSource`/`BookSource`、Pydantic 模型、`LocalConfigStore`/`LocalSecretVault`、`exec_subprocess`；统一 `SafeHttpClient` / `CookieSessionResolver` 已实现，含 SSRF/DNS rebinding 防护、跨源重定向脱敏与 HTTP CONNECT 环境代理）
- `plugins/`：v2 纯 Python 插件集合（`plugin.json` + `plugin.py`）。当前共 25 个插件目录，全部完成 v2 迁移/新建并通过 `omnidesk-pm verify`；其中 `plugin-music` 直接复用 `/home/zhu/Projects/qwen-translator` 模块
- `ui/`：React 19 + TypeScript + Tailwind v4 全键盘前端（2D 空间焦点引擎、Paper/Graphite 主题、五大工作台与 `F8` 伴读栏；通过 Tauri IPC 连接 `shell-tauri`，独立浏览器启动时提供离线示例预览）。桌面模式下可在媒体、文章、书籍工作台切换已启用来源；空结果与连接/鉴权失败显示真实状态，可手动刷新，策展写回及后端重连会自动刷新。
- `shell-tauri/`：Rust / Tauri v2 原生桌面壳（已实现 `control.sock` NDJSON 客户端、`EntityBus` 事件转发桥、真实 mpv 播放控制、libmpv 软件渲染画布与 MPRIS2 D-Bus 服务）
- `docs/`：跨插件能力协议、Web 登录会话规范、AI 双模策展协议与 `docs/schemas/` JSON Schema
- `tests/`：Python 微内核、SDK、`control.sock` RPC/流式协议与插件管理集成测试
- `.agents/`：AI 双模策展统一 Skill（[`.agents/skills/omnidesk-curation-writeback/SKILL.md`](.agents/skills/omnidesk-curation-writeback/SKILL.md)）
- `scripts/`：辅助脚本目录（当前为空）

> **v1 归档与外部插件仓说明**：
> - v1 C++17 Borealis 历史代码已归档至 `legacy-cpp` 分支**作只读参考**（`main` 分支已移除所有 v1 C++/CMake 目录）。
> - 外部插件仓库位于 `/home/zhu/Projects/omnidesk-plugin-*`（共 22 个 v1 CMake/C++ 仓库），已全部完成 v2 Python 迁移并收编至 `plugins/`；`journals` 无 v1 源码，按 v2 规范新写。

---

## 全键盘导航速查

- **全局切换**：`Ctrl+1..5` 切换「影视音乐 / 文字期刊 / 书本PDF / AI 工作台 / 插件中心」；`Alt+<字母>` 直达确定性分配的页签（如 `Alt+m/v/y/n/j/r/l/z/b/s/a/p/c`，`Alt+t` 切换主题）；`Ctrl+Tab` 在导航区、主舞台与 `F8` 伴读栏之间切换焦点区域。
- **空间移动与触发**：`h / j / k / l` 或 `↑ / ↓ / ← / →` 在二维平面按几何最近邻移动焦点环；`Enter` 激活；`Esc` 收起浮层或逐层回退；`f` 呼出全屏字母跳点角标。
- **效率面板与阅读控制**：`Ctrl+K` 全局命令面板；`/` 聚焦当前页搜索过滤；`F8` 开关伴读栏；`t` 切换 Paper / Graphite 主题；`Space` 在影视工作台控制播放/暂停（其余页面触发激活）；`?` 查看快捷键帮助（中文输入法候选词激活期间自动隔离单键快捷键）。

---

## 开发与常用命令（v2）

```bash
# 1. 初始化并运行 Python 微内核（测试 & 启动 control.sock 守护进程）
uv sync
uv run pytest -q
uv run python -m omnidesk_core serve

# 2. 启动前端（开发预览 / 构建产物）
npm --prefix ui install
npm --prefix ui run dev
npm --prefix ui run build

# 3. 编译并测试 Tauri 原生桌面壳
cargo build --manifest-path shell-tauri/Cargo.toml
cargo test --manifest-path shell-tauri/Cargo.toml

# 桌面发行版：先嵌入最新前端，再运行原生壳接入真实数据
npm --prefix ui run build
cargo build --release --features custom-protocol --manifest-path shell-tauri/Cargo.toml
./shell-tauri/target/release/omnidesk
# 注意：npm run dev 的独立浏览器页面仅用于示例预览，没有 Tauri IPC。

# 已启动 Core 且已有策展数据时，验证原生 IPC → control.sock → 真实数据源
cargo test --manifest-path shell-tauri/Cargo.toml --test live_data_flow -- --ignored --nocapture
# 强制验证 libmpv 实际视频帧与独立 mpv IPC（需要 mpv、libmpv、ffmpeg）
OMNIDESK_MPV_E2E=1 OMNIDESK_MPV_NULL_AUDIO=1 cargo test --manifest-path shell-tauri/Cargo.toml

# 4. 使用 omnidesk-pm（实际已实现命令：list / init / verify / install / update / rollback / enable / disable / config / mcp-serve / reload / rpc / emit / ping）
uv run omnidesk-pm list
uv run omnidesk-pm init plugins/plugin-mytool --id plugin-mytool --domain text --kind workspace --provides text.source.v1
uv run omnidesk-pm verify plugin-magpie-agent
uv run omnidesk-pm install <git-url-or-local-path>
uv run omnidesk-pm update plugin-mytool
uv run omnidesk-pm rollback plugin-mytool
uv run omnidesk-pm enable plugin-mytool
uv run omnidesk-pm disable plugin-mytool
uv run omnidesk-pm config plugin-mytool list
uv run omnidesk-pm mcp-serve
# 以下命令需先启动 `uv run python -m omnidesk_core serve` 监听 control.sock：
uv run omnidesk-pm ping
uv run omnidesk-pm reload plugin-demo
uv run omnidesk-pm emit entity.recommend.updated '{"mode":"interactive_ephemeral","domains":["media"],"written_count":1}'
uv run omnidesk-pm rpc recommend.feed.v1 '{"action":"bootstrap_and_rehydrate","pull_remote":true}'
uv run omnidesk-pm rpc ai.curation.v1 '{"action":"curate_interactive","prompt":"来5首适合深夜写代码的器乐爵士，以前推过的也可以","sync_apple_music":"ephemeral_playlist"}'
uv run omnidesk-pm rpc context.collect.v1 '{"items":[{"capability":"web.extract.v1","payload":{"url":"https://example.com"}}]}'
uv run omnidesk-pm rpc book.source.v1 '{"action":"list_books"}' --provider plugin-pdf-babeldoc
# 注：curate / rehydrate 已是一等 CLI 命令，无需手写 capability RPC。
```
