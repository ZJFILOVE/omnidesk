# 安卓字幕服务 API v1

YouTube 任务由手机负责下载视频与原字幕。飞牛接收字幕，使用 Codex 的 `gpt-6-luna` 生成中文与双语 SRT；服务不会访问 `source_url`，它仅是任务元数据。可选的 NAS 媒体模块接入飞牛已有的 Radarr / Sonarr，提供已入库影片的观看、下载、字幕选择和翻译接口，配置方法见文末。没有可用字幕时跳过字幕处理，视频仍可观看和下载。

## 启动与配置

要求 Linux、Python 3.11 或以上，以及当前运行账号可使用且已登录的 Codex CLI。YouTube 字幕服务只需 `requirements-mobile.txt`，无需 FFmpeg、yt-dlp、ASR 或 Qt。NAS 媒体模块额外需要 FFmpeg / ffprobe，不需要语音识别模型。已有飞牛 Codex 登录可通过相同账号与 `CODEX_HOME` 复用，登录凭据不发送到手机。

```sh
python3 -m venv .venv-mobile
.venv-mobile/bin/pip install -r requirements-mobile.txt
install -d -m 700 "$HOME/.config/qwen-mobile"
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$HOME/.config/qwen-mobile/token"
chmod 600 "$HOME/.config/qwen-mobile/token"
.venv-mobile/bin/python mobile_api.py \
  --token-file "$HOME/.config/qwen-mobile/token" \
  --data-dir "$HOME/.local/share/qwen-mobile"
```

默认监听 `127.0.0.1:8765`，端口、绑定地址、数据目录可以分别通过 `--port`、`--host`、`--data-dir` 设置。手机通过 HTTPS 反向代理或 Tailscale Serve 连接；本地 USB 验收也可使用 `adb reverse tcp:8765 tcp:8765` 加电脑到飞牛的 SSH 转发。安卓端要求公网地址使用 HTTPS；HTTP 仅允许本机、私有局域网或 Tailscale IP。

认证读取顺序为 `MOBILE_API_TOKEN` → `--token-file` / `MOBILE_API_TOKEN_FILE`。令牌必须是随机生成的 32–512 位可打印 ASCII 字符；没有有效令牌时拒绝启动。只将此服务令牌填入手机。修改令牌后重启服务。

`MOBILE_API_DATA_DIR` 可以设置默认数据目录。该目录保存 `jobs.sqlite3`、SQLite WAL 文件和每个任务的 `checkpoints/`。应使用本机持久文件系统，确保服务账号独占访问。保留数据库和 checkpoint 目录即可保留任务与翻译结果；不要只复制处于写入状态的单个 SQLite 文件，备份时停止服务或使用 SQLite backup API。

单进程运行；服务用文件锁拒绝两个实例同时使用一个数据目录，不支持 `uvicorn --workers 2`。默认最多 100 个待处理或执行中的任务，`--max-pending` 可设置为 1–1000。服务一次运行 1 个字幕任务，每个任务同时最多使用 4 路翻译会话。历史任务与输出不会自动删除，可以从手机的删除设置清理选定任务；部署者仍应按存储预算备份及维护。

可将以下配置保存为 systemd 服务模板，按实际路径与服务账号修改：

```ini
[Unit]
Description=Qwen mobile subtitle translation
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_SERVICE_USER
WorkingDirectory=/opt/qwen-translator
Environment=HOME=/home/YOUR_SERVICE_USER
Environment=CODEX_HOME=/home/YOUR_SERVICE_USER/.codex
Environment=PATH=/home/YOUR_SERVICE_USER/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/opt/qwen-translator/.venv-mobile/bin/python /opt/qwen-translator/mobile_api.py --token-file /home/YOUR_SERVICE_USER/.config/qwen-mobile/token --data-dir /home/YOUR_SERVICE_USER/.local/share/qwen-mobile
Restart=on-failure
RestartSec=5
KillMode=control-group
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=multi-user.target
```

`PATH` 必须包含实际 `codex` 可执行文件或现有飞牛封装。`KillMode=control-group` 也能在服务异常退出时清理模型子进程。服务默认关闭 HTTP access log，不在错误响应中回传模型原始错误、字幕正文或凭据。

## 认证与通用约定

所有路径（包括 `/health`）都需要请求头：

```http
Authorization: Bearer <mobile-service-token>
```

创建任务需要 `Content-Type: application/json`。最大请求体为 4 MiB，包含分块传输也按实际接收字节限制；最大 SRT 为 2 MiB UTF-8、20000 条、每条 8000 字符。字幕序号必须从 1 连续递增；时间戳采用 `00:00:00,000 --> 00:00:01,000`；开始时间不能倒序、结束时间必须晚于开始时间、正文不可为空。允许重叠字幕，拒绝损坏字幕，而不会悄悄跳过错误行。服务器保存并提供规范化后的原字幕。

返回的所有任务对象结构相同：

```json
{
  "id": "44d3b852-3d43-40df-89d5-0a164ad95b5d",
  "state": "queued",
  "progress": 0.0,
  "message": "等待翻译。",
  "model": "gpt-6-luna",
  "created_at": "2026-10-14T03:00:00.000Z",
  "updated_at": "2026-10-14T03:00:00.000Z"
}
```

`progress` 范围 0–1，仅 `ready` 为 1。时间使用 UTC ISO 8601。`message` 是可直接展示的提示，客户端按 `state` 判断行为，不能解析中文文案来推断状态。

## 路由

| 方法 | 路径 | 正常返回 |
| --- | --- | --- |
| GET | `/health` | 200 `{"status":"ok","model":"gpt-6-luna","api_version":1,"capabilities":["delete_requests"]}` |
| POST | `/v1/jobs` | 202，任务对象 |
| DELETE | `/v1/requests/{client_request_id}` | 200 `{"status":"deleted"}`；按手机保存的请求 UUID 删除，重复删除或尚未收到的请求同样成功 |
| GET | `/v1/jobs/{id}` | 200，任务对象 |
| POST | `/v1/jobs/{id}/cancel` | 200，任务对象；已完成任务保持 `ready` |
| POST | `/v1/jobs/{id}/retry` | 200，原任务对象，保留原 `id`；`ready` 返回 409 |
| GET | `/v1/jobs/{id}/subtitles/original` | 200，UTF-8 SRT，任务创建后立即可用 |
| GET | `/v1/jobs/{id}/subtitles/zh` | 200，UTF-8 SRT；未 `ready` 时 409 |
| GET | `/v1/jobs/{id}/subtitles/bilingual` | 200，中文在上、原文在下的 UTF-8 SRT；未 `ready` 时 409 |

`/health` 验证服务协议与令牌，不能证明 Codex 的模型权限、登录、可用额度或外网连接；完整翻译任务才验证这些条件。NAS 媒体模块配置后增加 `nas_media`、`delete_media_requests` 和 `compatible_audio` 能力。服务没有抓取任意 URL、模型配置或 Codex 登录操作接口。

创建任务示例：

```json
{
  "client_request_id": "52a6c2d0-8613-4c2a-a83a-a4a1503a304a",
  "title": "示例视频",
  "source_url": "https://www.youtube.com/watch?v=example",
  "subtitle_srt": "1\n00:00:00,000 --> 00:00:01,000\nHello.\n\n"
}
```

`client_request_id` 必需且必须是 UUID；`title`（最多 500 字符）、`source_url`（最多 2048 字符）可省略。`source_url` 如提供，必须是无用户名密码的 HTTP(S) 地址。其余未知字段拒绝接收。

手机应先持久保存 `client_request_id` 和原始提交正文再发送。断网、超时或手机进程被杀后，用完全相同的 UUID 与正文重发，会返回同一个任务且不会再次排队；同 UUID 使用不同标题、来源地址或字幕内容返回 409。改变内容需生成新 UUID。任务创建与幂等记录在同一 SQLite 事务提交。

## 删除手机与飞牛文件

手机可分别选择清理应用保存的视频、清理飞牛上的字幕，或两处一起清理。飞牛删除接口始终使用手机持久保存的 `client_request_id`，即使创建任务的响应丢失、手机还不知道服务端 `id`，也可以删除；服务端 `id` 不能替代请求 UUID。手机删除前必须确认 `/health` 的 `capabilities` 包含 `delete_requests`。旧服务的 404 不能当作清理完成，应提示更新服务。只有收到 HTTP 200 且 JSON `status` 为 `deleted` 才确认飞牛清理完成。

删除会先持久记录请求已删除，禁止迟到的提交、重试和翻译结果重新生成任务，然后等待该任务的翻译进程退出，再删除数据库中的原字幕、中文、双语字幕、标题和来源地址，以及这个任务的整个 checkpoint 目录。其他任务与文件保留。对相同请求 UUID 的后续创建返回 410；如需重新翻译，手机必须创建新请求 UUID。未知请求 UUID 也会留下删除记录，防止尚在网络中的创建请求稍后到达。

删除记录只保留请求 UUID 和删除时间，用来阻止旧请求重建数据。数据库使用 `secure_delete`、压缩数据库和截断 WAL 回收应用存储；这不承诺擦除文件系统快照、备份或存储介质上的历史副本。飞牛从未接收手机视频，因此该接口只处理此字幕服务保存的数据。手机应用本地文件和用户另外导出的副本由手机端各自管理。

手机断网或请求超时后可重复发送相同 DELETE。已开始的删除会继续；若清理失败，返回 503，保留删除记录并禁止重建，手机应保存待清理状态并允许重试。服务启动时会先恢复未完成的清理，再启动翻译队列。首次升级仅新增删除记录表，不会自动删除旧任务。

## 状态与恢复

```text
queued → running → ready
                 → failed → retry → queued
queued/running → cancelled → retry → queued
running + 服务停止/重启 → queued
```

取消等待翻译进程退出后才返回，并保留已完成的分段 checkpoint。取消后重试沿用原任务与 checkpoint。对 `queued` / `running` 再次重试直接返回现状，不生成重复执行。两种翻译结果与 `ready` 状态在同一事务提交，手机不会下载到只生成一半的结果。

建议手机在前台每 2–5 秒查询一次，网络错误时指数退避。视频下载与字幕任务应有独立状态，飞牛断网不会取消手机视频下载；手机退出也不会取消已经提交的飞牛翻译。

## 错误

JSON 错误格式为 `{"detail":"提示"}`；FastAPI 对无效路径 UUID 的 422 错误使用标准字段列表。

| 状态码 | 含义 | 客户端行为 |
| --- | --- | --- |
| 401 | 缺少或错误的 Bearer 令牌 | 提示修改连接设置 |
| 404 | 任务或字幕类型不存在 | 提示任务不存在；检查服务器是否更换数据目录 |
| 409 | 幂等冲突、结果未准备好、已完成任务重试 | 按任务状态处理；冲突不能盲目重发 |
| 410 | 请求已删除，或该任务正在删除 | 停止旧任务提交/重试；再次翻译使用新 UUID |
| 413 | 请求体大于 4 MiB | 提示缩小字幕 |
| 415 | 创建任务未使用 JSON | 修正客户端 |
| 422 | UUID、字段、字幕格式或大小无效 | 提示检查原字幕 |
| 429 | 待处理队列达到上限 | 按 `Retry-After: 30` 等待后重发原请求 |
| 503 | 字幕文件或数据库清理未完成 | 保留待清理状态，稍后使用同一请求 UUID 重发 DELETE |

模型翻译失败反映为任务 `failed`，查询本身仍为 HTTP 200。

## 自动验收

```sh
.venv-mobile/bin/python -m unittest discover -s tests -p test_mobile_api.py -v
```

测试使用内存 ASGI 请求、临时 SQLite、模拟翻译器和可取消的真实本地子进程，不消耗模型额度。覆盖令牌、逐段输入限制、严格 SRT 校验、幂等和队列容量、原子输出、实际子进程取消、首次执行前取消竞态、checkpoint 重试、服务停止与异常重启恢复，以及同目录重复启动拒绝。删除验收还覆盖全部字幕与 checkpoint 清理、其他任务保留、重复删除与未知请求、迟到提交拒绝、并发取消/重试、断连后清理、真实进程退出、符号链接边界与删除中断后的恢复。

## NAS 媒体模块

媒体模块复用相同服务地址与 Bearer 令牌。手机只保存服务令牌与媒体编号；Radarr / Sonarr API Key、挂载路径及模型凭据留在飞牛。未配置时 `/v1/media/catalog` 返回 `configured: false`，原有 YouTube 字幕任务仍可使用。

### 服务端配置

设置 `MOBILE_MEDIA_CONFIG` 为私有 JSON 文件的绝对路径，修改后重启字幕服务。每种服务最多一个实例，支持 `api_key_file` 或 `api_key_env`；密钥文件建议权限 `600`，不得放入 APK 或诊断包。示例对应当前飞牛部署：

```json
{
  "services": [
    {
      "kind": "sonarr",
      "url": "http://127.0.0.1:8989",
      "api_key_file": "/vol1/1000/docker/codex-mobile/subtitle-service/sonarr-api-key"
    },
    {
      "kind": "radarr",
      "url": "http://127.0.0.1:7878",
      "api_key_file": "/vol1/1000/docker/codex-mobile/subtitle-service/radarr-api-key"
    }
  ],
  "mappings": [
    {
      "kind": "sonarr",
      "remote": "/var/apps/transmission/shares/transmission/media/tv",
      "local": "/vol1/@appshare/transmission/media/tv"
    },
    {
      "kind": "radarr",
      "remote": "/var/apps/transmission/shares/transmission/media/movies",
      "local": "/vol1/@appshare/transmission/media/movies"
    }
  ]
}
```

`remote` 是 Radarr / Sonarr 返回的容器内媒体库路径，`local` 是字幕服务进程能读取的实际目录。仅加入实际电影和剧集媒体库，不要授权整个 NAS。服务根据 Arr 中的文件编号选择视频，拒绝目录越界、路径穿越、符号链接和未完成入库文件；客户端不能提交任意文件路径。

播放、字幕提取、翻译和兼容转换只读 NAS 源视频及原有外挂字幕。提取及翻译结果保存在 `--data-dir` 下的独立媒体缓存、SQLite 和 checkpoint 目录，不写回 Arr 媒体库。明确确认的作品 / 下载删除通过下述 Arr 管理接口执行，不提供任意路径删除。FFmpeg 和 ffprobe 必须能从服务 `PATH` 找到；本模块不需要手机安装额外播放器或把下载器凭据发给手机。

### 路由与返回

所有下列路径都要求相同 Bearer 令牌。媒体编号如 `sonarr:series:1`、`sonarr:file:2`、`radarr:movie:2` 是服务器返回的编号，客户端应视为不透明字符串。

| 方法 | 路径 | 正常返回 |
| --- | --- | --- |
| GET | `/v1/media/catalog` | `configured`、`items`、各服务 `connected` 与 `message`，以及管理能力 `capabilities` |
| GET | `/v1/media/discover?kind={kind}&query={term}` | 搜索尚未添加的电影 / 剧集，返回外部编号与是否已经管理 |
| GET | `/v1/media/services/{kind}/options` | 已授权的媒体库与质量方案编号、名称 |
| POST | `/v1/media/services/{kind}/add` | 201，添加作品并可选择开始搜索，返回作品对象 |
| GET | `/v1/media/titles/{id}` | 作品详情；剧集含按季 / 集排序的 `episodes` 与已入库 `media_id` |
| GET | `/v1/media/queue` | 两个服务的下载队列；每条含 `status`、`progress`、字节数、剩余时间 |
| POST | `/v1/media/queue/{id}/remove` | 删除下载器任务及对应下载数据，返回 `status: removed` 与原 `id` |
| POST | `/v1/media/titles/{id}/monitor` | 修改作品监控状态，返回最新作品详情 |
| POST | `/v1/media/titles/{id}/remove` | 从 Arr 移除作品，可明确选择同时删除 NAS 文件；返回 `status: removed` 与原 `id` |
| POST | `/v1/media/titles/{id}/search` | 202，提交已在 Arr 管理的作品搜索，返回命令状态 |
| GET | `/v1/media/files/{id}` | 文件详情、字幕选项与本媒体版本最新 `job` |
| GET / HEAD | `/v1/media/files/{id}/stream` | 原始媒体字节，支持单段 `Range`、`ETag`、`If-Range` |
| POST | `/v1/media/files/{id}/compatible` | 202，准备保留视频画质的 AAC 音轨副本；重复请求共用同一处理 |
| GET | `/v1/media/files/{id}/compatible` | 兼容副本准备状态、进度、文件大小与就绪后的 `stream_path` |
| GET / HEAD | `/v1/media/files/{id}/compatible/stream` | 已就绪兼容副本，支持 `Range`、`ETag`、`If-Range` |
| GET | `/v1/media/files/{id}/subtitles/{subtitle_id}` | 将原有文本字幕提取 / 转换为 UTF-8 SRT |
| POST | `/v1/media/files/{id}/prepare` | 202，持久化字幕准备 / 翻译任务 |
| GET | `/v1/media/jobs/{uuid}` | 字幕任务状态、`media_id`、`subtitle_source` |
| POST | `/v1/media/jobs/{uuid}/cancel` | 取消后保留已完成 checkpoint |
| POST | `/v1/media/jobs/{uuid}/retry` | 沿用原任务与 checkpoint 重试 |
| GET | `/v1/media/jobs/{uuid}/subtitles/{kind}` | `original`、`zh` 或 `bilingual` 的 UTF-8 SRT |
| DELETE | `/v1/media/requests/{client_request_id}` | 删除此任务生成的字幕和 checkpoint，重复删除成功 |

作品字段为 `id`、`kind`（`series` / `movie`）、`title`、`year`、`overview`、`monitored`、`available_count`、`total_count`、`media_id`。剧集中的每集另有 `season_number`、`episode_number`、`has_file`、`media_id`；尚未入库的条目不提供播放编号。

文件详情含 `id`、`title`、`filename`、`size`、`duration`（秒）、`stream_path`、`subtitles`、`audio`、`job`，兼容字段 `asr_available` 固定为 `false`。兼容播放字段为 `compatible`、`compatible_available` 和就绪后可用的 `compatible_stream_path`。字幕项提供 `id`、`title`、`language`、`source`（`sidecar` / `embedded` / `generated`）、`download_path`；音轨项提供 `id`、`title`、`language`。仅列出可转为文本的字幕，PGS / VobSub 等图片字幕不提供 OCR。返回的下载地址是当前服务内相对路径，手机仍需附加 Bearer 令牌，不能向其他域名转发令牌。

原始媒体流直接提供原文件。`Range` 成功返回 `206` 和 `Content-Range`；越界、多段或无效范围返回 `416`。`HEAD` 返回文件大小及范围信息。播放器拖动与手机离线保存都使用媒体流接口，网络需能承担原文件码率。

部分手机不能解码 EAC3 等音轨，可以准备兼容副本：视频流直接复制，不解码或重新压缩画面；所有音轨转为 AAC、双声道、每轨 160 kb/s，封装为 MKV，字幕仍使用独立 SRT。`POST compatible` 正文为 `{}`，状态为 `idle`、`queued`、`running`、`ready` 或 `failed`，并含 `progress`、`message`、`size`、`stream_path`。只有 `ready` 提供可播放路径；准备期间仍可访问原片。

兼容副本写入 `--data-dir/media-compatible`，原片保持只读。默认单片上限 **4 GiB**、总缓存 **10 GiB**，可在媒体 JSON 中以整数 `compatible_max_file_bytes`、`compatible_cache_bytes` 调整；允许 64 MiB–128 GiB，单片不得大于总量。空间不足时淘汰未被播放 / 下载使用的旧兼容副本，正在使用的副本不会被清除；副本被淘汰后可以再次准备。源文件变化后不复用旧版本。兼容转换状态持久化，服务重启后恢复队列；每次只有一个转换进程。

已管理作品的搜索接口触发 Arr 搜索；新作品通过 `discover` 查询，再用 `services/{kind}/options` 返回的编号添加。添加正文为 `{"external_id":123,"quality_profile_id":1,"root_folder_id":1,"monitored":true,"search":true}`。服务器重新核验外部条目与方案 / 媒体库编号，只允许已配置只读映射对应的媒体库；手机不提交路径。重复添加返回现有作品，不重复触发搜索。

`options` 还可返回 `default_quality_profile_id`；每个 `quality_profiles` 项可含 `description`。App 优先恢复该 NAS、该服务上记住且仍有效的选择，其次使用服务端默认方案；已删除的编号不会继续提交。旧服务缺少这些字段时仍可手动选择。

服务配置 `default_quality_profile_id` 指定默认方案，`seeders_first_profile_ids` 指定采用做种分档选片的方案。当前 Radarr / Sonarr 都使用编号 1 的 **Any**：仅排除枪版，其他画质同级，不设体积上限，不自动升级已入库文件。App 主动搜索先保留 Arr 审核通过的资源，以 `round(log10(seeders))` 取最高做种档，再选同档内字节数最少的版本；同体积才比较做种人数。对应人数档位为 1–3、4–31、32–316、317–3162 等。

电影搜索核对已有队列，避免重复下载；剧集搜索在服务端后台按季处理已播出、受监控且缺失的集数，整季包按平均每集大小比较，跳过已入库或已在队列的集数。加入与主动搜索的结果通过 `search_message` 返回，剧集详情刷新可读取搜索进度；停止追更、更换方案或移除作品会阻止后续下载提交。搜索状态为服务进程内状态，重启中断的搜索可先核对队列后手动重试，服务不会自动重放未获确认的下载提交。

Arr 自身 RSS 自动抓取采用同画质组及较小体积偏好，但其原生比较器还会比较 peers 等因素；App 主动搜索采用上述明确的分档与体积顺序。

资源下载由 Arr 现有下载客户端执行，添加或搜索成功不保证索引器立即找到资源。写请求超时时不要自动重复，应先查看片库和下载队列。

### 下载与作品管理

`catalog.capabilities` 包含 `manage_queue` 和 `manage_titles` 时，客户端才展示相应管理入口；旧服务缺少该字段时仍可正常浏览和播放。这三项 POST 都需要 `application/json`、严格布尔值，拒绝额外字段，且不自动重试。

- 下载删除正文为 `{"blocklist":false}`。服务重新读取当前队列核实编号，然后向对应 Arr 发出 `DELETE queue/{id}`，固定 `removeFromClient=true`、`skipRedownload=true`、`changeCategory=false`。Arr 会要求下载器移除整个下载任务并删除其下载数据，同一合集的多条队列记录可能一起消失。`blocklist=true` 排除此资源；不影响作品的长期监控状态。独立入库文件和手机副本需分别管理。
- 监控正文为 `{"monitored":false}` 或 `{"monitored":true}`。服务读取当前作品并保留其他配置，只修改作品级 `monitored`。停止监控保留文件，也不取消已提交的下载；恢复监控沿用原有季集选择，不立即发起搜索。
- 作品移除正文为 `{"delete_files":false,"add_import_exclusion":true}`。默认只移除 Arr 记录、保留 NAS 文件，并加入导入排除名单；Sonarr 使用 `addImportListExclusion`，Radarr 使用 `addImportExclusion`。只有显式 `delete_files=true` 且当前作品路径处于已授权媒体根目录的子目录时，才允许请求 Arr 同时删除文件。作品移除不等于删除下载器任务。

写入成功及未获确认时均清除服务端的短期清单缓存。网络错误、上游拒绝或无效响应不会返回成功；客户端保持错误提示，用户应刷新后核对实际状态。管理请求不携带媒体路径、Arr 密钥或下载器凭据。

### 字幕准备与无字幕跳过

创建媒体字幕任务需要 `application/json`：

```json
{
  "client_request_id": "52a6c2d0-8613-4c2a-a83a-a4a1503a304a",
  "subtitle_id": "embedded-2"
}
```

客户端先持久保存请求 UUID、媒体编号与全部选项，再发送。相同 UUID 与完全相同选项返回同一任务；不同选项返回 `409`。重新生成字幕需创建新的 UUID。`subtitle_id` 可省略：优先英文文本字幕，其次中文；有多个同等候选时要求明确选择，不会猜测某一轨。无字幕时返回 `422` 并提示「已跳过字幕处理」，不创建字幕任务，视频接口仍可用。保留的兼容字段 `force_asr` 只允许 `false`，`audio_id` 只允许 `null`；请求识别返回 `422`。服务不调用 ASR 或外部识别 API，也不上传视频和音轨。

原文提取后使用原有 `gpt-6-luna` 翻译流程，从英文等原文生成中文及双语字幕。已有双语字幕可保留原有中英文；只有中文的字幕可直接选择观看，生成双语时提示 `422` 并要求选择英文或已有双语字幕，不补写不存在的英文原文。字幕任务独立于视频播放和离线下载，取消字幕不停止观看。媒体文件大小、修改时间或身份变化后，旧结果不再作为该文件的有效字幕；需要刷新并重新创建任务。

媒体字幕队列、数据库和删除记录独立于 YouTube 字幕任务，状态与重启恢复语义相同。`/v1/media/requests/{uuid}` 仅删除此服务生成的媒体字幕与 checkpoint，不删除 NAS 原片、原有外挂字幕、Arr 记录或下载器任务。App 应确认健康能力包含 `delete_media_requests` 后执行该清理。
