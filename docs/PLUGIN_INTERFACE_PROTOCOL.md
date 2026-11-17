# 万象 · OmniDesk 插件中心与全插件接入接口协议规范（Protocol v2 · Zero-ABI Python）

本文档定义 **OmniDesk v2**（`shell-tauri` + `ui` + `omnidesk_core` / `omnidesk_sdk` + `omnidesk-pm`）与所有内置及外部插件之间的**零编译 Python 加载协议、插件清单契约、跨插件能力总线（`CapabilityRegistry` / `EntityBus`）JSON 协议、一号插件（Magpie AI 路由网关）反向打穿规范、二号插件（飞牛 fnOS NAS 连接器）自配置解耦规范、三号插件（FunASR / SponsorBlock）契约，以及四大前端工作台（影视音乐 / 文字社区与科学期刊 / 书本与 PDF / AI 策展）的视图与数据规范**。

配套机器可校验 Schema 位于：
- 插件清单 `plugin.json`：[`docs/schemas/plugin-manifest.schema.json`](schemas/plugin-manifest.schema.json)
- 市场目录索引 `catalog.json`：[`docs/schemas/marketplace-catalog.schema.json`](schemas/marketplace-catalog.schema.json)
- 能力调用与事件总线载荷：[`docs/schemas/capability-rpc.schema.json`](schemas/capability-rpc.schema.json)

---

## 一、零 ABI 插件目录与防膨胀开发铁律

### 1.1 五条核心工程铁律
1. **彻底废除二进制 ABI**：严禁使用 C++ `.so`、`CMake`、`dlopen` 或头文件指纹锁死。所有插件统一使用 **Python 3.14+（`uv` 管理）** 编写，通过 `omnidesk_sdk` 的 Pydantic 模型与 JSON 契约同内核交互，保存 `.py` 即可秒级热重载。
2. **单仓收编优先 + 外部仓库兼容**：核心与社区插件统一存放于主仓 `plugins/<plugin-id>/`（消除 22 个独立 CMake 仓库各自重复实现 HTTP/Cookie/解析器的维护灾难）；同时 `omnidesk-pm install` 仍支持从任意外部目录或 GitHub `<owner>/<repo>` 安装第三方纯 Python 插件。
3. **单文件行数红线（`<= 400` 行）**：任何插件的 `plugin.py`、`sources.py` 或解析模块严禁超过 `400` 行；HTTP 重试、代理策略、Cookie 解密与缓存一律复用 `omnidesk_sdk` 公共底座。
4. **零硬编码互引与强制优雅降级**：插件之间**严禁**直接 `import` 彼此目录（如 `from plugins.plugin_fnos_nas import ...`）。跨插件协作必须且只能通过 `ctx.capabilities` 与 `ctx.bus` 完成；调用前须检查 `ctx.capabilities.has("<cap>.v1")`，当底座插件未启用时优雅降级并给出清晰引导。
5. **活体复用兄弟项目**：涉及本机已成熟的翻译、双语歌词、字幕处理与模型会话逻辑时，直接同源调用 `/home/zhu/Projects/qwen-translator` 对应 Python 模块，拒绝重复搬运代码。

### 1.2 标准插件目录布局

可使用脚手架一键初始化：
```bash
uv run omnidesk-pm init plugins/plugin-example \
  --id plugin-example \
  --name "Example Plugin" \
  --kind workspace \
  --domain text \
  --provides text.source.v1 \
  --consumes llm.chat.v1
```

标准目录树如下：
```text
plugins/plugin-<slug>/
├── plugin.json              # 必填：插件清单与声明式配置 Schema（schema_version = 2）
├── plugin.py                # 必填：实现 BasePlugin 子类与 create_plugin()（<= 400 行）
├── sources.py               # 可选：拆分 MediaSource / TextSource / BookSource 实现
├── resources/               # 可选：插件随附提示词模板、配方或 Chrome MV3 扩展静态资源
├── tests/
│   └── test_plugin.py       # 必填：离线 pytest 契约与解析冒烟测试（< 2 秒）
└── README.md                # 可选：简明接入与配置说明
```

---

## 二、插件清单协议（`plugin.json`）与 `omnidesk-pm` CLI

### 2.1 `plugin.json` 字段规范（`schema_version = 2`）

| 字段路径 | 类型 | 必填 | 约束与语义说明 |
| :--- | :--- | :---: | :--- |
| `schema_version` | `int` | 是 | 当前规范为 `2` |
| `id` | `string` | 是 | 唯一插件 ID，正则 `^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`，严禁含 `/`、`\`、`..` |
| `name` | `string` | 是 | 插件中心与设置页展示名称 |
| `version` | `string` | 是 | SemVer 版本号，如 `"0.2.0"` |
| `runtime` | `string` | 否 | 默认 `"python"` |
| `entry` | `string` | 否 | Python 入口点 `"<module>:<callable>"`，默认 `"plugin:create_plugin"` |
| `kind` | `string` | 是 | `"provider"`（底座能力）/ `"workspace"`（领域工作区源）/ `"hybrid"`（兼具工作区与共享能力）/ `"extension"`（增强现有工作台） |
| `domain` | `string` | 是 | `"core"`（底座与路由）/ `"media"`（影视与音乐）/ `"text"`（文字与期刊）/ `"book"`（书本与 PDF） |
| `author` | `string` | 否 | 维护者名称或 GitHub 账号 |
| `description` | `string` | 否 | 一句话职责说明，支持插件中心全文检索 |
| `repository` | `object` | 否 | 外部插件仓库信息：`{"type": "git", "url": "https://...", "branch": "main"}` |
| `python_dependencies` | `string[]` | 否 | 额外 PyPI 依赖声明（由 `uv` 自动解析同步） |
| `verify_cmd` | `string` | 否 | 激活前执行的冒烟测试命令，默认 `"uv run pytest -q"` |
| `provides` | `string[]` | 否 | 本插件向 `CapabilityRegistry` 注册的能力 ID 列表 |
| `consumes` | `string[]` | 否 | 本插件可选消费的能力 ID 列表 |
| `permissions` | `string[]` | 否 | 声明式权限标签（见下表） |
| `config_schema` | `ConfigField[]` | 否 | 声明式配置项数组；前端插件中心据此自动生成设置表单 |

#### 推荐权限标签枚举（`permissions`）
- `config:read` / `config:write`：读写 `~/.config/omnidesk/plugins/<id>.json`
- `secret:vault`：读写 `0600` 权限的 `SecretVault` 隔离凭据项
- `network:loopback`：仅访问本机回环端口（如 `127.0.0.1:3425` Magpie 网关）
- `network:lan`：访问局域网私有服务（如飞牛 NAS / Radarr / Sonarr，强制 `trust_env=False` 禁用系统代理）
- `network:wan`：访问公网 HTTPS 接口（如 Bilibili / YouTube / arXiv / Linux.do / Reddit）
- `process:magpie`：调用本机 `magpie` CLI 读取状态与更新专属路由组
- `process:chrome`：拉起受管 Chrome `--app` 阅读/登录窗口
- `process:funasr`：调用本机 `llama-funasr-sensevoice` 执行离线语音识别
- `process:ffmpeg`：调用本机 `ffmpeg` / `ffprobe` 进行音轨探测与切片提取
- `gpu:vulkan`：允许推理子进程访问本机 Vulkan GPU 设备
- `fs:user_mount`：按用户显式配置的路径读写本地目录或 NAS 挂载点

#### 公网代理（`network:wan`）
- `omnidesk_sdk.SafeHttpClient` 默认读取 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` 与 `NO_PROXY`（大小写均支持），并可用 `proxy_url=` 显式覆盖；仅支持 `http://` CONNECT 代理。
- Tauri 启动 Python 内核时会将上述代理变量加入环境白名单；从 GUI 启动时需确保会话环境中存在这些变量。
- 代理只作用于公网请求；`network:lan` 插件必须继续使用 `trust_env=False`，避免把 NAS / Radarr / Sonarr 流量送进公网代理。

#### `config_schema` 单项结构
- `type = "string" | "bool" | "int" | "select"`：持久化至 `0600` 权限的 `ConfigStore`（`~/.config/omnidesk/plugins/<plugin_id>.json`）；当 `type == "select"` 时，`options` 数组必须非空。
- `type = "secret"`：**绝不写入普通配置文件、前端明文状态或日志**，直接存入 `0600` 权限的 `SecretVault`（`ctx.get_secret(key)` / `ctx.set_secret(key, val)`）。

### 2.2 `omnidesk-pm` CLI 管理与入站 RPC 协议

```bash
uv run omnidesk-pm list                                       # 列出全部内置与已安装插件状态
uv run omnidesk-pm enable plugin-fnos-nas                     # 启用并即时热加载插件
uv run omnidesk-pm reload plugin-zhihu                        # 秒级热重载单个 Python 插件
uv run omnidesk-pm install ZJFILOVE/omnidesk-plugin-custom    # 从外部 Git 仓库拉取并在 /tmp 跑通 pytest 后原子安装
uv run omnidesk-pm config plugin-fnos-nas set mount_root /mnt/feiniu-smb
uv run omnidesk-pm config plugin-fnos-nas set-secret api_token "<secret>"
uv run omnidesk-pm verify plugin-magpie-agent                 # 执行 pytest 与生命周期自检
uv run omnidesk-pm rollback plugin-custom                     # 1 秒回滚至上一快照版本
uv run omnidesk-pm rpc llm.router.v1 '{"action":"current_route"}'
```

---

## 三、Python 插件生命周期、声明式面板与故障隔离契约

### 3.1 `BasePlugin` 与 `PluginContext` 生命周期规范

```python
from omnidesk_sdk import BasePlugin, PluginContext


class ExamplePlugin(BasePlugin):
    async def on_load(self, ctx: PluginContext) -> None:
        """非阻塞快速初始化（建议 < 50ms），仅完成能力/数据源/事件订阅注册。"""

    async def on_unload(self) -> None:
        """可选清理钩子；通过 ctx 注册的资源与后台 Task 会由内核自动撤销取消。"""

    async def on_health_check(self) -> dict:
        return {"ok": True}
```

**插件实现者必须遵守以下四条运行时准则**：
1. **`on_load(ctx)` 必须非阻塞快速返回（`< 50 ms`）**：严禁在 `on_load` 中同步阻塞等待外部网络或子进程；耗时探测应采用按需惰性缓存或通过 `ctx.spawn_task(...)` 后台执行。
2. **统一使用 `ctx` 作用域方法注册资源**：通过 `ctx.register_capability(...)`、`ctx.register_streaming_capability(...)`、`ctx.subscribe(...)`、`ctx.register_media_source(...)`、`ctx.register_text_source(...)`、`ctx.register_book_source(...)`、`ctx.register_workspace_tab(...)`、`ctx.register_dock_widget(...)` 注册；卸载或热重载时由内核按 `plugin_id` 自动原子回收。
3. **异常屏障与外部进程组隔离**：
   - Python 协程内未捕获的异常会被 `omnidesk_core` 拦截并发布 `"plugin.fault"` 事件，绝不导致主进程退出。
   - 调用 `ffmpeg`、`ffprobe`、`funasr`、`yt-dlp`、`magpie` 等外部二进制时，必须使用 `await ctx.exec_subprocess(["bin", "arg1", ...], cancel_token=...)`（严禁 `shell=True`），内核自动创建独立进程组并在取消/超时/卸载时执行 `SIGTERM -> SIGKILL` 全组清理。
4. **声明式 Schema 面板（插件内零手写 UI 树）**：
   - 插件设置页由前端直接根据 `plugin.json` 的 `config_schema` 渲染。
   - 插件若需挂载专属页签（`WorkspaceTabSpec`）或 `F8` 伴读小部件（`DockWidgetSpec`），仅返回轻量 JSON Schema（`Section / Form / KeyValue / List / ActionRow / Markdown`）并处理 `on_action(action_id, state)`，由 React 前端统一渲染并接入 2D 空间键盘焦点环。

### 3.2 声明式页签与 F8 Dock 动作协议（`ui.action`）

`domain.snapshot` 返回的 `workspace_tabs` / `dock_widgets` 只包含可 JSON 序列化的 `tab_id`、`widget_id`、`plugin_id`、`title`、`domain` / `supported_domains` 与 `ui_schema`；`render` 和 `on_action` 回调不会跨进程返回。前端通过 `ui.action` 显式回传按钮动作与表单状态：

```json
{
  "op": "ui.action",
  "kind": "workspace_tab",
  "spec_id": "bili-home",
  "plugin_id": "plugin-bilibili",
  "action_id": "sync_favorites",
  "state": {"query": "rust", "limit": 20},
  "timeout": 30
}
```

- `kind`：`workspace_tab` 或 `dock_widget`。
- `spec_id`：对应 `WorkspaceTabSpec.tab_id` 或 `DockWidgetSpec.widget_id`。
- `plugin_id`：可选；提供时同时校验声明归属，防止跨插件调用同名控件。
- `state`：前端累积的表单字段（JSON object），会原样传给插件回调。
- `timeout`：秒；默认 30 秒，仍受 Core 单次 RPC 上限约束。

插件回调支持 `on_action(action_id, state)`、单参数 `on_action({...state, "action": action_id})` 与无参三种签名；同步回调在线程池执行，异步回调直接 await。成功响应可直接返回新的 `ui_schema`，前端用它替换当前声明式面板：

```json
{
  "ok": true,
  "provider_plugin_id": "plugin-bilibili",
  "data": {
    "ui_schema": {
      "type": "Markdown",
      "content": "已同步 20 条收藏"
    }
  }
}
```

`spec_id` 不存在、`plugin_id` 不匹配时返回 `not_found`；控件未注册 `on_action` 时返回 `capability_not_found`；插件异常隔离为 `plugin_fault`，超时返回 `timeout`。返回值统一经过密钥清洗，错误信息不得包含 Cookie、Token 或密钥明文。

---

## 四、核心总线协议：`CapabilityRegistry` 与 `EntityBus`

### 4.1 `CapabilityRegistry` 统一调用信封

- **调用方（Python 插件内）**：
  ```python
  if ctx.capabilities.has("llm.chat.v1"):
      res = await ctx.capabilities.call("llm.chat.v1", {
          "domain": "text",
          "context": article.title,
          "prompt": "提炼核心论点与方法局限",
      })
      if res.ok:
          reply = res.data.get("reply", "")
  ```
- **返回结构 `CapabilityResult`**：
  - `ok` (`bool`)：业务执行是否成功。
  - `data` (`dict`)：成功时返回结构化结果对象；失败时可附带 `{"error_code": "..."}`。
  - `error` (`str`)：面向人类可读的中文简明错误说明（直接供前端展示，**严禁混入密钥或完整 Token**）。
  - `provider_plugin_id` (`str`)：由 `CapabilityRegistry` 自动填充实际响应的插件 ID。
- **标准 `data.error_code` 枚举**：
  - `"not_configured"`：用户尚未填写必要路径或凭据
  - `"service_offline"`：本机守护进程（如 Magpie `3425`）或远端 NAS 不可达
  - `"rate_limited"`：上游 429 限流
  - `"quota_exceeded"`：当前订阅窗口或额度耗尽
  - `"invalid_argument"`：请求缺少必填字段
  - `"timeout"`：超时或协作取消

### 4.2 全平台标准能力 ID 矩阵

| 能力 ID | 提供者插件 | 消费方工作台 / 插件 | 核心职责 |
| :--- | :--- | :--- | :--- |
| `llm.router.v1` | `plugin-magpie-agent` | 前端 `CompanionDock`、设置中心 | 读取 Magpie 状态/Harness/模型/配额，并将用户所选路由反向写入专属组 `group/omnidesk-active` |
| `ai.agent.v1` | `plugin-magpie-agent` | 前端 `CompanionDock`、四大工作台 | 带领域上下文与预设动作（总结/问答/词源/拆句）的智能伴读对话入口 |
| `ai.curation.v1` | `plugin-magpie-agent` | 前端 `AiWorkbench` / `CompanionDock`、`omnidesk-pm rpc` | 内驻 AI Agent 策展编排器：基于统一 Skill 执行按需可重复推荐（`curate_interactive`）或每日不重复管线（`run_daily_pipeline`）并联动反写 |
| `llm.chat.v1` | `plugin-magpie-agent` | 全域工作区插件 | 通用 OpenAI 兼容多轮/单轮流式文本推理与 JSON Schema 结构化输出 |
| `llm.translate.v1` | `plugin-magpie-agent` | 字幕/网页/期刊/PDF/EPUB 插件 | 支持上下文窗口与术语锁定（Glossary）的单段/批量双语翻译 |
| `recommend.feed.v1` | `plugin-ai-curator` | 三大内容工作台、`plugin-magpie-agent`、外部入站 Agent | 自动克隆同步 `ZJFILOVE/obsidian-vault` 与 `ZJFILOVE/music-daily` 回灌全量历史推荐，并提供 `daily_immutable` 与 `interactive_ephemeral` 双模前端反写存储 |
| `media.playlist.v1` | `plugin-music` | `plugin-ai-curator`、`plugin-magpie-agent`、外部入站 Agent | Apple Music 国区曲库可播预检、经 QA 门禁同步固定累计歌单 `p.YJXV7dEIeb3GzZX`（`ChatGPT Daily 5`），以及受硬隔离保护的可重复灵感歌单 / `libmpv` 播放队列写回 |
| `speech.asr.v1` | `plugin-asr-funasr` | `plugin-bilibili` / `youtube` / `MediaWorkbench` | 本地 Vulkan/CPU FunASR SenseVoiceSmall 离线语音识别、音轨探测与断点分块转写 |
| `player.timeline.v1` | `plugin-sponsorblock` | `MediaWorkbench` / `plugin-bilibili` / `plugin-youtube` | 基于 B 站 (`bsbsb.top`) 与 YouTube (`sponsor.ajay.app`) K-匿名哈希前缀查询空降/章节片段并合并进播放器时间轴 |
| `subtitle.fetch.v1` | `plugin-sub-hunter` | `MediaWorkbench` / `plugin-fnos-nas` / `plugin-youtube` | SubHD 抓取、ASSRT API 检索下载、16 MB 安全解压与多编码 UTF-8 转换 |
| `subtitle.sync.v1` | `plugin-sub-hunter` | `MediaWorkbench` / `plugin-fnos-nas` | `ffsubsync` 音轨 VAD / 参考轴自动对齐与线性偏移/倍率缩放 |
| `subtitle.translate.v1` | `plugin-sub-hunter` | `MediaWorkbench` / `plugin-fnos-nas` | 调用 `llm.translate.v1` 将 SRT/VTT 批量翻译为双语 / 仅译文 / 仅原文；1-8 路并发、内容哈希断点缓存、批次失败自动二分重试与原子写回 |
| `nas.connection.v1` | `plugin-fnos-nas` | 音乐、PDF、EPUB、搜书插件 | 探测飞牛连接状态、列出受管媒体库/书库挂载根与受保护原子写回 |
| `media.source.v1` | `plugin-bilibili` / `youtube` / `fnos-nas` / `plugin-music` / `plugin-ai-curator` | `MediaWorkbench` | 通过 `MediaSource` 向影视音乐工作台注入视频流、番剧、NAS 原盘、Apple Music 曲库及 AI 推荐流 |
| `media.local_match.v1` | `plugin-fnos-nas` | `plugin-bilibili` / `plugin-arr-imdb` | 根据片名 / IMDb / TMDB / 剧集季集号查询飞牛 NAS 是否已有本地高码率片源 |
| `media.clip_jump.v1` | `plugin-fnos-nas` | `BookWorkbench` / `plugin-ebook` | 在本地美剧/电影双语字幕库检索英文单词或词组，返回匹配对白与一键跳播原片时间戳 |
| `web.embed.v1` / `web.extract.v1` / `web.cookies.v1` / `web.fetch.v1` | `plugin-chrome-web` | `plugin-journals` / `feed-recipe` / `blog` / `zhihu` / `linuxdo` / `shuiyuan` / `v2ex` / `reddit` / `x` | 拉起托管无边框 Chrome `--app` 登录/阅读窗、提取渲染后 DOM、同步真实登录 Cookie，或在 Chrome 上下文执行同源 `fetch` |
| `feed.recipe.v1` / `blog.subscription.v1` | `plugin-feed-recipe` / `plugin-blog` | `TextWorkbench`、`CompanionDock` | 管理 20 行声明式站点配方与用户自助收藏的博主订阅源 |
| `text.source.v1` | 各社区/期刊/博客插件 / `plugin-ai-curator` | `TextWorkbench` | 通过 `TextSource` 提供文章流、公式代码块段落、嵌套评论树与 AI 多栏目导读 |
| `book.source.v1` / `book.bilingual_pdf.v1` | `plugin-pdf-babeldoc` / `plugin-ebook` / `plugin-ai-curator` | `BookWorkbench` / 搜书与期刊插件 | 通过 `BookSource` 提供书架、双语分页 `BookSpread`、生词卡及 BabelDOC 双栏 PDF 生成 |
| `book.sync.v1` / `vocab.sm2.v1` | `plugin-fnos-nas` / `plugin-ebook` | `TextWorkbench` / `BookWorkbench` / `MediaWorkbench` | 跨设备同步阅读进度与划词 SM-2 间隔重复生词库 |

### 4.3 `EntityBus` 标准事件主题与载荷规范

| 主题（Topic） | 触发时机 | 标准 JSON Payload 结构 |
| :--- | :--- | :--- |
| `"asr.job.progress"` | `plugin-asr-funasr` 完成长音视频单个切片转写并保存断点后 | `{"job_id": "...", "media_path": "...", "completed_chunks": 2, "total_chunks": 5, "progress": 0.4}` |
| `"asr.job.completed"` | `plugin-asr-funasr` 完成整片转写与 `.srt` 合并写入后 | `{"job_id": "...", "media_path": "...", "output_srt_path": "...", "cue_count": 148}` |
| `"entity.recommend.updated"` | `plugin-ai-curator` 完成双仓历史归档回灌、每日精选发布或按需可重复推荐反写后 | `{"mode": "daily_immutable \| interactive_ephemeral", "set_id": "...", "operation_id": "...", "domains": ["media", "text"], "written_count": 5, "auto_focus": true}` |
| `"entity.music.playlist_synced"` | `plugin-music` 完成 Apple Music 歌单写回并通过读回校验后 | `{"mode": "daily_immutable \| interactive_ephemeral", "playlist_id": "p.YJXV7dEIeb3GzZX", "playlist_name": "ChatGPT Daily 5", "added_count": 5, "verified_count": 5}` |
| `"curation.job.progress"` | 内驻 `ai.curation.v1` 在后台执行克隆回灌、检索核验或歌单同步期间回报阶段进度 | `{"job_id": "...", "mode": "interactive_ephemeral", "stage": "searching \| verifying \| writing_feed \| syncing_playlist \| done", "message": "..."}` |
| `"entity.media.open"` | 从任意插件（如原版书台词检索、选片页）请求打开视频/音频片段 | `{"item_id": "...", "source_id": "fnos-nas", "title": "...", "kind": "nas", "play_url": "file:///...", "start_sec": 842.5, "end_sec": 856.0}` |
| `"entity.text.open"` | 打开指定帖子、专栏文章或 DOI 期刊论文 | `{"article_id": "...", "source_id": "journals", "url": "https://...", "doi": "10.1038/..."}` |
| `"entity.book.open"` | 下载或排版完成后在书本工作台打开 PDF / EPUB 并定位页码 | `{"book_id": "...", "file_path": "/path/to/paper.dual.pdf", "format": "pdf", "page": 1}` |
| `"entity.vocab.add"` | 在期刊、社区帖或字幕中选中生词送入伴读生词本 | `{"word": "ephemeral", "context_sentence": "...", "source_title": "..."}` |
| `"entity.ai.route_changed"` | 用户在 `plugin-magpie-agent` 切换了底层 Harness / 模型 / 路由策略 | `{"summary": "group/omnidesk-active -> antigravity/gemini-4-carbon:high", "group_id": "omnidesk-active", "primary_member": "antigravity/gemini-4-carbon:high"}` |
| `"plugin.state_changed"` | 插件完成安装、更新、启用、停用或热重载 | `{"plugin_id": "plugin-fnos-nas", "state": "loaded \| unloaded \| reloaded"}` |
| `"plugin.fault"` | 插件协程或外部工具子进程异常被内核隔离拦截后 | `{"plugin_id": "plugin-demo", "component": "capability:...", "error": "..."}` |

---

## 五、一号核心插件规范：`plugin-magpie-agent`（Magpie 路由探测与反向打穿协议）

### 5.1 核心设计约束（零全局污染 + `magpie group` 反向打穿 + `3425` 网关直连）

1. **默认前置条件**：本机已安装并运行 [Magpie](https://usemagpie.ai)（CLI `magpie` 位于 `PATH` 或 `~/.local/bin/magpie`，本地网关记录于 `~/.config/magpie/gateway.json`，默认监听 `http://127.0.0.1:3425`）。
2. **严禁改动终端各 Harness 的全局默认绑定**：
   - **禁止执行** `magpie codex <model>`、`magpie antigravity-cli <model>`、`magpie claude <model>`、`magpie pi <model>` 等命令，以免干扰用户终端编码会话。
   - **禁止直接编辑** `~/.config/magpie/providers.json` 或 `library.json`（必须通过 `magpie` CLI 原子落盘校验）。
   - **禁止调用** `magpie quota reset`。
3. **专属路由组反向打穿机制**：
   - 仅维护属于 OmniDesk 的专属路由组 `group/omnidesk-active`。
   - 用户在界面选择底层 Harness/Provider + 模型 + 推理强度（Effort）+ 回退链后，通过 `magpie group add/set/pick` 实时反向写入 `omnidesk-active`。
   - 所有内部能力（`ai.agent.v1`、`llm.chat.v1`、`llm.translate.v1`）统一使用 `httpx.AsyncClient(trust_env=False)` 请求 `http://127.0.0.1:3425/v1/chat/completions` 并指定 `"model": "group/omnidesk-active"`。

### 5.2 读取与写回契约（`llm.router.v1` / `ai.agent.v1` / `llm.chat.v1`）

- **`llm.router.v1` 支持动作**：
  - `{"action": "current_route"}`：读取 `~/.config/magpie/gateway.json` / `magpie status --json` 与当前 `omnidesk-active` 配置快照（忽略 CLI 首行可能出现的 `YYYY/MM/DD HH:MM:SS plugin: ...` 日志）。
  - `{"action": "list_harnesses"}`：解析 `magpie ls` 已检测 Harness 状态。
  - `{"action": "list_models"}`：解析 `magpie models` 分组头、模型 ID 与支持档位 `efforts`（`low/medium/high/xhigh/max`）。
  - `{"action": "list_quotas"}`：请求 `GET /v1/magpie/quotas`（或回退 `magpie quota --json`）归一化剩余窗口百分比与重置时间。
  - `{"action": "set_route", "group_id": "omnidesk-active", "model_id": "...", "effort": "...", "routing": "order", "stays": "turn", "fallbacks": [...]}`：校验嵌套不自指后执行 `magpie group add/set`（`manual` 时追加 `magpie group pick`），保存配置并广播 `"entity.ai.route_changed"`。
- **`ai.agent.v1` / `llm.chat.v1`**：支持同步与流式 SSE 转发，返回 `reply`、`served_model`、`route_group` 与 `usage`。
- **`ai.agent.v1` 会话持久化**：普通对话可带 `session_id` 继续已有会话；不传时自动创建。每轮用户消息、助手回复、动作、领域、模型、路由与 usage 都写入插件数据目录下的 `agent-sessions/`（目录 `0700`、文件 `0600`）。`session_action` 支持 `list` / `get` / `rename` / `delete` / `export`，其中导出支持 `markdown` 与 `json`。聊天响应返回 `session_id`、`session` 摘要与 `persisted`，前端据此恢复历史、继续会话和下载导出。

---

## 六、关键底座插件契约：`plugin-fnos-nas`、`plugin-asr-funasr` 与 `plugin-sponsorblock`

### 6.1 `plugin-fnos-nas`（解耦式飞牛 fnOS 连接器）
1. **零私密拓扑硬编码**：严禁写死挂载路径、固定局域网 IP 或默认凭据；按用户配置渐进激活：
   - **L1 本地挂载直通（`mount_root`）**：校验可读且无 `..` 路径穿越后激活 `MediaSource` 原盘硬解播放、`nas.connection.v1` 与原子双语字幕写回（`.<name>.omnidesk.tmp` + `os.replace`，默认拒绝覆盖人工精校字幕）。
   - **L2 伴随台词检索与进度同步（`media_service_url` / `companion_sync_url`）**：激活 `media.clip_jump.v1`（读原版书点单词一键跳播美剧对应台词片段并发布 `"entity.media.open"`）与 `book.sync.v1`。
   - **L3 Arr 片源核验（`radarr_url` / `sonarr_url` + `SecretVault` API Key）**：激活 `media.local_match.v1`（所有局域网 `httpx` 请求强制 `trust_env=False`、`follow_redirects=False`）。

### 6.2 `plugin-asr-funasr`（`speech.asr.v1`）
- 零硬编码模型路径；未配置时 `< 5ms` 正常加载并在调用时返回 `error_code = "not_configured"`。
- 通过 `ctx.exec_subprocess` 独立进程组调用 `ffprobe` / `ffmpeg` / `llama-funasr-sensevoice`；支持 `status`、`probe`、`list_streams`、`transcribe_audio`、`transcribe_media`（按 `chunk_seconds` 切片断点续传 `.checkpoint.json`，流式推送 `resumed` / `stage` / `chunk` 事件并广播 `"asr.job.progress"` / `"asr.job.completed"`）及 `cancel` / `clear_cache`。

### 6.3 `plugin-sponsorblock`（`player.timeline.v1`）
- 对 Bilibili (`bsbsb.top`) 与 YouTube (`sponsor.ajay.app`) 采用视频 ID SHA-256 前 4 位 K-匿名查询，返回合并排序后的 `ChapterSegment` 数组（`kind = "sponsor" | "ad" | "content"`），支持进度条琥珀色标记与 `F9` 跳过。

---

## 七、全键盘前端工作台 UI 规范与数据契约（`ui/`）

所有工作台由 `ui/`（React 19 + TypeScript + Tailwind v4）统一承载，完整保留并强化 **「Paper & Graphite + 全键盘空间焦点」** 体验：

### 7.1 全局视觉 Token、响应式 `F8` 伴读栏与二维空间焦点引擎

1. **语义色 CSS 变量（禁止在组件中散落硬编码色值）**：
   - 画布与表面：`--bg-canvas`（Paper `#F6F5F3` / Graphite `#181718`）、`--bg-surface`（`#FCFBF9` / `#232122`）、`--bg-soft`（`#F1EFEC` / `#2B2729`）、`--bg-active`（`#F9E7ED` / `#422630`）、`--border-line`（`1px` 细线，无重阴影或花哨渐变）。
   - 领域语义色与状态色：`--domain-media`、`--domain-text`、`--domain-book`、`--domain-core` 及 `--status-{ok,warn,err,info}-{bg,fg}`（正文字体对比度全部 `>= 4.5:1`）。
2. **无遮罩响应式 `F8` 伴读栏公式**：
   - 严禁使用遮挡主舞台的大号模态弹窗展示总结、字幕、词源或问答。
   - 窗口宽高比 `width / height >= 1.45` 时：伴读栏停靠于**主舞台右侧**独立分栏。
   - 窗口宽高比 `width / height < 1.45` 时：伴读栏自动切换为**底部上下分栏**。
3. **二维空间键盘焦点引擎（2D Spatial Focus Engine）**：
   - 焦点始终绑定在稳定业务 `itemId` 上，异步数据刷新绝不丢失当前焦点位置。
   - 全局快捷键：`Ctrl+1..5` 切换五大板块；`Alt+<字母>` 直达确定性分配的插件页签；`h/j/k/l` 与方向键跨区域几何最近邻移动；`f` 全屏字母闪跳；`Ctrl+K` 命令面板；`/` 搜索；`F8` 开关伴读栏；`Ctrl+Tab` 在主舞台与伴读栏间切换焦点；`compositionstart` ~ `compositionend`（Fcitx5 / Rime 选词）期间自动屏蔽单字母快捷键。

### 7.2 影视与音乐工作台（`MediaWorkbench` · `MediaSource`）
- **Python 数据接口（`MediaSource`）**：`categories()`、`fetch_items(category, query, page)`、`fetch_chapters(item_id)`、`fetch_lyrics(item_id)`。
- **四子态**：`16:9` 视频网格态、左右双栏时间轴同步**原文/译文双语歌词态**、飞牛私有影库态、以及内嵌 `libmpv` 硬解画面与右侧 `F8` 伴读栏并排的**原生播放舞台态**（开关侧栏绝不打断播放）。

### 7.3 文字、社区与科学期刊工作台（`TextWorkbench` · `TextSource`）
- **Python 数据接口（`TextSource`）**：`category`、`channels()`、`fetch_feed(channel_id, query, page)`、`fetch_article(article_id)`（正文块 `kind = "heading" | "paragraph" | "quote" | "code" | "formula"`，评论区为递归 `CommentNode`）。
- **排版与联动**：两级动态栏目 Pill + **`双语对照 | 仅译文 | 仅原文`** 一键切换；原生渲染 Markdown、KaTeX 公式与语法高亮代码块；社区帖挂载可折叠嵌套评论树；`journal` 论文渲染专属 DOI 元数据卡，支持一键调用 `web.embed.v1` 打开托管 Chrome 阅读窗或 `book.bilingual_pdf.v1` 送入 BabelDOC。

### 7.4 书本与文档伴读工作台（`BookWorkbench` · `BookSource`）
- **Python 数据接口（`BookSource`）**：`list_books(format_filter, query)`、`fetch_spread(book_id, page_number)`、`list_vocab()`。
- **四子态**：`2:3` 纵向书架态、BabelDOC PDF 左右基线对齐双栏对照态、EPUB/MOBI 划词伴读态（原生文字选区气泡支持一键查词源/拆长难句送 `F8`、联动 `media.clip_jump.v1` 跳播飞牛美剧原片对白、一键入生词本）、以及 SM-2 闪卡复习台。

---

## 八、专项配套规范与新插件接入检查清单

- **Web 社区统一登录、多源 Cookie 优先级与 Chrome 同源请求代理**：见 [`docs/WEB_AUTH_AND_SESSION_PROTOCOL.md`](WEB_AUTH_AND_SESSION_PROTOCOL.md)。
- **AI Agent 进驻、双仓历史归档回灌与双模前端/歌单反写协议**：见 [`docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md`](AI_AGENT_RECOMMENDATION_PROTOCOL.md) 与唯一入口 Skill [`.agents/skills/omnidesk-curation-writeback/SKILL.md`](../.agents/skills/omnidesk-curation-writeback/SKILL.md)。

### 新插件 4 步零摩擦接入清单
1. **脚手架创建**：`uv run omnidesk-pm init plugins/plugin-<slug> --id plugin-<slug> ...` 生成 `plugin.json` 与 `plugin.py`。
2. **严守代码红线**：复用 `omnidesk_sdk` 的 HTTP/Cookie 客户端，单文件 `<= 400` 行；API Key 与 Cookie 一律声明为 `"type": "secret"` 走 `SecretVault`（`0600`）；局域网请求强制 `trust_env=False`。
3. **验证软依赖降级**：跨插件能力调用前检查 `ctx.capabilities.has(...)`。
4. **跑通秒级自检**：执行 `uv run omnidesk-pm verify plugin-<slug>`（自动验证清单合法性、离线 `pytest` 与 `on_load -> on_health_check -> on_unload` 热加载闭环）。
