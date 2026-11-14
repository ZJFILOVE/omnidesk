# Android 容器与真实 4K 验证

本目录是与原生客户端分离的实验工具。2026-10-05 已启动 redroid、运行官方客户端、导入用户扫码批准的会话并实际运行最小请求 hook。最终可用的原生取流不需要容器。

## 本机实验

Arch 内核 7.2.7 使用内置 Rust binder，配置为 `CONFIG_ANDROID_BINDER_IPC_RUST=y`；没有修改内核、加载额外模块或创建宿主 binder 设备。Android 12 使用 memfd 启动成功。官方客户端为 arm64，redroid 内置 native bridge 正常运行它。NVIDIA 宿主的容器实验使用 guest 渲染，不挂载个人主目录。

实际启动命令：

```sh
docker run --name wiliwili-android-lab --privileged -d --restart=no --memory=4g --cpus=4 \
  --mount type=volume,source=wiliwili-android-lab-data,target=/data \
  -p 127.0.0.1:15555:5555 \
  redroid/redroid@sha256:52332b2d74f337982d5ac281a8020ec297fb1ea05cbdcdaaa9c19a2065ae1adc \
  androidboot.use_memfd=1 androidboot.redroid_gpu_mode=guest \
  androidboot.redroid_width=720 androidboot.redroid_height=1280 \
  androidboot.redroid_dpi=240 ro.adb.secure=0
```

使用 [官方 platform-tools](https://developer.android.com/tools/releases/platform-tools) 37.0.1，放在 `/tmp/wiliwili-android-lab/platform-tools`。独立 ADB server 的监听语法是 `-L tcp:15037`，实际监听地址经 ss 确认为 127.0.0.1。客户端固定连接该本机服务：

```sh
env ANDROID_USER_HOME=/tmp/wiliwili-android-lab/adb-home \
  /tmp/wiliwili-android-lab/platform-tools/adb -L tcp:15037 server nodaemon
# 另一个终端：
env ANDROID_USER_HOME=/tmp/wiliwili-android-lab/adb-home \
  ADB_SERVER_SOCKET=tcp:127.0.0.1:15037 \
  /tmp/wiliwili-android-lab/platform-tools/adb connect 127.0.0.1:15555
```

官方 APK 来自 [B 站下载页](https://app.bilibili.com/) 的 android64 下载链接，版本 9.13.0 / build 9130500，SHA256 为 `32bbe9cd9c559ec9594ff511a663b218c7670e34a260232d966d5aa7720bced3`。启动组件是 `tv.danmaku.bili/.MainActivityV2`，已实际打开首页。

## 登录与请求对照

`app_login.py prepare/poll` 走官方 TV 二维码授权，得到的真实 App 会话只保存到 `/tmp/wiliwili-android-lab` 的 0600 文件。已有网页 Cookie 自动确认 QR 再次返回 -101，因此本次由用户手机扫码确认。`account_login.js` 使用官方 `BiliAccounts.signedInWithToken` 与账号信息请求验证登录，不 hook VIP 状态；服务器返回普通非会员账号。

| 实际路径 | 普通请求最高带地址的流 | trial 请求最高带地址的流 |
|---|---|---|
| 容器官方 Android Moss，匿名 | 480P | 480P |
| 容器官方 Android Moss，真实登录 | 1080P | 1080P |
| 同一真实会话的直接 Android gRPC | 1080P | 4K，完整文件已验证 |

不要用返回菜单的 qn 或本地 VIP 标志判断成功。登录后的官方 Moss 路径与直接请求的元数据 / 传输行为仍有差异，未将它们称为等价；最小 hook 没有给当前官方 Moss 路径带来 4K 地址。

直接请求使用 `grpc.biliapi.net/bilibili.app.playerunite.v1.Player/PlayViewUnite`，包含 `authorization: identify_v1 <实际会话>` 与 `x-bili-metadata-bin` 的 access key 字段。只改变 trial 的对照使用相同账号、视频、qn、fnval、设备和客户端参数。视频请求关键字段：

| VideoVod 字段 | protobuf 编号 | 测试值 |
|---|---|---|
| aid / cid / qn | 1 / 2 / 3 | 视频 aid、cid / 120 或 127 |
| fnval | 5 | 4048 |
| forceHost | 7 | 2 |
| fourk | 8 | true |
| preferCodecType | 9 | 2（HEVC），另验证原生 H.264 偏好请求 |
| isNeedTrial | 11 | false / true |

还实测了 aid=0、只有 BV 和 cid 的请求，trial 仍返回同样的 4K 流。响应 `StreamInfo.intact=1`；完整下载进一步证明它不是短试看。媒体 CDN 返回 403 的原因经范围请求对照确认为网页 Referer：无 Referer + `Bilibili Freedoooooom/MarkII` 成功，加入 `https://www.bilibili.com/` 则失败。

## 最小模块复现

核对 [BiliRoamingX](https://github.com/BiliRoamingX/BiliRoamingX) 提交 `ae58109f3acdd53ec2d2b3fb439c2a2ef1886221`：

- `VideoQualityPatch.unlockLimit` 设置 fnval=4048、fourk=true。
- `BangumiPlayUrlHook.hookPlayViewUniteBefore` 设置 `VideoVod.isNeedTrial=true`。
- `TrialQualityPatch.makeVipFree` 清除 qnTrialInfo，只修改已经含 DASH 地址的流的 needVip / vipFree。它不会凭空产生 URL。

项目 README 的完整预构建仓库返回 404，主仓库没有可下载的 release / 保留的构建产物。未安装完整 BiliRoamingX 修补 APK，也未安装 LSPosed；本次用 [Frida Android](https://frida.re/docs/android/) 实际运行提取出的最小 hook。Frida 17.22.1 在此镜像出现 Android helper 断言失败，16.7.19 可正常 attach 官方客户端。测试服务仅监听容器内 127.0.0.1:27042，ADB 只转发到本机 17042。

```sh
/tmp/wiliwili-android-lab/venv16/bin/python native-wiliwili/android-lab/observe.py \
  native-wiliwili/android-lab/play_probe.js \
  --prelude native-wiliwili/android-lab/trial_hook.js \
  --rpc probe --state-dir /tmp/wiliwili-android-lab/account-hook
```

hook 在发送前实际设置 trial，并在修改本地标志前保存原始 reply。匿名最高 480P；登录后仍最高 1080P；VIP 画质条目无 DASH 地址，不能通过修改标志制造流。正向结果来自直接请求和原生 C++ 复现，不能声称新版手机客户端已通过完整模块解锁。

## 验证与保密边界

`report_captures.py` 只导出画质、分辨率、带宽、时长、URL 是否存在及响应哈希。`verify_media.py` 将签名地址通过 curl stdin 传递，完整媒体与原始 protobuf 留在仓库外私有目录；ffprobe 检查完整视频包直到末尾。共享证据位于上一层 `validation/quality-android-*.json`，不含账号标识、Cookie、Token、二维码地址或媒体签名 URL。

原生实现使用现有 libcurl，不需要 Python 取流伴随服务、Frida、ADB、redroid 或额外 gRPC 库。3.0.0 日常登录使用原生 Ctrl+F5 授权界面并保存到独立 0600 文件；本目录 `app_login.py` 仅保留研究复现用途，当前只接纳未加密 DASH 流。会话过期或服务器不再提供试用流时回退网页，可重新扫码；此次结果不保证长期可用。

实验结束后停止本次容器和独立 ADB 服务，保留镜像 / 专用数据卷以便复查。原生验证使用独立临时偏好，只操作测试 PID；`validate_android_4k.py --installed` 验证实际解码、AAC 音轨和 90% 跳转。
