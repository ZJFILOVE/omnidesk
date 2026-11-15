# 万象 · OmniDesk 插件市场与全插件接入接口协议规范（ABI v1）

本文档定义 **OmniDesk** 宿主微内核（`omnidesk` + `libomnidesk_sdk.so` + `omnidesk-pm`）与所有外部独立插件仓库之间的**二进制握手协议、仓库与市场清单契约、跨插件能力总线（`CapabilityRegistry` / `EntityBus`）JSON 协议、一号插件（Magpie AI 路由网关）反向打穿规范、二号插件（飞牛 fnOS NAS 连接器）自配置解耦规范，以及三大领域前端工作台（影视音乐 / 文字社区与科学期刊 / 书本与 PDF）的视图与数据契约**。

配套机器可校验 Schema 位于：
- 插件清单 `plugin.json`：[`docs/schemas/plugin-manifest.schema.json`](schemas/plugin-manifest.schema.json)
- 市场目录索引 `catalog.json`：[`docs/schemas/marketplace-catalog.schema.json`](schemas/marketplace-catalog.schema.json)
- 能力调用与事件总线载荷：[`docs/schemas/capability-rpc.schema.json`](schemas/capability-rpc.schema.json)

---

## 一、单插件独立目录与独立 GitHub 仓库规范

### 1.1 解耦铁律
1. **一插件一目录一仓库**：每个插件必须存放在独立的本地目录并对应独立的 GitHub 仓库（命名惯例：`omnidesk-plugin-<slug>`，如 `omnidesk-plugin-magpie-agent`、`omnidesk-plugin-fnos-nas`），严禁将具体站点、网盘路径、私有模型路由硬编码进宿主仓库。
2. **零编译期互赖**：插件之间**严禁**直接链接彼此的 `.so` 或包含彼此的私有头文件。跨插件协作必须且只能通过 `libomnidesk_sdk.so` 暴露的 `CapabilityRegistry`（版本化 RPC）与 `EntityBus`（主题事件）完成。
3. **强制优雅降级**：消费其他插件能力的插件，调用前必须检查 `ctx.capabilities().has("<cap>.v1")`。当底座插件（如 `plugin-magpie-agent` 或 `plugin-fnos-nas`）未安装或被禁用时，上层工作区插件必须保持可浏览、不崩溃，并在操作处给出清晰的提示或跳转插件市场入口。
4. **磁盘友好构建（`/tmp` Staging）**：`omnidesk-pm` 安装或升级插件时，克隆与 CMake 编译均在 `/tmp` 临时区执行，跑通 `verify_cmd` 与独立子进程 `omnidesk-pm abi-check` 后，仅将产物 `.so` 与必要资源原子写入 `~/.local/share/omnidesk/plugins/<plugin-id>/`，保留历史备份支持一键 `omnidesk-pm rollback <plugin-id>`。

### 1.2 独立插件仓库标准目录布局

可使用脚手架一键初始化新仓库：
```bash
omnidesk-pm init ~/Projects/omnidesk-plugin-example \
  --id plugin-example \
  --name "Example Plugin" \
  --kind workspace \
  --domain text \
  --provides text.source.v1 \
  --consumes llm.chat.v1 \
  --owner ZJFILOVE
```

标准仓库目录树如下：
```text
omnidesk-plugin-<slug>/
├── plugin.json              # 必填：插件清单与配置 Schema（schema_version = 1）
├── CMakeLists.txt           # 必填：find_package(OmniDeskSDK REQUIRED) 构建共享库
├── include/                 # 可选：插件内部私有头文件（严禁导出内部符号）
├── src/
│   └── plugin_main.cpp      # 必填：实现 omnidesk::IPlugin 并调用 OMNIDESK_DECLARE_PLUGIN
├── resources/               # 可选：插件随附图标、提示词模板或 Chrome MV3 扩展静态资源
├── tests/
│   └── test_abi.cpp         # 推荐：由 build.verify_cmd 执行的 3 秒离线 ABI 与契约冒烟测试
├── README.md
└── LICENSE
```

### 1.3 标准 `CMakeLists.txt` 规范

插件仅需依赖宿主导出的 `OmniDesk::SDK` 导入目标（由 `OmniDeskSDKConfig.cmake` 提供，或通过 `pkg-config --cflags --libs omnidesk-sdk` 获取）：

```cmake
cmake_minimum_required(VERSION 3.20)
project(plugin_example LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
set(CMAKE_CXX_VISIBILITY_PRESET hidden)
set(CMAKE_VISIBILITY_INLINES_HIDDEN ON)

find_package(OmniDeskSDK REQUIRED)

add_library(plugin_example SHARED
    src/plugin_main.cpp
)
target_link_libraries(plugin_example PRIVATE OmniDesk::SDK)
set_target_properties(plugin_example PROPERTIES
    OUTPUT_NAME "plugin_example"
    PREFIX "lib"
)

enable_testing()
add_executable(test_abi tests/test_abi.cpp)
target_link_libraries(test_abi PRIVATE OmniDesk::SDK dl)
add_test(NAME abi_smoke COMMAND test_abi $<TARGET_FILE:plugin_example> "plugin-example")
```

---

## 二、插件清单协议（`plugin.json`）与插件市场索引（`catalog.json`）

### 2.1 `plugin.json` 字段规范（`schema_version = 1`）

校验逻辑对应 `omnidesk::pm::parseManifestJson` 与 `validateManifest`（见 [`pm/src/manifest.cpp`](../pm/src/manifest.cpp)）：

| 字段路径 | 类型 | 必填 | 约束与语义说明 |
| :--- | :--- | :---: | :--- |
| `schema_version` | `int` | 是 | 固定为 `1` |
| `id` | `string` | 是 | 唯一插件 ID，正则 `^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`，严禁含 `/`、`\`、`..` |
| `name` | `string` | 是 | 插件市场与设置页展示名称 |
| `version` | `string` | 是 | SemVer 版本号，如 `"0.1.0"` |
| `abi_version` | `int` | 是 | 必须等于宿主 `OMNIDESK_PLUGIN_ABI_VERSION`（当前为 `1`） |
| `kind` | `string` | 是 | `"provider"`（纯底座能力）/ `"workspace"`（领域工作区源）/ `"hybrid"`（兼具工作区与共享能力）/ `"extension"`（增强现有工作台） |
| `domain` | `string` | 是 | `"core"`（底座与路由）/ `"media"`（影视与音乐）/ `"text"`（文字与期刊）/ `"book"`（书本与 PDF） |
| `author` | `string` | 否 | 维护者名称或 GitHub 账号 |
| `description` | `string` | 否 | 一句话职责说明，支持插件市场全文检索 |
| `homepage` | `string` | 否 | 插件主页或文档链接 |
| `repository.type` | `string` | 否 | 默认 `"git"` |
| `repository.url` | `string` | 是 | 独立 GitHub 仓库克隆地址（`https://github.com/<owner>/<repo>.git`） |
| `repository.branch` | `string` | 否 | 默认跟踪分支，如 `"main"` |
| `build.system` | `string` | 否 | 固定为 `"cmake"` |
| `build.entry_target` | `string` | 否 | CMake Library Target 名（如 `"plugin_magpie_agent"`） |
| `build.output_lib` | `string` | 否 | 相对构建目录的 `.so` 路径（如 `"libplugin_magpie_agent.so"`，禁止 `/` 开头或含 `..`） |
| `build.cmake_args` | `string[]` | 否 | 附加给 CMake 的编译开关数组 |
| `build.verify_cmd` | `string` | 否 | 激活前在构建目录执行的冒烟校验命令（如 `"ctest --output-on-failure"`） |
| `provides` | `string[]` | 否 | 本插件向 `CapabilityRegistry` 注册的能力 ID 列表 |
| `consumes` | `string[]` | 否 | 本插件可选消费的能力 ID 列表 |
| `permissions` | `string[]` | 否 | 声明式权限标签（见下表） |
| `isolation` | `string` | 否 | 故障隔离级别：`"in_process"`（默认，宿主内 `dlopen` + 异常屏障）或 `"subprocess"`（在独立 `omnidesk-plugin-worker` 进程加载 `.so`） |
| `worker_transport` | `string` | 否 | 当 `isolation == "subprocess"` 时生效：`"pipe"`（默认 NDJSON 管道）或 `"unix_socket"`（`AF_UNIX` 局部套接字） |
| `config_schema` | `ConfigField[]` | 否 | 声明式配置项数组；当插件未重写自定义设置页时，插件市场据此自动生成原生 Borealis 设置表单 |

#### 推荐权限标签枚举（`permissions`）
- `config:read` / `config:write`：读写 `~/.config/omnidesk/plugins/<id>.json`
- `secret:vault`：读写 `0600` 权限的 `SecretVault` 隔离凭据项
- `network:loopback`：仅访问本机回环端口（如 `127.0.0.1:3425` Magpie 网关）
- `network:lan`：访问局域网私有服务（如飞牛 NAS / Radarr / Sonarr，强制禁用系统代理）
- `network:wan`：访问公网 HTTPS 接口（如 Bilibili / YouTube / arXiv / Linux.do / Reddit）
- `process:magpie`：调用本机 `magpie` CLI 读取状态与更新专属路由组
- `process:chrome`：拉起受管 Chrome `--app` 阅读窗口
- `process:funasr`：调用本机 `llama-funasr-sensevoice` 执行离线语音识别
- `process:ffmpeg`：调用本机 `ffmpeg` / `ffprobe` 进行音轨探测与切片提取
- `gpu:vulkan`：允许推理子进程访问本机 Vulkan GPU 设备
- `fs:user_mount`：按用户显式配置的路径读写本地目录或 NAS 挂载点

#### `config_schema` 单项结构
- `type = "string" | "bool" | "int" | "select"`：持久化至 `0600` 权限的 `ConfigStore`（`~/.config/omnidesk/plugins/<plugin_id>.json`）；当 `type == "select"` 时，`options` 数组必须非空。
- `type = "secret"`：**绝不写入普通配置文件或日志**，由插件市场表单直接存入 `0600` 权限的 `SecretVault`（`ctx.getSecret(key)` / `ctx.setSecret(key, val)`）。

### 2.2 插件市场目录索引（`catalog.json`）与 CLI 管理协议

市场支持内置官方目录（[`host/resources/marketplace/catalog.json`](../host/resources/marketplace/catalog.json)）、远程 HTTPS JSON 源合并（`MarketplaceCatalog::mergeRemoteUrl`）以及直接从任意 GitHub 简写 `<owner>/<repo>` 或本地目录安装：

```bash
omnidesk-pm catalog --domain core                     # 浏览市场目录
omnidesk-pm install plugin-magpie-agent               # 按市场 ID 一键拉取 GitHub 仓库、编译、校验并启用
omnidesk-pm install ZJFILOVE/omnidesk-plugin-fnos-nas # 直接按 GitHub owner/repo 安装任意独立仓库插件
omnidesk-pm install ~/Projects/omnidesk-plugin-dev    # 本地开发目录安装验证
omnidesk-pm config plugin-fnos-nas set mount_root /mnt/feiniu-smb
omnidesk-pm config plugin-fnos-nas set-secret api_token "<secret>"
omnidesk-pm abi-status --strict                       # 检查全部已安装 .so 的 SDK ABI 布局签名是否过期
omnidesk-pm rebuild --stale                           # 按当前宿主 SDK 一键批量重编 ABI 过期插件
omnidesk-pm update --all                              # 检查远端 commit 并在 /tmp 冒烟通过后原子升级
omnidesk-pm rollback plugin-magpie-agent              # 1 秒回滚至上一备份版本
```

---

## 三、C ABI v1 二进制握手、`sdk_abi_signature` 与故障隔离契约

### 3.1 C ABI 握手层、布局指纹与子进程 RPC（[`sdk/include/omnidesk/plugin_abi.h`](../sdk/include/omnidesk/plugin_abi.h)）

宿主与 `omnidesk-pm` / `omnidesk-plugin-worker` 通过 `dlopen(..., RTLD_NOW | RTLD_LOCAL)` 加载插件 `.so` 后，在调用任何 `omnidesk_plugin_create()` 实例化 C++ 对象**之前**，只首先调用纯 C 符号 `omnidesk_plugin_abi_info()` 并校验：
- `info->abi_version == OMNIDESK_PLUGIN_ABI_VERSION`（`1u`）
- `info->struct_size >= sizeof(OmniDeskPluginInfo)`（安全拒绝更小的旧结构体 `.so`，杜绝越界读与段错误）
- `info->sdk_abi_signature` 非空且与宿主 `omnidesk::sdkAbiSignature()` 完全一致。CMake 对 SDK/Borealis 公共头文件生成 SHA-256 摘要，头文件变化自动触发重新配置；编译期指纹进一步包含类型大小、对齐、接口修订、编译器/标准库 ABI 和 nlohmann JSON 版本及选项。同大小字段改动、虚函数调整也会改变签名，无需依赖手动更新 `ABI_VERSION`。此策略偏保守，头文件注释改动也可能触发重编。
- `info->id` 与 `plugin.json` 中的 `id` 完全一致

宏生成的握手数据和签名均为编译期常量，调用入口时不构造 SDK 对象、不分配字符串内存。安装、`abi-status` 和批量重编的二进制检查一律由独立、限时的 `omnidesk-pm abi-check` 进程执行；检查器缺失时直接失败，不退回宿主内 `dlopen`。UI 刷新只比较安装记录中的签名，实际加载与 CLI 完整检查仍校验二进制。批量重编保留插件原有启用/停用状态。

每个插件只需在 `plugin_main.cpp` 末尾调用宏导出三个标准 C 入口（`omnidesk_plugin_abi_info`、`omnidesk_plugin_create`、`omnidesk_plugin_destroy`）：

```cpp
OMNIDESK_DECLARE_PLUGIN(
    MagpieAgentPlugin,          // 继承自 omnidesk::IPlugin 的实现类
    "plugin-magpie-agent",      // 必须与 plugin.json 的 id 一致
    "Magpie AI Agent Router",   // 展示名
    "0.1.0",                    // 版本
    "provider",                 // provider | workspace | hybrid | extension
    "core"                      // core | media | text | book
)
```

对于 `"isolation": "subprocess"` 的实验性或第三方插件：
- 宿主不在主进程内 `dlopen` 该 `.so`，而是由 `PluginManager` 将其托管在 `WorkerSupervisor` 拉起的 `omnidesk-plugin-worker` 子进程（支持 `pipe` 与 `unix_socket` 传输）。
- 能力调用、流式响应（`cap.invoke`）与三大领域数据源（`WorkerMediaSource` / `WorkerTextSource` / `WorkerBookSource`）全部通过 `types_json.hpp` 纯 JSON 协议跨进程边界传输。
- 若插件子进程触发 `SIGSEGV` / `SIGABRT` 或异常退出，故障限制在 Worker 进程内，工作器会自动拉起（连续崩溃则触发熔断隔离），并通过 `"worker.fault"` 在主舞台底部显示一行黄字提示。
- 排队、写入管道/Socket、等待响应都支持超时或取消；超时和响应协议错误计入故障与熔断。单帧及累计流式文本上限为 8 MiB，stderr 最多保留 100 段、每段 4096 字节；停止 Worker 会清理同进程组子进程。启动连接最多等待 5 秒，停止还受 `stop_grace_ms` 约束。
- 自动重启不自动重放失败的插件能力调用，避免下载、写笔记等副作用重复执行。只有明确幂等的自定义 Worker 才启用 `retry_on_crash`；已向调用者交付部分流式结果后不重试。卸载/注销后，正在执行的请求不能重新拉起 Worker。

需要 NanoVG/Borealis 自定义视图时，保留受信任的进程内 UI 薄层，用 `registerWorker`、`registerWorkerCapability`、`registerWorkerMediaSource` / `registerWorkerTextSource` / `registerWorkerBookSource` 将抓取和解析交给独立 Worker。整插件 `subprocess` 模式支持能力与领域数据源，会拒绝 UI 注册；借助 `--duplex` 全双工复用信道（`WorkerHostBridge` + Worker 端 `RemoteBridge`），Worker 内部的 `CapabilityRegistry`（`has` / `providersFor` / `listAll` / 同步与流式 `call*` / 取消传播）与 `EntityBus`（`subscribe` / `unsubscribe` / `publish` 无回声广播与宿主事件推送）均已透明向宿主转发跨插件能力调用和宿主事件订阅（包含 `onLoad` 期间调用及可重入嵌套调用）。

宿主文章列表、正文、书库、书页及媒体歌词/章节的数据请求在后台执行，只有结果应用和视图构建回到 UI 线程。新请求替换旧请求、视图销毁后，旧结果不会覆盖界面。进程内异常屏障只能捕获 C++ 异常，不能隔离段错误、死锁或 `dlopen` 静态初始化崩溃；进程隔离也不等于权限沙箱或无限期稳定的 C++ ABI。

### 3.2 `IPlugin` 与 `PluginContext` 生命周期规范（[`sdk/include/omnidesk/plugin.hpp`](../sdk/include/omnidesk/plugin.hpp)）

```cpp
class IPlugin {
public:
    virtual ~IPlugin() = default;
    virtual bool onLoad(PluginContext& ctx) = 0;
    virtual void onUnload() = 0;
    virtual nlohmann::json onHealthCheck() const { return {{"ok", true}}; }
    virtual brls::View* createSettingsView(PluginContext& ctx) { return nullptr; }
};
```

**插件实现者必须遵守以下四条运行时准则**：
1. **`onLoad(PluginContext& ctx)` 必须非阻塞快速返回（建议 `< 50 ms`）**：严禁在 `onLoad` 主线程内同步发起长耗时网络请求或阻塞式外部进程调用；探测任务应采用惰性按需缓存或后台线程刷新。
2. **使用 `ctx` 作用域辅助方法注册资源**：优先调用 `ctx.registerCapability(...)`、`ctx.subscribe(...)`、`ctx.registerMediaSource(...)`、`ctx.registerTextSource(...)`、`ctx.registerBookSource(...)`、`ctx.registerWorkspaceTab(...)`、`ctx.registerDockWidget(...)`。它们会自动绑定 `ctx.pluginId()`，卸载时由宿主统一回收。
3. **`onUnload()` 线程收敛铁律**：在 `onUnload()` 返回前，插件**必须终止或 `join()` 其创建的全部后台线程/子进程**，确保卸载 `dlclose()` 后不存在悬空回调访问已释放的代码段。
4. **UI 线程亲和性**：Borealis / NanoVG 视图树仅允许在主渲染线程读写。若插件后台线程需更新自定义视图，必须通过 `brls::sync([...] {})` 投递回主线程。

---

## 四、核心总线协议：`CapabilityRegistry` 与 `EntityBus`

### 4.1 `CapabilityRegistry` 统一调用信封

- **调用方**：
  ```cpp
  if (ctx.capabilities().has("llm.chat.v1")) {
      auto res = ctx.capabilities().call("llm.chat.v1", {
          {"domain", "text"},
          {"context", article.title},
          {"prompt", "提炼核心论点与方法局限"}
      });
      if (res.ok) {
          std::string reply = res.data.value("reply", "");
      }
  }
  ```
- **返回结构 `CapabilityResult`**：
  - `ok` (`bool`)：业务执行是否成功。
  - `data` (`nlohmann::json`)：成功时返回结构化结果对象；失败时可附带 `{"error_code": "..."}`。
  - `error` (`std::string`)：面向人类可读的中文简明错误说明（直接可供 UI 展示，**严禁混入密钥或完整 Token**）。
  - `provider_plugin_id` (`std::string`)：由 `CapabilityRegistry` 自动填充实际响应的插件 ID。
- **标准 `data.error_code` 枚举**：
  - `"not_configured"`：用户尚未填写必要路径或凭据
  - `"service_offline"`：本机守护进程（如 Magpie `3425`）或远端 NAS 不可达
  - `"rate_limited"`：上游 429 限流
  - `"quota_exceeded"`：当前订阅窗口或额度耗尽
  - `"invalid_argument"`：请求缺少必填 JSON 字段
  - `"timeout"`：超时取消

### 4.2 全平台标准能力 ID 矩阵

| 能力 ID | 提供者插件（示例） | 消费方工作台 / 插件 | 核心职责 |
| :--- | :--- | :--- | :--- |
| `llm.router.v1` | `plugin-magpie-agent` | 宿主 `AiCompanionDock`、设置中心 | 读取 Magpie 状态/Harness/模型/配额，并将用户所选路由反向写入专属路由组 |
| `ai.agent.v1` | `plugin-magpie-agent` | 宿主 `AiCompanionDock`、三大工作台 | 带领域上下文与预设动作（总结/问答/词源/拆句）的智能伴读对话入口 |
| `ai.curation.v1` | `plugin-magpie-agent` | 宿主 `AiCompanionDock`、三大工作台、`omnidesk-pm rpc` | 内驻 AI Agent 策展编排器：基于统一 Skill 执行按需可重复交互推荐（`curate_interactive`）或每日不重复管线（`run_daily_pipeline`）并自动联动反写 |
| `llm.chat.v1` | `plugin-magpie-agent` | 全域工作区插件 | 通用 OpenAI 兼容多轮/单轮文本推理与 JSON Schema 结构化输出 |
| `llm.translate.v1` | `plugin-magpie-agent` | 字幕/网页/期刊/PDF/EPUB 插件 | 支持上下文窗口与术语锁定（Glossary）的单段/批量双语翻译 |
| `recommend.feed.v1` | `plugin-ai-curator` | 三大工作台、`plugin-magpie-agent`、外部入站 Agent | 自动克隆同步 `ZJFILOVE/obsidian-vault` 与 `ZJFILOVE/music-daily` 回灌全量历史推荐，并提供 `daily_immutable`（每日不重复）与 `interactive_ephemeral`（按需可重复）双模前端反写存储 |
| `media.playlist.v1` | `plugin-music` | `plugin-ai-curator`、`plugin-magpie-agent`、外部入站 Agent | Apple Music 国区曲库可播预检、经 QA 门禁同步固定累计歌单 `p.YJXV7dEIeb3GzZX`（`ChatGPT Daily 5`），以及受硬隔离保护的可重复灵感歌单 / `MPVCore` 播放队列写回 |
| `speech.asr.v1` | `plugin-asr-funasr` | `plugin-bilibili` / `youtube` / `MediaWorkbench` | 本地 Vulkan/CPU FunASR SenseVoiceSmall Q8 离线语音识别、音轨探测与断点分块转写 |
| `player.timeline.v1` | `plugin-sponsorblock` | `MediaWorkbench` / `plugin-bilibili` / `plugin-youtube` | 基于 B 站 (`bsbsb.top`) 与 YouTube (`sponsor.ajay.app`) K-匿名哈希前缀查询空降/章节片段并合并进播放器时间轴 |
| `subtitle.fetch.v1` | `plugin-sub-hunter` | `MediaWorkbench` / `plugin-fnos-nas` / `plugin-youtube` | SubHD 浏览器抓取、ASSRT API 检索下载（`status`/`probe`/`derive_query`/`search`/`download`）、16 MB 安全解压与多编码 UTF-8 转换 |
| `subtitle.sync.v1` | `plugin-sub-hunter` | `MediaWorkbench` / `plugin-fnos-nas` | `ffsubsync` 音轨 VAD / 参考轴自动对齐（`status`/`probe`/`shift`/`sync`）与内置线性偏移/倍率缩放 |
| `nas.connection.v1` | `plugin-fnos-nas` | 音乐、PDF、EPUB、搜书插件 | 探测飞牛连接状态、列出受管媒体库/书库挂载根与受保护原子写回 |
| `media.source.v1` | `plugin-bilibili` / `youtube` / `fnos-nas` / `plugin-music` / `plugin-ai-curator` | `MediaWorkbench` | 通过 `IMediaSource` 向影视音乐工作台注入视频流、番剧、NAS 原盘、Apple Music 无损曲库及 AI 推荐流 |
| `media.local_match.v1` | `plugin-fnos-nas` | `plugin-bilibili` / `plugin-arr-imdb` | 根据片名 / IMDb / TMDB / 剧集季集号查询飞牛 NAS 是否已有本地高码率片源 |
| `media.clip_jump.v1` | `plugin-fnos-nas` | `BookWorkbench` / `plugin-ebook` | 在本地美剧/电影双语字幕库检索英文单词或词组，返回匹配对白与一键跳播原片时间戳 |
| `web.embed.v1` / `web.extract.v1` / `web.cookies.v1` / `web.fetch.v1` | `plugin-chrome-web` | `plugin-journals` / `plugin-feed-recipe` / `plugin-blog` / `zhihu` / `linuxdo` / `shuiyuan` / `v2ex` / `reddit` / `x` | 拉起托管无边框 Chrome `--app` 登录/阅读窗、提取挑战后正文 DOM、同步指定域名真实登录态 Cookie，或代表插件在 Chrome 上下文执行同源 `fetch` |
| `feed.recipe.v1` | `plugin-feed-recipe` | `TextWorkbench`、配方工坊与设置面板 | 管理与热重载 20 行 JSON/Lua 声明式站点配方，联合 Chrome Cookie 过验证、双语对照翻译与折叠评论树 |
| `blog.subscription.v1` | `plugin-blog` | `TextWorkbench`、博主订阅页、`AiCompanionDock` | 管理用户自助收藏的个人博客（增删改查/刷新），支持像挑 UP 主一样点进单个博客拉取文章流（含 Chrome SPA/付费墙抽取） |
| `text.source.v1` | `plugin-feed-recipe` / `plugin-blog` / `zhihu` / `linuxdo` / `shuiyuan` / `v2ex` / `reddit` / `x` / `journals` / `plugin-ai-curator` | `TextWorkbench` | 通过 `ITextSource` 提供文章流、公式代码块段落、嵌套评论树与 AI 多栏目导读（`category == "multi"`） |
| `book.source.v1` | `plugin-pdf-babeldoc` / `plugin-ebook` / `plugin-ai-curator` | `BookWorkbench` | 通过 `IBookSource` 提供书架、双语分页 `BookSpread` 与 `VocabCard` |
| `book.bilingual_pdf.v1` | `plugin-pdf-babeldoc` | `plugin-journals` / `plugin-zlib` / `plugin-annas-archive` | 提交本地 PDF 生成 BabelDOC 双栏对照文档并回流书架 |
| `book.sync.v1` | `plugin-fnos-nas` | `plugin-ebook` / `plugin-zlib` / `plugin-annas-archive` | 跨设备同步阅读进度、句段精读笔记与电子书资产至飞牛 NAS |
| `vocab.sm2.v1` | `plugin-ebook` | `TextWorkbench` / `MediaWorkbench` / `plugin-ai-curator` | 划词入库、词根词源缓存查询与 SM-2 间隔重复打分更新 |

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
| `"plugin.state_changed"` | 插件市场完成安装、更新、启用、禁用或回滚 | `{"plugin_id": "plugin-fnos-nas", "state": "loaded" \| "unloaded" \| "updated"}` |
| `"plugin.fault"` | 进程内能力调用、域源接口或总线订阅抛出异常被宿主隔离拦截后 | `{"plugin_id": "plugin-demo", "component": "capability:...", "error": "..."}` |
| `"worker.fault"` | `subprocess` 隔离工作进程崩溃（`SIGSEGV` 等）、重启或连续崩溃触发熔断隔离 | `{"worker_id": "plugin:...", "plugin_id": "...", "signal_name": "SIGSEGV", "crash_count": 1, "restart_succeeded": true, "quarantined": false, "error": "..."}` |
| `"plugin.abi_drift"` | 宿主启动或状态校验检测到已安装 `.so` 与当前 SDK ABI 布局签名不一致 | `{"count": 1, "stale_plugins": ["plugin-bilibili"], "entries": [...]}` |

---

## 五、一号示范插件规范：`plugin-magpie-agent`（Magpie 路由探测与反向打穿协议）

对应市场条目：`plugin-magpie-agent`（独立仓库 `https://github.com/ZJFILOVE/omnidesk-plugin-magpie-agent.git`）。

### 5.1 核心设计约束（零全局污染 + `magpie group` 反向打穿 + `3425` 网关直连）

1. **默认前置条件**：默认用户本机已安装并运行 [Magpie](https://usemagpie.ai)（CLI `magpie` 位于 `PATH` 或 `~/.local/bin/magpie`，本地网关信息记录于 `~/.config/magpie/gateway.json`，默认监听 `http://127.0.0.1:3425`）。
2. **严禁改动终端各 Harness 的全局默认绑定**：
   - **禁止执行** `magpie codex <model>`、`magpie antigravity-cli <model>`、`magpie claude <model>`、`magpie pi <model>` 等命令，以免干扰用户正在终端中运行的日常编码会话（如 `~/.codex/config.toml`、`~/.gemini/antigravity-cli/settings.json`）。
   - **禁止直接编辑** `~/.config/magpie/providers.json` 或 `library.json`（必须通过 `magpie` CLI 原子落盘校验）。
   - **禁止调用** `magpie quota reset`（会消耗账号宝贵的限额重置次数）。
3. **专属路由组反向打穿机制**：
   - 插件在 Magpie 中维护属于 OmniDesk 自己的专属路由组（默认 ID 为 `omnidesk-active`，对应的模型路由标识为 `group/omnidesk-active`）。
   - 用户在 OmniDesk 界面选择任意已接入 Magpie 的**底层 Harness 订阅源 / Provider（如 `codex`、`antigravity`、`zcode`、`zhipu`，或直接复用已有路由组 `group/smart-split`）+ 模型 + 推理强度（Effort）+ 备选回退链**后，插件通过 `magpie group` 命令将该选择实时反向写入 `group/omnidesk-active`。
   - 所有 OmniDesk 内部能力（`ai.agent.v1`、`llm.chat.v1`、`llm.translate.v1`）统一通过 HTTP POST `http://127.0.0.1:3425/v1/chat/completions` 并指定 `"model": "group/omnidesk-active"` 完成调用。

### 5.2 本机 Magpie 信息读取协议（Read Path）

插件通过无 Shell 的参数数组子进程（`execvp`）与本地回环 HTTP 读取四类实时元数据（解析 CLI 输出时须忽略首行可能出现的 `YYYY/MM/DD HH:MM:SS plugin: ...` 日志输出）：

1. **网关存活与地址探测**：
   - 优先读取 `~/.config/magpie/gateway.json` 或执行 `magpie status --json`：
     ```json
     {
       "running": true,
       "url": "http://127.0.0.1:3425",
       "addr": "127.0.0.1:3425",
       "role": "serve",
       "pid": 969,
       "version": "0.1.4043"
     }
     ```
2. **已检测 Harness 与订阅生态列表**：
   - 执行 `magpie ls` 读取本机已检出的 Harness（如 `Codex`、`Antigravity CLI`、`Antigravity`、`Pi`、`DeepSeek Harness`、`ZCode`）及其当前状态，映射出本机已激活的底层生态分组（`codex/*`、`antigravity/*`、`zcode/*`、`zhipu/*` 等），供前端展示「按底层 Harness 筛选模型」。
3. **全量可选模型、已有路由组与推理强度（Effort）解析**：
   - 执行 `magpie models`，按缩进分组头（`Routing groups`、`Codex`、`Antigravity`、`Zhipu GLM`、`ZCode` 等）提取：
     - `id`：如 `"group/smart-split"`、`"codex/gpt-6-luna"`、`"antigravity/gemini-4-carbon"`、`"zhipu/glm-5.3-flash"`
     - `display_name`：如 `"GPT-6-Luna"`、`"Gemini 4 Carbon"`
     - `supported_efforts`：解析行尾斜杠分隔档位，如 `["low", "medium", "high", "xhigh", "max"]`
4. **订阅窗口余量与余额读取**：
   - 直接请求 `GET http://127.0.0.1:3425/v1/magpie/quotas`（或回退执行 `magpie quota --json`），读取各订阅的 5 小时 / 周度窗口剩余百分比（`windows[].remaining`、`resetsAt`）及 API Key 余额（`balance`），用于在路由选择器旁标注绿色/琥珀色余量徽章。

### 5.3 反向操控 Magpie 路由组协议（Write-Back Path）

当用户在 OmniDesk 选择目标模型与路由参数时：
1. **构造 Magpie 成员表达式（Member Spec）**：
   - 若用户选择具体 Provider/Harness 下的模型：按 Magpie 语法拼接 `<provider>/<model>[:<effort>][:fast][#<account>]`，例如：
     - `antigravity/gemini-4-carbon:high`
     - `codex/gpt-6-luna:low`
     - `codex/gpt-6-astra:xhigh#work`
   - 若用户直接选择复用已有的 Magpie 路由组（如 `group/smart-split` 或 `group/quota-gemini-flash-first`）：保留原样 `group/<id>`（嵌套深度不超过 Magpie 限制的 8 层，且严禁将 `group/omnidesk-active` 自身放入成员列表）。
   - 将主选成员与可选 `fallbacks` 用英文逗号拼接为 `<models_csv>`。
2. **执行 `magpie group` 反向写回**：
   - 检查 `magpie groups` 中是否已存在 `group/omnidesk-active`：
     - **首次创建**：
       ```text
       magpie group add "OmniDesk 工作台路由" id=omnidesk-active models=<models_csv> routing=<routing> stays=<stays>
       ```
     - **更新已有组**（使用 `group set` 幂等覆盖，避免同名递增编号）：
       ```text
       magpie group set omnidesk-active models=<models_csv> routing=<routing> stays=<stays>
       ```
     - **若 `routing == "manual"`**（强制锁定单模型、禁止自动漂移回退）：追加执行：
       ```text
       magpie group pick omnidesk-active <primary_member>
       ```
3. **广播路由变更事件**：
   - 持久化当前选择至 `ConfigStore`，并立即向 `EntityBus` 发布 `"entity.ai.route_changed"`，驱动宿主 `AiCompanionDock` 顶部状态栏即时刷新。

### 5.4 `plugin-magpie-agent` 标准能力 RPC 契约

#### (1) `llm.router.v1` —— 路由查询与反向操控接口
- **查询当前路由摘要**（宿主 [`companion_dock.cpp`](../host/src/companion_dock.cpp) 启动与展开时自动调用）：
  - Request:
    ```json
    { "action": "current_route" }
    ```
  - Response (`CapabilityResult.data`):
    ```json
    {
      "summary": "group/omnidesk-active → antigravity/gemini-4-carbon:high",
      "gateway_url": "http://127.0.0.1:3425",
      "gateway_running": true,
      "group_id": "omnidesk-active",
      "harness_provider": "antigravity",
      "primary_member": "antigravity/gemini-4-carbon:high",
      "routing": "order",
      "stays": "turn",
      "fallbacks": ["group/quota-gemini-flash-first", "codex/gpt-6-luna:low"]
    }
    ```
- **枚举 Harness / 模型 / 配额**：
  - `{"action": "list_harnesses"}` → 返回 `{"harnesses": [{"name": "Codex", "provider": "codex", "detected": true, "current_target": "group/smart-split"}, ...]}`
  - `{"action": "list_models"}` → 返回 `{"groups": [...], "models": [{"id": "codex/gpt-6-luna", "provider": "codex", "name": "GPT-6-Luna", "efforts": ["low","medium","high","xhigh","max"]}, ...]}`
  - `{"action": "list_quotas"}` → 透传并归一化 `/v1/magpie/quotas` 窗口剩余百分比。
- **反向修改路由（`set_route`）**：
  - Request:
    ```json
    {
      "action": "set_route",
      "group_id": "omnidesk-active",
      "harness_provider": "codex",
      "model_id": "codex/gpt-6-luna",
      "effort": "medium",
      "routing": "order",
      "stays": "turn",
      "fallbacks": ["antigravity/gemini-4-hydrogen:low"]
    }
    ```
  - Response (`CapabilityResult.data`)：同 `current_route` 最新快照。

#### (2) `ai.agent.v1` 与 `llm.chat.v1` —— 跨领域统一推理接口
- Request（完全兼容宿主 `AiCompanionDock::submitPrompt`）：
  ```json
  {
    "action": "chat",
    "domain": "media",
    "context": "BV1heam6TExz · Linux 内核调度器深度解析",
    "system_prompt": "可选领域系统提示词",
    "prompt": "生成视频时间轴章节摘要",
    "route_override": "",
    "timeout_ms": 60000
  }
  ```
- 网关转发行为：向 `http://127.0.0.1:3425/v1/chat/completions` 发送 JSON（显式设置 `CURLOPT_NOPROXY = "127.0.0.1,localhost"`；若 `SecretVault` 中配置了可选 `gateway_key` 则附带 `Authorization: Bearer <key>`），`model` 字段默认填 `"group/omnidesk-active"`。
- Response (`CapabilityResult.data`)：
  ```json
  {
    "reply": "00:00 引言与背景...\n03:40 CFS 与 EEVDF 核心差异...",
    "served_model": "antigravity/gemini-4-carbon",
    "route_group": "group/omnidesk-active",
    "usage": {
      "prompt_tokens": 1420,
      "completion_tokens": 385,
      "total_tokens": 1805
    }
  }
  ```

### 5.5 `plugin-magpie-agent` 标准 `plugin.json` 参考定义

```json
{
  "schema_version": 1,
  "id": "plugin-magpie-agent",
  "name": "Magpie AI Agent Router",
  "version": "0.1.0",
  "abi_version": 1,
  "kind": "provider",
  "domain": "core",
  "author": "OmniDesk",
  "description": "探测本机 Magpie 已接入的 Harness、模型、路由组与实时额度，通过专属路由组反向打穿模型选择并统一走 127.0.0.1:3425/v1 网关提供全域 AI 能力。",
  "homepage": "https://github.com/ZJFILOVE/omnidesk-plugin-magpie-agent",
  "repository": {
    "type": "git",
    "url": "https://github.com/ZJFILOVE/omnidesk-plugin-magpie-agent.git",
    "branch": "main"
  },
  "build": {
    "system": "cmake",
    "entry_target": "plugin_magpie_agent",
    "output_lib": "libplugin_magpie_agent.so",
    "cmake_args": [],
    "verify_cmd": "ctest --output-on-failure"
  },
  "provides": ["ai.agent.v1", "llm.router.v1", "llm.chat.v1", "llm.translate.v1"],
  "consumes": [],
  "permissions": ["config:read", "config:write", "secret:vault", "network:loopback", "process:magpie"],
  "config_schema": [
    {
      "key": "magpie_bin",
      "label": "Magpie CLI 路径",
      "type": "string",
      "default": "magpie",
      "description": "默认从 PATH 查找 magpie，亦可指定绝对路径",
      "required": true
    },
    {
      "key": "gateway_url",
      "label": "Magpie 本地网关地址",
      "type": "string",
      "default": "http://127.0.0.1:3425",
      "description": "留空或默认时自动同步 ~/.config/magpie/gateway.json",
      "required": true
    },
    {
      "key": "route_group_id",
      "label": "OmniDesk 专属路由组 ID",
      "type": "string",
      "default": "omnidesk-active",
      "description": "仅修改此路由组，绝不改动终端 Codex / AGY 全局绑定",
      "required": true
    },
    {
      "key": "routing_strategy",
      "label": "路由调度策略",
      "type": "select",
      "default": "order",
      "options": ["manual", "order", "smart", "usage", "pace"],
      "description": "manual 严格锁定所选单模型；order 按主模型+回退链顺延；smart 优先最早重置窗口",
      "required": true
    },
    {
      "key": "gateway_key",
      "label": "Magpie Gateway Key（可选）",
      "type": "secret",
      "default": "",
      "description": "仅当本机网关开启鉴权或连接局域网共享 Magpie 时填写（0600 SecretVault 存储）",
      "required": false
    }
  ]
}
```

---

## 六、二号示范插件规范：`plugin-fnos-nas`（解耦式飞牛 fnOS 连接器协议）

对应市场条目：`plugin-fnos-nas`（独立仓库 `https://github.com/ZJFILOVE/omnidesk-plugin-fnos-nas.git`）。

### 6.1 解耦与「用户自主连接」设计原则
1. **零私密拓扑假设**：宿主与插件代码严禁硬编码 `/mnt/feiniu-smb`、任何局域网固定 IP 或默认端口凭据。如何挂载或打通网络（如局域网 SMB/NFS/WebDAV 挂载、Tailscale/WireGuard 组网、飞牛 WebDAV/API 映射）完全交由用户自主配置。
2. **渐进式三级连接能力**（用户填哪一级即激活对应能力，互不强绑）：
   - **L1 本地挂载目录直通（`mount_root`）**：用户填入自己已挂载好的飞牛媒体/书库本地路径（如 `/mnt/feiniu-smb` 或 `~/NAS`）。插件校验路径可读且无 `..` 越界后，立即激活 `IMediaSource` 原盘硬解播放、`nas.connection.v1` 与受保护字幕写回。
   - **L2 伴随检索与同步接口（可选 `media_service_url` / `companion_sync_url`）**：对接用户部署在飞牛上的字幕台词索引或电子书进度同步服务（如 `8765` / `8766`），激活毫秒级 `media.clip_jump.v1`（读原版书点单词跳播美剧对应台词片段）与 `book.sync.v1`。
   - **L3 Arr 媒体库只读核验（可选 `radarr_url` / `sonarr_url` + `SecretVault` API Key）**：激活精确的 `media.local_match.v1` 片源入库对比。
3. **局域网与文件安全护栏**：
   - 所有局域网 HTTP(S) 请求强制设置 `CURLOPT_NOPROXY = "*"`（防止 API Key 泄露至环境变量代理）且禁止跟随重定向转发认证头（`CURLOPT_FOLLOWLOCATION = 0L`）。
   - 向 NAS 写回双语字幕时，必须采用同目录临时文件 `.<name>.omnidesk.tmp` + `rename` 原子替换，且默认拒绝覆盖非 OmniDesk 生成的人工精校 `.ass`/`.srt` 字幕。

### 6.2 `plugin-fnos-nas` 标准能力 RPC 契约

1. **`nas.connection.v1`**：
   - Request: `{"action": "status" | "probe"}`
   - Response `data`:
     ```json
     {
       "connected": true,
       "mount_root": "/mnt/feiniu-smb",
       "mount_writable": true,
       "video_dirs": ["/mnt/feiniu-smb/Movies", "/mnt/feiniu-smb/TV"],
       "book_dir": "/mnt/feiniu-smb/Books",
       "clip_search_ready": true,
       "arr_linked": true
     }
     ```
2. **`media.local_match.v1`**（查询某部影视是否已在飞牛入库）：
   - Request: `{"title": "Severance", "year": 2022, "imdb_id": "tt11280740", "season": 1, "episode": 1}`
   - Response `data`:
     ```json
     {
       "matched": true,
       "item_id": "fnos:tv:severance-s01e01",
       "play_url": "file:///mnt/feiniu-smb/TV/Severance/S01E01.mkv",
       "quality_label": "4K DV HEVC",
       "has_bilingual_sub": true
     }
     ```
3. **`media.clip_jump.v1`**（书本/生词工作台「美剧原片台词检索与一键跳播」）：
   - Request: `{"query": "spill the beans", "limit": 5, "auto_open_player": true}`
   - Response `data`:
     ```json
     {
       "clips": [
         {
           "series": "Breaking Bad S03E06",
           "line_en": "Don't spill the beans before we're ready.",
           "line_zh": "在我们准备好之前别走漏风声。",
           "play_url": "file:///mnt/feiniu-smb/TV/Breaking.Bad/S03E06.mkv",
           "start_sec": 1248.2,
           "end_sec": 1255.0
         }
       ]
     }
     ```
   - 当 `auto_open_player == true` 且存在命中片段时，插件同时向 `EntityBus` 发布 `"entity.media.open"`，宿主自动切至影视舞台定位到 `start_sec` 播放原片语境。
4. **`book.sync.v1`**（电子书书架与伴读进度双端同步）：
   - Request: `{"action": "pull_progress" | "push_progress", "book_id": "...", "chapter_id": "...", "page": 42, "updated_at": "2026-11-14T10:30:00Z"}`
   - Response `data`: `{"synced": true, "resolved_page": 42, "nas_path": "/mnt/feiniu-smb/Books/omnidesk-sync.json"}`

---

## 六（续）、三号示范插件规范：`plugin-asr-funasr`（本地 FunASR 语音转写协议）

对应市场条目：`plugin-asr-funasr`（独立仓库 `https://github.com/ZJFILOVE/omnidesk-plugin-asr-funasr.git`，`kind = "provider"`，`domain = "core"`）。

### 6.3 核心设计约束（按需安装 + 零宿主耦合 + Vulkan/CPU 回退 + 断点续传）

1. **二进制与模型零硬编码**：插件清单默认值禁止写死用户本机模型路径；优先读取 `config_schema` 配置（`runtime_bin`、`model_path`、`vad_path`），次级读取 `OMNIDESK_FUNASR_*` 环境变量及 `~/.local/share/omnidesk/models/funasr/`。未配置时 `onLoad()` 仍 `< 5ms` 正常完成并在调用转写时返回标准 `error_code = "not_configured"`。
2. **安全子进程调用**：通过 `fork()` + `execvp()` 直接传递参数数组（禁止 `sh -c`），配置独立进程组以支持秒级取消与超时清理；内存 PCM（`pcm_s16le_base64`）经 `memfd_create` + `/proc/self/fd/<fd>` 无盘直传 FunASR，自动去除 `<|zh|>` / `<|NEUTRAL|>` 等 SenseVoice 标签（可设 `keep_tags: true` 保留）。
3. **长媒体断点续传**：`transcribe_media` 通过 `ffprobe` 识别音轨并按 `chunk_seconds` 切片提取 16kHz 单声道 WAV，每片完成后原子更新 `.checkpoint.json` 并向 `EntityBus` 广播 `"asr.job.progress"`；损坏或参数不匹配的缓存自动清空重算。

### 6.4 `speech.asr.v1` 标准能力 RPC 契约

- 通过 `ctx.registerStreamingCapability("speech.asr.v1", ...)` 注册（卸载时 `onUnload()` 自动调用 `unregisterCapability` 注销并安全排空 `AsyncExecutor`）：同时兼容同步 `call()`、协作取消 `callCancellable()` / `callAsync()` 及流式 `callStream()` / `callStreamAsync()`。
- **协作取消语义**：通过 `CancelToken`、内部 `action = "cancel"` 或 `onUnload()` 停机触发取消时，均立即终止底层 `ffprobe` / `ffmpeg` / `funasr` 独立进程组并统一返回 `CapabilityResult.cancelled == true` 与 `data.error_code = "timeout"`（真实超时则保持 `cancelled == false`）。
- **流式 `StreamChunk` 契约**（调用方推荐经宿主 `JobCenter` 绑定 `job.cancelToken()` 与 `job.reportProgress()`）：
  - `transcribe_audio` 发送 `event = "stage"`（`done = false`、`delta = ""`、`data: { job_id, stage: "normalizing" | "transcribing", progress, message }`）。
  - `transcribe_media` 在存在历史断点或 100% 缓存命中时先发送 `event = "resumed"`（`data: { job_id, media_path, resumed_chunks, completed_chunks, total_chunks, progress, cues[] }`；全新任务不发送），随后每完成一片发送 `event = "chunk"`（`data: { job_id, media_path, chunk_index, completed_chunks, total_chunks, progress, cues[] }`）。
  - 调用结束由 SDK 统一追加终态块 `done = true`（`event = "done"` / `"cancelled"` / `"error"`，完成时 `data` 含完整结果载荷）。
- **`action = "status" | "probe"`**：检查 `runtime_bin` / `model_path` / `vad_path` / `ffmpeg` / `ffprobe` 就绪状态与运行配置（`probe` 额外执行 `--help` 冒烟检测）。
- **`action = "list_streams"`**：传入 `media_path`，返回容器时长 `duration_sec` 与全部音轨列表 `audio_streams[]`（含 `audio_index`、`codec`、`channels`、`sample_rate`、`language`）；支持 `CancelToken` 协作取消并中断 `ffprobe`。
- **`action = "transcribe_audio"`**：传入 `audio_path` 或 `pcm_s16le_base64`（可选 `sample_rate`、`channels`、`normalize`、`backend`、`keep_tags`、`offset_ms`、`output_srt_path`），返回 `cues[]`、`srt`、`text`、`backend_used`、`fallback_used`。
- **`action = "transcribe_media"`**：传入 `media_path`（可选 `audio_stream`、`chunk_seconds`、`vad_maxseg_ms`、`reset_cache`、`output_srt_path`、`job_id`），分块识别并原子输出规范 `.srt`，同时发布 `"asr.job.progress"` 与 `"asr.job.completed"` 事件。
- **`action = "cancel" | "clear_cache"`**：取消运行中的 `job_id`（`SIGTERM -> SIGKILL` 清理子进程组并置位 `cancelled = true`）或清理断点缓存文件。

### 6.5 `player.timeline.v1` 标准能力 RPC 契约（`plugin-sponsorblock`）

对应市场条目：`plugin-sponsorblock`（独立仓库 `https://github.com/ZJFILOVE/omnidesk-plugin-sponsorblock.git`，`kind = "extension"`，`domain = "media"`）。

- **`action = "fetch_segments" | "query" | "merge_chapters"`**：传入 `video_id`（支持 BV/av 号、YouTube 11 位 ID 或完整视频 URL，可选 `platform`、`cid`、`duration_sec`、`categories[]`、`existing_chapters[]`、`force_refresh`），使用 SHA-256 前 4 位 K-匿名向 `bsbsb.top` 或 `sponsor.ajay.app` 拉取空降片段并按投票/锁定状态过滤，返回 `segments[]` 与合并排序后的 `chapters[]`（标准 `ChapterSegment` 数组，`kind = "sponsor" | "ad" | "content"`）。
- **`action = "check_position"`**：传入 `position_sec` 与 `segments[]`（或 `video_id`），判断当前时间是否处于可跳过区间并返回 `active`、`skip_to_sec`、`auto_skip`。
- **`action = "status" | "clear_cache"`**：查看当前启用平台、订阅分类、缓存条目数或清空内存 TTL 缓存。

---

## 七、三大领域前端工作台 UI 规范与插件视图扩展协议

所有工作台与插件 UI 统一继承 `native-wiliwili` **「边看边读（Paper & Graphite）」** 视觉与键盘交互规范，由 `libomnidesk_sdk.so` 中的 `omnidesk::ui`（[`sdk/include/omnidesk/ui_theme.hpp`](../sdk/include/omnidesk/ui_theme.hpp)）与 `DomainRegistry`（[`sdk/include/omnidesk/domain_registry.hpp`](../sdk/include/omnidesk/domain_registry.hpp)）承载。

### 7.1 全局视觉 Token、响应式伴读栏与键盘契约

1. **语义色 Token（严禁在插件代码中硬编码裸 RGB）**：
   - 画布与层次：`brls/background`（浅 `#F6F5F3` / 深 `#181718`）、`desktop/surface`（浅 `#FCFBF9` / 深 `#232122`）、`desktop/soft`（浅 `#F1EFEC` / 深 `#2B2729`）、`desktop/active`（浅 `#F9E7ED` / 深 `#422630`）、`desktop/line`（`1px` 细分隔线，不使用重阴影或装饰渐变）。
   - 文字与强调：`brls/text`、`font/grey`、`brls/accent`（品牌操作色），以及领域语义色 `domain/media`、`domain/text`、`domain/book`、`domain/core` 和状态色 `status/{ok,warn,err,info}_{bg,fg}`（正文字体对比度全部 `>= 4.5:1`，全局字号基准缩放 `0.82`）。
2. **无遮罩响应式 `F8` 伴读栏公式**：
   - 严禁使用遮挡主舞台的居中模态大弹窗展示总结、字幕、词源或问答。
   - 窗口宽高比 `width / height >= 1.45` 时：伴读栏停靠于**主舞台右侧**（独立分栏，不覆盖视频画面或书页）。
   - 窗口宽高比 `width / height < 1.45` 时：伴读栏自动切换为**底部上下分栏**，确保主内容舞台高度充足。
   - 插件可通过 `ctx.registerDockWidget(DockWidgetSpec{...})` 将领域专属小部件挂载进 `F8` 伴读栏中部插槽（`widget_slot_box_`）。
3. **统一键盘流规范**：
   - 全局导航：`Ctrl+1..4` 切换「影视音乐 / 文字与期刊 / 书本与PDF / 插件市场」；域内 `1..5` 切换顶部子模式页签；`/` 搜索；`F8` 展开或收起伴读栏；`Ctrl+Tab` 在主内容舞台与伴读栏之间切换焦点；`hjkl` 与 `Ctrl+D / Ctrl+U` 列表与阅读滚动；IME 中文选词（Fcitx5 / Rime）激活期间自动隔离单字母快捷键。

### 7.2 情景一：影视与音乐工作台（`ContentDomain::Media` · `IMediaSource`）

面向 **Bilibili（`plugin-bilibili`）、飞牛影院（`plugin-fnos-nas`）、YouTube（`plugin-youtube`）与 Apple Music 无损双语歌词（`plugin-music`）**：

- **插件数据接口**：实现 `omnidesk::IMediaSource` 并调用 `ctx.registerMediaSource(source)`：
  - `categories()`：返回栏目列表（如 `{"推荐", "动态", "热门", "追番", "直播"}` 或 `{"电影", "剧集", "无损音乐"}`）。
  - `fetchItems(category, query)`：返回 `std::vector<MediaItem>`（含 `kind`: `"video" | "music" | "live" | "nas"`、`quality_label`: `"4K HEVC" | "FLAC 24bit"`、`has_bilingual_sub`）。
  - `fetchChapters(item_id)`：返回 `std::vector<ChapterSegment>`（`kind`: `"content"` 普通章节 | `"ad"` / `"sponsor"` 空降/恰饭片段，供播放器进度条绘制琥珀色提示区间并支持 `F9` 一键跳过）。
  - `fetchLyrics(item_id)`：返回 `std::vector<LyricLine>`（`time_sec`、`original`、`translation`）。
- **专属工作台四子态（`MediaWorkbench`）**：
  1. **视频网格态（`Videos`）**：严格保持 `16:9` 封面比例，封面下方设独立两行标题区、创作者行与来源/画质 Pill（`Bilibili · 4K` / `飞牛 · 原盘`），窄窗自动减列不裁字。
  2. **音乐与双语歌词态（`MusicLyrics`）**：左侧曲目队列与专辑信息卡 + 右侧时间轴同步滚动的**原文/译文双行歌词舞台**，点击或选中任意句歌词可直送 `F8` 伴读栏拆解语法与文化俚语。
  3. **飞牛私有媒体库态（`NasLibrary`）**：展示 `plugin-fnos-nas` 已索引的本地影视库与连接健康度；未安装时显示一键前往市场安装引导卡。
  4. **原生播放舞台态（`PlayerStage`）**：`MPVCore` 硬解画面与右侧 `F8` 伴读栏并排，支持边看边读章节摘要、时间点一键 `seek` 跳转与视频内容追问，开关侧栏决不打断播放状态。

### 7.3 情景二：文字、社区与科学期刊工作台（`ContentDomain::Text` · `ITextSource`）

面向 **独立 Blog（`plugin-blog`）、知乎（`plugin-zhihu`）、Linux.do（`plugin-linuxdo`）、交大水源社区（`plugin-shuiyuan`）、V2EX（`plugin-v2ex`）、Reddit（`plugin-reddit`）、X（`plugin-x`）以及科学期刊网站（arXiv / Nature / Science / PubMed，`plugin-journals` + `plugin-chrome-web`）**：

- **插件数据接口**：实现 `omnidesk::ITextSource` 并调用 `ctx.registerTextSource(source)`：
  - `category()`：标识垂直类型 `"blog" | "zhihu" | "linuxdo" | "shuiyuan" | "v2ex" | "reddit" | "x" | "journal"`。
  - `channels()`：返回该来源内部的动态子栏目 `std::vector<TextChannel>`（`id`、`name`、`badge`、`requires_auth`），如知乎的 `热榜/推荐/关注/收藏夹`、Discourse 的 `最新/热门/板块`、或个人博客里每位收藏博主本身作为一个栏目（`subprocess` Worker 通过 `op = "text.channels"` 透传）。
  - `fetchFeed(query)` / `fetchFeedInChannel(channel_id, query)`：返回 `std::vector<TextFeedItem>`（含 `source_name`、`author`、`score`、`comment_count`、`tags`、`bilingual_cached`）；当用户选中第二行子栏目 Pill 时，宿主自动调用 `fetchFeedInChannel(channel_id, query)`（Worker 通过 `op = "text.fetch_feed"` 携带 `channel_id`）。
  - `fetchArticle(article_id)`：返回完整 `TextArticle`，其正文拆分为有序 `std::vector<TextParagraph>`（`kind`: `"heading" | "paragraph" | "quote" | "code" | "formula"`，分别保存 `original` 与 `translated`），评论区为递归树结构 `std::vector<CommentNode>`。
- **专属 UI 排版规则（`TextWorkbench`）**：
  1. **顶部两级来源与子栏目过滤 + 三态双语切换栏**：第一行按 `全部 | 知乎 | 个人博客 | Linux.do | 交大水源 | V2EX | Reddit | X | 科学期刊 | 配方源` 快速筛选；选中具体来源且其返回 `channels()` 时，第二行自动展开该来源的动态子栏目 Pill；阅读区顶部提供 **`双语对照（Parallel）| 仅译文 | 仅原文`** 一键切换。
  2. **分体裁自适应阅读器**：
     - **Blog / 知乎长文**：舒适纸感行宽与 `1.55` 行距，`code` 段落使用软底色等宽卡片，`quote` 段落带左边框导引。
     - **Linux.do / Reddit 社区帖**：正文下方直接挂载原生可折叠嵌套 **`CommentTreeView`**（按深度缩进并绘制纵向层级线，显示 `role_badge` 如 `OP` / `Mod` 与点赞 `score`，点击或按回车可折叠/展开任意楼中楼子树，支持单条外语高赞回复即点即译）。
     - **科学期刊（`category == "journal"`）**：顶部专属渲染**论文元数据卡**（期刊来源、发布日期、完整作者列表、`DOI` 标识，以及调用 `web.embed.v1` 在托管 Chrome 阅读窗打开原网页、或调用 `book.bilingual_pdf.v1` 转交 BabelDOC 生成双栏 PDF 的联动按钮）；正文严格保护 `formula` 公式块与上标文献引用编号 `[1]` 不被机器翻译破坏。

### 7.4 情景三：书本与文档伴读工作台（`ContentDomain::Book` · `IBookSource`）

面向 **PDF 双语排版（`plugin-pdf-babeldoc`）与 EPUB / MOBI / AZW3 / TXT 原版书伴读（`plugin-ebook`）**：

- **插件数据接口**：实现 `omnidesk::IBookSource` 并调用 `ctx.registerBookSource(source)`：
  - `listBooks(format_filter, query)`：返回 `std::vector<BookItem>`（`format`: `"pdf" | "epub" | "mobi" | "azw3" | "txt"`、`current_page` / `total_pages`、`bilingual_ready`、`vocab_count`、`nas_synced`）。
  - `fetchSpread(book_id, page_number)`：返回单跨页结构 `BookSpread`（含 `chapter_title`、`page_number`、`total_pages` 与段落块 `std::vector<BookPageBlock>`，每个块携带 `original`、`translated` 及可点查重点词列表 `highlighted_words`）。
  - `listVocab()`：返回 `std::vector<VocabCard>`（含词元 `lemma`、音标、词根词源 `etymology`、语境原句 `context_sentence` 及 SM-2 复习参数 `interval_days` / `ease_factor`）。
- **专属四子态舞台设计（`BookWorkbench`）**：
  1. **纵向 `2:3` 书架态（`Bookshelf`）**：展示封面比例卡片、阅读进度百分比、格式徽章（`PDF · 双语就绪` / `EPUB · 伴读`）与飞牛云同步状态徽章。
  2. **PDF BabelDOC 左右对照舞台（`PdfBabelDoc`）**：左栏渲染原文页块（保留 `caption` 图注与 `formula` 公式原貌），右栏严格按段落块基线对齐渲染中文译文，底部提供页码跳转与重排触发入口。
  3. **EPUB / MOBI 沉浸伴读舞台（`EpubCompanion`）**：章节内分页排版，底部重点词 chip 条支持键盘快速切词，提供三大核心联动动作：
     - **点词查词源 / 点句拆长难句**：将当前词与所在原句上下文送入 `F8` 伴读栏（调用 `ai.agent.v1`），**绝不挤压或重排当前书页**；
     - **原片对白跳播（`triggerClipJump`）**：当检测到 `plugin-fnos-nas` 提供了 `media.clip_jump.v1` 时，一键检索该词在飞牛美剧库中的真实对白并跳播对应片段；
     - **一键收入生词本**：发布 `"entity.vocab.add"` 或写入 `vocab.sm2.v1`。
  4. **SM-2 闪卡复习台（`VocabReview`）**：正面展示单词与原书真实语境原句，翻面揭晓词根词源与释义，按记忆熟练度（`重来 / 模糊 / 熟悉 / 轻松`）更新下次复习间隔。

---

## 八（零）、Web 社区插件统一登录、Cookie 同步与反爬代理规范

面向 **知乎（`plugin-zhihu`）、交大水源社区（`plugin-shuiyuan`）、Linux.do（`plugin-linuxdo`）、V2EX（`plugin-v2ex`）、Reddit（`plugin-reddit`）、X（`plugin-x`）与个人博客订阅（`plugin-blog`）** 等需要浏览器登录、SPA 动态渲染或对抗 Cloudflare / 前端动态签名的插件，统一复用 `plugin-chrome-web` 底座，不再按站点各自堆砌 Go 或独立浏览器运行时。

- **完整三层登录架构、多源 Cookie 优先级、Chrome 同源 `web.fetch.v1` 代理契约及逐站适配清单**：见独立规范文档 [`docs/WEB_AUTH_AND_SESSION_PROTOCOL.md`](WEB_AUTH_AND_SESSION_PROTOCOL.md)。
- **参考实现**：交互窗登录参考 `apple-music-online` 与 `web.embed.v1`；Cookie 同步与 `SecretVault`/`cookies.txt` 回退参考 `omnidesk-plugin-youtube`（`src/auth.cpp`）与 `omnidesk-plugin-feed-recipe`（`src/cookie_bridge.cpp`）；原生扫码持久化参考 `omnidesk-plugin-bilibili`（`src/auth.cpp`）。

---

## 八（一）、AI Agent 进驻、双仓历史推荐回灌与双模前端/歌单反写规范

面向 **内驻策展大脑（`plugin-magpie-agent` · `ai.curation.v1`）**、**外部入站编码/定时 Agent（`control.sock` · `omnidesk-pm rpc`）**、**跨三域前端反写中心（`plugin-ai-curator` · `recommend.feed.v1`）** 以及 **Apple Music 歌单写回（`plugin-music` · `media.playlist.v1`）**：

- **宿主入站控制网关、双仓（`ZJFILOVE/obsidian-vault` + `ZJFILOVE/music-daily`）自动克隆与历史推荐全量回灌、`daily_immutable`（每日不重复）与 `interactive_ephemeral`（按需可重复、零账本污染）隔离防火墙、以及 Apple Music `p.YJXV7dEIeb3GzZX` 门禁同步完整规范**：见独立规范文档 [`docs/AI_AGENT_RECOMMENDATION_PROTOCOL.md`](AI_AGENT_RECOMMENDATION_PROTOCOL.md)。
- **统一 Agent Skill 入口（供内驻 Agent 与外部 Antigravity / Codex / Claude Code 共用）**：见 [`.agents/skills/omnidesk-curation-writeback/SKILL.md`](../.agents/skills/omnidesk-curation-writeback/SKILL.md)。

---

## 八、新插件接入检查清单（Pre-Release Checklist）

后续开发任意独立插件仓库（无论是 AI 底座、飞牛存储、还是新的视频/社区/电子书站点）时，按以下 6 步即可零摩擦上架 OmniDesk 插件市场：

1. **初始化独立仓库**：运行 `omnidesk-pm init <repo-dir> --id plugin-<slug> ...` 生成符合规范的 `plugin.json` 与 `CMakeLists.txt`。
2. **核对二进制与清单字段**：确保 `OMNIDESK_DECLARE_PLUGIN` 中的 `PluginId`、`PluginKind`、`PluginDomain` 与 `plugin.json` 完全一致，`abi_version` 为 `1`；重新基于最新 SDK 编译以生成匹配的 `sdk_abi_signature`；若插件包含实验性或重型第三方依赖（如爬虫/本地推理库），建议声明 `"isolation": "subprocess"`。
3. **隔离敏感配置**：凡涉及 API Key、Cookie、NAS 密码的字段，在 `plugin.json` 的 `config_schema` 中一律声明 `"type": "secret"` 并通过 `ctx.getSecret(key)` 读取；局域网请求显式禁用代理。
4. **验证软依赖降级**：所有跨插件调用均包裹在 `if (ctx.capabilities().has("..."))` 内，底座未装时不崩溃。
5. **本地全链路自检**：
   ```bash
   omnidesk-pm install ./omnidesk-plugin-<slug>
   omnidesk-pm abi-status --strict
   omnidesk-pm verify plugin-<slug>
   ```
   确认 `ctest`、`abi-check`（含 `sdk_abi_signature` 与 `struct_size` 校验）及 `dlopen -> onLoad -> onHealthCheck -> onUnload` 生命周期全部返回 `0`。
6. **登记上架**：推送到独立 GitHub 仓库后，在 `catalog.json` 追加一条仓库元数据即可在图形化「插件市场」中一键检索、安装、更新与回滚。
