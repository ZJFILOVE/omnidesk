import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QProcess
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from worker_shutdown import stop_worker


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


class WorkerShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def exercise_tree(self, ignore_parent_term):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'child'
            child = ('import os,signal,time; from pathlib import Path; '
                     'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                     f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')
            parent = ('import subprocess,sys,time,signal; '
                      + ('signal.signal(signal.SIGTERM,signal.SIG_IGN); ' if ignore_parent_term else '')
                      + f'subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); '
                      'time.sleep(60)')
            process = QProcess()
            unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
            child_pid = None
            try:
                process.start(sys.executable, ['-c', parent])
                self.assertTrue(process.waitForStarted(2000))
                self.wait_for(lambda: marker.exists() and bool(marker.read_text()))
                child_pid = int(marker.read_text())
                with patch('worker_shutdown.STOP_GRACE_MS', 50), patch('worker_shutdown.KILL_GRACE_MS', 100):
                    stop_worker(process, lambda: True)
                    self.wait_for(lambda: process.state() == QProcess.ProcessState.NotRunning)
                    self.wait_for(lambda: not alive(child_pid))
                self.assertIsNone(unrelated.poll(), 'unrelated process must survive cleanup')
            finally:
                if child_pid and alive(child_pid):
                    os.kill(child_pid, signal.SIGKILL)
                process.kill()
                process.waitForFinished(1000)
                unrelated.kill()
                unrelated.wait()
                QTest.qWait(150)

    def test_force_stop_kills_separate_session_descendant(self):
        self.exercise_tree(ignore_parent_term=True)

    def test_descendant_cleanup_when_worker_exits_on_term(self):
        self.exercise_tree(ignore_parent_term=False)

    def test_reparented_descendants_without_pidfd(self):
        with patch('worker_shutdown.os.pidfd_open', side_effect=OSError('unsupported')):
            self.exercise_tree(ignore_parent_term=False)


if __name__ == '__main__':
    unittest.main()
