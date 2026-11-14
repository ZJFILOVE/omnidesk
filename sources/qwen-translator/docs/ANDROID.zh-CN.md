# 安卓下载、NAS 影视、飞牛翻译与离线观看

安卓项目位于 `android/`，应用名 **飞牛字幕**，包名 `cn.qwensub.app`。YouTube 视频与原字幕由手机通过 yt-dlp 下载，飞牛接收字幕并使用 **gpt-6-luna** 翻译。新增 **飞牛片库** 接入飞牛上的 Sonarr 剧集、Radarr 电影及下载队列，已入库视频可直接观看或存到手机。下载、字幕准备与翻译有独立进度和停止 / 重试入口。

## 构建和安装

需要 JDK 21、Android SDK Platform 36、Build Tools 35.0.0 和 platform-tools。Gradle Wrapper 固定版本及 SHA-256，不需要全局 Gradle，也不需要打开 Android Studio。

```bash
cd /home/zhu/Projects/qwen-translator
scripts/android.sh
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
```

`scripts/android.sh` 优先使用 `runtime/android/jdk-21*`，避免 Android Studio 自带的 JDK 25 与 Gradle 8.11.1 不兼容。也可用 `ANDROID_JAVA_HOME=/你的/jdk21 scripts/android.sh` 明确指定。SDK 默认 `$HOME/Android/Sdk`，可用 `ANDROID_HOME` 修改。构建不会改系统 Java 设置。

当前提供可安装的 debug APK，包含 arm64-v8a、armeabi-v7a、x86_64 的 Python、FFmpeg 和 QuickJS，体积较大。支持 Android 8.0 及以上。QuickJS 由下载库自动配置，无需手机额外安装 JavaScript 运行时。

## 飞牛服务

服务端入口 `mobile_api.py`，完整接口和通用部署步骤见 [接口契约](mobile-api-contract.md)。它独立于桌面 GUI，关闭电脑不影响已提交的翻译、NAS 观看和下载。

本次在飞牛现有 Codex 环境旁新增：

- 程序：`/vol1/1000/docker/codex-mobile/subtitle-service/`
- 数据与检查点：程序目录的 `data/`
- 访问令牌：程序目录的 `token`（私有文件，不写入 APK）
- 启动脚本：`/vol1/1000/docker/codex-mobile/bin/start-subtitles`
- 日志：`/vol1/1000/docker/codex-mobile/logs/subtitles.log`
- 监听：`100.90.158.48:8765`，仅 Tailscale 接口；所有接口要求 Bearer 令牌。
- 重启启动：当前 NAS 账号的 crontab，标记 `codex-mobile-subtitles`；新增前的 crontab 备份在程序目录 `crontab.before`。

默认 CLI 监听 `127.0.0.1:8765`；此次部署显式绑定 Tailscale IP。未改变系统 SSH 转发设置。模型凭据继续留在飞牛既有 Codex HOME。守护脚本通过文件锁防止重复启动，异常退出后重启服务；SQLite 和翻译检查点恢复未完成任务。

## 手机独立联网（Tailscale + Clash Meta）

手机不需要电脑中继。Tailscale 负责连接飞牛；手机 Clash Meta 只运行本地代理核心，下载器显式连接这个代理。

Android 同一用户只能运行一个 VPN 服务。同时开启 Tailscale 和 Clash 的 VPN 模式会互相顶掉；仅添加 `100.64.0.0/10,DIRECT` 规则不能解决这个冲突。

1. 在 Clash Meta 首页停止核心，进入「设置 → 网络」，关闭 **自动路由系统流量**，再回首页启动核心。
2. Tailscale「Settings → App split tunneling」勾选 **Clash Meta for Android**，使 Clash 的节点连接和 DNS 绕过 Tailscale。保留原有其他排除项，不要排除「飞牛字幕」。保持 Tailscale 已连接，本方案无需选择出口节点。
3. 当前手机启用 **VPS-IP-Tailscale** 配置：四个节点的 `server` 使用已核对的 VPS IPv4 `189.24.110.19`，原 SNI `db.zhujunfeng.top`、Reality servername `dl.google.com`、密钥、端口和证书校验均保留。原「新配置」仍在手机中可供回退。节点参数已与 VPS 逐项核对，VPS 服务未修改。若 VPS 以后更换 IP，需要更新这个本地配置。
4. 如果手机启用了 **Cirno 墓碑模块**，在 Cirno 的应用管理界面，将 **Clash Meta for Android** 和 **飞牛字幕**（`cn.qwensub.app`）都加入白名单。本机使用用户 `0`，应选择对应的 `#0` 条目；只放行其中一个仍可能使代理或下载任务被冻结。已通过界面完成这两项设置，并在 `/data/system/Cirno/ApplicationSettings.json` 核实保存结果。
5. 切换配置或完成白名单设置后，**完整停止并重新启动 Clash 核心**，确认首页显示正确的已激活配置，再打开飞牛字幕。当前使用配置自带的规则模式和 PROXY fallback 组；排障时的临时全局模式已随核心重启清除。
6. 在「飞牛字幕 → 连接」保存下列设置。令牌继续使用原有加密保存的服务令牌。

| 设置 | 手机独立联网值 |
| --- | --- |
| 飞牛地址 | `http://100.90.158.48:8765` |
| 下载代理 | `http://127.0.0.1:7890` |

`7890` 是本次在手机上实际确认的 Clash 监听端口，不是电脑端口。其他配置应先核对 HTTP / 复合端口。API 客户端显式绕过下载代理，字幕请求经 Tailscale；视频从代理节点下载到手机。Clash 本地代理模式不自动代理其他未配置代理的 App。

本机另一个已确认的阻塞原因是 Cirno：普通前台服务退到后台约 **3 秒**后仍会被模块冻结，包括 Clash 的 `:background` 代理进程和飞牛字幕。此时通知和任务状态可能仍显示运行中，代理请求却一直等待。排障时，同一条已等待约 12.2 秒的请求在切回 Clash 前台后立即返回 HTTP 200；加入上述白名单并重启核心后，后台超过 3 秒的 `cgroup.freeze` 从 `1` 变为 `0`，飞牛字幕 App UID 下的后台代理请求约 0.35 秒返回 HTTP 200。随后 Wi-Fi、5G 独立联网完整下载与翻译，以及本地双语字幕播放均已通过，见下方验收记录。

本次移除了 `tcp:18765`、`tcp:17897` 两条 ADB reverse。USB 线可保留用于诊断，但不承载下载或字幕请求。不要再运行下节的 USB 联调脚本，除非有意恢复电脑中继测试。恢复 Clash 全机 VPN 时需先断开 Tailscale，再停止 Clash、重新开启“自动路由系统流量”；恢复后此共存方案不再成立。

依据：[Android VPN 限制](https://developer.android.com/develop/connectivity/vpn)、[Clash Meta 服务启动源码](https://github.com/MetaCubeX/ClashMetaForAndroid/blob/main/app/src/main/java/com/github/kr328/clash/util/Clash.kt)、[Tailscale 应用分流](https://tailscale.com/docs/features/client/android-app-split-tunneling)。

## USB 联调

以下保留为开发联调方式，日常使用采用上面的手机独立联网配置。电脑需能通过 Tailscale 访问飞牛，手机需开启 USB 调试并授权。

```bash
scripts/android_usb.sh
```

保持这个命令运行，在 App 的「连接」页设置：

| 设置 | USB 联调值 |
| --- | --- |
| 飞牛地址 | `http://127.0.0.1:18765` |
| 访问令牌 | 飞牛服务私有 `token` 文件的内容 |
| 下载代理 | `http://127.0.0.1:17897` |

18765 经 ADB 和电脑转到飞牛 Tailscale 接口；17897 经 ADB 转到电脑现有代理 7897。这两个回环地址仅用于 USB 联调。电脑的代理须正在运行。这里没有把飞牛的回环代理地址直接用于手机。

公网 HTTP 会被 App 拒绝；内网和 Tailscale IP 可以使用 HTTP，公网部署应提供 HTTPS。

## 日常操作

1. 在「连接」页保存服务地址与令牌，点测试连接；令牌和代理凭据由 Android Keystore 加密保存。
2. 在「任务」页粘贴单条 YouTube 链接，或从其他 App 分享链接到飞牛字幕。视频和字幕独立开始。
3. 原字幕一拿到就提交飞牛，视频可以继续下载。飞牛暂时不可达时原字幕留在手机，WorkManager 自动退避重试。
4. 视频完成即可播放，字幕完成后可以选择双语、中文、原文或关闭；支持倍速、快进、拖动进度和横屏全屏。
5. 「片库」保留 App 私有目录内的完整文件，断网可播放。用「保存文件」通过系统目录选择器导出视频和各份 SRT，生成新目录，不覆盖用户已有文件。卸载 App 会删除内部文件，永久保存请先导出。
6. 「连接」页可导出诊断，包含任务编号、状态、错误和播放器错误码，不包含令牌、字幕正文或视频标题。
7. 「连接 → 外观」可选择跟随系统、浅色或深色，默认跟随系统；切换立即生效并自动保存，重新打开 App 后保留选择。任务、片库、连接及播放页共用对应配色，视频画面保持黑色背景。
8. 「处理记录」和「离线片库」的每条任务都有 **清理文件** 入口；「连接 → 文件清理 → 管理已保存的文件」支持多选和全选。选择 **手机 App 文件**、**飞牛字幕文件**，或同时勾选两处，确认后删除实际文件。

### 清理文件

- 手机清理删除该任务的视频、原文 / 中文 / 双语字幕、下载临时文件和缓存；清理前停止相关下载与字幕同步。无需连接飞牛即可完成。
- 飞牛清理删除该任务的原字幕、译文和翻译缓存，并停止仍在执行的翻译；仅清理飞牛时，手机已下载的视频和字幕仍可离线使用。
- 两处清理状态分别显示。飞牛离线时保留待清理请求，恢复连接或重新打开 App 后继续。服务地址变更时，需要恢复该任务原来的飞牛地址；不会向另一台服务发送旧任务的清理请求。
- 所选文件全部清理成功后，自动从「处理记录」移除；失败或等待联网的任务继续保留以便重试。两处文件都已清理时，同时删除内部任务记录。只清理一处时，另一处的文件继续在片库或文件管理页保留，之后仍可清理。仅清理飞牛而视频尚未完成时，暂保留下载控制入口，视频就绪后再移除处理记录。旧版本留下的已清理记录在升级后自动整理。
- 连接设置、外观设置和「另存为」导出的副本会保留；导出副本需在系统文件管理器中自行删除。清理不能撤销；之后可通过原链接重新下载。
- 飞牛字幕服务必须升级到支持 `delete_requests` 的版本。旧服务会明确提示更新，并保留待清理状态。只有服务器确认删除后，App 才显示「飞牛字幕：已清理」。

停止视频不会取消飞牛翻译。停止字幕会保留检查点并同步取消远端；网络断开时取消请求也会排队。重试同一字幕任务沿用已持久化的请求 UUID 和提交内容，避免因丢失响应重复计费。修改为另一台 NAS 地址后旧任务不会被误发送到新服务。

## 飞牛片库

「飞牛片库」与原 YouTube 任务共用「连接」页的飞牛地址和令牌，手机不需要填写 Radarr / Sonarr API Key。手机继续使用 Tailscale 连接飞牛；NAS 影视请求不经过 YouTube 下载代理。底部入口为「任务」「飞牛片库」「离线片库」「连接」。

1. 打开「飞牛片库」，按「剧集」「电影」浏览 Sonarr / Radarr 已管理的作品，或在「下载队列」查看已有下载进度。下载完成并被 Arr 导入媒体库后，刷新即可观看。
2. 「添加剧集」「添加电影」可搜索新作品，选择实际「画质方案」「保存位置」后点「添加并搜索下载」。已有作品可在详情中搜索缺集或搜索电影下载，任务交给飞牛现有下载工具处理。
3. 进入剧集选择季 / 集，或打开电影。已入库文件提供「直接观看」和「存到手机」。「字幕选项」显示影片自带的文本字幕、同目录外挂字幕，以及飞牛已经生成的字幕。可直接选择字幕观看，或点「翻译为中文与双语字幕」；播放页也能处理字幕。字幕准备不影响继续观看或保存视频。
4. 没有可用文本字幕时，跳过字幕处理，视频仍可观看和下载。本版不调用语音识别模型或识别 API。PGS / VobSub 等图片字幕不做 OCR，可为原片添加匹配的文本字幕后刷新。
5. 「存到手机」复制视频和当前选择的字幕，直接保存原有字幕不会自动重新翻译。需要中文 / 双语离线字幕时，先完成「翻译为中文与双语字幕」，再选择生成的字幕保存。完整文件进入现有任务、离线片库与文件清理流程，断开飞牛后可播放。手机文件和飞牛生成的字幕分别清理；NAS 原片、原有外挂字幕、Arr 记录和下载器任务不受清理影响。

### 管理下载和追更

- 添加电影和剧集的「选片规则」会记住上次选择，分别绑定飞牛地址与电影 / 剧集服务。当前两个服务的默认 **Any** 都是「最高做种档内选较小版本、排除枪版、不限大小」，蓝光、Remux、原盘均允许。选择框下会显示这条规则；做种人数来自源站报告，不代表下载器当前实际连接人数。
- App 主动搜索按做种档位优先，同档按体积从小到大选择。剧集按季搜索缺集，整季包比较平均每集体积；搜索结果可刷新作品详情与下载队列查看。规则不会自动替换已经入库的视频。

2026-10-18：Radarr、Sonarr 的 Any 已实际保存为上述规则，服务端默认方案均为 1；移动接口已返回 Any 的规则说明。原 49.9GB《失眠》下载已取消，替换后的 1.98GB 1080p 文件已完成并由 Radarr 入库。此后更新的分档选小规则适用于后续搜索，保留已完成文件。没有触发《熊家餐馆》的补集下载。服务源码更新前备份位于 `subtitle-service/update-selection-rules-20261018-125921/`，配置备份保存在 NAS 私有目录。

移动后台 54 项测试、安卓 50 项单元测试通过，Lint 0 错误；`dist/feiniu-subtitles-0.3.1-selection-rules-debug.apk` 已构建并附 SHA-256。现有 App 选择 Any 即可使用服务端规则；新版增加默认选择、记住选择及规则 / 搜索结果说明。此次没有连接 ADB 设备，尚未安装新版或做手机界面验收。

- 「飞牛片库 → 下载队列」每项提供「删除下载任务」。确认后由 Sonarr / Radarr 移除下载器中的整个任务，并清理该任务的下载目录数据；整季合集会同时影响同一下载任务中的其他集数。已单独入库的媒体文件和手机副本需分别管理。可选择将这个资源加入排除名单；本次删除不立即搜索替代资源。作品仍受监控时，以后仍可能匹配并下载新资源。
- 剧集 / 电影详情可停止或恢复自动追更。此操作改变作品的监控状态，保留已下载文件；正在下载的任务需另到「下载队列」删除。
- 详情中的「移除作品」默认保留 NAS 文件。只有勾选同时删除 NAS 文件并确认，才让 Sonarr / Radarr 删除该作品的媒体文件；默认加入导入排除名单，避免列表自动重新添加。手机副本和单独生成的字幕仍通过「清理文件」管理。已有下载任务也需另到「下载队列」处理。
- 这些操作需要手机和飞牛同时在线，不会在后台自动重试。请求未获确认时先刷新队列或片库，再决定是否重试。旧服务仍能浏览和播放，管理入口会提示更新飞牛服务。

遇到有画面但没有声音时，在影片详情选择「没有声音？兼容播放」，或使用播放页的「声音兼容」。飞牛保留视频画面，只把音轨转换为通用 AAC 双声道；准备完成后切换到兼容版本，原片保持不变。「兼容版存到手机」可保存这个有声版本供离线观看，字幕仍采用当前所选字幕。

播放和保存支持拖动及断点续传。原片码率决定网络与手机存储用量；兼容版本也保留原画质，4K 剧集仍可能每集数 GB。飞牛兼容缓存默认单片 4 GiB、合计 10 GiB，按使用时间清理没有正在播放或下载的旧副本；缓存被清理后可重新准备。

飞牛服务的私有配置位于 `/vol1/1000/docker/codex-mobile/subtitle-service/media-config.json`，由启动脚本的 `MOBILE_MEDIA_CONFIG` 指向。Radarr / Sonarr 密钥分别保存在同目录的 `radarr-api-key`、`sonarr-api-key`，三份文件权限均为 `600`。媒体目录仅授权：

| 内容 | 字幕服务读取目录 |
| --- | --- |
| Sonarr 剧集 | `/vol1/@appshare/transmission/media/tv` |
| Radarr 电影 | `/vol1/@appshare/transmission/media/movies` |

播放、字幕处理和兼容转换只读取媒体库中的视频及原有字幕，生成结果写入字幕服务自己的数据目录。明确确认的作品删除交给 Sonarr / Radarr 执行；手机不能提交任意 NAS 路径。旧 `/vol1/1000/serial/` 未由 Sonarr 管理，因此不会自动出现在本页；可先在 Sonarr 中导入并配置相应媒体映射。不要手动移动已经入库或仍在做种的媒体。

## 当前范围

支持单条 YouTube 视频 / Shorts / 直播回放，优先英文人工字幕，其次自动字幕。正在直播的视频、播放列表、需要登录或验证的内容不在首版自动处理范围。没有可用字幕时说明原因，视频下载继续；手机端暂未提供 ASR 兜底。下载优先 AVC/AAC、最高 1080p。Android 强行停止 App 后需重新打开才能恢复后台调度。

这台 OPPO 的后台冻结已定位到 Cirno 墓碑模块，并按上文为 Clash 与飞牛字幕加入白名单。仅允许系统后台活动不足以解除该模块的冻结。App 的「后台运行」入口打开自身系统设置，不自动改变手机省电策略；长期息屏稳定性仍需日常使用观察。

## 验证

```bash
python3 scripts/test.py --suite core
scripts/android.sh :app:testDebugUnitTest :app:assembleDebug :app:assembleDebugAndroidTest
```

真实设备测试 `DeviceSmokeTest` 是显式运行的联调测试。原有下载/恢复用例需要私有 `files/smoke-config.json`，没有该文件时跳过。配置不进入 Git 或 APK；测试可以调用真实模型。分别验证真实下载与翻译、切断 USB 服务/代理通道后的本地字幕解码，以及 NAS 掉线后视频独立完成、进程重启后字幕恢复。不要不加筛选地运行全部联调用例，它们要求不同的通道状态。

独立联网用例直接读取手机已经加密保存的设置，严格检查上述两个地址，无需复制令牌。必须显式传入 `directNetworkSmoke=true`，否则跳过。运行前确认 `adb reverse --list` 无中继，且手机 Tailscale 与 Clash 本地代理同时运行：

```bash
scripts/android.sh :app:assembleDebugAndroidTest
adb install -r android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb shell am instrument -w -e directNetworkSmoke true \
  -e class cn.qwensub.app.DeviceSmokeTest#downloadAndTranslateWithoutComputerRelay \
  cn.qwensub.app.test/androidx.test.runner.AndroidJUnitRunner
```

成功须出现 `OK (1 test)`；`Process crashed`、超时或跳过都不能算通过。测试期间不要覆盖安装或强制停止 App。测试输出的 `files/smoke-result.json` 不包含令牌，可辅助核对 health、任务状态和完成标记。

### 2026-10-14 验收记录

- 独立联网修复后，`downloadAndTranslateWithoutComputerRelay` 在 Wi-Fi 下 **25.134 秒通过**、关闭 Wi-Fi 的 5G 网络下 **26.237 秒通过**，两轮均为 `OK (1 test)`。新下载视频、原字幕、含中英文的双语字幕均实际落盘，并核实 NAS 任务经 `http://100.90.158.48:8765` 创建。全程 `adb reverse --list` 为空，下载代理是手机本机 `127.0.0.1:7890`。
- 随后的本地视频与中文字幕解码测试 **1.775 秒通过**。Clash 留在后台时多次 HTTP 200、内核 `frozen=0`；验收后已恢复 Wi-Fi。USB 保留为控制与诊断连接，不承担中继。此次短测不代表整夜息屏验收。
- JDK 21 + Gradle 8.11.1 成功构建 APK，并安装至已授权 OPPO PKJ110（Android 16 / API 36）。
- Python 后台回归 **168 项通过**，包含新增移动字幕服务的 13 项；安卓单元测试 **13 项通过**；Android Lint 无错误（存在依赖升级和配置建议）。
- 真机五项联调用例通过：真实 YouTube 下载并调用飞牛 `gpt-6-luna`；移除 USB 服务 / 代理通道后从本地文件解码中文字幕；飞牛掉线时视频仍完成；强制停止 App 后恢复原任务；取消两路原生下载后无对应进程残留。
- 实测短片为 “Me at the zoo”，手机保存视频、原文、中文及双语字幕；飞牛只接收字幕。首页、片库及双语播放已目视检查。
- SAF 文件导出已实现，尚未完成系统目录选择器的真机点击验收。手机独立联网配置见上文。
- 最终包：`dist/feiniu-subtitles-0.1.0-debug.apk`，旁边提供 SHA-256。联调结束已移除手机的私有测试配置和测试 APK，保留正式测试版 App、加密连接设置及样例媒体。
- 深色模式增量：构建和原有 13 项安卓单元测试通过，Android Lint 0 错误；OPPO 真机已验证三种外观模式、重新打开界面后保留选择、系统主题变化、片库与双语播放，以及全屏退出后的系统栏配色。验收后恢复系统深色、App 跟随系统。
- 深色模式安装包：`dist/feiniu-subtitles-0.1.0-dark-mode-debug.apk`，附同名 `.sha256` 校验文件，已更新安装到测试手机。

依赖来源：[youtubedl-android](https://github.com/yausername/youtubedl-android)、[Media3](https://developer.android.com/media/media3/exoplayer/media-items)、[WorkManager](https://developer.android.com/topic/libraries/architecture/workmanager)、[系统文件选择器](https://developer.android.com/training/data-storage/shared/documents-files)。

### 2026-10-14 文件清理更新

- 「处理记录」与「离线片库」增加每条任务的清理入口；「连接」页增加批量清理。手机文件与飞牛字幕分别勾选，确认后执行，分别显示结果。
- 后台回归 **176 项通过**（包含移动 API 的 **21 项**）；安卓单元测试 **20 项通过**；Lint **0 错误**。
- OPPO 真机 `CleanupTest` **9 项通过**，只使用独立模拟文件，覆盖实际文件删除、其他任务保留、路径 / 软链接边界、并行下载、取消后等待线程退出与清理状态持久化。现有视频与字幕未用于删除测试。
- 网络验收结束后，已实际点击核对「处理记录」与「离线片库」的清理入口及确认框，验证手机 / 飞牛分别勾选、两处同时勾选、空选时禁用确认，以及取消后返回。设置中的批量管理页面已检查；批量全选及最终确认框尚未完成点击验收。此次只检查界面，核对前后 **8 个任务、49 个媒体文件及清理状态完全一致**，未排队任何删除。
- 飞牛已更新 `mobile_api.py` / `mobile_jobs.py`，认证健康检查已返回 `delete_requests`；更新前的三个已完成任务均保留，未发起实际用户任务的删除请求。此前源码保存在服务目录 `update-cleanup-20261014-1108/previous-source/`。
- 最新包已覆盖安装到手机，另保存为 `dist/feiniu-subtitles-0.1.0-cleanup-debug.apk`，附 SHA-256；测试包保留供后续验收使用。

### 2026-10-14 清理后移除记录

- 所选位置清理成功后移除处理记录；两处均清理时删除内部记录。仅清理一处会保留另一处的管理信息，失败任务继续显示重试入口。仅清理飞牛时，未完成的视频保留下载控制，视频就绪后再移除记录。
- 启动时整理旧版本留下的已清理记录；清理成功状态与旧错误在同一事务内更新，避免进程退出后残留已完成记录。旧工作线程不能恢复被删除的记录，也不能隐藏后来重试的下载。
- 构建、**20 项单元测试**和 Lint（**0 错误**）通过。OPPO 真机 `CleanupTest` **19 项通过**（`OK (19 tests)`，0.549 秒），全部使用独立模拟文件，包含重启迁移、单处删除保留另一处文件、失败重试和防止旧工作线程恢复记录。
- 已通过字幕服务 API 手动清理旧 USB 地址绑定的两条失败任务 `d2fee016`、`3afeb97f`，核对对应字幕任务和检查点均已删除，另一条无关 NAS 任务仍为就绪状态。手机同步成功状态后，新版启动自动移除了 **8 条**已清理旧记录，待清理数和 App 媒体文件数均为 **0**。
- 新包已安装到 OPPO：`dist/feiniu-subtitles-0.1.0-cleanup-records-debug.apk`，附同名 `.sha256`。


### 2026-10-14 视频下载 403 修复

- 实机任务 `3a67093f` 的视频下载多次返回 `HTTP Error 403: Forbidden`，字幕在 14:55 已成功翻译并落盘。实际下载器为 `2025.11.12`，选择了 `android_sdkless` 的媒体 URL；上游已在 [2026.01.29 修复该客户端的 403 问题](https://github.com/yt-dlp/yt-dlp/issues/15712)。
- APK 额外内置官方 `2026.08.19` zipimport 下载核心（含 EJS 脚本），SHA-256 与该发布的 `SHA2-256SUMS` 核对。保留库提供的 Python、FFmpeg 和 QuickJS；初始化锁内校验完整文件后原子替换旧核心，新于内置版本的已安装核心会保留。安装不依赖手机在线更新。后续 YouTube 协议变动时需要刷新资产并发布 APK。
- 视频下载增加格式可用性探测，HTTPS 请求强制按 **1 MiB** 分段。下载器原默认请求 **10 MiB**，此次视频在该大小下仍返回 403；仅请求约 10 KB 的格式探测却成功，说明探测不能替代完整下载验证。保留下载器告警，连接诊断增加实际 `downloader_version`。视频失败不会重新提交已经完成的字幕翻译。
- 真机回归用例仅重试传入的已有任务，核对完整视频、可读取的音视频样本，以及三个 SRT 的 SHA-256、字幕状态、远端身份和连接设置。

```bash
adb shell am instrument -w -e repairDownload true \
  -e taskId 3a67093f-1534-4ad4-9131-3eb64b2da46d \
  -e class cn.qwensub.app.DownloadRepairTest#retryVideoPreservesCompletedSubtitles \
  cn.qwensub.app.test/androidx.test.runner.AndroidJUnitRunner
```

- 最终修复 APK 已覆盖安装到手机；**25 项单元测试**通过、Lint **0 错误**（22 项既有警告），下载核心校验和原任务视频重试两项真机验证 **18.776 秒通过，`OK (2 tests)`**。原视频和字幕都为 `READY`，可读取音视频样本，三个 SRT 的 SHA-256、请求身份及连接设置保持不变。APK 保存为 `dist/feiniu-subtitles-0.1.0-download-fix-debug.apk`，附 SHA-256。

### 2026-10-14 播放字幕重叠修复

- 当前视频的 447 条双语字幕中，439 对相邻字幕存在时间交叠。播放器此前会把两句叠画在底部；播放时现在将上一句的结束时间限制到下一句开始，中英文仍作为一组显示。处理仅发生在播放器内，保存及导出的 SRT 保持原样。
- 降低竖屏及全屏字幕字号，并按视频区域高度和系统字号设置限制大小；播放控制条出现时为字幕预留底部空间，隐藏后恢复。
- 补充播放中快进、快退及从字幕中间恢复的处理，包含最后一条字幕的读取边界。真机 `PlayerSubtitlesTest` **3 项通过**（`OK (3 tests)`，4.922 秒），使用本地模拟音频和字幕，不创建下载或翻译任务；**25 项单元测试**通过，Lint **0 错误**（22 项既有警告）。
- 修复版已覆盖安装到 OPPO，并用现有视频核对双语字幕。APK 保存为 `dist/feiniu-subtitles-0.1.0-subtitle-fix-debug.apk`，附同名 `.sha256`。

### 2026-10-18 飞牛片库服务接入

- 飞牛字幕服务已配置并实际连接 Sonarr / Radarr；认证健康检查提供 `nas_media` 与 `delete_media_requests`。服务令牌和 Arr API Key 留在飞牛，手机沿用既有连接。
- 实际片库为 **The Bear**（47 集条目，8 集已下载）和 **Dear You**（尚无已入库视频），当前两个下载队列为空。旧目录 `/vol1/1000/serial/` 未自动导入。
- 已读取 The Bear S01E01 的英文内嵌 SDH 字幕，共 **602 条**，调用 `gpt-6-luna` 生成中文及双语字幕；三份字幕均通过实际下载核对，中文与双语均含 602 条，并在服务重启后保持就绪。该结果保留在飞牛片库中可选用。
- 实际原片大小 **2,223,022,478 字节**；媒体 `HEAD`、32 字节分段 `206`、越界 `416`、`ETag` 及缺少令牌时 `401` 已核验。字幕准备与视频流独立。
- OPPO 实测原片可显示 4K HEVC 画面，但 EAC3 音轨未解码；已为首集生成 **2,203,737,467 字节**的 AAC 兼容副本。实际核对 HEVC 分辨率保持 3840×1920、前 5 个视频数据包校验值相同，音轨为 48 kHz AAC 双声道；原片大小、修改时间、inode 和设备编号保持不变，兼容流 `HEAD` / 分段请求通过。
- 按最终范围，无文本字幕时跳过处理，视频继续可看 / 可存。本次准备的识别运行时及模型已从飞牛移除，配置不包含识别服务或新增 API 密钥；识别请求返回明确的 `422`，不创建识别任务。
- 更新前源码位于服务目录 `update-media-20261018-0340/previous-source/`，最终无识别版本更新前的源码在 `update-media-noasr-20261018-0340/previous-source/`；启动脚本原件为 `bin/start-subtitles.before-media-20261018T033409Z`。数据库、既有任务、连接令牌与 Codex 登录均保留。
- AAC 兼容功能更新前源码位于服务目录 `update-compatible-20261018/previous-source/`，服务健康能力新增 `compatible_audio`；兼容副本位于独立 `data/media-compatible/` 缓存中。
- 最终回归：Python 核心 **193 项通过**，其中移动服务 **38 项**；安卓单元测试 **37 项通过**，Lint **0 错误**。OPPO 真机 `CleanupTest` **22 项通过**（0.586 秒），使用独立模拟文件，没有清理用户媒体。
- 最终安装包为 `dist/feiniu-subtitles-0.2.0-nas-debug.apk`，附同名 `.sha256`，已覆盖安装至 OPPO PKJ110，连接设置及既有媒体保留。断点续传逻辑与 NAS 分段流已验证，尚未在手机完成整集约 2.2 GB 的离线复制验收。
- OPPO 真机 `NasMediaSmokeTest` 最终 **1 项通过**（`OK (1 test)`，1.715 秒）：读取实际片库、季 / 集、字幕及下载队列，使用首集兼容流，明确选取已生成双语字幕并跳转到首条字幕后，实际确认 **HEVC 视频首帧、AAC 音频解码和中文字幕内容**。原片 EAC3 无声音的问题由兼容音轨处理解决，原视频保持不变。

### 2026-10-18 手机媒体管理更新

- App 更新至 **0.3.0 / versionCode 3**，下载队列增加删除任务，作品详情增加停止 / 恢复自动追更和移除作品。移除作品默认保留 NAS 文件；同时删除文件需要明确勾选。管理请求均需确认，不自动重试；失败保留确认框并提供刷新核对入口。
- 飞牛已更新 `mobile_media.py`；`catalog.capabilities` 返回 `manage_queue`、`manage_titles`。更新前源码位于服务目录 `update-queue-management-20261018-120332/previous-source/`。实际核对两项媒体服务均已连接，片库 2 部作品、下载队列 0 项、字幕任务 4 个就绪、媒体字幕任务 1 个就绪，更新前后记录一致。
- 核心回归 **202 项通过**；随后补充上游队列格式边界，并通过媒体接口 **20 项**测试。安卓单元测试 **48 项通过**，构建成功，Lint **0 错误**。
- 真机页面验收发现服务端空闲连接过期时偶发 `unexpected end of stream`，已将读写连接分开：GET / HEAD 可以恢复失效连接；管理写请求使用独立连接、不保留空闲连接且禁止自动重试。补充了过期读连接恢复及连续写请求隔离测试。
- OPPO 真机 `NasManagementTest` **6 项通过**（最终 `OK (6 tests)`，25.412 秒），覆盖取消不发删除请求、确认后刷新、失败保留提示、默认保留 NAS 文件、明确勾选删除文件、追更确认及旧服务兼容。全部连接手机本机模拟服务，未删除用户媒体。测试包使用 `-PnasManagementUiTest=true` 和隔离 `NasManagementTestRunner`，以普通 `Application` 避免自动恢复真实用户任务；默认测试 runner 保持不变。
- 更新后纯只读 `NasMediaSmokeTest` **1 项通过**（最终 `OK (1 test)`，1.644 秒），再次确认真实片库、下载队列、HEVC 画面、兼容 AAC 音频和双语字幕。
- 安装包 `dist/feiniu-subtitles-0.3.0-management-debug.apk`（附同名 `.sha256`）已覆盖安装到 OPPO，原有连接设置及 3 条视频 / 字幕均就绪的本地任务保留。实际剧集的管理入口与移除确认框已检查并取消，没有向真实片库提交删除。

### 2026-10-18 vivo PA2573 安装与验收

- 当前 USB 设备为 **vivo PA2573，Android 16 / API 36**。首次安装 **0.3.0 / versionCode 3** 成功，应用正常打开。安装包与上面的 0.3.0 发布文件 SHA-256 一致：`fa393cba104c6a25c627f330fdccd8bd96f37ea07b264cc02ee19399792bd09a`。
- 用户自行将 VPN 切换至 Tailscale 后，沿用飞牛现有服务地址和令牌；App 显示 **连接成功 · gpt-6-luna**，设置由 Android Keystore 加密保存。未添加 ADB reverse，也未更改手机 VPN 配置。
- 真机定向测试最终 **27 项通过**（`OK (27 tests)`，7.297 秒）：`CleanupTest` 22 项、`PlayerSubtitlesTest` 3 项、下载核心校验 1 项、真实 NAS 浏览与播放 1 项。实际验证片库、下载队列、视频首帧、兼容 AAC 音频解码和中文字幕；字幕播放测试包括重叠替换及拖动进度后的正确字幕。清理测试仅使用隔离模拟文件。
- 本次构建成功，当前源码对应的安卓单元测试结果为 **48 项通过**。上述 NAS 测试只读，不创建翻译或下载任务，不删除 NAS 媒体。
- `NasManagementTest` 首轮卡在首个界面用例启动阶段，已主动停止；单独复测时测试包安装返回 `INSTALL_FAILED_ABORTED: User rejected permissions`，未再次尝试安装。该组用例未计入本机通过项，不能沿用 OPPO 的结果作为 vivo 验收。
- 切换后手机未发现常用端口 `7890` 的本地下载代理，下载代理设置保留为空；本次未完成 YouTube 实际下载与翻译验收。NAS 浏览与播放不依赖该代理。
- 验收结束移除临时测试 APK 和界面转储文件，保留正式 App、已加密的飞牛连接设置及不含令牌的 NAS 播放测试结果。
