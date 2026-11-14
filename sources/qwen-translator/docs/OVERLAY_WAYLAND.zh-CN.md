# Wayland / Hyprland 悬浮字幕与歌词

实时翻译字幕和歌词各有一个独立小窗口。可在 App 的外观设置中调整字体、字号、文字颜色、背景颜色、背景与文字不透明度、圆角、行距和原译文顺序；两个窗口的外观分别保存。透明度由字幕内容自身绘制，降低背景不透明度不会同时降低文字不透明度。

## 移动、尺寸和置顶

拖动字幕文字或标题区域可请求系统移动窗口，右下角可调整尺寸。Wayland 的窗口位置由桌面合成器管理，保存窗口几何信息不代表下次一定能恢复绝对位置。应用通过 Qt `startSystemMove()` 发起交互式移动；它需要在真实鼠标按下事件中调用，不能用定时器不断修改全局坐标来替代。Qt 官方说明 Wayland 不支持用 `setPosition()` 任意移动普通窗口。[Qt 窗口移动说明](https://doc.qt.io/qt-6/qwindow.html#startSystemMove)

标准 Qt 的 `WindowStaysOnTopHint` 是给窗口管理器的提示。Wayland 上浮动、跨工作区、窗口层级及覆盖全屏窗口都由合成器决定。这里使用的是普通 Qt 顶层窗口，没有宣称所有 Wayland 桌面都支持强制置顶；窗口标志本身也可能无法被满足。[Qt 窗口标志说明](https://doc.qt.io/qt-6/qwindow.html#flags-prop)

## Hyprland 0.55 及更新版本

应用检测到可识别的 Hyprland 版本后，使用其 Lua 接口将自己的悬浮窗口设为浮动；启用悬浮窗的置顶选项时，同时固定到各工作区并提升一次窗口层级。关闭选项会解除跨工作区固定，窗口继续保持浮动。运行期操作只匹配本进程、应用标识及具体悬浮窗口标题，不针对当前活动窗口，不修改配置文件，也不持续争抢焦点或窗口层级。

底板有效不透明度为 0（包括「背景完全透明」开关）时，运行期请求会设置 `no_blur`，消除透明窗口背后的模糊灰层；悬浮窗口同时使用 `no_shadow` 和 `border_size = 0` 去掉整窗阴影和边框。恢复有色底板后重新允许桌面模糊。复制的静态规则默认禁用窗口模糊，以便没有运行期接口的旧版也能显示透明区域。接口依据 [Hyprland 0.56 窗口规则](https://wiki.hypr.land/0.56.0/Configuring/Basics/Window-Rules/)。

歌词默认启用屏幕适配。Qt 使用逻辑像素计算当前显示器可用区域，Hyprland 的 `resize` / `move` 调度接口对本进程的歌词窗口应用尺寸与底部居中位置；不会使用当前活动窗口作为目标。关闭屏幕适配后保留手动尺寸。

Hyprland 的 `pin` 表示浮动窗口在各工作区显示；它与其他桌面的“始终置顶”含义不完全相同，全屏应用、其他浮动窗口和桌面规则仍可能影响显示顺序。运行期接口使用明确的 enable / disable 状态，重复打开窗口不会把固定状态反向切换。[Hyprland 0.56 窗口规则](https://wiki.hypr.land/0.56.0/Configuring/Basics/Window-Rules/)、[窗口调度接口](https://wiki.hypr.land/0.56.0/Configuring/Basics/Dispatchers/)

在 App 的字幕或歌词外观设置内，可以查看检测到的桌面、选择配置格式并点击“复制悬浮窗口规则”。需要让窗口创建时即浮动，或希望由自己的桌面配置管理行为时，可将规则加入正在使用的配置。应用不会替你写入、覆盖或重载配置。

本项目保留以下匹配标识，主窗口不匹配这些标题：

| 属性 | 实时字幕 | 悬浮歌词 |
| --- | --- | --- |
| Wayland app ID / Hyprland class | `qwen-live-translate` | `qwen-live-translate` |
| 固定标题 | `Qwen 悬浮字幕` | `Qwen 悬浮歌词` |

歌名放在窗口内容中，不改变窗口标题，以免切歌后桌面规则失效。Lua 示例可粘贴到正在使用的 `hyprland.lua`：

```lua
hl.window_rule({
    name = "qwen-captions-overlay",
    match = { class = "^qwen-live-translate$", initial_title = "^Qwen 悬浮字幕$" },
    float = true,
    pin = true,
    no_initial_focus = true,
    no_blur = true,
    no_shadow = true,
    border_size = 0,
})

hl.window_rule({
    name = "qwen-lyrics-overlay",
    match = { class = "^qwen-live-translate$", initial_title = "^Qwen 悬浮歌词$" },
    float = true,
    pin = true,
    no_initial_focus = true,
    no_blur = true,
    no_shadow = true,
    border_size = 0,
})
```

静态规则只禁止窗口刚出现时自动获取焦点，仍允许点击窗口操作设置。运行期仅在开启鼠标穿透时设置 `no_focus`，关闭穿透后解除，避免已有焦点影响后方窗口的鼠标操作。没有通过合成器的整窗 opacity 把文字一起变淡。规则不强制窗口大小或绝对坐标，尺寸由 App 和实际显示区域决定。

若不想跨工作区显示，把对应的 `pin` 改为 `false`，并关闭 App 悬浮窗的置顶选项。重载配置后重新打开悬浮窗口；若原有窗口仍保留创建时状态，重启 App。已有同标题规则时请合并或替换对应规则，避免重复的尺寸、工作区或浮动设置互相覆盖。

## Hyprland 0.53 / 0.54 与更早版本

配置格式与版本有关。App 会根据版本推荐格式，也允许手动选择：

- 0.55 起优先使用 Lua；不要将 Lua 内容直接粘贴到旧 `.conf` 文件。
- 0.53 / 0.54 选择 `conf`，使用 `windowrule { ... match:initial_title ... }`。
- 0.52 及更早选择 `windowrulev2`，使用旧名称 `initialTitle` 和 `noinitialfocus`。

旧版接入依赖复制的规则，应用不会向旧版发送 Lua 运行期操作。无法识别版本时默认显示 Lua 模板，请按实际配置格式选择。[0.54 官方规则文档](https://wiki.hypr.land/0.54.0/Configuring/Window-Rules/)、[0.52 官方规则文档](https://wiki.hypr.land/0.52.0/Configuring/Window-Rules/)

原生 Wayland 使用上表 app ID。通过 XWayland / `QT_QPA_PLATFORM=xcb` 启动时，class 可能不同，可以用 `hyprctl clients` 检查 class、title、floating 和 pinned；只有确认属于 Qwen 的窗口后，才相应调整自己的匹配规则。不要用全局 `class = ".*"` 代替应用标识。

## 其他 Wayland 桌面

KDE、GNOME、Sway 等桌面需要各自的窗口规则或扩展。应用保留透明绘制和系统拖动能力，但 Hyprland 规则不能在这些桌面通用。更强的桌面覆盖通常需要合成器支持的 layer-shell 等协议以及对应窗口后端；当前实现没有引入这种额外后端，不保证遮盖全屏应用。

## 实现和验证

`overlay_platform.py` 提供纯 Python 的桌面信息、版本对应规则和限定窗口的运行期命令。Qt 界面传入 `QApplication.platformName()`，避免把 Wayland 会话里的 XWayland 应用误判为原生 Wayland；offscreen / minimal 测试会话不会联系桌面合成器。

2026-11-01 在本机 Hyprland 0.56.2 / Qt 6.12 原生 Wayland 下进行了以下验证：

- Lua 与现代 `.conf` 模板均通过 Hyprland `--verify-config`，没有加载到用户会话配置。
- 临时歌词窗口的 class / title 与上表一致；运行期命令使其从平铺变为浮动并固定到各工作区。
- 重复命令保持固定；错误进程 ID 不影响窗口；解除固定后窗口仍为浮动。
- 平台检测、版本分支、模板目标和运行期命令隔离有独立自动测试。

这不代表已经验证所有桌面、显示器布局或全屏播放器组合。
