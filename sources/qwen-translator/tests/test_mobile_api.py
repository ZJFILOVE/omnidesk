"""Subtitle-only API acceptance tests: no NAS account, network model, or Android required."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from mobile_api import create_app, read_token
from mobile_jobs import JobError, MODEL, MobileJobs
from text_translation import run_process
from video_subtitles import Cue, compose, parse_srt, translate_cues

TOKEN = 'mobile-test-token-' + 'a' * 32
CUES = [Cue(0, 1000, 'Hello.'), Cue(600_000, 601_000, 'Next chapter.')]


def payload(**overrides):
    return {'client_request_id': str(uuid4()), 'title': 'Example',
            'source_url': 'https://www.youtube.com/watch?v=example',
            'subtitle_srt': compose(CUES), **overrides}


async def mock_translate(cues, config, directory, emit):
    await emit({'progress': .5, 'message': 'mock translating'})
    return [replace(c, text='中文' + str(i)) for i, c in enumerate(cues)]


async def request(app, path, *, method='GET', body=None, token=TOKEN, headers=None, raw=None, chunked=False):
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else b''
    pairs = [(b'content-type', b'application/json')]
    if token is not None:
        pairs.append((b'authorization', ('Bearer ' + token).encode()))
    if headers:
        pairs.extend((k.lower().encode(), str(v).encode()) for k, v in headers.items())
    scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
             'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
             'query_string': b'', 'headers': pairs, 'client': ('127.0.0.1', 10000),
             'server': ('127.0.0.1', 8765), 'root_path': ''}
    messages = []
    chunks = [data[:len(data)//2], data[len(data)//2:]] if chunked else [data]

    async def receive():
        if chunks:
            part = chunks.pop(0)
            return {'type': 'http.request', 'body': part, 'more_body': bool(chunks)}
        await asyncio.Future()

    async def send(value):
        messages.append(value)

    await app(scope, receive, send)
    response = next(m for m in messages if m['type'] == 'http.response.start')
    content = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    headers = dict(response.get('headers', []))
    result = json.loads(content) if b'application/json' in headers.get(b'content-type', b'') else content.decode()
    return response['status'], result, headers


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


class MobileApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(data_dir=self.tmp.name, token=TOKEN, translator=mock_translate, start_worker=False)
        self.context = self.app.router.lifespan_context(self.app)
        await self.context.__aenter__()
        self.addAsyncCleanup(self.context.__aexit__, None, None, None)
        self.jobs = self.app.state.jobs

    async def submit(self, data=None):
        code, job, _ = await request(self.app, '/v1/jobs', method='POST', body=data or payload())
        self.assertEqual(code, 202, job)
        return job

    async def test_authentication_applies_to_health_jobs_and_unknown_routes(self):
        for path in ('/health', '/v1/jobs', '/anything'):
            for token in (None, 'incorrect'):
                code, _, headers = await request(self.app, path, token=token)
                self.assertEqual(code, 401)
                self.assertEqual(headers[b'www-authenticate'], b'Bearer')
        code, health, _ = await request(self.app, '/health')
        self.assertEqual(code, 200)
        self.assertEqual(health, {'status': 'ok', 'model': MODEL, 'api_version': 1,
                                  'capabilities': ['delete_requests']})
        self.assertEqual((await request(self.app, '/v1/video'))[0], 404)
        self.assertEqual((await request(self.app, '/v1/jobs/' + str(uuid4())))[0], 404)

    async def test_strict_validation_never_silently_drops_bad_cues(self):
        bad = ['', 'not SRT', compose(CUES) + '3\n00:11:00,000 --> 00:10:00,000\nBad\n\n',
               compose(CUES).replace('1\n', '2\n', 1),
               '1\n00:00:00,000 --> 00:00:01,000\n<b></b>\n\n',
               '1\n00:00:00,000 --> 00:00:01,000\nnull\x00\n\n',
               '1\n00:00:00,000 --> 00:00:01,000\n' + 'x' * 8001 + '\n\n',
               compose([CUES[1], CUES[0]])]
        for body in bad:
            code, result, _ = await request(self.app, '/v1/jobs', method='POST', body=payload(subtitle_srt=body))
            self.assertEqual(code, 422, result)
        for data in (payload(client_request_id='not-a-uuid'), payload(model='other'),
                     payload(source_url='https://user:password@example.com'), payload(subtitle_srt=42)):
            self.assertEqual((await request(self.app, '/v1/jobs', method='POST', body=data))[0], 422)
        self.assertEqual((await request(self.app, '/v1/jobs', method='POST', raw=b'{bad'))[0], 422)
        self.assertEqual(self.jobs.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    async def test_chunked_and_declared_request_size_caps_before_json_parsing(self):
        small = create_app(data_dir=self.tmp.name, token=TOKEN, max_request_bytes=64)
        for kw in ({'headers': {'Content-Length': 65}}, {'raw': b'x' * 65, 'chunked': True}):
            self.assertEqual((await request(small, '/v1/jobs', method='POST', **kw))[0], 413)
        self.assertEqual((await request(small, '/v1/jobs', headers={'Content-Length': '-1'}))[0], 400)
        # Auth is checked before processing even an oversized upload.
        self.assertEqual((await request(small, '/v1/jobs', token=None, raw=b'x' * 100))[0], 401)

    async def test_idempotence_conflict_queue_limit_and_persistent_request_id(self):
        self.jobs.max_pending = 1
        data = payload()
        first = await self.submit(data)
        self.assertEqual((await self.submit(data))['id'], first['id'])
        self.assertEqual((await request(self.app, '/v1/jobs', method='POST', body={**data, 'title': 'Different'}))[0], 409)
        self.assertEqual((await request(self.app, '/v1/jobs', method='POST', body=payload()))[0], 429)
        await self.jobs.cancel(first['id'])
        self.assertEqual((await self.submit(data))['state'], 'cancelled')
        second = await self.submit()
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual((await request(self.app, f"/v1/jobs/{first['id']}/retry", method='POST'))[0], 429)

    async def test_translate_and_atomically_serve_all_variants_with_fixed_model(self):
        captured = []
        async def translator(cues, config, directory, emit):
            captured.append(config)
            return await mock_translate(cues, config, directory, emit)
        self.jobs.translator = translator
        job = await self.submit()
        base = f"/v1/jobs/{job['id']}"
        self.assertEqual((await request(self.app, base + '/subtitles/zh'))[0], 409)
        self.assertEqual((await request(self.app, base + '/subtitles/original'))[1], compose(CUES))
        self.jobs.start()
        await until(lambda: self.jobs.get(job['id'])['state'] == 'ready')
        self.assertEqual(captured[0]['text_provider'], 'codex')
        self.assertEqual(captured[0]['codex_model'], 'gpt-6-luna')
        for kind in ('original', 'zh', 'bilingual'):
            code, text, headers = await request(self.app, base + '/subtitles/' + kind)
            self.assertEqual(code, 200)
            cues = parse_srt(text)
            self.assertEqual(len(cues), 2)
            self.assertEqual([(c.start, c.end) for c in cues], [(c.start, c.end) for c in CUES])
            self.assertIn(b'no-store', headers[b'cache-control'])
            if kind == 'bilingual':
                self.assertEqual(cues[0].text, '中文0\nHello.')
        self.assertEqual((await request(self.app, base + '/retry', method='POST'))[0], 409)
        self.assertEqual((await request(self.app, base + '/cancel', method='POST'))[1]['state'], 'ready')
        self.assertEqual((await request(self.app, base + '/subtitles/missing'))[0], 404)

    async def test_failed_translation_has_no_partial_outputs_and_hides_provider_error(self):
        async def fail(*args):
            raise RuntimeError('private prompt and sk-secret')
        self.jobs.translator = fail
        job = await self.submit()
        self.jobs.start()
        await until(lambda: self.jobs.get(job['id'])['state'] == 'failed')
        result = (await request(self.app, '/v1/jobs/' + job['id']))[1]
        self.assertNotIn('sk-secret', str(result))
        self.assertIsNone(self.jobs.record(job['id'])['zh'])
        self.assertIsNone(self.jobs.record(job['id'])['bilingual'])
        self.assertEqual((await request(self.app, f"/v1/jobs/{job['id']}/subtitles/bilingual"))[0], 409)

    async def test_retry_reuses_completed_batch_checkpoints(self):
        calls, failing = [], [True]
        class FakeModel:
            async def translate(self, text):
                data = json.loads(text)
                ids = [item['id'] for item in data]
                calls.extend(ids)
                if ids == [1] and failing[0]:
                    raise RuntimeError('temporary outage')
                return {'text': json.dumps([{'id': i, 'text': '翻译' + str(i)} for i in ids])}
            async def close(self):
                pass
        async def translator(cues, config, directory, emit):
            return await translate_cues(cues, {**config, 'translation_concurrency': 1}, directory, emit,
                                        factory=lambda config: FakeModel())
        self.jobs.translator = translator
        job = await self.submit()
        self.jobs.start()
        await until(lambda: self.jobs.get(job['id'])['state'] == 'failed')
        self.assertEqual(calls, [0, 1])
        self.assertTrue(list((Path(self.tmp.name) / 'checkpoints' / job['id']).glob('translation-*.json')))
        failing[0] = False
        code, result, _ = await request(self.app, f"/v1/jobs/{job['id']}/retry", method='POST')
        self.assertEqual(code, 200)
        self.assertEqual(result['id'], job['id'])
        await until(lambda: self.jobs.get(job['id'])['state'] == 'ready')
        self.assertEqual(calls, [0, 1, 1])

    async def test_cancel_reaps_running_translator_process_then_retry_completes(self):
        marker = Path(self.tmp.name) / 'translator.pid'
        async def subprocess_translator(cues, config, directory, emit):
            script = 'import os,time,pathlib; pathlib.Path(' + repr(str(marker)) + ').write_text(str(os.getpid())); time.sleep(60)'
            await run_process([sys.executable, '-c', script], timeout=65)
        self.jobs.translator = subprocess_translator
        job = await self.submit()
        self.jobs.start()
        await until(marker.exists)
        pid = int(marker.read_text())
        code, cancelled, _ = await request(self.app, f"/v1/jobs/{job['id']}/cancel", method='POST')
        self.assertEqual((code, cancelled['state']), (200, 'cancelled'))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.jobs.translator = mock_translate
        await request(self.app, f"/v1/jobs/{job['id']}/retry", method='POST')
        await until(lambda: self.jobs.get(job['id'])['state'] == 'ready')

    async def test_cancel_before_translation_coroutine_starts_still_returns_200(self):
        calls = []
        async def translator(*args):
            calls.append(True)
            await asyncio.Future()
        self.jobs.translator = translator
        job = await self.submit()
        self.jobs.start()
        # Let the scheduler create its task, then cancel before that task starts.
        await asyncio.sleep(0)
        self.assertIsNotNone(self.jobs.active)
        code, result, _ = await request(self.app, f"/v1/jobs/{job['id']}/cancel", method='POST')
        self.assertEqual((code, result['state']), (200, 'cancelled'))
        self.assertEqual(calls, [])

    async def test_only_one_worker_can_own_a_data_directory(self):
        with self.assertRaisesRegex(RuntimeError, 'already in use'):
            MobileJobs(self.tmp.name)

    async def test_delete_removes_only_selected_subtitles_and_files_and_is_idempotent(self):
        data = payload(subtitle_srt=compose([replace(CUES[0], text='PRIVATE-DELETE-PAYLOAD-' * 250)]))
        job = await self.submit(data)
        keep = await self.submit()
        self.jobs.start()
        await until(lambda: all(self.jobs.get(item['id'])['state'] == 'ready' for item in (job, keep)))
        cache = Path(self.tmp.name) / 'checkpoints' / job['id']
        (cache / 'translation.json').write_text('PRIVATE-CHECKPOINT')
        other = Path(self.tmp.name) / 'checkpoints' / keep['id'] / 'translation.json'
        other.write_text('keep')
        path = '/v1/requests/' + data['client_request_id']
        for token in (None, 'incorrect'):
            self.assertEqual((await request(self.app, path, method='DELETE', token=token))[0], 401)
        self.assertTrue(cache.exists())
        for _ in range(2):
            code, body, _ = await request(self.app, path, method='DELETE')
            self.assertEqual((code, body), (200, {'status': 'deleted'}))
        self.assertFalse(cache.exists())
        self.assertEqual(other.read_text(), 'keep')
        self.assertEqual(self.jobs.get(keep['id'])['state'], 'ready')
        self.assertEqual(self.jobs.db.execute('SELECT COUNT(*) FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], 0)
        for suffix in ('', '/subtitles/original', '/subtitles/zh', '/subtitles/bilingual'):
            self.assertEqual((await request(self.app, '/v1/jobs/' + job['id'] + suffix))[0], 404)
        self.assertEqual((await request(self.app, '/v1/jobs', method='POST', body=data))[0], 410)
        self.assertEqual((await request(self.app, '/v1/jobs', method='POST', body={**data, 'title': 'changed'}))[0], 410)
        self.assertNotIn(b'PRIVATE-DELETE-PAYLOAD', (Path(self.tmp.name) / 'jobs.sqlite3').read_bytes())
        self.assertEqual((Path(self.tmp.name) / 'jobs.sqlite3-wal').stat().st_size, 0)

    async def test_delete_unknown_request_blocks_late_submission_and_validates_uuid(self):
        data = payload()
        path = '/v1/requests/' + data['client_request_id']
        for _ in range(2):
            self.assertEqual((await request(self.app, path, method='DELETE'))[:2], (200, {'status': 'deleted'}))
        self.assertEqual((await request(self.app, '/v1/jobs', method='POST', body=data))[0], 410)
        for bad in ('not-a-uuid', '..', 'checkpoints'):
            self.assertEqual((await request(self.app, '/v1/requests/' + bad, method='DELETE'))[0], 422)
        self.assertEqual(self.jobs.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    async def test_delete_waits_for_translator_cleanup_and_fences_concurrent_operations(self):
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def translator(cues, config, directory, emit):
            (directory / 'initial.json').write_text('private')
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                (directory / 'last.json').write_text('late checkpoint')
        self.jobs.translator = translator
        data = payload()
        job = await self.submit(data)
        self.jobs.start()
        await started.wait()
        path = '/v1/requests/' + data['client_request_id']
        first = asyncio.create_task(request(self.app, path, method='DELETE'))
        try:
            await cleaning.wait()
            second = asyncio.create_task(request(self.app, path, method='DELETE'))
            await asyncio.sleep(0)
            self.assertFalse(first.done())
            self.assertFalse(second.done())
            self.assertEqual((await request(self.app, '/v1/jobs', method='POST', body=data))[0], 410)
            for action in ('retry', 'cancel'):
                self.assertEqual((await request(self.app, '/v1/jobs/' + job['id'] + '/' + action, method='POST'))[0], 410)
            self.assertTrue((Path(self.tmp.name) / 'checkpoints' / job['id']).exists())
        finally:
            release.set()
        self.assertEqual((await first)[:2], (200, {'status': 'deleted'}))
        self.assertEqual((await second)[:2], (200, {'status': 'deleted'}))
        self.assertFalse((Path(self.tmp.name) / 'checkpoints' / job['id']).exists())

    async def test_disconnected_delete_continues_cleanup_without_recancelling_translator(self):
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def translator(cues, config, directory, emit):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()
                (directory / 'last.json').write_text('late checkpoint')
        self.jobs.translator = translator
        data = payload()
        job = await self.submit(data)
        self.jobs.start()
        await started.wait()
        # Cancellation may already be cleaning up when a user chooses deletion.
        cancel = asyncio.create_task(self.jobs.cancel(job['id']))
        await cleaning.wait()
        deletion = asyncio.create_task(request(self.app, '/v1/requests/' + data['client_request_id'], method='DELETE'))
        try:
            await until(lambda: bool(self.jobs.deletions))
            deletion.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await deletion
            self.assertFalse(self.jobs.active.done())
        finally:
            release.set()
            await asyncio.gather(cancel, return_exceptions=True)
        await until(lambda: not self.jobs.deletions)
        self.assertFalse((Path(self.tmp.name) / 'checkpoints' / job['id']).exists())
        self.assertEqual((await request(self.app, '/v1/jobs/' + job['id']))[0], 404)

    async def test_delete_reaps_running_translator_process(self):
        marker = Path(self.tmp.name) / 'deleting-translator.pid'
        async def translator(cues, config, directory, emit):
            script = 'import os,time,pathlib; pathlib.Path(' + repr(str(marker)) + ').write_text(str(os.getpid())); time.sleep(60)'
            await run_process([sys.executable, '-c', script], timeout=65)
        self.jobs.translator = translator
        data = payload()
        job = await self.submit(data)
        self.jobs.start()
        await until(marker.exists)
        pid = int(marker.read_text())
        self.assertEqual((await request(self.app, '/v1/requests/' + data['client_request_id'], method='DELETE'))[0], 200)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse((Path(self.tmp.name) / 'checkpoints' / job['id']).exists())

    async def test_delete_before_translation_starts_does_not_create_outputs(self):
        calls = []
        async def translator(*args):
            calls.append(True)
            await asyncio.Future()
        self.jobs.translator = translator
        data = payload()
        job = await self.submit(data)
        self.jobs.start()
        await asyncio.sleep(0)  # The worker has scheduled, but not entered, translate().
        self.assertIsNotNone(self.jobs.active)
        self.assertEqual((await request(self.app, '/v1/requests/' + data['client_request_id'], method='DELETE'))[0], 200)
        self.assertEqual(calls, [])
        self.assertFalse((Path(self.tmp.name) / 'checkpoints' / job['id']).exists())

    async def test_delete_does_not_follow_checkpoint_symlinks(self):
        with tempfile.TemporaryDirectory() as outside:
            keep = Path(outside) / 'untouched.json'
            keep.write_text('keep')
            data = payload()
            job = await self.submit(data)
            root = Path(self.tmp.name) / 'checkpoints'
            root.mkdir()
            (root / job['id']).symlink_to(outside, target_is_directory=True)
            self.assertEqual((await request(self.app, '/v1/requests/' + data['client_request_id'], method='DELETE'))[0], 200)
            self.assertEqual(keep.read_text(), 'keep')
            self.assertFalse((root / job['id']).is_symlink())
            root.rmdir()
            root.symlink_to(outside, target_is_directory=True)
            data = payload()
            await self.submit(data)
            self.assertEqual((await request(self.app, '/v1/requests/' + data['client_request_id'], method='DELETE'))[0], 503)
            self.assertEqual(keep.read_text(), 'keep')
            root.unlink()
            self.assertEqual((await request(self.app, '/v1/requests/' + data['client_request_id'], method='DELETE'))[0], 200)


class MobileRestartTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_finishes_interrupted_deletion_and_retains_tombstone(self):
        for failure_method in ('_remove_checkpoints', '_checkpoint_database'):
            with self.subTest(failure_method=failure_method), tempfile.TemporaryDirectory() as directory:
                first = MobileJobs(directory)
                data = payload()
                job = first.submit(data['client_request_id'], data['title'], data['source_url'], data['subtitle_srt'])
                cache = Path(directory) / 'checkpoints' / job['id']
                cache.mkdir(parents=True)
                (cache / 'translation.json').write_text('private checkpoint')
                with patch.object(first, failure_method, side_effect=OSError('interrupted cleanup')):
                    with self.assertRaises(JobError) as failed:
                        await first.delete_request(data['client_request_id'])
                    self.assertEqual(failed.exception.status, 503)
                await first.close()
                second = MobileJobs(directory)
                try:
                    self.assertFalse(cache.exists())
                    self.assertEqual(second.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
                    self.assertEqual(second.db.execute('SELECT cleanup_pending FROM deleted_requests').fetchone()[0], 0)
                    self.assertEqual(await second.delete_request(data['client_request_id']), {'status': 'deleted'})
                    with self.assertRaises(JobError) as gone:
                        second.submit(data['client_request_id'], data['title'], data['source_url'], data['subtitle_srt'])
                    self.assertEqual(gone.exception.status, 410)
                finally:
                    await second.close()

    async def test_shutdown_restart_recovers_running_preserves_cancelled_and_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            first = MobileJobs(directory, translator=mock_translate)
            complete = first.submit(**{k if k != 'client_request_id' else 'request_id': v for k, v in payload().items()})
            first.start()
            await until(lambda: first.get(complete['id'])['state'] == 'ready')
            started = asyncio.Event()
            async def blocked(*args):
                started.set()
                await asyncio.Future()
            first.translator = blocked
            data = payload()
            work = first.submit(data['client_request_id'], data['title'], data['source_url'], data['subtitle_srt'])
            await started.wait()
            cancelled = first.submit(str(uuid4()), '', '', compose(CUES))
            await first.cancel(cancelled['id'])
            await first.close()
            second = MobileJobs(directory, translator=mock_translate)
            try:
                self.assertEqual(second.get(work['id'])['state'], 'queued')
                self.assertEqual(second.get(cancelled['id'])['state'], 'cancelled')
                self.assertEqual(second.get(complete['id'])['state'], 'ready')
                self.assertEqual(second.submit(data['client_request_id'], data['title'], data['source_url'], data['subtitle_srt'])['id'], work['id'])
                second.start()
                await until(lambda: second.get(work['id'])['state'] == 'ready')
            finally:
                await second.close()

    async def test_unclean_running_record_is_recovered_on_open(self):
        with tempfile.TemporaryDirectory() as directory:
            first = MobileJobs(directory)
            job = first.submit(str(uuid4()), '', '', compose(CUES))
            first.update(job['id'], state='running', progress=.4)
            await first.close()
            second = MobileJobs(directory)
            try:
                self.assertEqual(second.get(job['id'])['state'], 'queued')
                self.assertEqual(second.get(job['id'])['progress'], .4)
            finally:
                await second.close()


class TokenTests(unittest.TestCase):
    def test_token_file_and_environment_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                read_token()
            path = Path(directory) / 'token'
            path.write_text(TOKEN + '\n')
            self.assertEqual(read_token(path), TOKEN)
            with patch.dict(os.environ, {'MOBILE_API_TOKEN': 'short'}), self.assertRaises(ValueError):
                read_token(path)
            with patch.dict(os.environ, {'MOBILE_API_TOKEN': 'b' * 32}):
                self.assertEqual(read_token(path), 'b' * 32)


if __name__ == '__main__':
    unittest.main()
