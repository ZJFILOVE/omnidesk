import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from text_translation import run_process
from video_download import PREFIX, progress_event, progress_options
from video_jobs import command


def event(**fields):
    return progress_event((PREFIX + json.dumps({'status': 'downloading', **fields})).encode())


class DownloadProgressTests(unittest.TestCase):
    def test_audio_resume_estimated_total_and_unknown_size(self):
        update = event(vcodec='none', downloaded=512 * 1024, estimate=1024 * 1024,
                       speed=256 * 1024, eta=2)
        self.assertEqual(update['progress'], .5)
        self.assertIn('下载音轨', update['progress_label'])
        self.assertIn('约 1.0 MiB', update['message'])
        self.assertIn('256.0 KiB/s', update['message'])
        self.assertIn('00:02', update['message'])
        update = event(downloaded=1024, total='NA', estimate=None, speed='NA', eta='NA')
        self.assertIsNone(update['progress'])
        self.assertIn('1.0 KiB', update['message'])
        self.assertNotIn('NA', update['message'])
        self.assertEqual(event(status='finished', downloaded=1024)['progress'], 1)

    def test_merging_is_indeterminate_and_raw_diagnostics_are_not_forwarded(self):
        self.assertIsNone(progress_event(b'WARNING: https://example.com/?token=secret\n'))
        self.assertIsNone(progress_event((PREFIX + '{broken').encode()))
        self.assertIsNone(progress_event((PREFIX + '[]').encode()))
        update = event(postprocessor='Merger', status='started')
        self.assertIsNone(update['progress'])
        self.assertEqual(update['message'], '正在合并音视频…')
        self.assertIsNone(event(postprocessor='Merger', status='finished'))
        self.assertIsNone(event(total=float('nan'), downloaded=1)['progress'])


class StreamingDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_callbacks_arrive_before_exit_and_both_pipes_are_drained(self):
        first = asyncio.Event()
        lines = []
        async def on_line(line):
            lines.append(line)
            first.set()
        code = ('import sys,time; print("first", flush=True); '
                'sys.stderr.write("warning\\n" * 20000); sys.stderr.flush(); '
                'time.sleep(.4); print("last", flush=True)')
        task = asyncio.create_task(run_process([sys.executable, '-c', code], on_line=on_line))
        try:
            await asyncio.wait_for(first.wait(), 3)
            self.assertFalse(task.done(), 'progress must arrive while the child is running')
            result, out, err = await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(result, 0)
        self.assertEqual(out, b'first\nlast\n')
        self.assertEqual(err.count(b'warning'), 20000)
        self.assertIn(b'last\n', lines)

    async def test_real_ytdlp_download_preserves_filepath_and_emits_progress(self):
        downloader = Path(__file__).resolve().parents[1] / '.venv/bin/yt-dlp'
        if not downloader.is_file():
            self.skipTest('yt-dlp is not installed')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.mp4'
            source.write_bytes(b'x' * 1024 * 1024)
            output = root / 'output.mp4'
            updates = []
            async def on_line(line):
                if update := progress_event(line):
                    updates.append(update)
            raw = await command([str(downloader), '--ignore-config', '--no-cache-dir',
                '--no-progress', '--enable-file-urls', '--limit-rate', '1M',
                *progress_options(), '--print', 'after_move:filepath', '-o', str(output),
                source.as_uri()], on_line=on_line)
            self.assertEqual(raw.decode().strip().splitlines()[-1], str(output))
            self.assertEqual(output.read_bytes(), source.read_bytes())
            self.assertTrue(any(0 < (u['progress'] or 0) < 1 for u in updates), updates)
            self.assertTrue(any(u['progress'] == 1 for u in updates), updates)

    async def test_failed_streamed_command_does_not_expose_urls(self):
        with self.assertRaisesRegex(RuntimeError, '退出码 2') as raised:
            await command([sys.executable, '-c',
                           'import sys; print("https://example.com/?secret", file=sys.stderr); sys.exit(2)'],
                          on_line=AsyncMock())
        self.assertNotIn('secret', str(raised.exception))
