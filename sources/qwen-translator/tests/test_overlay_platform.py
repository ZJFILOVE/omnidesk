"""Desktop detection, safe compositor targets, and versioned overlay rules."""
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from overlay_platform import (APP_ID, OVERLAY_TITLES, DesktopInfo, desktop_help,
                              detect_desktop, hyprland_rules,
                              hyprland_runtime_command, preferred_rule_syntax)


class OverlayPlatformTests(unittest.TestCase):
    def test_qt_backend_overrides_wayland_session(self):
        env = {'XDG_SESSION_TYPE': 'wayland', 'XDG_CURRENT_DESKTOP': 'KDE',
               'WAYLAND_DISPLAY': 'wayland-1'}
        info = detect_desktop('xcb', environ=env, probe=False)
        self.assertFalse(info.is_wayland)
        self.assertTrue(info.is_xwayland)
        self.assertFalse(info.hyprland)
        self.assertTrue(detect_desktop('wayland-egl', environ=env, probe=False).is_wayland)

    def test_hyprland_version_and_syntax_migrations(self):
        env = {'XDG_CURRENT_DESKTOP': 'Hyprland:wlroots', 'WAYLAND_DISPLAY': 'wayland-1'}
        with patch('overlay_platform.shutil.which', return_value='/usr/bin/hyprctl'), \
                patch('overlay_platform.subprocess.run') as run:
            for value, expected in [('0.56.2', 'lua'), ('0.55.0', 'lua'),
                                    ('v0.54.3', 'conf'), ('0.53.0-12-gabc', 'conf'),
                                    ('0.52.2', 'legacy')]:
                run.return_value = subprocess.CompletedProcess([], 0, json.dumps({'tag': value}), '')
                info = detect_desktop('wayland', environ=env)
                self.assertTrue(info.hyprland)
                self.assertTrue(info.is_wayland)
                self.assertEqual(preferred_rule_syntax(info), expected)
                self.assertLessEqual(run.call_args.kwargs['timeout'], .5)
                self.assertEqual(run.call_args.args[0], ['/usr/bin/hyprctl', '-j', 'version'])

    def test_unknown_version_missing_program_timeout_and_bad_json_are_safe(self):
        env = {'HYPRLAND_INSTANCE_SIGNATURE': 'local-instance'}
        for payload in ('garbled', '[]', '{}', '{"version":"unknown"}'):
            with patch('overlay_platform.shutil.which', return_value='/bin/hyprctl'), \
                    patch('overlay_platform.subprocess.run',
                          return_value=subprocess.CompletedProcess([], 0, payload, '')):
                info = detect_desktop('wayland', environ=env)
                self.assertEqual(info.hyprland_version, '')
                self.assertEqual(preferred_rule_syntax(info), 'lua')
        with patch('overlay_platform.shutil.which', return_value=None), \
                patch('overlay_platform.subprocess.run') as run:
            self.assertFalse(detect_desktop('wayland', environ=env).hyprctl_available)
            run.assert_not_called()
        with patch('overlay_platform.shutil.which', return_value='/bin/hyprctl'), \
                patch('overlay_platform.subprocess.run', side_effect=subprocess.TimeoutExpired('hyprctl', .5)):
            self.assertEqual(detect_desktop('wayland', environ=env).hyprland_version, '')

    def test_offscreen_session_never_contacts_compositor(self):
        env = {'XDG_CURRENT_DESKTOP': 'Hyprland', 'HYPRLAND_INSTANCE_SIGNATURE': 'local-instance'}
        with patch('overlay_platform.subprocess.run') as run:
            info = detect_desktop('offscreen', environ=env)
            self.assertFalse(info.hyprland)
            self.assertFalse(info.is_wayland)
            run.assert_not_called()

    def test_rules_target_only_specific_stable_overlay_titles(self):
        for syntax in ('lua', 'conf', 'legacy'):
            captions = hyprland_rules('captions', syntax)
            lyrics = hyprland_rules('lyrics', syntax)
            self.assertIn(APP_ID, captions)
            self.assertIn(OVERLAY_TITLES['captions'], captions)
            self.assertNotIn(OVERLAY_TITLES['lyrics'], captions)
            self.assertIn(OVERLAY_TITLES['lyrics'], lyrics)
            self.assertNotIn(OVERLAY_TITLES['captions'], lyrics)
            self.assertNotIn('no_focus', lyrics)
            self.assertNotIn('opacity', lyrics)
            both = hyprland_rules(syntax=syntax)
            self.assertIn(OVERLAY_TITLES['captions'], both)
            self.assertIn(OVERLAY_TITLES['lyrics'], both)
        self.assertIn('pin = false', hyprland_rules('lyrics', pin=False))
        self.assertIn('pin = off', hyprland_rules('lyrics', 'conf', False))
        self.assertNotIn('windowrulev2 = pin,', hyprland_rules('lyrics', 'legacy', False))

    def test_rules_reject_unknown_targets_and_formats(self):
        with self.assertRaises(ValueError):
            hyprland_rules('main')
        with self.assertRaises(ValueError):
            hyprland_rules(syntax='guess')

    def test_help_is_honest_about_wayland_and_workspace_pinning(self):
        info = DesktopInfo(desktop='GNOME', session_type='wayland', qt_platform='wayland')
        self.assertIn('无法保证', desktop_help(info))
        info = DesktopInfo(desktop='Hyprland', session_type='wayland', qt_platform='wayland',
                           hyprland=True, hyprland_version='0.56.2')
        self.assertIn('跨工作区', desktop_help(info))
        self.assertIn('0.56.2', desktop_help(info))

    def test_runtime_command_is_atomic_scoped_and_explicit_not_toggle(self):
        command = hyprland_runtime_command('lyrics', pid=1234)
        self.assertEqual(command[:2], ['hyprctl', 'eval'])
        code = command[2]
        self.assertIn('window.pid == 1234', code)
        self.assertIn('window.mapped', code)
        self.assertIn(f'window.class == "{APP_ID}"', code)
        self.assertIn(f'window.title == "{OVERLAY_TITLES["lyrics"]}"', code)
        self.assertIn('action = "enable", window = window', code)
        self.assertNotIn('toggle', code)
        self.assertNotIn('get_active_window', code)
        self.assertNotIn('window_rule', code)
        unpin = hyprland_runtime_command('lyrics', pinned=False, pid=1234)[2]
        self.assertIn('pin({ action = "disable", window = window })', unpin)
        self.assertNotIn('alter_zorder', unpin)
        for pid in (0, -1, '1234', True):
            with self.assertRaises(ValueError):
                hyprland_runtime_command('lyrics', pid=pid)
        with self.assertRaises(ValueError):
            hyprland_runtime_command(None)

    @unittest.skipUnless(shutil.which('lua'), 'Lua interpreter is not installed')
    def test_runtime_script_only_changes_the_owned_mapped_lyrics_window(self):
        # Execute the exported Lua against a small compositor fixture, with
        # distractor windows that differ by one ownership criterion each.
        fixture = '''
local windows = {
    { pid=1234, class="qwen-live-translate", title="Qwen 悬浮歌词", mapped=true, floating=false, pinned=false },
    { pid=4321, class="qwen-live-translate", title="Qwen 悬浮歌词", mapped=true, floating=false, pinned=false },
    { pid=1234, class="another-app", title="Qwen 悬浮歌词", mapped=true, floating=false, pinned=false },
    { pid=1234, class="qwen-live-translate", title="Qwen 悬浮字幕", mapped=true, floating=false, pinned=false },
    { pid=1234, class="qwen-live-translate", title="Qwen 悬浮歌词", mapped=false, floating=false, pinned=false },
}
hl = { dsp = { window = {} } }
hl.get_windows = function() return windows end
hl.dsp.window.float = function(args) args.op="float"; return args end
hl.dsp.window.pin = function(args) args.op="pin"; return args end
hl.dsp.window.alter_zorder = function(args) args.op="zorder"; return args end
hl.dsp.window.set_prop = function(args) args.op="prop"; return args end
hl.dsp.window.resize = function(args) args.op="resize"; return args end
hl.dsp.window.move = function(args) args.op="move"; return args end
hl.dispatch = function(args)
    if args.op == "float" then args.window.floating = args.action == "enable" end
    if args.op == "pin" then args.window.pinned = args.action == "enable" end
    if args.op == "prop" then args.window[args.prop] = args.value end
    if args.op == "resize" then args.window.size = {args.x, args.y} end
    if args.op == "move" then args.window.at = {args.x, args.y} end
end
'''
        enable = hyprland_runtime_command('lyrics', pid=1234, transparent=True, click_through=True,
                                          geometry=(3000, 1100, 1200, 200))[2]
        disable = hyprland_runtime_command('lyrics', pinned=False, pid=1234)[2]
        assertions = '''
assert(windows[1].floating and windows[1].pinned)
assert(windows[1].no_blur == "1" and windows[1].no_shadow == "1" and windows[1].border_size == "0")
assert(windows[1].no_focus == "1")
assert(windows[1].at[1] == 3000 and windows[1].at[2] == 1100)
assert(windows[1].size[1] == 1200 and windows[1].size[2] == 200)
for i=2,#windows do
    assert(not windows[i].floating and not windows[i].pinned)
    assert(windows[i].no_blur == nil and windows[i].no_focus == nil and windows[i].at == nil and windows[i].size == nil)
end
'''
        script = '\n'.join([fixture, enable, enable, assertions, disable,
                            'assert(windows[1].floating and not windows[1].pinned and windows[1].no_blur == "0" and windows[1].no_focus == "0")'])
        result = subprocess.run([shutil.which('lua'), '-'], input=script,
                                capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
