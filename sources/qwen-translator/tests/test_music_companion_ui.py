"""Waybar preferences and typography without live models or media."""
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
from PyQt6.QtCore import QCoreApplication, QEvent, QProcess, QSettings
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QTextBrowser, QVBoxLayout, QWidget

from caption_style import CaptionAppearance
import music_companion_ui


def state():
    return {'track': {'id': 'track-123'}}

class FakePage(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.process = None
        self.stopping = self.closing = False
        self.state = state()
        self.commands = []
        self.caption_appearance = CaptionAppearance(window.settings, prefix='music_caption_')
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Lyrics'))
        self.reading = QTextBrowser()
        layout.addWidget(self.reading)
        self.error = QLabel()
        layout.addWidget(self.error)

    def command(self, action, value=None):
        self.commands.append((action, value, self.state['track']['id']))
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            self.process.write((json.dumps({'action': action, 'value': value}) + '\n').encode())


class MusicCompanionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-companion-')
        self.root = Path(self.tmp.name)
        self.window = QWidget()
        self.window.settings = QSettings(str(self.root / 'settings.ini'), QSettings.Format.IniFormat)
        self.window.theme = 'dark'
        self.window.sync_waybar = False
        self.page = FakePage(self.window)
        QVBoxLayout(self.window).addWidget(self.page)
        self.controller = music_companion_ui.install_music_companion(self.page)
        self.window.resize(680, 850)
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()

    def tearDown(self):
        self.page.closing = True
        for process in self.page.findChildren(QProcess):
            if not sip.isdeleted(process):
                process.kill()
                process.waitForFinished(1500)
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.tmp.cleanup()

    def wait_for(self, predicate, timeout=2000):
        end = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < end:
            QTest.qWait(10)
        self.assertTrue(predicate(), 'companion UI operation timed out')


    def launch_worker(self):
        process = QProcess(self.page)
        process.setProgram(sys.executable)
        process.setArguments(['-u', '-c', 'import sys\nfor line in sys.stdin:\n pass'])
        self.page.process = process
        self.controller.poll_worker()
        process.start()
        self.wait_for(lambda: process.state() == QProcess.ProcessState.Running)
        self.wait_for(lambda: self.page.commands and self.page.commands[-1][0] == 'update_waybar')
        return process

    def test_section_is_collapsed_installed_before_lyrics_and_idempotent(self):
        self.assertFalse(self.controller.body.isVisible())
        self.assertFalse(self.controller.toggle.isChecked())
        layout = self.page.layout()
        self.assertEqual(layout.indexOf(self.controller.section) + 1, layout.indexOf(self.page.reading))
        self.assertIs(music_companion_ui.install_music_companion(self.page), self.controller)
        self.assertEqual(self.controller.toggle.text(), "▸  Waybar 显示")


    def test_waybar_preferences_persist_while_disconnected_without_error_commands(self):
        self.controller.display.setCurrentIndex(self.controller.display.findData('bilingual'))
        self.controller.show_title.setChecked(False)
        self.controller.show_progress.setChecked(False)
        self.assertEqual(self.page.commands, [])
        self.assertEqual(self.window.settings.value('music/waybar_display'), 'bilingual')
        self.assertFalse(self.window.settings.value('music/waybar_show_title', True, type=bool))
        self.assertFalse(self.window.settings.value('music/waybar_show_progress', True, type=bool))
        self.assertEqual(self.controller.preferences(), {'display': 'bilingual', 'show_title': False, 'show_progress': False})

    def test_preferences_are_sent_to_real_worker_and_resent_once_after_restart(self):
        first = self.launch_worker()
        self.assertEqual(self.page.commands[-1][1], {'display': 'translation', 'show_title': True, 'show_progress': True})
        self.controller.poll_worker()
        self.assertEqual(len(self.page.commands), 1)
        self.controller.display.setCurrentIndex(self.controller.display.findData('original'))
        self.assertEqual(self.page.commands[-1][1]['display'], 'original')
        first.closeWriteChannel()
        self.wait_for(lambda: first.state() == QProcess.ProcessState.NotRunning)
        self.page.process = None
        self.controller.poll_worker()
        old_count = len(self.page.commands)
        self.launch_worker()
        self.assertEqual(len(self.page.commands), old_count + 1)
        self.assertEqual(self.page.commands[-1][1]['display'], 'original')

    def test_appearance_changes_do_not_write_live_styles_in_test_settings(self):
        with patch.object(music_companion_ui, 'sync_theme') as sync, \
                patch.object(QProcess, 'startDetached') as reload:
            self.page.caption_appearance.update(size=30)
            QTest.qWait(200)
        sync.assert_not_called()
        reload.assert_not_called()

    def test_real_profile_signal_debounces_and_syncs_only_lyric_appearance(self):
        self.window.sync_waybar = True
        with patch.object(music_companion_ui, 'sync_theme', return_value=self.root / 'qwen-theme.css') as sync, \
                patch.object(QProcess, 'startDetached', return_value=(True, 123)) as reload:
            self.page.caption_appearance.update(size=26)
            self.page.caption_appearance.update(size=28, font_family='Cascadia Code', foreground='#123456')
            self.wait_for(lambda: sync.call_count == 1)
            QTest.qWait(50)
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(sync.call_args.args, ('dark',))
        options = sync.call_args.kwargs['appearance']
        self.assertEqual(options['size'], 28)
        self.assertEqual(options['font_family'], 'Cascadia Code')
        self.assertEqual(options['foreground'], '#123456')
        reload.assert_called_once_with('pkill', ['-SIGUSR2', '-x', 'waybar'])

    def test_missing_appearance_and_theme_are_tolerated(self):
        window = QWidget()
        window.settings = self.window.settings
        window.sync_waybar = False
        page = FakePage(window)
        del page.caption_appearance
        try:
            controller = music_companion_ui.install_music_companion(page)
            self.assertIsNotNone(controller)
            controller.appearance_changed({'size': 22})
            self.assertFalse(controller.appearance_timer.isActive())
        finally:
            window.deleteLater()


if __name__ == '__main__':
    unittest.main()
