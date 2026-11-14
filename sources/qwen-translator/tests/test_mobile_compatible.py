"""AAC copy cache: real FFmpeg check plus durable/range/eviction acceptance."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mobile_api import create_app
from mobile_compatible import CompatibleMedia
from mobile_jobs import JobError
from mobile_media import MediaLibrary
from nas_api import digest
from test_mobile_media import FakeArr, TOKEN, request, translator


class Library:
    config = {}
    identifier = staticmethod(MediaLibrary.identifier)
    open_file = staticmethod(MediaLibrary.open_file)

    def __init__(self, source):
        self.source = source
        self.duration = 5.

    async def file_record(self, identifier):
        self.identifier(identifier, {'file'})
        info = self.source.stat()
        return {'id': identifier, 'title': 'Example', 'filename': self.source.name,
                'path': self.source, 'size': info.st_size, 'identity': digest([info.st_size, info.st_mtime_ns]),
                'mapping': {'local': self.source.parent}, 'relative': Path(self.source.name)}

    async def inspection(self, record):
        return {'duration': self.duration, 'audio': [{'index': 1, 'language': 'eng'}]}


class CompatibleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'original.mkv'
        self.source.write_bytes(b'original-video-content')
        self.library = Library(self.source)
        self.calls = []

        async def runner(args, **kwargs):
            self.calls.append((args, kwargs))
            Path(args[-1]).write_bytes(b'compatible-video-content')
            await kwargs['on_line'](b'out_time_us=5000000\n')
            return 0, b'', b''
        self.runner = runner
        self.cache = CompatibleMedia(self.root / 'cache', self.library, runner=runner)
        self.addAsyncCleanup(self.cache.close)
        async def probe(*args):
            return {'streams': [{'codec_type': 'video', 'codec_name': 'hevc'},
                                {'codec_type': 'audio', 'codec_name': 'aac'}]}, self.library.duration
        self.probe = patch('video_jobs.probe', side_effect=probe)
        self.probe.start()
        self.addCleanup(self.probe.stop)

    async def test_dedupes_video_copy_publishes_atomically_without_changing_source(self):
        statuses = await asyncio.gather(*(self.cache.submit('sonarr:file:5') for _ in range(3)))
        self.assertTrue(all(s['state'] == 'queued' and s['stream_path'] is None for s in statuses))
        self.assertEqual(len(self.cache.records), 1)
        key = next(iter(self.cache.records))
        self.assertFalse(self.cache.output(key).exists())
        await self.cache.convert(key)
        status = await self.cache.status('sonarr:file:5')
        self.assertEqual(status['state'], 'ready', status)
        self.assertEqual(self.source.read_bytes(), b'original-video-content')
        self.assertEqual(self.cache.output(key).read_bytes(), b'compatible-video-content')
        args, kwargs = self.calls[0]
        self.assertEqual(args[args.index('-c:v') + 1], 'copy')
        self.assertEqual(args[args.index('-c:a') + 1], 'aac')
        self.assertEqual(args[args.index('-ac') + 1], '2')
        self.assertEqual(args[args.index('-f') + 1], 'matroska')
        self.assertEqual(len(kwargs['pass_fds']), 1)
        self.assertFalse(any(p.suffix == '.partial' for p in self.cache.directory.iterdir()))
        await self.cache.submit('sonarr:file:5')
        self.assertEqual(len(self.calls), 1)

    async def test_stream_range_uses_same_confined_response_and_releases_cache_lease(self):
        from mobile_media import range_response
        from starlette.requests import Request
        await self.cache.submit('sonarr:file:5')
        key = next(iter(self.cache.records))
        await self.cache.convert(key)
        record, key = await self.cache.stream_record('sonarr:file:5')
        self.assertEqual(self.cache.readers[key], 1)
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'}, 'method': 'GET',
                 'headers': [(b'range', b'bytes=0-9')], 'path': '/', 'query_string': b''}
        result = range_response(Request(scope), record, on_close=lambda: self.cache.release(key))
        messages = []
        async def send(message):
            messages.append(message)
        async def receive():
            await asyncio.Future()
        await result(scope, receive, send)
        self.assertEqual(messages[0]['status'], 206)
        self.assertEqual(b''.join(m.get('body', b'') for m in messages), b'compatible')
        self.assertNotIn(key, self.cache.readers)

    async def test_failed_conversion_never_publishes_and_retry_requeues(self):
        async def failure(args, **kwargs):
            Path(args[-1]).write_bytes(b'partial')
            return 1, b'', b'/private/path secret-should-not-appear'
        self.cache.runner = failure
        await self.cache.submit('sonarr:file:5')
        key = next(iter(self.cache.records))
        await self.cache.convert(key)
        result = await self.cache.status('sonarr:file:5')
        self.assertEqual(result['state'], 'failed')
        self.assertNotIn('secret', json.dumps(result))
        self.assertFalse(self.cache.output(key).exists())
        with self.assertRaises(JobError) as context:
            await self.cache.stream_record('sonarr:file:5')
        self.assertEqual(context.exception.status, 409)
        self.assertEqual((await self.cache.submit('sonarr:file:5'))['state'], 'queued')

    async def test_shutdown_cancels_owned_process_and_restart_restores_queue(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def blocking(args, **kwargs):
            Path(args[-1]).write_bytes(b'partial')
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        self.cache.runner = blocking
        await self.cache.submit('sonarr:file:5')
        key = next(iter(self.cache.records))
        self.cache.start()
        await asyncio.wait_for(started.wait(), 2)
        await self.cache.close()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.cache.records[key]['state'], 'queued')
        restarted = CompatibleMedia(self.cache.directory, self.library, runner=self.runner)
        try:
            self.assertEqual((await restarted.status('sonarr:file:5'))['state'], 'queued')
            await restarted.convert(key)
            self.assertEqual((await restarted.status('sonarr:file:5'))['state'], 'ready')
        finally:
            await restarted.close()

    async def test_lru_evicts_only_idle_cache_outputs_and_enforces_budget(self):
        self.cache.max_file_bytes = 64 * 1024 * 1024
        self.cache.max_cache_bytes = 64 * 1024 * 1024
        keys = []
        for number in range(2):
            identity = str(number)
            key = self.cache.key(identity)
            keys.append(key)
            with self.cache.output(key).open('wb') as output:
                output.truncate(30 * 1024 * 1024)
            self.cache.records[key] = {'identity': identity, 'media_id': 'sonarr:file:5', 'state': 'ready',
                                       'progress': 1., 'size': 30 * 1024 * 1024, 'last_access': number}
            self.cache.save(key)
        self.cache.readers[keys[0]] = 1
        self.cache.reserve('new-conversion', 30 * 1024 * 1024)
        self.assertTrue(self.cache.output(keys[0]).exists())
        self.assertFalse(self.cache.output(keys[1]).exists())
        self.assertEqual(self.source.read_bytes(), b'original-video-content')
        with self.assertRaises(JobError):
            self.cache.reserve('new-conversion', 40 * 1024 * 1024)

    async def test_new_source_version_uses_different_cache_and_invalid_ids_fail(self):
        await self.cache.submit('sonarr:file:5')
        key = next(iter(self.cache.records))
        self.source.write_bytes(b'replaced source version')
        await self.cache.convert(key)
        self.assertEqual(self.cache.records[key]['state'], 'failed')
        self.assertEqual((await self.cache.status('sonarr:file:5'))['state'], 'idle')
        with self.assertRaises(JobError):
            await self.cache.submit('/etc/passwd')


class RealCompatibleTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/ffprobe required')
    async def test_real_ffmpeg_preserves_video_packets_and_converts_eac3_to_aac(self):
        from text_translation import run_process
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'original.mkv'
            code, _, _ = await run_process(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi',
                '-i', 'testsrc=size=64x64:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000',
                '-t', '1.2', '-c:v', 'mpeg4', '-c:a', 'eac3', '-y', source])
            self.assertEqual(code, 0)
            before = source.read_bytes()
            library = Library(source)
            import video_jobs
            _, library.duration = await video_jobs.probe(str(source))
            cache = CompatibleMedia(root / 'cache', library)
            try:
                await cache.submit('sonarr:file:5')
                key = next(iter(cache.records))
                await cache.convert(key)
                self.assertEqual((await cache.status('sonarr:file:5'))['state'], 'ready')
                output = cache.output(key)
                info, _ = await video_jobs.probe(str(output))
                self.assertEqual([s['codec_name'] for s in info['streams'] if s['codec_type'] == 'audio'], ['aac'])
                hashes = []
                for path in (source, output):
                    code, result, _ = await run_process(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_packets',
                        '-show_entries', 'packet=data_hash', '-show_data_hash', 'sha256', '-of', 'json', path])
                    self.assertEqual(code, 0)
                    hashes.append([p['data_hash'] for p in json.loads(result)['packets']])
                self.assertEqual(hashes[0], hashes[1])
                self.assertEqual(source.read_bytes(), before)
            finally:
                await cache.close()


if __name__ == '__main__':
    unittest.main()
