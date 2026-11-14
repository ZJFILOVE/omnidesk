import asyncio
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from subtitle_sources import WebClient
from text_translation import run_process
from codex_session import CodexSession


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


class ProcessCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pids = []

    async def asyncTearDown(self):
        for pid in self.pids:
            if alive(pid):
                os.kill(pid, signal.SIGKILL)
        self.tmp.cleanup()

    async def pid_from(self, path):
        async with asyncio.timeout(3):
            while not path.exists() or not path.read_text():
                await asyncio.sleep(.01)
        pid = int(path.read_text())
        self.pids.append(pid)
        return pid

    async def assert_dead(self, pid):
        async with asyncio.timeout(2):
            while alive(pid):
                await asyncio.sleep(.01)

    def sleeper(self, marker):
        return ('import os,signal,time; from pathlib import Path; '
                'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                f'Path({str(marker)!r}).write_text(str(os.getpid())); time.sleep(60)')

    async def test_exited_leader_does_not_leave_sigterm_ignoring_child(self):
        marker = self.root / 'child'
        child = self.sleeper(marker)
        parent = ('import subprocess,sys,time; from pathlib import Path; '
                  f'subprocess.Popen([sys.executable,"-c",{child!r}], '
                  'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
                  f'while not Path({str(marker)!r}).exists(): time.sleep(.01)\n')
        task = asyncio.create_task(run_process([sys.executable, '-c', parent]))
        pid = await self.pid_from(marker)
        code, _, _ = await asyncio.wait_for(task, 4)
        self.assertEqual(code, 0)
        await self.assert_dead(pid)

    async def test_timeout_kills_uncooperative_process(self):
        marker = self.root / 'timeout'
        task = asyncio.create_task(run_process([sys.executable, '-c', self.sleeper(marker)], timeout=.3))
        pid = await self.pid_from(marker)
        with self.assertRaises(TimeoutError):
            await task
        await self.assert_dead(pid)

    async def test_codex_close_reaps_helpers_after_service_exits(self):
        marker = self.root / 'codex-child'
        child = self.sleeper(marker)
        parent = ('import subprocess,sys,time; from pathlib import Path; '
                  f'subprocess.Popen([sys.executable,"-c",{child!r}], '
                  'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n'
                  f'while not Path({str(marker)!r}).exists(): time.sleep(.01)\n')
        session = CodexSession({})
        session.directory = tempfile.TemporaryDirectory(dir=self.root)
        directory = Path(session.directory.name)
        session.process = await asyncio.create_subprocess_exec(sys.executable, '-c', parent,
            start_new_session=True, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        pid = await self.pid_from(marker)
        await session.process.wait()
        await session.close()
        await self.assert_dead(pid)
        self.assertFalse(directory.exists())

    async def test_repeated_cancellation_waits_for_cleanup(self):
        marker = self.root / 'cancel'
        task = asyncio.create_task(run_process([sys.executable, '-c', self.sleeper(marker)]))
        pid = await self.pid_from(marker)
        task.cancel()
        await asyncio.sleep(.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_dead(pid)

    async def test_streaming_cancellation_reaps_process(self):
        marker = self.root / 'stream-cancel'
        task = asyncio.create_task(run_process([sys.executable, '-c', self.sleeper(marker)],
                                              on_line=AsyncMock()))
        pid = await self.pid_from(marker)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.assert_dead(pid)

    async def test_cancel_blocked_http_request_reaps_helper(self):
        marker = self.root / 'http'
        driver = self.root / 'http_helper.py'
        driver.write_text(
            'import sys,runpy,urllib.request,os,time\nfrom pathlib import Path\n'
            f'sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})\n'
            'def blocked(*args, **kwargs):\n'
            f'    Path({str(marker)!r}).write_text(str(os.getpid()))\n'
            '    time.sleep(60)\n'
            'urllib.request.OpenerDirector.open = blocked\n'
            'runpy.run_module("subtitle_sources", run_name="__main__")\n')
        with patch('subtitle_sources.__file__', str(driver)):
            task = asyncio.create_task(WebClient().arequest('https://example.com/subtitle'))
            pid = await self.pid_from(marker)
            started = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        self.assertLess(time.monotonic() - started, 2)
        await self.assert_dead(pid)

    async def test_http_helper_preserves_validation_errors(self):
        with self.assertRaisesRegex(RuntimeError, '本地地址'):
            await WebClient().arequest('http://127.0.0.1/subtitle')


if __name__ == '__main__':
    unittest.main()
