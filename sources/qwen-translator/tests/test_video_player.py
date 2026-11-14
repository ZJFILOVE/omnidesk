"""Exercise player shutdown with real processes and delayed termination."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QProcess, QSettings, qInstallMessageHandler
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from native_app import Translator
from video_player import VideoPlayer


class PlayerLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-player-test-')
        self.root = Path(self.tmp.name)
        self.wrapper = self.root / 'slow-player'
        self.wrapper.write_text(
            '#!' + sys.executable + '\n'
            'import signal, sys, time\n'
            'from pathlib import Path\n'
            'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
            "Path(sys.argv[-1] + '.ready').touch()\n"
            'time.sleep(30)\n'
        )
        self.wrapper.chmod(0o700)
        self.player = VideoPlayer()

    def tearDown(self):
        self.player.close()
        for process in self.player.findChildren(QProcess):
            if not sip.isdeleted(process):
                process.kill()
                process.waitForFinished(2000)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.tmp.cleanup()

    def wait_for(self, predicate, timeout=4000):
        deadline = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate(), 'player operation timed out')

    def launch(self, name='first', *, player=None, wait=True):
        player = player or self.player
        media = self.root / (name + '.mkv')
        with patch('video_player.shutil.which', return_value=str(self.wrapper)):
            player.play('mpv', str(media), str(self.root / 'subtitle.srt'))
        process = player.process
        if wait:
            self.wait_for(lambda: Path(str(media) + '.ready').exists())
        return process

    def test_close_keeps_a_live_process_until_exit_is_confirmed(self):
        process = self.launch()
        directory = Path(self.player.directory.name)
        messages, errors = [], []
        previous = qInstallMessageHandler(lambda kind, context, message: messages.append(message))
        try:
            # A process blocked on remote I/O may survive both signals and waits.
            # Its Qt wrapper must remain alive even after deferred deletion runs.
            with patch.object(QProcess, 'terminate'), patch.object(QProcess, 'kill'), \
                    patch.object(QProcess, 'waitForFinished', return_value=False), \
                    patch('sys.excepthook', side_effect=lambda *error: errors.append(error)):
                self.player.close()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertFalse(sip.isdeleted(process), 'live player was deleted')
                self.assertEqual(process.state(), QProcess.ProcessState.Running)
                self.assertTrue(directory.is_dir(), 'IPC directory was removed before exit')
            self.assertFalse(errors, errors)
            self.assertFalse(any('Destroyed while process' in message for message in messages), messages)
        finally:
            qInstallMessageHandler(previous)
        process.kill()
        self.wait_for(lambda: sip.isdeleted(process))
        self.assertFalse(directory.exists())
        self.assertTrue(self.player.close())

    def test_close_returns_promptly_and_force_stops_a_stubborn_player(self):
        process = self.launch()
        statuses = []
        self.player.status.connect(statuses.append)
        started = time.monotonic()
        self.assertFalse(self.player.close())
        self.assertLess(time.monotonic() - started, .25)
        self.assertFalse(self.player.close(), 'shutdown must wait for the live player')
        self.wait_for(lambda: sip.isdeleted(process))
        self.assertTrue(self.player.close())
        self.assertFalse(statuses, 'intentional stop must not report a player failure')

    def test_old_errors_and_exit_do_not_change_the_new_player(self):
        old = self.launch()
        old_directory = Path(self.player.directory.name)
        current = self.launch('second')
        directory = Path(self.player.directory.name)
        statuses = []
        self.player.status.connect(statuses.append)
        old.errorOccurred.emit(QProcess.ProcessError.Crashed)
        old.kill()
        self.wait_for(lambda: sip.isdeleted(old))
        self.assertIs(self.player.process, current)
        self.assertEqual(current.state(), QProcess.ProcessState.Running)
        self.assertEqual(self.player.current_media, str(self.root / 'second.mkv'))
        self.assertTrue(self.player.timer.isActive())
        self.assertTrue(directory.is_dir())
        self.assertFalse(old_directory.exists())
        self.assertFalse(statuses, 'retired player callbacks changed current playback')

    def test_failed_start_cleans_up_without_polling_for_ipc(self):
        statuses = []
        self.player.status.connect(statuses.append)
        with patch('video_player.shutil.which', return_value=str(self.root / 'missing-player')):
            self.player.play('mpv', '/tmp/media.mkv', '/tmp/subtitle.srt')
        directory = Path(self.player.directory.name) if self.player.directory else None
        self.wait_for(lambda: self.player.process is None)
        self.assertIsNone(self.player.current_media)
        self.assertIsNone(self.player.current_player)
        self.assertIsNone(self.player.directory)
        self.assertFalse(self.player.timer.isActive())
        if directory:
            self.assertFalse(directory.exists())
        self.assertTrue(statuses[-1].startswith('播放器启动失败：'), statuses)
        self.assertTrue(self.player.close())

    def test_close_while_the_player_is_still_starting(self):
        process = self.launch(wait=False)
        self.player.close()
        self.wait_for(lambda: sip.isdeleted(process))
        self.assertIsNone(self.player.process)
        self.assertFalse(self.player.timer.isActive())
        self.assertTrue(self.player.close())

    def test_natural_exit_releases_the_process_and_ipc_directory(self):
        statuses = []
        self.player.status.connect(statuses.append)
        with patch('video_player.shutil.which', return_value='/usr/bin/true'):
            self.player.play('mpv', '/tmp/media.mkv', '/tmp/subtitle.srt')
        process = self.player.process
        directory = Path(self.player.directory.name)
        self.wait_for(lambda: sip.isdeleted(process))
        self.assertIsNone(self.player.process)
        self.assertIsNone(self.player.current_media)
        self.assertIsNone(self.player.directory)
        self.assertFalse(directory.exists())
        self.assertFalse(self.player.timer.isActive())
        self.assertEqual(statuses[-1], '播放器已退出。')

    def test_window_waits_for_player_shutdown_before_closing(self):
        settings = QSettings(str(self.root / 'prefs.ini'), QSettings.Format.IniFormat)
        window = Translator(settings, initialize=False)
        try:
            process = self.launch(player=window.video_page.player)
            window.show()
            self.assertFalse(window.close(), 'window closed while the player was still running')
            self.assertTrue(window.isVisible())
            self.wait_for(lambda: not window.isVisible())
            self.assertTrue(sip.isdeleted(process))
        finally:
            for child in window.findChildren(QProcess):
                if not sip.isdeleted(child):
                    child.kill()
                    child.waitForFinished(2000)
            window.close()
            self.app.processEvents()


if __name__ == '__main__':
    unittest.main()
