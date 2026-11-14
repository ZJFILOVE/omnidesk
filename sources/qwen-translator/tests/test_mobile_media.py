"""NAS media API acceptance: fake Arr, real files/SQLite, no secrets or NAS writes."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4
from urllib.parse import parse_qs, urlencode, urlsplit
import urllib.error

from mobile_api import create_app
from mobile_jobs import JobError
from video_subtitles import Cue, compose

TOKEN = 'test-media-token-' + 'z' * 32
ORIGINAL = compose([Cue(0, 1000, 'Hello world.')])


async def request(app, path, *, method='GET', body=None, headers=None, token=TOKEN):
    path, _, query_string = path.partition('?')
    data = json.dumps(body).encode() if body is not None else b''
    pairs = [] if any(k.lower() == 'content-type' for k in (headers or {})) else [(b'content-type', b'application/json')]
    if token is not None:
        pairs.append((b'authorization', ('Bearer ' + token).encode()))
    pairs.extend((k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items())
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
             'http_version': '1.1', 'method': method, 'scheme': 'http', 'path': path,
             'raw_path': path.encode(), 'query_string': query_string.encode(), 'headers': pairs,
             'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 8765), 'root_path': ''}
    delivered, messages = False, []

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {'type': 'http.request', 'body': data, 'more_body': False}
        await asyncio.Future()

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    content = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    headers = dict(start.get('headers', []))
    result = json.loads(content) if b'application/json' in headers.get(b'content-type', b'') else content
    return start['status'], result, headers


async def translator(cues, config, directory, emit):
    await emit({'progress': .5})
    return [replace(c, text='你好。') for c in cues]


class FakeArr:
    calls = []
    writes = []
    records = {}

    def __init__(self, service, key):
        self.kind = service['kind']
        assert key == 'private-arr-secret'

    def get(self, resource, **query):
        self.calls.append((self.kind, resource, query))
        if (self.kind, resource) in self.records:
            return deepcopy(self.records[(self.kind, resource)])
        entity, _, number = resource.partition('/')
        if entity in {'movie', 'series'} and number.isdigit():
            return deepcopy(next((x for x in self.records[(self.kind, entity)] if x['id'] == int(number)), {}))
        raise KeyError(resource)

    def post(self, resource, data):
        self.calls.append((self.kind, resource, data))
        if resource in {'series', 'movie'}:
            result = {**data, 'id': 123}
            self.records[(self.kind, resource)] = self.records[(self.kind, resource)] + [result]
            return result
        return {'id': 22, 'status': 'queued'}

    def put(self, resource, data):
        self.writes.append(('PUT', self.kind, resource, deepcopy(data)))
        entity, _, number = resource.partition('/')
        values = self.records[(self.kind, entity)]
        self.records[(self.kind, entity)] = [deepcopy(data) if x['id'] == int(number) else x for x in values]
        return deepcopy(data)

    def delete(self, resource, **query):
        self.writes.append(('DELETE', self.kind, resource, query))
        entity, _, number = resource.partition('/')
        if entity == 'queue':
            queue = self.records[(self.kind, 'queue')]
            raw = next(x for x in queue['records'] if x['id'] == int(number))
            queue['records'] = [x for x in queue['records'] if x['id'] != int(number)
                                and not (raw.get('downloadId') and x.get('downloadId') == raw['downloadId'])]
            queue['totalRecords'] = len(queue['records'])
        else:
            self.records[(self.kind, entity)] = [x for x in self.records[(self.kind, entity)] if x['id'] != int(number)]
        return {}


class MediaApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / 'tv'
        self.library.mkdir()
        self.video = self.library / 'episode.mkv'
        self.video.write_bytes(b'0123456789')
        self.sidecar = self.library / 'episode.en.srt'
        self.sidecar.write_text(ORIGINAL)
        self.key = self.root / 'key'
        self.key.write_text('private-arr-secret')
        self.config = {'services': [{'kind': 'sonarr', 'url': 'http://127.0.0.1:8989', 'api_key_file': str(self.key)},
                                    {'kind': 'radarr', 'url': 'http://127.0.0.1:7878', 'api_key_file': str(self.key)}],
                       'mappings': [{'kind': 'sonarr', 'remote': '/media/tv', 'local': str(self.library)}]}
        FakeArr.calls = []
        FakeArr.writes = []
        FakeArr.records = {
            ('sonarr', 'series'): [{'id': 1, 'title': 'Example series', 'year': 2024, 'path': '/media/tv', 'monitored': True,
                                   'statistics': {'episodeFileCount': 1, 'episodeCount': 2}}],
            ('sonarr', 'episode'): [{'id': 2, 'title': 'Pilot', 'seasonNumber': 1, 'episodeNumber': 1,
                                     'hasFile': True, 'episodeFileId': 5, 'monitored': True},
                                    {'id': 3, 'title': 'Next', 'seasonNumber': 1, 'episodeNumber': 2, 'hasFile': False}],
            ('sonarr', 'episodefile/5'): {'id': 5, 'seriesId': 1, 'path': '/media/tv/episode.mkv', 'size': 10},
            ('radarr', 'movie'): [{'id': 4, 'title': 'Missing movie', 'year': 2025, 'path': '/media/movies/Missing', 'hasFile': False}],
            ('sonarr', 'queue'): {'records': [{'id': 99, 'title': 'Example download', 'status': 'downloading', 'size': 100, 'sizeleft': 25}], 'totalRecords': 1},
            ('radarr', 'queue'): {'records': [], 'totalRecords': 0}}
        FakeArr.records.update({
            ('sonarr', 'series/lookup'): [{'tvdbId': 42, 'title': 'New show', 'year': 2026,
                                         'titleSlug': 'new-show', 'seasons': [{'seasonNumber': 0}, {'seasonNumber': 1}]}],
            ('sonarr', 'rootfolder'): [{'id': 1, 'path': '/media/tv', 'accessible': True},
                                     {'id': 2, 'path': '/unapproved', 'accessible': True}],
            ('sonarr', 'qualityprofile'): [{'id': 3, 'name': 'HD-1080p'}]})

        async def inspect(*args, **kwargs):
            return {'duration': 100, 'audio': [{'index': 1, 'title': 'English', 'language': 'eng'}],
                    'subtitles': [{'path': str(self.sidecar), 'title': self.sidecar.name, 'language': 'en'},
                                  {'stream': 2, 'title': 'Embedded English', 'language': 'eng'}]}
        self.inspector = patch('video_jobs.inspect', side_effect=inspect)
        self.inspector.start()
        self.addCleanup(self.inspector.stop)
        await self.start()

    async def start(self):
        self.app = create_app(data_dir=self.root / 'service', token=TOKEN, translator=translator,
                              start_worker=False, media_config=self.config, media_client_factory=FakeArr)
        self.context = self.app.router.lifespan_context(self.app)
        await self.context.__aenter__()
        self.addAsyncCleanup(self.context.__aexit__, None, None, None)
        self.jobs = self.app.state.media_jobs

    async def submit(self, **options):
        body = {'client_request_id': str(uuid4()), 'subtitle_id': 'sidecar-' + __import__('hashlib').sha256(self.sidecar.name.encode()).hexdigest()[:20], **options}
        status, job, _ = await request(self.app, '/v1/media/files/sonarr:file:5/prepare', method='POST', body=body)
        self.assertEqual(status, 202, job)
        return job, body

    async def complete(self, job):
        self.jobs.update(job['id'], state='running')
        await self.jobs.translate(job['id'])
        return self.jobs.get(job['id'])

    async def test_auth_catalog_episodes_queue_and_managed_search(self):
        for path in ('/v1/media/catalog', '/v1/media/queue', '/v1/media/files/sonarr:file:5/stream'):
            self.assertEqual((await request(self.app, path, token=None))[0], 401)
        status, catalog, _ = await request(self.app, '/v1/media/catalog')
        self.assertEqual(status, 200)
        self.assertTrue(catalog['configured'])
        self.assertEqual(catalog['capabilities'], ['manage_queue', 'manage_titles'])
        self.assertEqual(len(catalog['items']), 2)
        text = json.dumps(catalog)
        for hidden in ('private-arr-secret', '/media/tv', str(self.library), 'api_key'):
            self.assertNotIn(hidden, text)
        detail = (await request(self.app, '/v1/media/titles/sonarr:series:1'))[1]
        self.assertEqual([e['media_id'] for e in detail['episodes']], ['sonarr:file:5', None])
        self.assertEqual((await request(self.app, '/v1/media/queue'))[1]['items'][0]['progress'], .75)
        self.assertEqual((await request(self.app, '/v1/media/titles/sonarr:series:1/search', method='POST', body={}))[0], 202)
        self.assertEqual(FakeArr.calls[-1], ('sonarr', 'command', {'name': 'SeriesSearch', 'seriesId': 1}))
        self.assertEqual((await request(self.app, '/v1/media/titles/sonarr:series:999/search', method='POST', body={}))[0], 404)
        self.assertIn('nas_media', (await request(self.app, '/health'))[1]['capabilities'])

    async def test_management_requires_authentication_and_strict_boolean_options(self):
        routes = [('/v1/media/queue/sonarr:queue:99/remove', {}, 'blocklist'),
                  ('/v1/media/titles/sonarr:series:1/monitor', {'monitored': True}, 'monitored'),
                  ('/v1/media/titles/sonarr:series:1/remove', {}, 'delete_files')]
        for url, body, field in routes:
            self.assertEqual((await request(self.app, url, method='POST', body=body, token=None))[0], 401)
            for value in ('false', 0, 1, None, [], {}):
                status, result, _ = await request(self.app, url, method='POST', body={**body, field: value})
                self.assertEqual(status, 422, result)
            for invalid in ({**body, 'path': '/etc'}, ['invalid'], 'invalid', None):
                self.assertEqual((await request(self.app, url, method='POST', body=invalid))[0], 422)
            self.assertEqual((await request(self.app, url, method='POST', body=body,
                                            headers={'Content-Type': 'text/plain'}))[0], 415)
        for field in ('add_import_exclusion',):
            self.assertEqual((await request(self.app, routes[2][0], method='POST', body={field: 'true'}))[0], 422)
        self.assertEqual((await request(self.app, routes[1][0], method='POST', body={}))[0], 422)
        self.assertEqual(FakeArr.writes, [])

    async def test_queue_removal_uses_fresh_membership_signed_ids_and_fixed_options(self):
        queue = FakeArr.records[('sonarr', 'queue')]
        queue['records'][0]['downloadId'] = 'whole-season'
        queue['records'].append({**queue['records'][0], 'id': -123})
        queue['totalRecords'] = 2
        items = (await request(self.app, '/v1/media/queue'))[1]['items']
        self.assertEqual([x['affected_count'] for x in items], [2, 2])
        url = '/v1/media/queue/sonarr:queue:-123/remove'
        status, result, _ = await request(self.app, url, method='POST', body={'blocklist': True})
        self.assertEqual((status, result['status'], result['affected_count']), (200, 'removed', 2))
        self.assertEqual(FakeArr.writes, [('DELETE', 'sonarr', 'queue/-123',
                         {'removeFromClient': True, 'blocklist': True, 'skipRedownload': True, 'changeCategory': False})])
        self.assertEqual(self.app.state.media.cache, {})
        self.assertEqual((await request(self.app, '/v1/media/queue'))[1]['items'], [])
        self.assertEqual((await request(self.app, url, method='POST', body={}))[0], 404)
        self.assertEqual(len(FakeArr.writes), 1)

    async def test_stale_queue_cache_and_polluted_identifiers_cannot_authorize_deletion(self):
        await request(self.app, '/v1/media/queue')
        FakeArr.records[('sonarr', 'queue')] = {'records': [], 'totalRecords': 0}
        url = '/v1/media/queue/sonarr:queue:99/remove'
        self.assertEqual((await request(self.app, url, method='POST', body={}))[0], 404)
        for identifier in ('sonarr:queue:99&removeFromClient=false', 'sonarr:queue:2147483648',
                           'sonarr:queue:-2147483649', 'sonarr:queue:099', 'sonarr:file:99',
                           'other:queue:99', 'sonarr:queue:1/../../series/1'):
            self.assertEqual((await request(self.app, '/v1/media/queue/' + identifier + '/remove', method='POST', body={}))[0], 404)
        for identifier in ('sonarr:movie:1', 'radarr:series:4', 'sonarr:series:1&deleteFiles=true', 'sonarr:file:5'):
            for action, body in (('remove', {}), ('monitor', {'monitored': False})):
                self.assertEqual((await request(self.app, f'/v1/media/titles/{identifier}/{action}', method='POST', body=body))[0], 404)
        self.assertEqual(FakeArr.writes, [])

    async def test_monitor_preserves_fresh_arr_settings_and_invalidates_catalog(self):
        for kind, entity, number in (('sonarr', 'series', 1), ('radarr', 'movie', 4)):
            await request(self.app, '/v1/media/catalog')
            raw = FakeArr.records[(kind, entity)][0]
            raw.update(qualityProfileId=17, tags=[4, 7], seasons=[{'seasonNumber': 1, 'monitored': True}],
                       monitored=True, minimumAvailability='released')
            expected = {**deepcopy(raw), 'monitored': False}
            url = f'/v1/media/titles/{kind}:{entity}:{number}/monitor'
            status, detail, _ = await request(self.app, url, method='POST', body={'monitored': False})
            self.assertEqual((status, detail['monitored']), (200, False))
            self.assertEqual(FakeArr.writes[-1], ('PUT', kind, f'{entity}/{number}', expected))
            catalog = (await request(self.app, '/v1/media/catalog'))[1]
            self.assertFalse(next(x for x in catalog['items'] if x['id'] == detail['id'])['monitored'])

    async def test_title_removal_defaults_to_preserving_files_and_correct_exclusion_parameter(self):
        for kind, entity, number, exclusion in (('sonarr', 'series', 1, 'addImportListExclusion'),
                                                 ('radarr', 'movie', 4, 'addImportExclusion')):
            await request(self.app, '/v1/media/catalog')
            status, result, _ = await request(self.app, f'/v1/media/titles/{kind}:{entity}:{number}/remove', method='POST', body={})
            self.assertEqual((status, result['status']), (200, 'removed'))
            self.assertEqual(FakeArr.writes[-1], ('DELETE', kind, f'{entity}/{number}', {'deleteFiles': False, exclusion: True}))
            self.assertEqual(self.app.state.media.cache, {})
        self.assertEqual(self.video.read_bytes(), b'0123456789')
        self.assertEqual(self.sidecar.read_text(), ORIGINAL)
        self.assertEqual((await request(self.app, '/v1/media/catalog'))[1]['items'], [])

    async def test_delete_files_requires_fresh_authorized_title_subdirectory(self):
        url = '/v1/media/titles/sonarr:series:1/remove'
        await request(self.app, '/v1/media/titles/sonarr:series:1')
        raw = FakeArr.records[('sonarr', 'series')][0]
        outside = self.root / 'outside'
        outside.mkdir()
        (self.library / 'linked').symlink_to(outside, target_is_directory=True)
        for path in ('/media/tv', '/media/tv/../outside', '/unapproved/title', '/media/tv/linked', None):
            raw['path'] = path
            status, result, _ = await request(self.app, url, method='POST', body={'delete_files': True})
            self.assertEqual(status, 403, result)
        self.assertEqual(FakeArr.writes, [])
        raw['path'] = '/media/tv/Example series'
        status, result, _ = await request(self.app, url, method='POST', body={'delete_files': True, 'add_import_exclusion': False})
        self.assertEqual((status, result['status']), (200, 'removed'))
        self.assertEqual(FakeArr.writes[-1], ('DELETE', 'sonarr', 'series/1', {'deleteFiles': True, 'addImportListExclusion': False}))
        self.assertTrue(self.video.is_file())  # Fake Arr never mutates real/NAS files.

    async def test_arr_failures_do_not_report_success_retry_or_leave_stale_cache(self):
        cases = [('/v1/media/queue/sonarr:queue:99/remove', {}, 'delete'),
                 ('/v1/media/titles/sonarr:series:1/remove', {}, 'delete'),
                 ('/v1/media/titles/sonarr:series:1/monitor', {'monitored': False}, 'put')]
        for url, body, method in cases:
            await request(self.app, '/v1/media/catalog')
            self.assertTrue(self.app.state.media.cache)
            with patch.object(FakeArr, method, side_effect=TimeoutError('private-arr-secret')) as write:
                status, result, _ = await request(self.app, url, method='POST', body=body)
                self.assertEqual(status, 503, result)
                self.assertIn('刷新', result['detail'])
                self.assertNotIn('private-arr-secret', json.dumps(result))
                self.assertEqual(write.call_count, 1)
            self.assertEqual(self.app.state.media.cache, {})
        self.assertEqual(FakeArr.writes, [])

    async def test_malformed_fresh_queue_and_wrong_title_identity_never_authorize_writes(self):
        queue_url = '/v1/media/queue/sonarr:queue:99/remove'
        for value in ({'records': 'invalid'}, {'records': [None]}, {'records': [], 'totalRecords': 'one'}, None):
            FakeArr.records[('sonarr', 'queue')] = value
            self.assertEqual((await request(self.app, queue_url, method='POST', body={}))[0], 503)
        FakeArr.records[('sonarr', 'series/1')] = {'id': 2, 'path': '/media/tv/Other title'}
        for action, body in (('remove', {}), ('monitor', {'monitored': False})):
            url = f'/v1/media/titles/sonarr:series:1/{action}'
            self.assertEqual((await request(self.app, url, method='POST', body=body))[0], 404)
        self.assertEqual(FakeArr.writes, [])

    async def test_private_file_details_sidecar_and_generated_subtitle_choices(self):
        status, info, _ = await request(self.app, '/v1/media/files/sonarr:file:5')
        self.assertEqual(status, 200)
        self.assertEqual([t['source'] for t in info['subtitles']], ['sidecar', 'embedded'])
        self.assertIsNone(info['job'])
        self.assertNotIn(str(self.library), json.dumps(info))
        track = info['subtitles'][0]
        status, body, _ = await request(self.app, track['download_path'])
        self.assertEqual(status, 200)
        self.assertEqual(body.decode(), ORIGINAL)
        job, _ = await self.submit()
        result = await self.complete(job)
        self.assertEqual(result['state'], 'ready', result)
        detail = (await request(self.app, '/v1/media/files/sonarr:file:5'))[1]
        self.assertEqual(detail['job']['id'], job['id'])
        self.assertEqual(len([t for t in detail['subtitles'] if t['source'] == 'generated']), 3)
        self.assertIn('你好。', self.jobs.subtitle(job['id'], 'bilingual'))
        self.assertIn('Hello world.', self.jobs.subtitle(job['id'], 'bilingual'))

    async def test_range_stream_head_etag_and_if_range(self):
        url = '/v1/media/files/sonarr:file:5/stream'
        status, body, headers = await request(self.app, url)
        self.assertEqual((status, body), (200, b'0123456789'))
        etag = headers[b'etag'].decode()
        for value, expected in [('bytes=2-5', b'2345'), ('bytes=8-', b'89'), ('bytes=-3', b'789'), ('bytes=0-99', b'0123456789')]:
            status, body, headers = await request(self.app, url, headers={'Range': value, 'If-Range': etag})
            self.assertEqual((status, body), (206, expected))
            self.assertEqual(int(headers[b'content-length']), len(expected))
        for value in ('bytes=10-', 'bytes=-0', 'bytes=5-2', 'bytes=0-1,3-4', 'garbage'):
            status, _, headers = await request(self.app, url, headers={'Range': value})
            self.assertEqual(status, 416)
            self.assertEqual(headers[b'content-range'], b'bytes */10')
        self.assertEqual((await request(self.app, url, headers={'Range': 'bytes=2-3', 'If-Range': 'old'}))[:2], (200, b'0123456789'))
        status, body, headers = await request(self.app, url, method='HEAD', headers={'Range': 'bytes=2-3'})
        self.assertEqual((status, body, headers[b'content-length']), (206, b'', b'2'))

    async def test_path_escape_and_unmanaged_files_are_never_streamed(self):
        raw = FakeArr.records[('sonarr', 'episodefile/5')]
        for value in ('/etc/passwd', '/media/tv/../key', 'relative.mkv'):
            self.app.state.media.cache.clear()
            raw['path'] = value
            self.assertEqual((await request(self.app, '/v1/media/files/sonarr:file:5/stream'))[0], 403)
        outside = self.root / 'outside.mkv'
        outside.write_bytes(b'0123456789')
        link = self.library / 'link.mkv'
        link.symlink_to(outside)
        raw['path'] = '/media/tv/link.mkv'
        self.app.state.media.cache.clear()
        self.assertEqual((await request(self.app, '/v1/media/files/sonarr:file:5/stream'))[0], 403)
        self.assertEqual((await request(self.app, '/v1/media/files/not-a-file/stream'))[0], 404)

    async def test_durable_idempotency_cancel_retry_tombstone_only_removes_job(self):
        job, body = await self.submit()
        again = await request(self.app, '/v1/media/files/sonarr:file:5/prepare', method='POST', body=body)
        self.assertEqual(again[1]['id'], job['id'])
        conflict = {**body, 'subtitle_id': 'embedded-2'}
        self.assertEqual((await request(self.app, '/v1/media/files/sonarr:file:5/prepare', method='POST', body=conflict))[0], 409)
        self.assertEqual((await request(self.app, f"/v1/media/jobs/{job['id']}/cancel", method='POST', body={}))[1]['state'], 'cancelled')
        self.assertEqual((await request(self.app, f"/v1/media/jobs/{job['id']}/retry", method='POST', body={}))[1]['state'], 'queued')
        self.assertEqual((await self.complete(job))['state'], 'ready')
        cache = self.jobs.directory / 'checkpoints' / job['id']
        (cache / 'private').write_text('source and translation checkpoint')
        url = '/v1/media/requests/' + body['client_request_id']
        for _ in range(2):
            self.assertEqual((await request(self.app, url, method='DELETE'))[:2], (200, {'status': 'deleted'}))
        self.assertFalse(cache.exists())
        self.assertEqual(self.video.read_bytes(), b'0123456789')
        self.assertEqual(self.sidecar.read_text(), ORIGINAL)
        self.assertEqual((await request(self.app, '/v1/media/files/sonarr:file:5/prepare', method='POST', body=body))[0], 410)

    async def test_missing_subtitles_and_explicit_bad_track_skip_without_asr(self):
        with patch('video_jobs.asr_file') as asr:
            for options in ({'subtitle_id': 'missing-track'}, {'subtitle_id': None, 'force_asr': True}):
                body = {'client_request_id': str(uuid4()), **options}
                status, result, _ = await request(self.app, '/v1/media/files/sonarr:file:5/prepare', method='POST', body=body)
                self.assertEqual(status, 422, result)
            asr.assert_not_called()
        async def no_subtitles(*args, **kwargs):
            return {'duration': 100, 'audio': [], 'subtitles': []}
        with patch('video_jobs.inspect', side_effect=no_subtitles), patch('video_jobs.asr_file') as asr:
            body = {'client_request_id': str(uuid4()), 'subtitle_id': None}
            status, result, _ = await request(self.app, '/v1/media/files/sonarr:file:5/prepare', method='POST', body=body)
            self.assertEqual(status, 422)
            self.assertIn('跳过字幕处理', result['detail'])
            asr.assert_not_called()
        self.assertFalse((await request(self.app, '/v1/media/files/sonarr:file:5'))[1]['asr_available'])

    async def test_version_change_does_not_publish_mismatched_subtitles(self):
        job, _ = await self.submit()
        self.video.write_bytes(b'newversion')
        result = await self.complete(job)
        self.assertEqual(result['state'], 'failed')
        self.assertIn('版本已改变', result['message'])

    async def test_concurrent_submissions_are_single_durable_job(self):
        rid = str(uuid4())
        values = await asyncio.gather(*(self.jobs.submit_media(rid, 'sonarr:file:5', 'embedded-2') for _ in range(3)))
        self.assertEqual(len({value['id'] for value in values}), 1)

    async def test_discover_validates_add_options_and_never_accepts_client_paths(self):
        query = urlencode({'kind': 'sonarr', 'query': 'New show'})
        status, results, _ = await request(self.app, '/v1/media/discover?' + query)
        self.assertEqual(status, 200)
        self.assertEqual(results['items'][0]['external_id'], 42)
        self.assertFalse(results['items'][0]['already_added'])
        options = (await request(self.app, '/v1/media/services/sonarr/options'))[1]
        self.assertEqual(options['root_folders'], [{'id': 1, 'name': 'tv'}])
        self.assertNotIn('/media/tv', json.dumps(options))
        body = {'external_id': 42, 'root_folder_id': 1, 'quality_profile_id': 3, 'monitored': True, 'search': True}
        url = '/v1/media/services/sonarr/add'
        for bad in ({**body, 'root_folder_id': 2}, {**body, 'quality_profile_id': 999}, {**body, 'root_folder_path': '/etc'}):
            self.assertEqual((await request(self.app, url, method='POST', body=bad))[0], 422)
        status, added, _ = await request(self.app, url, method='POST', body=body)
        self.assertEqual((status, added['id']), (201, 'sonarr:series:123'))
        posted = [c for c in FakeArr.calls if c[1] == 'series' and c[2].get('rootFolderPath')]
        self.assertEqual(posted[0][2]['rootFolderPath'], '/media/tv')
        self.assertEqual(posted[0][2]['addOptions']['searchForMissingEpisodes'], True)
        self.assertEqual([s['monitored'] for s in posted[0][2]['seasons']], [False, True])
        self.assertEqual((await request(self.app, url, method='POST', body=body))[1]['id'], added['id'])
        self.assertEqual(len([c for c in FakeArr.calls if c[1] == 'series' and c[2].get('rootFolderPath')]), 1)

    async def test_seeders_band_then_size_ignores_quality_and_keeps_arr_rejections(self):
        self.config['services'][1]['seeders_first_profile_ids'] = [7]
        FakeArr.records[('radarr', 'movie')][0]['qualityProfileId'] = 7
        base = {'approved': True, 'rejected': False, 'rejections': [], 'protocol': 'torrent', 'indexerId': 2}
        FakeArr.records[('radarr', 'release')] = [
            {**base, 'guid': 'quality-first', 'seeders': 121, 'customFormatScore': 1000, 'size': 50_000_000_000},
            {**base, 'guid': 'most-seeders', 'seeders': 158, 'customFormatScore': 0, 'size': 60_000_000_000},
            {**base, 'guid': 'small-in-top-band', 'seeders': 39, 'size': 1_000_000_000},
            {**base, 'guid': 'lower-band', 'seeders': 30, 'size': 500_000_000},
            {**base, 'guid': 'rejected-cam', 'seeders': 999, 'approved': False, 'rejected': True, 'rejections': ['CAM not wanted']},
            {**base, 'guid': 'unknown-count', 'seeders': None},
        ]
        status, result, _ = await request(self.app, '/v1/media/titles/radarr:movie:4/search', method='POST')
        self.assertEqual(status, 202)
        self.assertIn('39', result['message'])
        grabs = [x for x in FakeArr.calls if x[1] == 'release' and 'guid' in x[2]]
        self.assertEqual(grabs[0][2], {'guid': 'small-in-top-band', 'indexerId': 2, 'movieId': 4})
        self.assertFalse(any(x[1] == 'command' for x in FakeArr.calls))

    async def test_seeders_first_does_not_duplicate_queue_or_force_rejected_release(self):
        self.config['services'][1]['seeders_first_profile_ids'] = [7]
        FakeArr.records[('radarr', 'movie')][0]['qualityProfileId'] = 7
        FakeArr.records[('radarr', 'queue')] = {'records': [{'id': 22, 'movieId': 4}], 'totalRecords': 1}
        url = '/v1/media/titles/radarr:movie:4/search'
        self.assertIn('已有下载任务', (await request(self.app, url, method='POST'))[1]['message'])
        self.assertFalse(any(x[1] == 'release' for x in FakeArr.calls))
        FakeArr.records[('radarr', 'queue')] = {'records': [], 'totalRecords': 0}
        FakeArr.records[('radarr', 'release')] = [{'guid': 'cam', 'seeders': 900, 'approved': False, 'rejected': True}]
        self.assertEqual((await request(self.app, url, method='POST'))[1]['status'], 'no_match')
        self.assertFalse(any(x[1] == 'release' and 'guid' in x[2] for x in FakeArr.calls))

    async def test_add_seeders_profile_searches_after_add_without_native_quality_search(self):
        self.config['services'][1]['seeders_first_profile_ids'] = [7]
        FakeArr.records.update({
            ('radarr', 'rootfolder'): [{'id': 1, 'path': '/media/tv', 'accessible': True}],
            ('radarr', 'qualityprofile'): [{'id': 7, 'name': 'Seeders first'}],
            ('radarr', 'movie/lookup'): [{'tmdbId': 320, 'title': 'Insomnia'}],
            ('radarr', 'release'): [{'guid': 'best', 'indexerId': 2, 'protocol': 'torrent', 'seeders': 158, 'size': 60_000_000_000,
                                    'approved': True, 'rejected': False, 'rejections': []}],
        })
        self.app.state.media.mappings.append({**self.app.state.media.mappings[0], 'kind': 'radarr'})
        status, added, _ = await request(self.app, '/v1/media/services/radarr/add', method='POST',
                                        body={'external_id': 320, 'root_folder_id': 1, 'quality_profile_id': 7})
        self.assertEqual(status, 201, added)
        self.assertIn('158', added['search_message'])
        payload = next(x[2] for x in FakeArr.calls if x[1] == 'movie' and 'rootFolderPath' in x[2])
        self.assertFalse(payload['addOptions']['searchForMovie'])

    async def test_default_selection_only_exposes_current_profile_ids(self):
        self.config['services'][0]['default_quality_profile_id'] = 3
        url = '/v1/media/services/sonarr/options'
        self.assertEqual((await request(self.app, url))[1]['default_quality_profile_id'], 3)
        self.config['services'][0]['default_quality_profile_id'] = 999
        self.assertIsNone((await request(self.app, url))[1]['default_quality_profile_id'])

    async def test_sonarr_search_picks_top_band_pack_once_and_skips_unrelated_releases(self):
        self.config['services'][0]['seeders_first_profile_ids'] = [1]
        FakeArr.records[('sonarr', 'series')][0]['qualityProfileId'] = 1
        FakeArr.records[('sonarr', 'queue')] = {'records': [], 'totalRecords': 0}
        for e in FakeArr.records[('sonarr', 'episode')]:
            e['airDateUtc'] = '2020-01-01T00:00:00Z'
        FakeArr.records[('sonarr', 'episode')].append({'id': 4, 'seasonNumber': 1, 'episodeNumber': 3,
            'hasFile': False, 'monitored': True, 'airDateUtc': '2020-01-01T00:00:00Z'})
        base = {'approved': True, 'rejected': False, 'protocol': 'torrent', 'indexerId': 1, 'seasonNumber': 1}
        FakeArr.records[('sonarr', 'release')] = [
            {**base, 'guid': 'top-pack-large', 'fullSeason': True, 'seeders': 600, 'size': 60_000_000_000},
            {**base, 'guid': 'top-pack-small', 'fullSeason': True, 'seeders': 400, 'size': 30_000_000_000},
            {**base, 'guid': 'lower-band-single', 'episodeNumbers': [2], 'seeders': 100, 'size': 100_000_000},
            {**base, 'guid': 'wrong-series', 'approved': False, 'rejected': True, 'seeders': 9000, 'size': 50},
        ]
        url = '/v1/media/titles/sonarr:series:1/search'
        self.assertEqual((await request(self.app, url, method='POST'))[0], 202)
        task = self.app.state.media.search_tasks['sonarr:series:1']
        await request(self.app, url, method='POST')
        self.assertIs(task, self.app.state.media.search_tasks['sonarr:series:1'])
        await task
        grabs = [x for x in FakeArr.calls if x[1] == 'release' and 'guid' in x[2]]
        self.assertEqual([x[2]['guid'] for x in grabs], ['top-pack-small'])
        detail = (await request(self.app, '/v1/media/titles/sonarr:series:1'))[1]
        self.assertIn('1 个下载任务', detail['search_message'])

    async def test_sonarr_search_stops_before_grab_when_monitoring_is_disabled(self):
        self.config['services'][0]['seeders_first_profile_ids'] = [1]
        FakeArr.records[('sonarr', 'series')][0].update(qualityProfileId=1, monitored=False)
        for e in FakeArr.records[('sonarr', 'episode')]:
            e['airDateUtc'] = '2020-01-01T00:00:00Z'
            e['monitored'] = True
        FakeArr.records[('sonarr', 'release')] = [{'guid': 'pack', 'indexerId': 1, 'protocol': 'torrent',
            'approved': True, 'seeders': 500, 'size': 1000, 'seasonNumber': 1, 'fullSeason': True}]
        await self.app.state.media.search('sonarr:series:1')
        await self.app.state.media.search_tasks['sonarr:series:1']
        self.assertFalse(any(x[1] == 'release' and 'guid' in x[2] for x in FakeArr.calls))
        self.assertIn('已停止追更', self.app.state.media.search_messages['sonarr:series:1'])

    async def test_durable_restart_restores_running_job_and_preserves_request_identity(self):
        from mobile_media_jobs import MediaJobs
        directory = self.root / 'restart-test'
        first = MediaJobs(directory, self.app.state.media, translator=translator)
        rid = str(uuid4())
        job = await first.submit_media(rid, 'sonarr:file:5', self.app.state.media.track_id({'path': str(self.sidecar)}))
        first.update(job['id'], state='running')
        await first.close()
        second = MediaJobs(directory, self.app.state.media, translator=translator)
        try:
            restored = second.get(job['id'])
            self.assertEqual(restored['state'], 'queued')
            self.assertEqual(restored['client_request_id'], rid)
            self.assertEqual((await second.submit_media(rid, 'sonarr:file:5', self.app.state.media.track_id({'path': str(self.sidecar)})))['id'], job['id'])
            second.update(job['id'], state='running')
            await second.translate(job['id'])
            self.assertEqual(second.get(job['id'])['state'], 'ready')
        finally:
            await second.close()


class MediaArrWriteTests(unittest.TestCase):
    def client(self, status=204, body=b''):
        from mobile_media import MediaArrClient
        client = MediaArrClient({'kind': 'sonarr', 'url': 'http://127.0.0.1:8989'}, 'private-arr-secret')
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.read.return_value = body
        client.opener = Mock()
        client.opener.open.return_value = response
        return client

    def test_delete_empty_204_and_put_resource_use_correct_method_and_json(self):
        client = self.client()
        self.assertEqual(client.delete('queue/-123', removeFromClient=True, blocklist=False,
                                       skipRedownload=True, changeCategory=False), {})
        call = client.opener.open.call_args
        sent = call.args[0]
        self.assertEqual(sent.method, 'DELETE')
        self.assertEqual(parse_qs(urlsplit(sent.full_url).query),
                         {'removeFromClient': ['true'], 'blocklist': ['false'],
                          'skipRedownload': ['true'], 'changeCategory': ['false']})
        self.assertIsNone(sent.data)
        self.assertNotIn('private-arr-secret', sent.full_url)
        self.assertEqual(call.kwargs, {'timeout': 30})
        client = self.client(202, b'{"id":1,"monitored":false}')
        self.assertEqual(client.put('series/1', {'id': 1, 'monitored': False})['monitored'], False)
        sent = client.opener.open.call_args.args[0]
        self.assertEqual(sent.method, 'PUT')
        self.assertEqual(json.loads(sent.data), {'id': 1, 'monitored': False})

    def test_http_timeout_invalid_json_and_oversized_responses_never_look_successful(self):
        for error in (urllib.error.HTTPError('http://private', 404, 'missing', {}, None),
                      urllib.error.HTTPError('http://private', 500, 'failure', {}, None),
                      TimeoutError('private-arr-secret'), urllib.error.URLError('private-arr-secret')):
            client = self.client()
            client.opener.open.side_effect = error
            with self.assertRaises(RuntimeError) as raised:
                client.delete('series/1', deleteFiles=False)
            self.assertNotIn('private-arr-secret', str(raised.exception))
            self.assertEqual(client.opener.open.call_count, 1)
        for status, body in ((200, b'not JSON'), (200, b'x' * (4 * 1024 * 1024 + 1)), (304, b'')):
            client = self.client(status, body)
            with self.assertRaises(ValueError):
                client.delete('series/1', deleteFiles=False)


if __name__ == '__main__':
    unittest.main()
