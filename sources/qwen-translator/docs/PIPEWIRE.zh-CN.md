# Arch / PipeWire 双向同传

应用菜单搜索 **Qwen 双向同传 / Qwen LiveTranslate** 即可启动原生 Qt 桌面窗口。不使用浏览器或 localhost，也不监听 HTTP 端口；界面通过匿名管道与音频工作进程通信。退出前点“停止”，关闭主窗口也会结束 API 会话和音频进程。运行环境使用 Arch 的 `python-pyqt6`、`libsecret`（secret-tool）、GNOME Keyring，以及项目 `.venv` 中的音频/API 依赖。

中文、英文输出可分别选择预设音色，默认 Tina；也可选择跟随说话人（会话内复刻一次或逐段更新）。变更下次开始时生效。原生应用记住设备、模式和音色；API Key 单独保存至系统密钥库，按区域区分，下次自动读取。第一次需要在新应用内输入一次，点击“保存 Key”或勾选开始时保存。“忘记 Key”可删除对应区域的保存项，不会把 Key 写入普通配置文件。复刻模式需要输入说话人的语音，实际效果和延迟需要另行试听。

“显示 / 隐藏悬浮字幕”打开独立字幕窗口，可移动、调整大小及字号，保留两个方向的最新译文；隐藏字幕窗口不会停止翻译。当前 Hyprland 已添加 `qwen-floating-captions` 规则，字幕窗口浮动并跨工作区显示。关闭主窗口会同时关闭字幕窗口。字幕不写入磁盘。

以下 localhost 地址是保留的旧浏览器 Demo，原生应用不依赖它。需要旧版时才手动启动：

启动：

```bash
cd ~/Projects/qwen-translator
.venv/bin/python -m uvicorn duplex:app --host 127.0.0.1 --port 8010
```

官方 Demo：http://127.0.0.1:8010/

双向页面：http://127.0.0.1:8010/duplex

使用官方 Demo 的 Qwen session 配置和事件解析，音频通过 PipeWire 的 PulseAudio 兼容接口（parec/pacat）传输。输入 PCM16 / 单声道 / 16 kHz，输出 PCM16 / 单声道 / 24 kHz。每个方向独立连接大陆或国际 API。

1. 在双向页面创建虚拟音频，选择实际耳机；输入对应区域的 API Key。双向页面不保存 Key。
2. 暂无麦克风时选“只听”。在 pavucontrol 的播放页面，把播放英语的浏览器/播放器输出切到 meeting-remote，然后开始翻译。原始英语不会自动混入耳机。
3. 接入大疆麦克风后刷新设备，选真实输入，确认系统未静音；选择“只说”或“双向”。
   若要自己听到英文译音，选择“麦克风试听：说中文 → 耳机听英文”。该模式只建立一个中译英会话，直接播放到所选耳机；可展开页面中的中文朗读测试稿。
4. 腾讯会议扬声器选 meeting-remote，麦克风选 Monitor of meeting-translator。耳机由翻译页面使用。
5. 停止会关闭 API 与音频进程。设备断开或任一方向失败时整个会话停止，不自动重连；重新选择设备并开始。字幕仅在页面内存保留。

音频路由：

```text
会议/播放器 → meeting-remote.monitor → 英译中 API → 耳机
真实麦克风 → 中译英 API → meeting-translator.monitor → 会议麦克风
```

虚拟 sink 保留到 PipeWire 重启，不修改系统默认设备。退出测试后将应用输出恢复为耳机；不要将翻译输出再送回输入。程序只绑定本机 8010 端口；新增控制接口检查本机 Origin。不要把该演示服务暴露到局域网或公网。

官方 Demo 会按上游设计将 Key 存入浏览器 localStorage；需要删除时用其 Forget key 按钮。双向页面独立输入 Key，不读取该存储。

本地服务：`systemctl --user status qwen-live-translate.service`；停止：`systemctl --user stop qwen-live-translate.service`；日志：`journalctl --user -u qwen-live-translate.service -n 50`。这是临时用户服务，不开机自启。
