"""NAS acceptance tests: temporary media and mocked models, never a real NAS/model."""
import asyncio
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nas_api import ArrClient, base_url, digest, service_id
from nas_io import (AtomicRenameUnsupported, Conflict, VersionChanged, Waiting, file_hash,
                    fingerprint, probe_mapping, publish, rename_noreplace, resolve_media, valid_srt)
from nas_jobs import ManualChoice, exact_release, generate
from nas_queue import Queue, processing_config
from nas_worker import Scheduler, video_action
from video_subtitles import Cue, compose, parse_srt

SERVICE = {'kind': 'radarr', 'url': 'http://nas.test:7878', 'enabled': True}
CONFIG = {'text_provider': 'api', 'text_endpoint': 'http://model.test/v1', 'text_model': 'mock',
          'search_provider': 'none', 'sync_mode': 'none', 'outputs': ['zh', 'bilingual']}
EN = [Cue(100, 900, 'Hello world.'), Cue(1200, 2200, 'This is the next line.')]
ZH = [Cue(100, 900, '你好世界。'), Cue(1200, 2200, '这是下一句。')]


def media(version='v1', file_id='1', path='/media/Film.2024.1080p.WEB-DL-GRP.mkv'):
    return {'file_id': file_id, 'path': path, 'version': version, 'title': 'Film', 'episodes': [], 'size': None}


class ApiTests(unittest.TestCase):
    def test_sonarr_multiepisode_is_one_file(self):
        client = ArrClient({'kind': 'sonarr', 'url': 'http://nas.test:8989'}, 'secret')
        values = {'series': [{'id': 9, 'path': '/tv/Show', 'title': 'Show'}],
                  'episode': [{'hasFile': True, 'episodeFileId': 12, 'seasonNumber': 1, 'episodeNumber': e} for e in (1, 2)],
                  'episodefile': [{'id': 12, 'relativePath': 'Show.S01E01E02.mkv', 'size': 100, 'dateAdded': 'today'}]}
        with patch.object(client, 'get', side_effect=lambda resource, **kw: values[resource]):
            result = client.inventory()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['episodes'], ['S01E01', 'S01E02'])
        self.assertEqual(result[0]['path'], '/tv/Show/Show.S01E01E02.mkv')

    def test_only_imported_final_movie_paths_and_version_fields(self):
        client = ArrClient(SERVICE, 'secret')
        movies = [{'hasFile': False, 'title': 'Still downloading'},
                  {'hasFile': True, 'title': 'Movie', 'path': '/media/Movie',
                   'movieFile': {'id': 1, 'path': '/media/Movie/final.mkv', 'size': 20, 'dateAdded': '1'}}]
        with patch.object(client, 'get', return_value=movies):
            before = client.inventory()
            movies[1]['movieFile']['dateAdded'] = '2'
            after = client.inventory()
        self.assertEqual(len(before), 1)
        self.assertNotEqual(before[0]['version'], after[0]['version'])

    def test_credentials_cannot_be_put_in_service_or_model_url(self):
        for url in ('http://user:password@nas/a', 'http://nas/?apikey=key', 'file:///tmp/a', 'http://nas/#secret'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                base_url(url)
        with self.assertRaises(ValueError):
            processing_config({**CONFIG, 'text_endpoint': 'http://api/v1?key=secret'})


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'queue.sqlite3'
        self.q = Queue(self.path)
        self.addCleanup(lambda: self.q.close())

    def test_first_connection_baselines_even_when_auto_enabled(self):
        result = self.q.sync(SERVICE, [media()], CONFIG, auto=True)
        self.assertTrue(result['first'])
        self.assertIsNone(self.q.next_job())
        self.q.sync(SERVICE, [media()], CONFIG, auto=True)
        self.assertIsNone(self.q.next_job())
        self.q.sync(SERVICE, [media(), media(file_id='2')], CONFIG, auto=True)
        self.assertEqual(self.q.next_job()['data']['file_id'], '2')

    def test_duplicate_sync_and_selected_enqueue_are_idempotent(self):
        self.q.sync(SERVICE, [media(), media()], CONFIG)
        mid = self.q.snapshot()[0]['id']
        first = self.q.enqueue([mid], {**CONFIG, 'text_api_key': 'DO-NOT-SAVE', 'assrt_token': 'SECRET'})
        self.q.update(first[0], state='written')
        for _ in range(4):
            self.q.sync(SERVICE, [media()], CONFIG, auto=True)
            self.assertEqual(self.q.enqueue([mid], CONFIG), first)
        self.assertIsNone(self.q.next_job())
        self.assertEqual(len(self.q.snapshot()[0]['jobs']), 1)
        self.assertNotIn('DO-NOT-SAVE', str(self.q.snapshot()))
        self.assertNotIn('SECRET', str(self.q.snapshot()))

    def test_restart_preserves_result_pause_cancel_and_processing_snapshot(self):
        self.q.sync(SERVICE, [media(file_id=str(i)) for i in range(4)], CONFIG)
        ids = self.q.enqueue([m['id'] for m in self.q.snapshot()], CONFIG)
        self.q.update(ids[0], state='running')
        self.q.update(ids[1], state='writing', result={'outputs': {'zh': 'cached'}})
        self.q.update(ids[2], state='paused')
        self.q.update(ids[3], state='cancelled')
        self.q.close()
        self.q = Queue(self.path)
        self.q.recover()
        self.assertEqual([self.q.job(i)['state'] for i in ids], ['queued', 'waiting_write', 'paused', 'cancelled'])
        self.assertEqual(self.q.job(ids[1])['result'], {'outputs': {'zh': 'cached'}})
        self.assertEqual(self.q.job(ids[0])['config']['text_model'], 'mock')

    def test_failed_sync_never_removes_files_or_establishes_baseline(self):
        self.q.sync_error(SERVICE, 'offline')
        self.assertTrue(self.q.sync(SERVICE, [media()], CONFIG, auto=True)['first'])
        self.q.sync_error(SERVICE, 'offline')
        self.assertTrue(self.q.snapshot()[0]['present'])
        with self.assertRaises(ValueError):
            self.q.sync(SERVICE, [{'file_id': 'bad'}], CONFIG, auto=True)
        self.assertTrue(self.q.snapshot()[0]['present'])

    def test_upgrade_same_name_new_version_queues_again_and_invalidates_old(self):
        self.q.sync(SERVICE, [media()], CONFIG)
        first = self.q.enqueue([self.q.snapshot()[0]['id']], CONFIG)[0]
        self.q.update(first, state='written')
        self.q.sync(SERVICE, [media('v2')], CONFIG, auto=True)
        self.assertEqual(self.q.job(first)['state'], 'superseded')
        self.assertNotEqual(self.q.next_job()['id'], first)
        self.assertEqual(self.q.next_job()['version'], 'v2')

    def test_changed_configuration_or_service_has_distinct_identity(self):
        self.q.sync(SERVICE, [media()], CONFIG)
        mid = self.q.snapshot()[0]['id']
        a = self.q.enqueue([mid], CONFIG)[0]
        b = self.q.enqueue([mid], {**CONFIG, 'text_model': 'other-model'})[0]
        self.assertNotEqual(a, b)
        other = {**SERVICE, 'url': 'http://another.test:7878'}
        self.q.sync(other, [media()], CONFIG)
        self.assertEqual(len(self.q.snapshot()), 2)

    def test_disabled_service_does_not_starve_enabled_queue(self):
        self.q.sync(SERVICE, [media()], CONFIG)
        self.q.enqueue([self.q.snapshot()[0]['id']], CONFIG)
        self.assertIsNone(self.q.next_job([]))
        self.assertIsNotNone(self.q.next_job([service_id(SERVICE)]))


class MappingTests(unittest.TestCase):
    def test_verified_prefix_boundaries_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'mapped'
            root.mkdir()
            video = root / 'movie.mkv'
            video.write_bytes(b'video')
            mapping = probe_mapping({'kind': 'radarr', 'remote': '/movies', 'local': str(root), 'require_mount': False})
            self.assertTrue(mapping['write_ok'])
            self.assertEqual(resolve_media('/movies/movie.mkv', [mapping], 'radarr'), video)
            for remote in ('/movies2/movie.mkv', '/movies/../movie.mkv'):
                with self.assertRaises(Waiting):
                    resolve_media(remote, [mapping], 'radarr')
            outside = Path(tmp) / 'outside.mkv'
            outside.write_bytes(b'private')
            (root / 'link.mkv').symlink_to(outside)
            with self.assertRaises(Waiting):
                resolve_media('/movies/link.mkv', [mapping], 'radarr')
            mapping['proof']['mount']['source'] = 'unmounted'
            with self.assertRaises(Waiting):
                resolve_media('/movies/movie.mkv', [mapping], 'radarr')

    def test_readonly_probe_reports_read_success_without_write_claim(self):
        with tempfile.TemporaryDirectory() as tmp, patch('nas_io.write_probe', side_effect=PermissionError()):
            result = probe_mapping({'kind': 'radarr', 'remote': '/media', 'local': tmp, 'require_mount': False})
            self.assertTrue(result['read_ok'])
            self.assertFalse(result['write_ok'])

    def test_unsupported_rename_keeps_files_and_reports_mount_capability(self):
        for number in (errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
            with self.subTest(errno=number), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, target = root / 'source', root / 'target'
                source.write_bytes(b'new')
                target.write_bytes(b'existing subtitle')
                function = Mock(return_value=-1)
                with patch('nas_io.ctypes.CDLL', return_value=Mock(renameat2=function)), \
                        patch('nas_io.ctypes.get_errno', return_value=number):
                    with self.assertRaises(AtomicRenameUnsupported) as raised:
                        rename_noreplace(source, target)
                    self.assertEqual(raised.exception.errno, number)
                    result = probe_mapping({'kind': 'radarr', 'remote': '/media',
                                            'local': tmp, 'require_mount': False})
                self.assertTrue(result['read_ok'])
                self.assertFalse(result['write_ok'])
                self.assertEqual(result['write_error']['errno'], number)
                self.assertIn('SMB/CIFS', result['message'])
                self.assertNotIn('只读', result['message'])
                self.assertEqual(source.read_bytes(), b'new')
                self.assertEqual(target.read_bytes(), b'existing subtitle')
                self.assertEqual(sorted(p.name for p in root.iterdir()), ['source', 'target'])

    def test_probe_distinguishes_readonly_from_permission_denied(self):
        for number, expected in ((errno.EROFS, '当前进程看到的挂载为只读'),
                                 (errno.EACCES, '当前账号没有目录写入权限')):
            with self.subTest(errno=number), tempfile.TemporaryDirectory() as tmp, \
                    patch('nas_io.write_probe', side_effect=OSError(number, os.strerror(number))):
                result = probe_mapping({'kind': 'radarr', 'remote': '/media',
                                        'local': tmp, 'require_mount': False})
                self.assertFalse(result['write_ok'])
                self.assertEqual(result['write_error']['errno'], number)
                self.assertIn(expected, result['message'])

    def test_same_size_same_mtime_replacement_is_detected_by_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'same.mkv'
            path.write_bytes(b'a' * 100)
            before = fingerprint(path)
            path.write_bytes(b'b' * 100)
            os.utime(path, ns=(before['mtime_ns'], before['mtime_ns']))
            after = fingerprint(path)
            self.assertEqual(before['size'], after['size'])
            self.assertEqual(before['mtime_ns'], after['mtime_ns'])
            self.assertNotEqual(before, after)


@unittest.skipUnless(shutil.which('ffmpeg'), 'synthetic video requires ffmpeg')
class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / 'NAS'
        self.library.mkdir()
        self.video = self.library / Path(media()['path']).name
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=32x32:rate=5',
                        '-t', '3', '-c:v', 'ffv1', str(self.video)], check=True, capture_output=True)
        self.original_bytes = self.video.read_bytes()
        self.q = Queue(self.root / 'cache' / 'queue.sqlite3')
        self.addCleanup(self.q.close)
        self.mapping = probe_mapping({'kind': 'radarr', 'remote': '/media', 'local': str(self.library), 'require_mount': False})
        self.q.sync(SERVICE, [media()], CONFIG)
        self.jid = self.q.enqueue([self.q.snapshot()[0]['id']], CONFIG)[0]
        self.emit = AsyncMock()
        async def model(text, config):
            return {'text': json.dumps([{'id': x['id'], 'text': '译文' + str(x['id'])} for x in json.loads(text)], ensure_ascii=False)}
        self.model = AsyncMock(side_effect=model)
        patcher = patch('text_translation.translate_text', self.model)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.asr = AsyncMock(return_value=EN)
        patcher = patch('video_jobs.asr_file', self.asr)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sidecar(self, language, cues):
        path = self.video.with_suffix('.' + language + '.srt')
        path.write_text(compose(cues))
        return path

    async def make(self):
        result = await generate(self.q, self.q.job(self.jid), [self.mapping], {}, self.emit)
        self.q.update(self.jid, result=result, state='generated')
        return result

    async def test_english_direct_translation_and_writeback_preserve_video(self):
        self.sidecar('en', EN)
        result = await self.make()
        self.asr.assert_not_awaited()
        self.model.assert_awaited_once()
        written = publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertFalse(written['library_verified'])
        self.assertEqual(len(written['paths']), 2)
        for path in written['paths']:
            cues = valid_srt(path)
            self.assertEqual([(c.start, c.end) for c in cues], [(100, 900), (1200, 2200)])
        bilingual = valid_srt(self.video.with_suffix('.bilingual.srt'))
        self.assertEqual(bilingual[0].text, '译文0\nHello world.')
        self.assertEqual(self.video.read_bytes(), self.original_bytes)
        publish(self.q, self.q.job(self.jid), [self.mapping])
        self.model.assert_awaited_once()

    async def test_chinese_reuses_without_model_or_asr_and_pairs_english(self):
        path = self.sidecar('zh', ZH)
        # Preserve the user's original encoding and formatting too.
        path.write_bytes(compose(ZH).replace('\n', '\r\n').encode('utf-16'))
        original = path.read_bytes()
        self.sidecar('en', EN)
        await self.make()
        self.model.assert_not_awaited()
        self.asr.assert_not_awaited()
        publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertEqual(path.read_bytes(), original)
        self.assertIn('你好世界。\nHello world.', self.video.with_suffix('.bilingual.srt').read_text())
        self.assertIsNone(self.q.db.execute('SELECT * FROM outputs WHERE path=?', (str(path),)).fetchone())

    async def test_pure_chinese_does_not_claim_fake_bilingual(self):
        self.sidecar('zh', ZH)
        with self.assertRaisesRegex(ManualChoice, '尚未生成最终双语'):
            await self.make()
        self.model.assert_not_awaited()
        self.asr.assert_not_awaited()
        self.assertFalse(self.video.with_suffix('.bilingual.srt').exists())

    async def test_pure_chinese_supplements_english_asr_without_retranslation(self):
        zh = self.sidecar('zh', ZH)
        info = {'title': self.video.name, 'duration': 3, 'audio': [{'index': 1, 'language': 'eng'}],
                'subtitles': [{'path': str(zh), 'language': 'zh'}]}
        with patch('video_jobs.inspect', AsyncMock(return_value=info)):
            result = await self.make()
        self.assertIn('bilingual', result['outputs'])
        self.model.assert_not_awaited()
        self.asr.assert_awaited_once()
        self.assertEqual(valid_srt(result['outputs']['bilingual']['path'])[0].text, '你好世界。\nHello world.')

    async def test_unpaired_chinese_never_publishes_a_single_language_final(self):
        self.sidecar('zh', [Cue(2500, 2900, '时间轴不匹配')])
        self.sidecar('en', EN)
        with self.assertRaisesRegex(ManualChoice, '无法逐条可靠配对'):
            await self.make()
        self.assertFalse(self.video.with_suffix('.bilingual.srt').exists())

    async def test_embedded_english_extraction_is_used(self):
        srt = self.root / 'embedded.srt'
        srt.write_text(compose(EN))
        muxed = self.root / 'mux.mkv'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.video), '-i', str(srt), '-map', '0:v', '-map', '1',
                        '-c', 'copy', '-metadata:s:s:0', 'language=eng', str(muxed)], check=True, capture_output=True)
        self.video.write_bytes(muxed.read_bytes())
        result = await self.make()
        self.assertEqual(result['origin_label'], '内嵌英文文本字幕')
        self.asr.assert_not_awaited()
        self.model.assert_awaited_once()

    async def test_no_text_subtitles_then_asr(self):
        await self.make()
        self.asr.assert_awaited_once()
        self.model.assert_awaited_once()

    async def test_read_failure_or_malformed_subtitle_never_falls_into_asr(self):
        path = self.sidecar('en', EN)
        with patch('nas_jobs.read_text_subtitle', AsyncMock(side_effect=PermissionError('offline'))):
            with self.assertRaises(Waiting):
                await self.make()
        path.write_text('broken SRT')
        with self.assertRaises(ManualChoice):
            await self.make()
        self.asr.assert_not_awaited()
        self.model.assert_not_awaited()

    async def test_probe_failure_never_falls_into_asr(self):
        with patch('video_jobs.probe', AsyncMock(side_effect=RuntimeError('read failed'))):
            with self.assertRaises(Waiting):
                await self.make()
        self.asr.assert_not_awaited()

    async def test_existing_manual_output_is_preserved(self):
        self.sidecar('en', EN)
        await self.make()
        target = self.sidecar('zh', ZH)
        before = target.read_bytes()
        with self.assertRaises(Conflict):
            publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(self.video.with_suffix('.bilingual.srt').exists())

    async def test_write_failure_retry_and_restart_do_not_translate_again(self):
        self.sidecar('en', EN)
        await self.make()
        with patch('nas_io.rename_noreplace', side_effect=PermissionError('readonly')):
            with self.assertRaises(PermissionError):
                publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertFalse(self.video.with_suffix('.zh.srt').exists())
        self.assertFalse(self.video.with_suffix('.bilingual.srt').exists())
        other = Queue(self.q.path)
        try:
            publish(other, other.job(self.jid), [self.mapping])
        finally:
            other.close()
        self.model.assert_awaited_once()
        self.assertEqual(self.video.read_bytes(), self.original_bytes)

    async def test_cancel_during_staging_never_exposes_incomplete_final(self):
        self.sidecar('en', EN)
        await self.make()
        with patch('nas_io.write_probe'), patch('nas_io.flush_file', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertFalse(self.video.with_suffix('.zh.srt').exists())
        self.assertFalse(self.video.with_suffix('.bilingual.srt').exists())
        self.assertFalse(list(self.library.glob('.qwen-nas-*.tmp')))

    async def test_external_edit_blocks_update_even_with_permission(self):
        self.sidecar('en', EN)
        await self.make()
        publish(self.q, self.q.job(self.jid), [self.mapping])
        target = self.sidecar('zh', ZH)
        before = target.read_bytes()
        self.q.update(self.jid, override={'update_managed': True})
        with self.assertRaises(Conflict):
            publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertEqual(target.read_bytes(), before)

    async def test_replacement_invalidates_artifacts_and_ignores_old_managed_subs(self):
        self.sidecar('en', EN)
        first = await self.make()
        publish(self.q, self.q.job(self.jid), [self.mapping])
        oldstat = self.video.stat()
        # Same size and timestamps: content fingerprint must still reject publication.
        data = bytearray(self.video.read_bytes())
        data[-1] ^= 1
        self.video.write_bytes(data)
        os.utime(self.video, ns=(oldstat.st_atime_ns, oldstat.st_mtime_ns))
        with self.assertRaises(VersionChanged):
            publish(self.q, self.q.job(self.jid), [self.mapping])
        self.video.write_bytes(self.original_bytes)
        self.q.sync(SERVICE, [media('v2')], CONFIG, auto=True)
        self.jid = self.q.next_job()['id']
        second = await self.make()
        self.assertNotEqual(first['video'], second['video'])
        # Both the old managed Chinese and previously observed old English sidecar are ignored.
        self.asr.assert_awaited_once()
        with self.assertRaises(Conflict):
            publish(self.q, self.q.job(self.jid), [self.mapping])
        self.q.update(self.jid, override={'update_managed': True})
        publish(self.q, self.q.job(self.jid), [self.mapping])

    async def test_completed_translation_cache_resumes_without_extra_calls(self):
        self.sidecar('en', EN)
        await self.make()
        await self.make()
        self.model.assert_awaited_once()

    async def test_corrupt_source_cue_is_not_silently_dropped(self):
        path = self.sidecar('en', EN)
        path.write_text(compose(EN) + '3\n00:00:02,400 --> 00:00:02,300\nInvalid timing\n\n')
        with self.assertRaises(ManualChoice):
            await self.make()
        self.asr.assert_not_awaited()
        self.model.assert_not_awaited()

    async def test_recovery_after_publication_before_journal_ack(self):
        self.sidecar('en', EN)
        result = await self.make()
        import nas_io
        real_rename = nas_io.rename_noreplace
        crashed = False
        def rename(source, target):
            nonlocal crashed
            real_rename(source, target)
            if str(target).endswith('.zh.srt') and not crashed:
                crashed = True
                raise OSError('simulated crash after atomic publication')
        with patch('nas_io.rename_noreplace', side_effect=rename), self.assertRaises(OSError):
            publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertEqual(file_hash(self.video.with_suffix('.zh.srt')), result['outputs']['zh']['hash'])
        publish(self.q, self.q.job(self.jid), [self.mapping])
        records = list(self.q.db.execute('SELECT state FROM outputs'))
        self.assertEqual([r['state'] for r in records], ['published', 'published'])
        self.model.assert_awaited_once()

    async def test_racing_manual_edit_is_preserved_during_managed_update(self):
        self.sidecar('en', EN)
        result = await self.make()
        publish(self.q, self.q.job(self.jid), [self.mapping])
        local = Path(result['outputs']['zh']['path'])
        local.write_text(compose(ZH))
        result['outputs']['zh']['hash'] = file_hash(local)
        self.q.update(self.jid, result=result, override={'update_managed': True})
        import nas_io
        real_rename = nas_io.rename_noreplace
        target = self.video.with_suffix('.zh.srt')
        def rename(source, destination):
            if Path(source) == target:
                target.write_text('human raced the writer')
            real_rename(source, destination)
        with patch('nas_io.rename_noreplace', side_effect=rename), self.assertRaises(Conflict):
            publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertEqual(target.read_text(), 'human raced the writer')

    async def test_video_replacement_during_staging_blocks_publication(self):
        self.sidecar('en', EN)
        await self.make()
        import nas_io
        real_flush = nas_io.flush_file
        def flush(f):
            real_flush(f)
            self.video.write_bytes(self.original_bytes + b'changed')
        with patch('nas_io.write_probe'), patch('nas_io.flush_file', side_effect=flush), self.assertRaises(VersionChanged):
            publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertFalse(self.video.with_suffix('.zh.srt').exists())

    async def test_image_subtitle_codec_is_not_selected_as_text(self):
        data = {'streams': [{'index': 0, 'codec_type': 'video', 'codec_name': 'h264'},
                            {'index': 1, 'codec_type': 'subtitle', 'codec_name': 'hdmv_pgs_subtitle',
                             'tags': {'language': 'eng'}}]}
        with patch('video_jobs.probe', AsyncMock(return_value=(data, 3))):
            result = await self.make()
        self.asr.assert_awaited_once()
        self.assertEqual(result['origin'], 'asr')

    async def test_no_clobber_collision_never_adopts_human_file(self):
        self.sidecar('en', EN)
        result = await self.make()
        import nas_io
        real_rename = nas_io.rename_noreplace
        target = self.video.with_suffix('.zh.srt')
        def rename(source, destination):
            if Path(destination) == target:
                target.write_bytes(Path(result['outputs']['zh']['path']).read_bytes())
            real_rename(source, destination)
        with patch('nas_io.rename_noreplace', side_effect=rename), self.assertRaises(Conflict):
            publish(self.q, self.q.job(self.jid), [self.mapping])
        self.assertIsNone(self.q.db.execute('SELECT * FROM outputs WHERE path=?', (str(target),)).fetchone())

    async def test_download_first_and_empty_search_falls_back(self):
        self.sidecar('en', EN)
        mid = self.q.snapshot()[0]['id']
        self.jid = self.q.enqueue([mid], {**CONFIG, 'search_provider': 'subhd'})[0]
        downloaded = self.root / 'download.srt'
        downloaded.write_text(compose(ZH))
        candidate = {'provider': 'subhd', 'id': 'abc', 'title': self.video.stem, 'url': 'https://subhd.tv/a/abc'}
        with patch('nas_jobs.search', AsyncMock(return_value=[candidate])), \
             patch('nas_jobs.download', AsyncMock(return_value=[{'title': self.video.stem + '.zh.srt', 'path': str(downloaded)}])):
            result = await self.make()
        self.assertIn('下载字幕', result['origin_label'])
        self.model.assert_not_awaited()
        self.asr.assert_not_awaited()
        with patch('nas_jobs.search', AsyncMock(return_value=[])):
            await self.make()
        self.model.assert_awaited_once()

    async def test_ambiguous_or_verification_stops_for_manual_not_asr(self):
        self.jid = self.q.enqueue([self.q.snapshot()[0]['id']], {**CONFIG, 'search_provider': 'subhd'})[0]
        for response in ([{'provider': 'subhd', 'id': 'x', 'title': 'Film 2024'}], RuntimeError('challenge')):
            search = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=response)
            with patch('nas_jobs.search', search), self.assertRaises(ManualChoice):
                await self.make()
        self.asr.assert_not_awaited()
        self.model.assert_not_awaited()

    async def test_explicit_subtitle_overrides_broken_existing_sidecar(self):
        self.sidecar('en', EN).write_text('corrupt')
        selected = self.root / 'selected.srt'
        selected.write_text(compose(EN))
        self.q.update(self.jid, override={'subtitle_path': str(selected)})
        await self.make()
        self.asr.assert_not_awaited()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_test_saves_only_verified_key_and_redacts_failure(self):
        from nas_tasks import action
        config = {'action': 'nas_test', 'service': SERVICE, 'key': 'ARR-NEVER-LOG-THIS'}
        events = []
        async def emit(value):
            events.append(value)
        with patch('credentials.nas_credential') as credential, \
                patch('nas_api.fetch', AsyncMock(return_value={'appName': 'Radarr', 'version': 'test'})):
            await action(config, emit)
        credential.assert_called_once_with('store', SERVICE, config['key'])
        with patch('credentials.nas_credential') as credential, \
                patch('nas_api.fetch', AsyncMock(side_effect=RuntimeError('invalid ' + config['key']))):
            await action(config, emit)
        credential.assert_not_called()
        self.assertNotIn(config['key'], json.dumps(events))

    async def test_waiting_write_retries_only_publication_and_reports_separate_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Scheduler({'database': str(Path(tmp) / 'queue.sqlite3'), 'services': [SERVICE], 'processing': CONFIG})
            try:
                s.queue.sync(SERVICE, [media()], CONFIG)
                jid = s.queue.enqueue([s.queue.snapshot()[0]['id']], CONFIG)[0]
                result = {'origin_label': 'mock', 'outputs': {}, 'missing_outputs': []}
                responses = [{'nas_result': result}, {'nas_error': {'state': 'waiting', 'message': 'readonly'}},
                             {'nas_result': {'paths': ['/NAS/test.zh.srt'], 'library_verified': False}}]
                with patch('nas_worker.output'), patch('nas_worker.video_action', AsyncMock(side_effect=responses)) as worker:
                    # Avoid keychain access in a model-free queue test.
                    s.config['text_api_key'] = 'ephemeral-secret'
                    await s.run_job(s.queue.job(jid))
                    self.assertEqual(s.queue.job(jid)['state'], 'generated')
                    await s.run_job(s.queue.job(jid))
                    self.assertEqual(s.queue.job(jid)['state'], 'waiting_write')
                    await s.run_job(s.queue.job(jid))
                self.assertEqual([c.args[0]['action'] for c in worker.await_args_list], ['nas_generate', 'nas_publish', 'nas_publish'])
                self.assertEqual(s.queue.job(jid)['state'], 'written')
                self.assertNotIn('ephemeral-secret', str(s.queue.snapshot()))
            finally:
                s.queue.close()

    async def test_real_worker_probe_and_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            response = await video_action({'action': 'nas_probe', 'mapping':
                {'kind': 'radarr', 'remote': '/media', 'local': tmp, 'require_mount': False}}, timeout=15)
            self.assertTrue(response['nas_result']['write_ok'])
        # Cancellable process protocol tested by waiting on the shared video lock.
        import fcntl
        from video_jobs import CACHE
        CACHE.mkdir(exist_ok=True)
        with (CACHE / '.processing.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = asyncio.Event()
            async def emit(event):
                if event.get('stage') == '等待视频任务':
                    started.set()
            task = asyncio.create_task(video_action({'action': 'nas_generate'}, emit))
            await asyncio.wait_for(started.wait(), 10)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 8)


@unittest.skipUnless(shutil.which('ffmpeg'), 'synthetic video requires ffmpeg')
class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_arr_to_real_worker_mock_model_to_bilingual_write_and_restart(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        state = {'movies': [], 'model_calls': 0}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def send_json(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                self.send_json(state['movies'] if self.path == '/api/v3/movie' else {'version': 'test'})
            def do_POST(self):
                state['model_calls'] += 1
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                transcript = body['messages'][0]['content'].split('Transcript (JSON string): ', 1)[1]
                cues = json.loads(json.loads(transcript))
                self.send_json({'choices': [{'message': {'content': json.dumps(
                    [{'id': x['id'], 'text': '本地模拟译文' + str(x['id'])} for x in cues])}}]})
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                library = root / 'media'
                library.mkdir()
                video = library / 'Synthetic.mkv'
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=size=32x32:rate=5',
                                '-t', '3', '-c:v', 'ffv1', str(video)], check=True, capture_output=True)
                video.with_suffix('.en.srt').write_text(compose(EN))
                original = file_hash(video)
                url = f'http://127.0.0.1:{server.server_port}'
                service = {**SERVICE, 'url': url}
                config = {'database': str(root / 'local' / 'queue.sqlite3'), 'services': [service], 'auto': True,
                          'text_api_key': 'MODEL-EPHEMERAL', 'processing': {**CONFIG, 'text_endpoint': url},
                          'mappings': [probe_mapping({'kind': 'radarr', 'remote': '/media', 'local': str(library), 'require_mount': False})]}
                scheduler = Scheduler(config)
                with patch('nas_worker.nas_credential', return_value='ARR-EPHEMERAL'), patch('nas_worker.output'):
                    await scheduler.sync()  # Empty first baseline.
                    state['movies'] = [{'title': 'Synthetic', 'path': '/media', 'hasFile': True,
                        'movieFile': {'id': 8, 'path': '/media/Synthetic.mkv', 'size': video.stat().st_size, 'dateAdded': 'now'}}]
                    await scheduler.sync()
                    job = scheduler.queue.next_job()
                    self.assertIsNotNone(job)
                    await scheduler.run_job(job)
                    self.assertEqual(scheduler.queue.job(job['id'])['state'], 'generated')
                    scheduler.queue.close()
                    scheduler = Scheduler(config)  # Real local artifacts survive a restart before writeback.
                    await scheduler.run_job(scheduler.queue.job(job['id']))
                    self.assertEqual(scheduler.queue.job(job['id'])['state'], 'written')
                    await scheduler.sync()
                    self.assertIsNone(scheduler.queue.next_job())
                    self.assertEqual(state['model_calls'], 1)
                    self.assertEqual(valid_srt(video.with_suffix('.bilingual.srt'))[0].text,
                                     '本地模拟译文0\nHello world.')
                    self.assertEqual(file_hash(video), original)
                    scheduler.queue.close()
                for path in (root / 'local').rglob('*'):
                    if path.is_file():
                        self.assertNotIn(b'MODEL-EPHEMERAL', path.read_bytes())
                        self.assertNotIn(b'ARR-EPHEMERAL', path.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
