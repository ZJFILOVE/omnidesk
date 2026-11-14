# Qwen 双向同传（Arch / PipeWire）

原生 Qt 桌面应用：中英双向语音翻译、麦克风试听、独立悬浮字幕、系统密钥库保存 API Key。语音同传独立运行；可选网页翻译通过配套 Firefox 扩展连接本地服务。

实时语音同传已升级为 **Qwen3.8 LiveTranslate**（`qwen3.8-livetranslate-flash-realtime`），桌面与网页入口共用新版协议。模型自动识别源语言并返回原文转写，网页支持新版增量字幕；现有区域和密钥设置继续使用。接口依据[官方模型说明](https://help.aliyun.com/zh/model-studio/qwen3-8-livetranslate-flash-realtime)及[事件文档](https://help.aliyun.com/zh/model-studio/live-translator-server-events)。

当前开发重点是 **网页翻译与视频字幕**：网页译文保存在 Firefox 本地，可按段重译；本地和 YouTube 视频优先使用现有字幕，英文字幕由 Codex 或 Antigravity 翻成中文，无字幕时再做 ASR。视频保留时间轴校正与原文 / 中文 / 双语导出。实时语音继续沿用已有 Qwen 同传能力，不再把 Codex 实时字幕作为功能拓展重点。

界面支持云白 / 石墨两种主题，可从侧栏底部一键切换。实时翻译、会话记录、设备与音频、偏好设置分为独立页面；会话记录仅保存元数据，不保存字幕或音频。见[界面说明与预览](docs/UI.zh-CN.md)。

实时字幕与歌词都有独立悬浮窗口，可在 App 内选择陪读蛙全部八种网页样式预设、叠加三种字幕效果，并调整字体、颜色、透明度和双语顺序；两类窗口分别保存设置。支持 Wayland 系统拖动和 Hyprland 浮动 / 跨工作区固定。见[字幕与歌词外观](docs/CAPTIONS.zh-CN.md)。

新增 **只有字幕**：FunASR SenseVoiceSmall Q8 通过 llama.cpp 使用 Vulkan GPU 本地识别，默认调用已登录的 Codex Luna 翻译文字；也可选择本机 Antigravity、配置兼容 API 或仅显示原文。原声照常播放，不生成译音。见[字幕模式说明](docs/LOW_COST_PLAN.zh-CN.md)。

新增 **视频字幕**：本地 / 飞牛挂载视频与 YouTube 链接，SubHD 浏览器下载、ASSRT API、已有字幕提取、无字幕 ASR、中文翻译、时间轴对齐和外挂 SRT 导出。接入 mpv 播放控制，先完整准备再观看。见[视频字幕使用说明](docs/VIDEO_SUBTITLES.zh-CN.md)。

新增 **NAS 自动字幕**：Radarr / Sonarr 入库清单、字幕下载优先、持久单视频队列和受保护回写。最终输出为中文在上、英文在下的 `视频名.bilingual.srt`，可额外输出 `.zh.srt`；首次连接只建清单，应用关闭后停止，重新打开继续。见[NAS 自动字幕使用与验收记录](docs/NAS_SUBTITLES.zh-CN.md)。飞牛已配置 [Tailscale + SMB 自动挂载](docs/FEINIU_SMB.zh-CN.md)，新媒体目录映射及防覆盖写回测试通过；当前入库清单为空，真实影片字幕与播放验收尚待完成。

新增 **Firefox 网页翻译**：项目自带扩展，整页正文交给本机 Codex 或 Antigravity、多个页面串行处理，支持双语 / 仅译文、保留链接与格式、动态内容补译、译文持久保存、单段重新翻译和恢复原文。侧栏“网页翻译”管理本地服务与配对，复用偏好设置中的翻译服务和模型。见[安装与使用说明](docs/WEB_TRANSLATION.zh-CN.md)。

新增 **PDF 翻译**：BabelDOC 在本地解析、识别版面并重排 PDF，文字交给已有 Codex（包括 `gpt-6-luna`）、Antigravity 或兼容 API。支持指定页码、自动术语提取、纯译文与双语 PDF、左右对照或交替页，以及取消和段落缓存。侧栏“PDF 翻译”复用偏好设置中的模型；首次安装运行 `python3 scripts/setup_pdf.py --warmup`。见 [PDF 翻译说明](docs/PDF_TRANSLATION.zh-CN.md)。

## 启动

新增 **Apple Music 与双语歌词**：自动连接本机 Adam，显示歌曲和时间轴歌词，控制切歌、音量、播放进度与本机输出设备；优先读取已有中文，缺少部分使用本机 Codex `gpt-6-luna`，可选择自动或手动翻译。歌词保存在本机并接入 Waybar，不下载音乐。见 [音乐与歌词说明](docs/MUSIC.zh-CN.md)。

应用菜单搜索 **Qwen 双向同传 / Qwen LiveTranslate**，或运行：

```bash
cd ~/Projects/qwen-translator
.venv/bin/python launch_app.py
```

语音同传首次在应用中输入并保存百炼 API Key，此后从系统密钥库自动读取。只有字幕模式使用 Codex 时无需百炼 Key。运行环境：Arch 系统 Python + PyQt6，项目 `.venv` 中的 API 依赖，PipeWire 与 `secret-tool`。

## 环境与验证

安装系统 PyQt6、PipeWire 工具和 uv 后，可以从任意 Python 入口运行以下脚本：

```bash
python3 scripts/setup.py                       # 核心后台依赖
python3 scripts/setup.py --video --firefox-tests # 可选视频组件和 Firefox 测试运行时
python3 scripts/check_environment.py            # 只检查安装，不采音频、不调用模型
python3 scripts/test.py                         # 后台、Qt 和扩展后台测试
python3 scripts/test.py --with-firefox           # 加上真实 Firefox DOM 测试
```

测试入口自动把 Qt 测试交给安装了 PyQt6 的解释器，其余 Python 测试使用 `.venv`。也可用 `--suite core|qt|extension|firefox` 单独验证。启动器同样自动寻找 Qt 解释器；需要指定时使用 `QWEN_QT_PYTHON`。后台仍固定使用项目 `.venv`，无需混入系统的 site-packages。

`constraints.txt` 记录已验证的核心直接依赖版本，视频依赖在 `requirements-video.txt` 中固定；系统包及间接依赖尚未完整锁定。测试会启动回环 HTTP 服务、播放器 IPC 和可选无头浏览器，运行环境须允许这些本机能力。测试使用模拟翻译响应，不调用真实模型。

## 文件结构

```text
qwen-translator/
├── launch_app.py           桌面启动入口
├── native_app.py           原生界面与悬浮字幕
├── desktop_ui.py           页面与可复用 Qt 控件
├── ui_theme.py             深浅主题与语义颜色
├── local_subtitles.py      本地 GPU ASR、分句与字幕管道
├── text_translation.py     Codex / Antigravity / 兼容 API 文本翻译
├── scripts/                模型安装脚本（校验下载）
├── runtime/                下载的 FunASR llama.cpp 运行时（不提交）
├── models/                 下载的 GGUF 权重（不提交）
├── native_worker.py        音频工作进程与界面通信
├── credentials.py          系统密钥库读写
├── duplex.py               双向音频管道及兼容网页接口
├── server.py               上游 Qwen 协议配置与 Demo 服务
├── check_access.py         上游 API 权限检查工具
├── requirements.txt        Python API 依赖
├── tests/                  无需 API 的桌面交互与进程测试
├── static/                 保留的网页 Demo 资源
├── docs/
│   ├── PIPEWIRE.zh-CN.md    使用与音频路由说明
│   ├── LOW_COST_PLAN.zh-CN.md  本地 GPU 字幕模式
│   └── upstream/           上游中英文 README
├── .venv/                  本机运行环境（不提交）
└── .git/                   保留的 Git 历史
```

## 文档

- [使用说明与音频路由](docs/PIPEWIRE.zh-CN.md)
- [本地 GPU FunASR + 文本模型字幕模式](docs/LOW_COST_PLAN.zh-CN.md)
- [上游官方 Demo 说明](docs/upstream/README.zh-CN.md)
- [后续使用场景与功能规划](docs/SCENARIOS.zh-CN.md)

密钥保存在系统密钥库，界面偏好由 QSettings 管理；它们不在项目文件夹中，移动项目不会删除已保存的 Key。代码保留在根目录以保持现有导入和音频链路不变。

### Antigravity 翻译

在终端运行 `agy` 完成登录，然后在偏好设置 → 翻译服务选择 **本机 Antigravity**，刷新并选择模型。字幕、视频、网页共用此选择；视频和网页支持默认 4 路、最高 8 路独立会话。模型列表来自 `agy models`，无需填写 API Key。

每批建立独立会话，不续接前一批上下文；取消和超时会结束对应进程。Antigravity CLI 可能保存自身会话历史，不等同于 Codex 的 ephemeral 模式。详见 [Antigravity 接入说明](docs/ANTIGRAVITY.zh-CN.md)。

## 安卓客户端

新增 **飞牛字幕** 原生安卓 App：支持 YouTube 下载，以及飞牛上的 Radarr / Sonarr 电影与分季剧集片库。可在 App 内添加作品、搜索下载、查看与删除下载任务、停止 / 恢复追更，以及移除作品（默认保留 NAS 文件），选择内嵌 / 外部字幕，直接从 NAS 播放或保存到手机离线观看。飞牛使用 `gpt-6-luna` 准备中文 / 双语字幕；NAS 视频没有可用文本字幕时跳过字幕处理，视频仍可下载和观看。支持任务持久化、独立取消 / 重试、连接测试及诊断导出。

构建、APK 安装、飞牛部署及 USB 联调见 [安卓使用说明](docs/ANDROID.zh-CN.md)，服务接口见 [移动端字幕 API](docs/mobile-api-contract.md)。运行 `scripts/android.sh` 生成测试 APK。
