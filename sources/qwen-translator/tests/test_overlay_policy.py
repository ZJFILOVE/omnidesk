"""Exercise the real QProcess overlay lifecycle without contacting a desktop."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6 import sip
from PyQt6.QtCore import QProcess, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget

from caption_window import CaptionWindow
from overlay_platform import DesktopInfo


WORKER = r'''
import json, pathlib, sys, time
root = pathlib.Path(sys.argv[1])
args = sys.argv[2:]
options = json.loads((root / 'options.json').read_text())
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
if args == ['-j', 'version']:
    time.sleep(options.get('version_delay', 0))
    print(json.dumps({'version': options.get('version', '0.56.2')}), flush=True)
else:
    time.sleep(options.get('runtime_delay', 0))
    if options.get('runtime_exit'):
        print('fixture compositor rejected request', file=sys.stderr, flush=True)
        sys.exit(options['runtime_exit'])
    print('ok', flush=True)
'''


class PolicyWindow(CaptionWindow):
    def showEvent(self, event):
        # Individual tests start the policy explicitly; automatic show timers
        # are covered by the main desktop tests and would obscure race tests.
        QWidget.showEvent(self, event)


class OverlayPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.options = {}
        self.write_options()
        test = self

        class LocalProcess(QProcess):
            def start(self, program, args):
                if test.options.get('missing_program'):
                    super().start(str(test.root / 'missing-program'), [])
                else:
                    super().start(sys.executable, ['-c', WORKER, str(test.root), *args])

        self.process_patch = patch('caption_window.QProcess', LocalProcess)
        self.process_patch.start()
        self.detect_patch = patch('overlay_platform.detect_desktop', return_value=DesktopInfo(
            desktop='Hyprland', session_type='wayland', qt_platform='wayland',
            hyprland=True, hyprctl_available=True))
        self.detect = self.detect_patch.start()
        self.command_patch = patch('overlay_platform.hyprland_runtime_command',
                                   side_effect=lambda kind, pinned, **kwargs: ['fake-runtime', 'apply', str(pinned)])
        self.command = self.command_patch.start()
        self.window = PolicyWindow(kind='lyrics', directions=('inbound',))
        self.window.show()

    def tearDown(self):
        self.window.hide()
        if self.window.desktop_process is not None:
            self.window.desktop_process.kill()
            self.wait_for(lambda: self.window.desktop_process is None)
        self.window.deleteLater()
        self.app.processEvents()
        self.command_patch.stop()
        self.detect_patch.stop()
        self.process_patch.stop()
        self.tmp.cleanup()

    def write_options(self, **kwargs):
        self.options.update(kwargs)
        (self.root / 'options.json').write_text(json.dumps(self.options))

    def calls(self):
        path = self.root / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, predicate, timeout=3000):
        end = time.monotonic() + timeout / 1000
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.assertTrue(predicate(), 'Overlay QProcess operation timed out')

    def test_version_probe_does_not_block_ui_then_runs_policy(self):
        self.write_options(version_delay=.2)
        ticked = []
        QTimer.singleShot(20, lambda: ticked.append(True))
        started = time.monotonic()
        self.window.apply_desktop_policy()
        self.assertLess(time.monotonic() - started, .1)
        self.wait_for(lambda: ticked)
        self.assertIsNotNone(self.window.desktop_process)
        self.wait_for(lambda: len(self.calls()) == 2 and self.window.desktop_process is None)
        self.assertEqual(self.calls(), [['-j', 'version'], ['apply', 'True']])
        self.assertEqual(self.detect.call_args.kwargs, {'probe': False})
        self.assertEqual(self.window.desktop_info.hyprland_version, '0.56.2')
        self.assertIn('跨工作区', self.window.pin.toolTip())

    def test_unknown_and_old_versions_never_execute_runtime(self):
        for version in ('unknown', '0.54.3'):
            self.write_options(version=version)
            self.window.__dict__.pop('desktop_info', None)
            self.window.__dict__.pop('_desktop_version_probed', None)
            self.window.apply_desktop_policy()
            self.wait_for(lambda: self.window.desktop_process is None)
            QTest.qWait(30)
            self.command.assert_not_called()
            self.assertIn('规则', self.window.pin.toolTip())
        self.assertTrue(all(call == ['-j', 'version'] for call in self.calls()))

    def test_missing_hyprctl_never_starts_process(self):
        self.detect.return_value = DesktopInfo(hyprland=True, qt_platform='wayland')
        self.window.apply_desktop_policy()
        self.assertIsNone(self.window.desktop_process)
        self.assertFalse(self.calls())
        self.assertIn('未找到 hyprctl', self.window.pin.toolTip())

    def test_compositor_failure_is_visible_and_process_is_released(self):
        self.write_options(runtime_exit=7)
        self.window.apply_desktop_policy()
        self.wait_for(lambda: len(self.calls()) == 2 and self.window.desktop_process is None)
        self.assertIn('fixture compositor rejected request', self.window.pin.toolTip())
        QTest.qWait(30)
        self.assertFalse(self.window.findChildren(QProcess))

    def test_failed_start_is_visible_and_does_not_keep_a_process(self):
        self.write_options(missing_program=True)
        self.window.apply_desktop_policy()
        self.wait_for(lambda: self.window.desktop_process is None)
        self.assertIn('设置未完成', self.window.pin.toolTip())
        self.command.assert_not_called()

    def test_timeout_kills_only_the_request_and_leaves_a_visible_reason(self):
        self.write_options(runtime_delay=5)
        self.window.apply_desktop_policy()
        self.wait_for(lambda: len(self.calls()) == 2 and self.window.desktop_process is None)
        self.assertIn('超时', self.window.pin.toolTip())
        self.assertTrue(self.window.isVisible())

    def test_latest_pin_choice_is_applied_after_a_pending_request(self):
        self.write_options(runtime_delay=.4)
        self.window.apply_desktop_policy()
        self.wait_for(lambda: len(self.calls()) == 2)
        self.window.pin.setChecked(False)
        self.window.apply_desktop_policy()
        self.wait_for(lambda: len(self.calls()) >= 3 and self.window.desktop_process is None)
        self.assertEqual(self.calls()[-1], ['apply', 'False'])
        self.assertIn('解除', self.window.pin.toolTip())

    def test_deleting_window_kills_and_reaps_an_inflight_request(self):
        self.write_options(runtime_delay=5)
        self.window.apply_desktop_policy()
        self.wait_for(lambda: len(self.calls()) == 2)
        process = self.window.desktop_process
        old_window = self.window
        old_window.deleteLater()
        QTest.qWait(30)
        self.wait_for(lambda: sip.isdeleted(process))
        self.assertTrue(sip.isdeleted(old_window))
        self.window = PolicyWindow(kind='lyrics', directions=('inbound',))

    def test_latest_transparency_is_applied_after_pending_request(self):
        self.write_options(runtime_delay=.4)
        self.window.apply_desktop_policy()
        self.wait_for(lambda: len(self.calls()) == 2)
        self.window.appearance.update(transparent_background=True)
        self.wait_for(lambda: len(self.calls()) >= 3 and self.window.desktop_process is None)
        self.assertTrue(self.command.call_args.kwargs['transparent'])
        self.window.appearance.update(transparent_background=False)
        self.wait_for(lambda: len(self.calls()) >= 4 and self.window.desktop_process is None)
        self.assertFalse(self.command.call_args.kwargs['transparent'])

    def test_click_through_updates_compositor_and_restores_focus_after_pending_request(self):
        self.write_options(runtime_delay=.4)
        self.window.apply_desktop_policy()
        self.wait_for(lambda: len(self.calls()) == 2)
        # The old request is still running when the focused overlay switches
        # to output-only mode. Its completion must not overwrite that choice.
        self.window.appearance.update(click_through=True)
        self.wait_for(lambda: len(self.calls()) >= 3 and self.window.desktop_process is None)
        self.assertTrue(self.command.call_args.kwargs['click_through'])
        self.window.appearance.update(click_through=False)
        self.wait_for(lambda: len(self.calls()) >= 4 and self.window.desktop_process is None)
        self.assertFalse(self.command.call_args.kwargs['click_through'])


if __name__ == '__main__':
    unittest.main()
