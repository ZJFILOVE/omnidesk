# wiliwili AI 3.0.0 · 原生桌面客户端

已编译并安装到 `/home/zhu/.local/opt/wiliwili-ai`。应用菜单中选择 **wiliwili AI**，或执行：

```sh
~/.local/bin/wiliwili-ai
~/.local/bin/wiliwili-ai --bv BV1heam6TExz
```

这是 wiliwili 1.6.0 的 C++17 / Borealis / NanoVG 原生界面，播放和章节跳转使用 libmpv。本机 Python 服务承接资料采集、对话与广告逻辑；调用现有 `127.0.0.1:43188/v1` 和 `gpt-6-luna`，不使用 embeddings。接口令牌运行时从现有 0600 配置读取，不进入代码或日志。启动器随客户端启动、回收伴随服务；现有 Codex 桥接服务已在运行时继续复用。

功能沿用 bili-helper 2.11.0.5 已验证的客户端流程和广告规则，原版私有服务器的核心算法没有还原，不能称为完全复刻。

## 画质解锁与自动选择

设置 → 播放中新增 **解锁高清（尝试兼容取流）**（默认开启）、**默认画质**（新配置默认自动最高）和 **自动最高时允许 HDR / 杜比视界**（默认关闭）。旧配置明确选择的画质继续保留。播放时按 F2 可选自动最高或手动画质；显示的当前档位以实际视频流为准。自动模式按真实返回的流选最高档，遵守横竖屏上限和编码偏好；未提供的画质重新请求后回退，不再用本地登录判断提前挡住请求。杜比／无损音频仍在原有音质设置中选择，缺少首选音轨会回退。

参考 [Bilibili-Auto-Quality](https://github.com/AHCorn/Bilibili-Auto-Quality) 的能力声明与自动选择思路，原生端直接处理播放接口。普通视频的网页 DASH 返回不足目标画质时，若存在单独扫码批准的 App 会话，先尝试 Android `PlayViewUnite` 的 trial 请求；仅接纳带实际地址、`intact` 为真、时长与网页结果一致且更清晰的未加密流。没有 App 会话或请求失败时继续原有网页 / HTML5 路径。HTML5 高清 MP4 也必须更清晰并提供完整时长，短试看不会替换原视频。

2026-10-05 的接口对照使用两个公开视频：匿名 DASH 均只返回最高 480P；HTML5 分别拿到完整 1280×720 / 1018.389 秒和 2378×1080 / 295.067 秒（第二个是宽画幅）。ffprobe 检查了媒体本身。原生客户端已有普通账号的 DASH 返回最高 1080P。结果见 `validation/quality-api-research.json`、`quality-api-followup.json`、`quality-native-account-research.json`，不包含账号标识、Cookie 或签名播放链接。

安卓模块的机制也做了源码核对：[文章](https://blog.csdn.net/m0_63737826/article/details/130910121) 使用 SimpleHook 改本地 VIP 方法与字段；[BiliRoamingX 请求处理](https://github.com/BiliRoamingX/BiliRoamingX/blob/main/integrations/app/src/main/java/app/revanced/bilibili/patches/protobuf/BangumiPlayUrlHook.kt) 则设置 `VideoVod.isNeedTrial`，并通过 [TrialQualityPatch](https://github.com/BiliRoamingX/BiliRoamingX/blob/main/integrations/app/src/main/java/app/revanced/bilibili/patches/TrialQualityPatch.java) 清理已返回视频流的 VIP／试看标志。这些操作不改变服务端账号会员状态。此前旧版与新版安卓 gRPC 匿名实测均没有 4K 地址；新建 redroid 容器的匿名官方客户端亦如此。

### 已验证的真实 4K 路径（2026-10-05）

redroid Android 12 容器已实际启动，安装并运行 B 站官方客户端 9.13.0。用户扫码批准的 TV 授权会话被 Android 账号接口接受，并通过官方 `signedInWithToken` 登录容器内客户端；服务器确认 `vip_type=0`、`vip_status=0`。同一账号、视频和请求元数据下，普通 gRPC 请求最高提供 1080P，`VideoVod.isNeedTrial=true` 后返回带 URL 的 4K DASH。没有伪造账号会员状态。

完整下载文件经 ffprobe 验证，检查到末尾视频包；编码网格为 4K，不是客户端将 1080P 视频放大。两条素材的实际结果如下（码率按完整文件大小与时长计算）：

| 视频 | 编码分辨率 / 帧率 | 完整文件时长 | 平均视频码率 | 末尾视频包 |
|---|---|---|---|---|
| BV1heam6TExz | 3840×2160 / 59.94 fps，HEVC | 1018.235 秒 | 2.657 Mbps | 1018.367 秒 |
| BV1fK4y1t7hj | 4096×1860 / 120 fps，HEVC，宽画幅 | 295.000 秒 | 5.849 Mbps | 295.000 秒 |

请求、完整媒体哈希与 ffprobe 证据见 [quality-android-account-direct.json](validation/quality-android-account-direct.json)、[quality-android-account-media.json](validation/quality-android-account-media.json)。原生窗口实际解码以上分辨率与 AAC 音轨，并跳到 90% 后继续播放，记录见 [quality-native-4k.json](validation/quality-native-4k.json)。容器和 ADB 关闭后的安装版也通过 H.264 两条视频复测，见 [quality-native-4k-installed.json](validation/quality-native-4k-installed.json)；日常播放无需运行容器。

轻量取流实现位于 [android_playurl.cpp](android_playurl.cpp)，直接用现有 libcurl 的 HTTP/2 发送并解析 protobuf，不依赖 Android 容器或新增 gRPC / protobuf 库。Android 签名媒体地址必须清空网页 Referer；否则 CDN 实测返回 403。播放器为这类流单独设置媒体请求头。App 会话存放于 `~/.config/wiliwili/android-session.json`，要求本用户所有、0600 权限；`WILI_ANDROID_SESSION` 可指定仓库外的测试会话。现有网页 Cookie 和偏好继续独立保存。

本次确实运行了源码提取的最小 Frida trial hook，没有安装完整 BiliRoamingX 修补 APK。当前官方客户端 Moss 自带元数据的请求，即使最小 hook 生效仍只返回 1080P；可用的是已单独验证的直接 gRPC 请求。完整模块预构建仓库当前不可访问，因此不能声称在此版本客户端上完整复现模块全部效果。容器步骤、源码差异和这项负向证据见 [android-lab/README.md](android-lab/README.md)。

该结果适用于此次普通账号与两条公开视频，依赖服务器当前 trial 策略和有效 App 会话，不保证所有视频或长期有效。

## 新版登录与授权

在“我的”页面点账号区域，或在任意非输入状态按 **Ctrl+F5**，打开“登录与高清播放授权”。已有授权先向官方服务器校验并复用；界面分别展示 B站登录和高清授权，并验证账号是否一致。高清扫码成功后，若服务器同时返回有效网页 Cookie，则同一次扫码统一两种会话。账号不同会暂停高清授权，避免串用另一个账号。

点击“高清播放扫码 / 重新授权”后，等界面显示二维码再用手机 B站扫码并确认。界面支持倒计时、等待扫码、待确认、成功、过期重新生成、错误重试和取消。可用刷新接口已经在独立新授权上真实验证成功；无法刷新时直接从原生界面重新扫码。“退出高清授权”只移除 App 会话，“退出全部登录”清除两种本地会话。它不撤销手机或其他设备的登录。

会话文件位于 `~/.config/wiliwili/android-session.json`；网页登录保存在同目录 `wiliwili_config.json`。凭据必须在仓库外、由当前用户所有且权限为 0600，原子更新，不写入日志或诊断。网页 Cookie 始终不会用作 Android access key。授权取消、切视频、切分 P、切账号会取消或丢弃旧请求。退出全部登录会清空“我的”和动态页的旧内容；资料请求返回时再次检查账号，防止迟到响应恢复旧列表。

按 **Ctrl+F3** 查看实际媒体信息：编码尺寸、帧率、视频 / 音频编码、声道、采样率、时长及可获得的码率。数据通过 libmpv 异步属性通知更新；加载中的菜单画质不会作为实际清晰度。流元数据码率与播放器实时估计可能不同，缺失时显示暂无数据。

验证：`bash native-wiliwili/test-quality.sh` 检查选流、HDR、编码、音轨和短试看回退；`python3 native-wiliwili/validate_quality_ui.py` 在独立临时配置中实际播放并操作 F2 菜单，使用 `--installed` 可检查安装版。测试只操作自己的进程。

Android 解析与完整流替换检查：`bash native-wiliwili/test-android-playurl.sh`；可传入仓库外原始 `.bin` 响应进一步验证实际解析。真实 4K 原生播放检查：`python3 native-wiliwili/validate_android_4k.py --installed`，使用独立临时偏好及已批准的 App 会话，只操作自己的测试窗口，原始日志保存在临时私有目录。

### 播放性能

日常 4K 播放直接取流和解码，不会启动 Android 容器。桌面新配置默认启用自动硬解，用户已保存的开关继续优先。按 **Ctrl+F3** 可以检查实际解码方式（例如 `nvdec`，或 CPU 软件解码）及解码／输出丢帧；硬解开关开启不代表当前编码一定能硬解。

倍速会增加每秒需要显示的帧数。2026-10-10 在本机 165 Hz 屏幕上，对同一段 3840×2160、59.94 fps 视频各测量 20 秒：3 倍速约需 180 fps，输出少显示 301 帧；2.5 倍速约需 150 fps，输出丢帧为 0。两次均使用 `nvdec`，解码丢帧为 0，也没有网络缓冲等待。此对照说明该样例的倍速掉帧与屏幕上限一致，不代表所有视频的掉帧原因相同。Ctrl+F3 同时显示倍速所需帧率和可获取的屏幕刷新率；不会自动降低画质或修改用户倍速。

AI 阅读栏按数据变化更新内容，避免每次画面刷新都复制、序列化整份章节与问答，或给相同文字重新排版。播放位置和广告提示仍随播放更新。

在固定 60 FPS 界面、本地 640×360 视频与约 136 KB 合成资料的对照中，10 秒平均 CPU 从单核口径 20.8% 降至 10.9%（约降低 48%）；相同数据重复轮询没有重建正文。此数字仅代表该阅读栏测试负载，不是所有 4K 视频的性能增幅。安装版通过总结、章节、问答异步更新和窄窗布局检查。

`python3 native-wiliwili/benchmark_playback.py --installed --codec 7 --seconds 15 --output /tmp/wiliwili-performance.json` 在独立临时配置中测量真实 4K 播放的 CPU、内存、播放推进与丢帧；`--codec 12` 可对比 HEVC。CPU 的 100% 表示占满一个逻辑核心。原始日志和授权副本只放在临时私有目录，结束后清理；输出报告不含 Cookie、账号或签名媒体地址。

加 `--speed 3` 可复测倍速；报告包含网络缓存时长、缓冲等待与音画同步差。`benchmark_reader.py` 使用本地视频和明确标记的合成资料，独立验证长文阅读栏的 CPU、内容更新和交互，不调用模型。相关证据保存于 `validation/performance-*.json`。

## 桌面界面与键盘

界面使用 [ui-ux-design-pro](https://github.com/saifyxpro/ui-ux-design-pro-skill) 统一改造：首页导航、搜索框、视频卡片、播放器、设置与助手共享纸白、石墨灰和 B 站粉色，规范见 [design-system.md](design-system.md)。助手采用“边看边读”布局，宽窗口位于视频右侧，窄窗口位于视频下方；为播放器预留独立区域，没有全屏遮罩。顶部页签、独立滚动正文、总结下方的可跳转时间线和固定提问入口保持可达。总结支持段落、标题与列表排版；切换页签和后台结果更新会保留阅读位置。广告设置与纠错位于“广告”页。全局基础字号缩小到原来的 82%，并限制文字随窗口放大；本机桌面密度设为 1080p。亮暗主题共享同一套语义色与焦点样式。打开、收起助手不改变播放状态；Space 与倍速键在阅读时继续可用，提问编辑器位于阅读栏区域，输入时隔离播放器快捷键。

小窗保持原有密度；宽度超过 1600 像素后文字适度增大，最多增加 25%，字号落在整数像素上。大窗口的布局缩放最高为 1，额外空间用于内容；阅读栏宽度最多 560 像素。调整窗口时重新测量文字、重排换行，SVG 图标按实际像素尺寸分别缓存，避免复用小图后放大。阅读栏打开时收起重复入口；窄窗上下布局暂时隐藏视频详情区，留出足够的视频高度，收起阅读栏后恢复。

搜索使用系统文字输入，不再显示 A–Z 电视键盘。支持中文输入法、Ctrl+V 粘贴、Ctrl+A 全选、Home / End；输入法选词时 Enter 不提交搜索。输入框内的普通字母不触发导航或视频加速。

候选框兼容修复：Hyprland 下的启动器默认使用 SDL 的 X11 / XWayland 后端，让 Fcitx 候选窗口在普通窗口与全屏中都可见，规避 [Hyprland 0.56.2 的原生输入法弹窗渲染问题](https://github.com/hyprwm/Hyprland/issues/16010)。其他桌面继续使用 SDL 默认后端；显式设置 `SDL_VIDEODRIVER=wayland` 可覆盖此兼容选择。客户端仅在文字编辑器打开时启用输入法，并在缩放、全屏切换后重新计算候选框位置，不使用打开输入框时缓存的缩放值。`python3 native-wiliwili/validate_ime_ui.py --installed` 使用独立配置、真实 Fcitx / Rime 选词和 XTest 按键，检查候选框、选词回车与搜索提交；验证图与结果位于 `validation/ime-*.png` 和 `validation/ime-*-results.json`。

首页横向视频与直播封面按宽度保持 16:9，网格行高随封面宽度重新计算，标题和作者使用独立区域。顶部五个栏目显示数字提示，滚到列表下面仍可用 `1`…`5` 直接切换；按当前栏目的数字可将焦点移回顶部按钮，并保留列表的滚动位置。

`v` 在视频和阅读栏焦点下均切换播放器全屏，默认同步进入显示器全屏，再按一次恢复原窗口；显式关闭“应用内全屏时同步窗口全屏”的设置仍有效。全屏状态独立于视频区域尺寸，侧栏打开后仍可退出全屏。`Esc` / F8 收起阅读栏；提问、搜索输入框中的 `v` 正常输入。已修复底层按键码与动作编号均为整数时的注册冲突，数字跳转键不会再覆盖 `v` 等先注册的快捷键。默认音量键统一为 j/k，解除原版 9/0 音量键与百分比跳转的冲突；用户显式自定义的音量快捷键仍优先。

| 按键 | 操作 |
|---|---|
| `/`、`i`、首页搜索按钮 | 直接搜索关键词、BV 号或视频链接 |
| 首页中 `1`…`5` | 直播 / 推荐 / 热门 / 追番 / 影视；按当前数字回到顶部栏目按钮 |
| 首页中 `r` | 刷新当前栏目（列表深处也可用） |
| `hjkl` | 列表向左、下、上、右导航 |
| `f` | 为当前可见控件显示字母提示，输入提示字母点击；Esc 取消 |
| `gg` / `G`、Ctrl+D / Ctrl+U | 列表开头 / 结尾、向下 / 向上翻页 |
| Enter、Esc、`H` | 打开、返回；输入框内提交、取消 |
| `?` | 打开快捷键帮助 |
| 视频中 `h/l`、`H/L` | 后退 / 前进 5 秒、30 秒 |
| 视频中 `j/k` | 音量减 / 加 5% |
| Space、`[` / `]` | 暂停 / 播放、原生后退 / 前进 |
| `,` / `.`、`<` / `>`、`=` | 倍速减 / 加 0.25、减 / 加 0.5、恢复 1×；范围 0.25–4× |
| `0`…`9`、`v` | 跳到视频 0%…90%、切换视频全屏 |
| Ctrl+F3 / Ctrl+F5 | 实际媒体信息 / 原生登录与高清授权 |
| F8 | 打开 / 收起 AI 阅读栏 |
| Ctrl+Tab | 在视频与 AI 阅读栏之间切换焦点 |
| 面板中 `1`…`4`、`h/l` | 总结 / 章节 / 问答 / 广告，上一页 / 下一页 |
| 面板中 `j/k`、PageDown / PageUp、Ctrl+D / Ctrl+U | 向下 / 向上阅读 |
| Tab / Shift+Tab | 依次选择页签、操作、章节和提问入口 |
| F6 / F7 | 生成总结与章节 / 输入问题 |
| F9 / F10 | 跳过广告 / 这段想看，撤回刚才的跳过 |
| Ctrl+F10 | 当前区间不是广告，永久保存纠错 |
| Ctrl+M / Ctrl+Shift+M | 开始 / 结束手动区间，取消未完成补录 |
| Ctrl+G / Ctrl+Shift+A | 切换自动跳过 / 开关 AI 并取消请求 |
| Ctrl+L | 输入 BV 号或链接，复用当前原生播放器 |
| Ctrl+PageDown / Ctrl+PageUp、F4 | 下一 P / 上一 P、原生分 P / 合集列表 |


广告标记绘制在原生播放器进度条上。橙色代表置信度至少 0.7，黄色代表较低置信度；自动跳过需先显示 3 秒提示。各片段保持独立置信度和边界。人工补录与“不是广告”按确切区间覆盖模型；较新的人工补录只恢复其自身区间，不撤销周围的纠错。

## 独立登录与资料

按用户选择保留独立登录，没有导入 Zen 登录资料。可在用户入口或 Ctrl+F5 的原生授权界面扫码登录。登录后在视频 AI 面板按“识别广告 / 重试”重新采集资料，或重新打开视频。

采集 BV、cid、分 P、分 P 总时长、简介、UP 名称与签名、标签、字幕、分段 protobuf 弹幕、评论和置顶链接。字幕连续分批送给模型，未知品牌与英文推广也参与判断；弹幕只作旁证。自动检测门槛是视频总长 180 秒，与当前进度无关，短视频可按按钮手动识别。

问答支持视频内容、延伸知识及与视频无关的一般问题；视频观点与原话以资料为准，补充知识和推断会与视频依据区分。无字幕时显示不可用，仍支持基于已有简介、弹幕、评论提问、一般问答和手动区间；无法生成可靠字幕章节或自动广告判断。此次未登录真实视频均未返回字幕，已验证这种情况下没有自动跳过或“确认无广告”缓存。新版使用已有 B站登录真实采集 BV1heam6TExz：371 行字幕、13717 条弹幕和 20 条评论，未出现采集警告，证据见 `validation/release-3.0-real-materials.json`。实际广告准确率尚未进行人工标注评估。

缓存位于 `~/.local/state/wiliwili-ai`，按 BV、cid、模型、算法版本区分。失败、格式错误、部分批次失败或无字幕不会成为无广告缓存；人工纠错独立于模型缓存，跨次播放保留。“这段想看”只作用于本次播放。换视频 / P、关闭 AI、退出的请求均有取消与身份校验，旧结果不能修改新播放器。

## 验证与安装记录

详细证据见 [report.html](report.html)，原始结果在 `validation/`。规则测试使用模拟模型返回；桌面测试实际操作原生窗口、键盘、libmpv 和已登录的本机 Codex，字幕与广告样例明确使用本地测试资料。另有未导入登录的真实 B 站网络和播放测试。

本次升级保留被替换的安装备份。安装脚本不依赖旧安装，也不生成原版入口。

后续重复安装会先备份被替换的目录、启动器和桌面文件。`validation/install-results.json` 记录本次路径。未修改现有 Zen 扩展、网页屏蔽设置或全局 Codex 模型配置；原助手工作区的原有改动由 `baseline.json` 的哈希核验保留。

## 源码与重建

上游为官方 `xfangfang/wiliwili`，版本 1.6.0，提交 `88e5876bea9502d06f46a8656e3530684d3aaf7d`，当前完整检出在 `upstream/`。授权沿用上游 GPL-3.0（见 LICENSE）；Borealis 子模块保留自身许可证。

本地重建：

```sh
bash native-wiliwili/rebuild.sh
python3 -m unittest discover -s native-wiliwili -p test_service.py
python3 native-wiliwili/build_release.py
python3 native-wiliwili/install.py --source /path/to/unpacked/wiliwili-ai-3.0.0-arch-x86_64
```

如从新检出恢复，先递归获取上游与子模块，再应用两份补丁：

```sh
git clone --recursive https://github.com/xfangfang/wiliwili.git native-wiliwili/upstream
git -C native-wiliwili/upstream checkout 88e5876bea9502d06f46a8656e3530684d3aaf7d
git -C native-wiliwili/upstream submodule update --init --recursive
git -C native-wiliwili/upstream apply ../integration.patch
git -C native-wiliwili/upstream/library/borealis apply ../../../borealis-keyboard.patch
bash native-wiliwili/rebuild.sh
```

构建依赖本机 libmpv、SDL2、curl、OpenSSL、WebP；fmt、tinyxml2、Borealis 使用上游随附版本。CMake / Ninja 位于本项目独立 toolchain 环境，不更改系统工具链。

重复桌面回归：`python3 native-wiliwili/validate_ui.py --installed`。此脚本会关闭测试中的 wiliwili 窗口、启动已安装程序，使用独立临时客户端配置与纠错缓存，调用真实模型，所有测试样例均在 `validation/fixture.json`。测试媒体可用 FFmpeg 生成 200 秒的 640×360 H.264 静音片段到 `validation/clip.mp4`。

不遮挡播放回归：`python3 native-wiliwili/validate_dock_ui.py --installed`。使用本地明确标记的测试媒体、真实模型和原生窗口，验证左右 / 上下区域不重叠、播放状态、倍速、章节跳转、焦点切换、输入隔离、窄窗口与全屏；证据位于 `validation/redesign-*.png` 和 `validation/dock-ui-results.json`。

界面回归：`python3 native-wiliwili/validate_redesign_ui.py --installed`。使用独立临时偏好检查首页、搜索及真实 B 站播放器中的阅读栏、提问编辑器和全屏返回；截图与结果位于 `validation/redesign-*.png` 和 `validation/redesign-ui-results.json`。

新版桌面回归：`python3 native-wiliwili/validate_keyboard_ui.py --installed`。使用本地测试媒体和样例字幕、真实原生窗口和 libmpv，检查跳转、倍速、输入模式隔离、中文粘贴、提示点击、总结滚动与问答。截图与结果位于 `validation/desktop-*.png` 和 `validation/keyboard-ui-results.json`。

全屏与清晰度回归：`python3 native-wiliwili/validate_fullscreen_ui.py --installed`；加 `--real` 检查真实 B 站播放页、首页与搜索。使用独立临时偏好，仅操作测试进程 PID，不关闭用户的播放器；本地模式不调用模型。检查实际窗口尺寸和 activity 数量、阅读栏焦点下的全屏往返、输入隔离、数字跳转与倍速、小窗 / 全屏 / 窄窗布局以及字号。证据位于 `validation/fullscreen-*-results.json` 和 `validation/fullscreen-*.png`。

首页封面与导航回归：`python3 native-wiliwili/validate_home_ui.py --installed`。使用独立临时偏好和测试进程，检查小窗 / 全屏 / 窄窗的真实视频封面比例与标题空间、深处列表的数字切栏与返回顶部焦点、保留滚动位置及搜索输入数字隔离。证据位于 `validation/home-ui-results.json` 和 `validation/home-*.png`。

## 发行与升级

运行包：`dist/wiliwili-ai-3.0.0-arch-x86_64.tar.zst`；完整 GPL 源码包：`dist/wiliwili-ai-3.0.0-source.tar.zst`；校验和：`dist/SHA256SUMS`。运行包只包含原生程序、资源、本机 AI 伴随服务、同一现有模型接口的桥接代码、安装器、文档与许可证。没有会话、测试媒体、原始响应、研究脚本、容器或私有日志。

当前 Arch Linux 的动态依赖为 Python 3、libmpv、SDL2、curl、OpenSSL、WebP 及它们的系统依赖。本机 AI 继续使用已有 `~/.config/bili-helper-codex/config.json` 和已配置的 gpt-6-luna；不更换模型或另接服务。每个应用进程拥有独立伴随端口，可与另一个原生窗口并行运行。

解压后运行包内的 `python3 install.py` 安装或升级到 `~/.local/opt/wiliwili-ai`；安装前备份被替换的完整目录、启动器和桌面文件到 `~/.local/share/wiliwili-ai/backups/时间戳/`。启动器始终为 `~/.local/bin/wiliwili-ai`，日常点击应用菜单即可，无需打开终端。`--version` 可查看版本而不启动模型服务。源码包已含完整上游和子模块源码，直接 `bash rebuild.sh` 可重建，无需另行获取 Android 环境。

新版脱敏证据见 `validation/release-3.0-*.json`。合成单元测试、明确标记的本地 AI 资料和真实服务器 / 原生窗口的结果分别注明，不能互相替代。二维码只在私有测试目录短期展示，不进入这些证据或发行包。

实际核验范围和限制见 `validation/release-3.0-summary.json`。两条真实 4K、AAC、末段跳转、扫码与会话刷新、普通流回退、分 P 和重播均有真实原生播放证据。已验证退出账号清理旧结果；没有第二个真实账号可验证双账号互切。HDR / AV1 / 杜比音轨保留选流与回退逻辑，但未逐项实播。UGC 合集入口沿用正式取流流程，未另外完成合集实播。广告功能有真实模型与本地素材的交互回归，准确率尚未作人工评测。
