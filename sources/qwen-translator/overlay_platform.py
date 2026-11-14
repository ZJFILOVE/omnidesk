"""Desktop detection and narrowly scoped Hyprland support for caption overlays.

No helper writes compositor configuration. Rule snippets are explicitly exported
by the UI; runtime commands affect only this process's named overlay windows.
The module deliberately has no Qt dependency so detection and rule generation
can also be used by diagnostics and tested without a desktop session.
"""
from dataclasses import dataclass
import json
import os
import re
import shutil
import subprocess


APP_ID = 'qwen-live-translate'
# Keep these titles stable and set them before the first show. They are also
# compositor matching keys, so song titles belong inside the lyrics surface.
OVERLAY_TITLES = {'captions': 'Qwen 悬浮字幕', 'lyrics': 'Qwen 悬浮歌词'}
RULE_SYNTAXES = {'lua': 'Lua（Hyprland 0.55+）',
                 'conf': 'Hyprlang .conf（0.53 / 0.54）',
                 'legacy': '旧版 .conf（0.52 及更早）'}


@dataclass(frozen=True)
class DesktopInfo:
    desktop: str = ''
    session_type: str = ''
    qt_platform: str = ''
    hyprland: bool = False
    hyprland_version: str = ''
    hyprctl_available: bool = False

    @property
    def is_wayland(self):
        # QApplication.platformName() is authoritative for this application:
        # an XWayland client can run inside a Wayland desktop session.
        if self.qt_platform:
            return self.qt_platform.startswith('wayland')
        return self.session_type == 'wayland'

    @property
    def is_xwayland(self):
        return self.qt_platform == 'xcb' and self.session_type == 'wayland'


def _version_tuple(value):
    match = re.search(r'(?<!\d)v?(\d+)\.(\d+)\.(\d+)', str(value))
    return tuple(map(int, match.groups())) if match else None


def parse_hyprland_version(output):
    """Parse hyprctl JSON output shared by synchronous and Qt async probes."""
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return ''
    if isinstance(payload, dict):
        for field in ('version', 'tag', 'branch'):
            parsed = _version_tuple(payload.get(field, ''))
            if parsed:
                return '.'.join(map(str, parsed))
    return ''


def detect_desktop(qt_platform=None, *, environ=None, probe=True):
    """Read the desktop and optionally query its version (maximum 0.5 seconds).

    Pass QApplication.platformName() after creating the app. The Qt environment
    setting is only a fallback: it may contain multiple candidate backends.
    Offscreen/minimal test sessions never run hyprctl.
    """
    env = os.environ if environ is None else environ
    platform = str(qt_platform if qt_platform is not None else
                   env.get('QT_QPA_PLATFORM', '')).lower().split(';')[0].split(':')[0]
    desktop = env.get('XDG_CURRENT_DESKTOP') or env.get('XDG_SESSION_DESKTOP', '')
    session = env.get('XDG_SESSION_TYPE', '').lower()
    if not session and env.get('WAYLAND_DISPLAY'):
        session = 'wayland'
    hyprland = ('hyprland' in re.split(r'[:;\s]+', desktop.lower()) or
                bool(env.get('HYPRLAND_INSTANCE_SIGNATURE')))
    hyprland = hyprland and platform not in ('offscreen', 'minimal', 'vnc')
    executable = shutil.which('hyprctl') if hyprland else None
    version = ''
    if executable and probe:
        try:
            result = subprocess.run([executable, '-j', 'version'], capture_output=True,
                                    text=True, timeout=.5, check=False)
            if result.returncode == 0:
                version = parse_hyprland_version(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return DesktopInfo(desktop, session, platform, hyprland, version, bool(executable))


def preferred_rule_syntax(info):
    """Recommend version-appropriate syntax; unknown versions default to Lua."""
    version = _version_tuple(info.hyprland_version)
    if version is None or version >= (0, 55, 0):
        return 'lua'
    return 'conf' if version >= (0, 53, 0) else 'legacy'


def desktop_help(info):
    """Plain text suitable for the app's overlay settings help label."""
    if info.hyprland:
        version = ' ' + info.hyprland_version if info.hyprland_version else '（版本未识别）'
        backend = 'XWayland' if info.is_xwayland else 'Wayland'
        return (f'当前桌面：Hyprland{version} · {backend}。'
                '悬浮窗口需要桌面允许浮动；“跨工作区”对应 Hyprland 的 pin。'
                '置顶和全屏覆盖最终由桌面决定。可以复制适合当前配置格式的规则；'
                '复制后需自行加入配置并重新打开窗口。应用不会修改桌面配置文件。')
    if info.is_wayland:
        return (f'当前桌面：{info.desktop or "Wayland"} · Wayland。'
                '拖动字幕可请求系统移动窗口；置顶、跨工作区和全屏覆盖由桌面管理器决定，'
                '标准 Qt 窗口无法保证。请使用当前桌面的窗口规则；Hyprland 规则不适用于其他桌面。')
    return ('拖动字幕移动窗口，拖动右下角调整大小。置顶效果由窗口管理器决定；'
            '背景透明度在应用内独立绘制，不依赖整窗透明度。')


def _kinds(kind):
    if kind is None:
        return tuple(OVERLAY_TITLES)
    if kind not in OVERLAY_TITLES:
        raise ValueError('Unknown overlay kind: ' + str(kind))
    return (kind,)


def hyprland_rules(kind=None, syntax='lua', pin=True):
    """Return copyable rules for one or both overlays, never the main window.

    These are compositor rules, not shell commands. Background opacity remains
    under app control. We avoid no_focus because it would break settings input.
    """
    if syntax not in RULE_SYNTAXES:
        raise ValueError('Unknown Hyprland rule syntax: ' + str(syntax))
    blocks = []
    for name in _kinds(kind):
        title = OVERLAY_TITLES[name]
        if syntax == 'lua':
            blocks.append('\n'.join([
                'hl.window_rule({',
                f'    name = "qwen-{name}-overlay",',
                f'    match = {{ class = "^{APP_ID}$", initial_title = "^{title}$" }},',
                '    float = true,',
                f'    pin = {"true" if pin else "false"},',
                '    no_initial_focus = true,',
                '    no_blur = true,',
                '    no_shadow = true,',
                '    border_size = 0,',
                '})',
            ]))
        elif syntax == 'conf':
            blocks.append('\n'.join([
                'windowrule {',
                f'    name = qwen-{name}-overlay',
                f'    match:class = ^{APP_ID}$',
                f'    match:initial_title = ^{title}$',
                '    float = on',
                f'    pin = {"on" if pin else "off"}',
                '    no_initial_focus = on',
                '    no_blur = on',
                '    no_shadow = on',
                '    border_size = 0',
                '}',
            ]))
        else:
            match = f'class:^({APP_ID})$,initialTitle:^({title})$'
            effects = ['float', 'noinitialfocus', 'noblur', 'noshadow', 'bordersize 0'] + (['pin'] if pin else [])
            blocks.append('\n'.join(f'windowrulev2 = {effect},{match}' for effect in effects))
    prefix = '--' if syntax == 'lua' else '#'
    return (f'{prefix} Qwen overlays: floating; pin means visible across workspaces.\n'
            f'{prefix} Keep application titles stable. Reopen overlays after adding rules.\n\n' +
            '\n\n'.join(blocks) + '\n')


def hyprland_runtime_command(kind, pinned=True, *, pid=None, transparent=False, geometry=None,
                             click_through=False):
    """Build a Hyprland 0.55+ command for QProcess or subprocess (no shell).

    The caller must check the detected version, run asynchronously, and inspect
    completion. Run after the window is mapped. No match is a harmless no-op.
    All matching happens inside one compositor Lua invocation, including the
    PID check, so another application's active window is never targeted.
    """
    _kinds(kind)
    if kind is None:
        raise ValueError('A runtime command must target one overlay kind')
    target_pid = os.getpid() if pid is None else pid
    if type(target_pid) is not int or target_pid <= 0:
        raise ValueError('A positive process ID is required')
    placement = []
    if geometry is not None:
        if (len(geometry) != 4 or any(type(value) is not int for value in geometry)
                or geometry[2] <= 0 or geometry[3] <= 0):
            raise ValueError('Geometry must be integer x, y, width, height with positive dimensions')
        x, y, width, height = geometry
        placement = [
            f'        hl.dispatch(hl.dsp.window.resize({{ x = {width}, y = {height}, relative = false, window = window }}))',
            f'        hl.dispatch(hl.dsp.window.move({{ x = {x}, y = {y}, relative = false, window = window }}))',
        ]
    title = json.dumps(OVERLAY_TITLES[kind], ensure_ascii=False)
    action = 'enable' if pinned else 'disable'
    code = '\n'.join([
        'for _, window in ipairs(hl.get_windows()) do',
        f'    if window.pid == {target_pid} and window.mapped',
        f'        and window.class == "{APP_ID}" and window.title == {title} then',
        '        if not window.floating then',
        '            hl.dispatch(hl.dsp.window.float({ action = "enable", window = window }))',
        '        end',
        f'        hl.dispatch(hl.dsp.window.set_prop({{ prop = "no_focus", value = "{1 if click_through else 0}", window = window }}))',
        f'        hl.dispatch(hl.dsp.window.set_prop({{ prop = "no_blur", value = "{1 if transparent else 0}", window = window }}))',
        '        hl.dispatch(hl.dsp.window.set_prop({ prop = "no_shadow", value = "1", window = window }))',
        '        hl.dispatch(hl.dsp.window.set_prop({ prop = "border_size", value = "0", window = window }))',
        *placement,
        f'        hl.dispatch(hl.dsp.window.pin({{ action = "{action}", window = window }}))',
        *(['        hl.dispatch(hl.dsp.window.alter_zorder({ mode = "top", window = window }))']
          if pinned else []),
        '    end',
        'end',
    ])
    return ['hyprctl', 'eval', code]
