# OmniDesk · 万象

**OmniDesk** 是一个采用 **极薄原生硬解壳（`Tauri v2 + libmpv`）+ 全键盘空间导航前端（`React 19 / TS / Tailwind v4`）+ 零 ABI 纯 Python 微内核与插件生态（`Python 3.14 + uv`）** 的桌面内容工作台与 AI 协作中心。

视觉与交互完整延续 **Paper `#F6F5F3`（纸感亮色）& Graphite `#181718`（石墨深色）** 设计体系与 **「键盘控制一切」** 的操作手感（稳定 ID 绑定的 2D 空间焦点环、`h/j/k/l` 与方向键几何跳转、显式键帽角标、`f` 全屏字母闪跳、`Ctrl+K` 全局命令面板、`F8` 无遮罩响应式伴读栏），同时将全栈代码规模严格约束在 **`~16,000` 行以内（单文件上限 `<= 400` 行）**，彻底告别 C++ `.so` 二进制 ABI 与重复造轮子。

---

## 五大核心板块

1. **零编译插件中心 (Plugin Store & `omnidesk-pm`)**：彻底废除 CMake 编译与 `.so` ABI 指纹锁死；所有内置与外部 Git 插件均以 `plugin.json + plugin.py`（`150~350` 行纯 Python）形态运行，支持 `uv` 环境管理、秒级热重载、动态参数/`0600` 密钥表单、冒烟门禁与一键回滚。
2. **影视与音乐 (Media Workbench)**：16:9 视频卡片流、1:1 封面与双语 LRC 歌词对照舞台、飞牛 fnOS 私有影库视图、原生 `libmpv` OpenGL 硬解播放舞台与空降章节导航。
3. **文字 · 社区 · 科学期刊 (Text Workbench)**：个人博客、知乎、Linux.do、交大水源、V2EX、Reddit、X 与 arXiv/Nature/PubMed 高密度阅读排版，原生支持 Markdown、KaTeX 公式、代码块高亮、三态双语对照与可折叠嵌套评论树。
4. **书本与 PDF (Book Workbench)**：2:3 书架、BabelDOC PDF 左右双栏对照、EPUB/MOBI 原版书划词伴读（联动 `media.clip_jump.v1` 一键跳播飞牛美剧原声切片）及 SM-2 间隔重复生词闪卡。
5. **AI Agent 进驻与双模策展反写中心 (AI Workbench & Curation)**：内驻 Magpie Agent（复用本机 `127.0.0.1:3425` 网关与专属组 `group/omnidesk-active`）与外部入站 Agent（`control.sock` / `omnidesk-pm rpc`）共享唯一渐进式 Skill（[`.agents/skills/omnidesk-curation-writeback/SKILL.md`](.agents/skills/omnidesk-curation-writeback/SKILL.md)），自动克隆 [`ZJFILOVE/obsidian-vault`](https://github.com/ZJFILOVE/obsidian-vault) 与 [`ZJFILOVE/music-daily`](https://github.com/ZJFILOVE/music-daily) 回灌全量历史推荐，并用硬防火墙隔离 **「每日不重复精选（`daily_immutable`）」** 与 **「按需可重复推荐（`interactive_ephemeral`）」** 反写三大工作台与 Apple Music 歌单（严格失败关闭：禁止合成占位推荐，Apple Music 须经国区实时 `verified_ids` 预检，每日发布须具备匹配的 `PUBLISHED` 事务收据）。

> 架构总览见 [ARCHITECTURE.md](ARCHITECTURE.md)；插件规范与标准 Capability 协议见 [PLUGIN_SPEC.md](PLUGIN_SPEC.md) 与 [docs/PLUGIN_INTERFACE_PROTOCOL.md](docs/PLUGIN_INTERFACE_PROTOCOL.md)；Web 社区统一登录与会话桥接见 [docs/WEB_AUTH_AND_SESSION_PROTOCOL.md](docs/WEB_AUTH_AND_SESSION_PROTOCOL.md)；AI 策展回灌与反写协议见 [docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md](docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md)。

---

## 目录结构（v2 目标布局）

- `shell-tauri/`：极薄 Rust / Tauri v2 原生 Wayland 窗口壳 + `libmpv` OpenGL 硬解画布 + MPRIS2 媒体键（`~500` 行，零业务逻辑）
- `ui/`：React 19 + TypeScript + Tailwind v4 全键盘前端（2D 空间焦点引擎、Paper/Graphite 主题、五大工作台与 `F8` 伴读栏）
- `core/`：`omnidesk_core` 与 `omnidesk_sdk` Python 微内核（`CapabilityRegistry`、`EntityBus`、`JobCenter`、`0600` `ConfigStore`/`SecretVault`、SQLite FTS5 全文检索、`control.sock` 与 `omnidesk-pm` CLI）
- `plugins/`：纯 Python 插件集合（`plugin.json` + `plugin.py`，单插件 `150~350` 行，活体复用 `/home/zhu/Projects/qwen-translator` 核心能力）
- `docs/`：跨插件能力协议、Web 登录会话规范、AI 双模策展协议与 JSON Schema
- `sources/`：只读上游参考资产

> **迁移期说明**：当前根目录下的 `host/`、`sdk/`、`pm/`、`CMakeLists.txt` 为 v1 C++17 Borealis 冻结参考代码，将在 v2 四阶段迁移完成后归档移除。

---

## 全键盘导航速查

- **全局切换**：`Ctrl+1..5` 切换「影视音乐 / 文字期刊 / 书本PDF / AI 工作台 / 插件中心」；`Alt+<字母>` 直达确定性分配的插件专属页签；`Ctrl+Tab` 在主舞台与 `F8` 伴读栏之间切换焦点区域。
- **空间移动与触发**：`h / j / k / l` 或 `↑ / ↓ / ← / →` 在二维平面按几何最近邻移动焦点环；`Enter` 激活；`Esc` 收起浮层或逐层回退；`f` 呼出全屏字母跳点角标。
- **效率面板与阅读控制**：`Ctrl+K` 全局命令面板；`/` 聚焦当前页搜索过滤；`F8` 开关伴读栏；`t` 切换 Paper / Graphite 主题；`Space` 播放/暂停；`?` 查看快捷键帮助（中文输入法候选词激活期间自动隔离单键快捷键）。

---

## 开发与常用命令（v2）

```bash
# 1. 初始化并运行 Python 微内核测试
uv sync
uv run pytest -q

# 2. 启动前端与 Tauri + libmpv 原生桌面开发环境
npm --prefix ui install
cargo tauri dev

# 3. 使用 omnidesk-pm 管理零编译 Python 插件与入站 RPC
uv run omnidesk-pm list
uv run omnidesk-pm init plugins/plugin-mytool --id plugin-mytool --domain text --kind workspace --provides text.source.v1
uv run omnidesk-pm verify plugin-magpie-agent

# 4. 克隆双仓回灌全部历史推荐 / 入站触发 AI 反写前端与 Apple Music 歌单
uv run omnidesk-pm rpc recommend.feed.v1 '{"action":"bootstrap_and_rehydrate","pull_remote":true}'
uv run omnidesk-pm rpc ai.curation.v1 '{"action":"curate_interactive","prompt":"来5首适合深夜写代码的器乐爵士，以前推过的也可以","sync_apple_music":"ephemeral_playlist"}'
```
