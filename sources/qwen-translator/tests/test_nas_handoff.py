"""Manual preparation -> persistent NAS queue -> protected publication."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from nas_handoff import adopt_prepared
from nas_io import Conflict, VersionChanged, file_hash, fingerprint, probe_mapping, publish
from nas_queue import Queue
from nas_worker import video_action
from video_cache import source_directory
from video_subtitles import Cue, compose


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = self.root / 'library'
        self.library.mkdir()
        self.video = self.library / 'Film.mp4'
        self.video.write_bytes(b'read-only video placeholder')
        self.mapping = probe_mapping({'kind': 'radarr', 'remote': '/movies',
                                      'local': str(self.library), 'require_mount': False})
        self.config = {'search_provider': 'subhd', 'outputs': ['zh', 'bilingual']}
        self.service = {'kind': 'radarr', 'url': 'http://nas.test:7878', 'enabled': True}
        self.media = {'file_id': '1', 'path': '/movies/Film.mp4', 'version': 'v1',
                      'title': 'Film', 'episodes': [], 'size': self.video.stat().st_size}
        self.queue = Queue(self.root / 'nas' / 'queue.sqlite3')
        self.addCleanup(self.queue.close)
        self.queue.sync(self.service, [self.media], self.config)
        self.mid = self.queue.snapshot()[0]['id']
        self.jid = self.queue.enqueue([self.mid], self.config)[0]
        self.queue.update(self.jid, state='manual', error='Choose website release')
        self.cache = self.root / 'video-cache'
        self.directory = source_directory(self.cache, str(self.video)) / 'prepared' / 'test'
        self.directory.mkdir(parents=True)
        # ASR can contain punctuation or non-Latin sounds. Preserve the actual pair.
        for name, text in [('zh', '你好。'), ('bilingual', '你好。\nHello.'), ('source', 'Hello.')]:
            (self.directory / (name + '.srt')).write_text(compose([
                Cue(100, 900, text), Cue(1000, 1100, '。\n.' if name == 'bilingual' else '.')]))
        self.prepared = {'source': str(self.video), 'media': str(self.video), 'title': 'Film',
                         'directory': str(self.directory), 'subtitle': str(self.directory / 'zh.srt'),
                         'bilingual': str(self.directory / 'bilingual.srt'),
                         'timed_source': str(self.directory / 'source.srt'), 'cues': 2}
        self.save()

    def save(self):
        (self.directory / 'result.json').write_text(json.dumps(self.prepared))

    def adopt(self, **kwargs):
        return adopt_prepared(self.queue, self.prepared, [self.mapping], self.config,
                              cache=self.cache, **kwargs)

    def test_existing_manual_job_reconnects_without_asr_or_translation_and_survives_cache_deletion(self):
        with patch('video_jobs.prepare', AsyncMock(side_effect=AssertionError('must not prepare'))):
            accepted = self.adopt()
        self.assertEqual(accepted['job_id'], self.jid)
        job = self.queue.job(self.jid)
        self.assertEqual(job['state'], 'generated')
        self.assertEqual(job['error'], '')
        expected = {key: file_hash(artifact['path']) for key, artifact in job['result']['outputs'].items()}
        import shutil
        shutil.rmtree(self.cache)
        written = publish(self.queue, job, [self.mapping])
        self.assertEqual(len(written['paths']), 2)
        for suffix in expected:
            self.assertEqual(file_hash(self.library / ('Film.' + suffix + '.srt')), expected[suffix])
        self.assertEqual(self.video.read_bytes(), b'read-only video placeholder')

    def test_first_manual_preparation_can_create_nas_job(self):
        with self.queue.db:
            self.queue.db.execute('DELETE FROM jobs')
        self.adopt()
        self.assertEqual(self.queue.next_job()['state'], 'generated')

    def test_existing_human_subtitle_is_protected(self):
        target = self.library / 'Film.zh.srt'
        target.write_text('my own subtitle')
        self.adopt()
        with self.assertRaises(Conflict):
            publish(self.queue, self.queue.job(self.jid), [self.mapping])
        self.assertEqual(target.read_text(), 'my own subtitle')
        self.assertFalse((self.library / 'Film.bilingual.srt').exists())

    def test_additional_chinese_export_follows_nas_output_setting(self):
        with self.queue.db:
            self.queue.db.execute('DELETE FROM jobs')
        config = {**self.config, 'outputs': ['bilingual']}
        adopt_prepared(self.queue, self.prepared, [self.mapping], config, cache=self.cache)
        job = self.queue.next_job()
        self.assertEqual(set(job['result']['outputs']), {'bilingual'})
        publish(self.queue, job, [self.mapping])
        self.assertFalse((self.library / 'Film.zh.srt').exists())

    def test_video_outside_configured_library_is_never_adopted(self):
        other = self.root / 'other.mp4'
        other.write_bytes(self.video.read_bytes())
        self.prepared['source'] = str(other)
        with self.assertRaises(ValueError):
            self.adopt()
        self.assertEqual(self.queue.job(self.jid)['state'], 'manual')

    def test_video_change_invalidates_old_manual_cache(self):
        self.video.write_bytes(b'a different video')
        with self.assertRaises(VersionChanged):
            self.adopt()
        self.assertEqual(self.queue.job(self.jid)['state'], 'manual')

    def test_fingerprint_detects_same_size_and_timestamp_replacement(self):
        self.prepared['source_fingerprint'] = fingerprint(self.video)
        self.save()
        before = self.video.stat()
        self.video.write_bytes(b'X' * before.st_size)
        os.utime(self.video, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(VersionChanged):
            self.adopt()

    def test_paused_job_and_wrong_version_are_not_resumed(self):
        self.queue.update(self.jid, state='paused')
        with self.assertRaises(ValueError):
            self.adopt(job_id=self.jid)
        self.queue.sync(self.service, [{**self.media, 'version': 'v2'}], self.config)
        with self.assertRaises(VersionChanged):
            self.adopt(job_id=self.jid)

    def test_one_line_and_misaligned_pairs_cannot_publish(self):
        for cues in ([Cue(100, 900, 'single language')], [Cue(200, 900, '你好\nHello')]):
            (self.directory / 'bilingual.srt').write_text(compose(cues))
            with self.assertRaises(ValueError):
                self.adopt()
        self.assertEqual(self.queue.job(self.jid)['state'], 'manual')

    def test_cancellation_during_copy_is_preserved(self):
        def cancel(path):
            if Path(path).parent.name.startswith('manual-'):
                self.queue.update(self.jid, state='cancelled')
            return file_hash(path)
        with patch('nas_handoff.file_hash', side_effect=cancel), self.assertRaises(ValueError):
            self.adopt(job_id=self.jid)
        self.assertEqual(self.queue.job(self.jid)['state'], 'cancelled')

    def test_real_worker_imports_existing_result(self):
        import shutil
        cache = Path(__file__).resolve().parents[1] / 'video-cache'
        destination = source_directory(cache, str(self.video)) / 'prepared' / 'test'
        destination.parent.mkdir(parents=True)
        shutil.copytree(self.directory, destination)
        self.addCleanup(lambda: shutil.rmtree(destination.parent.parent, ignore_errors=True))
        prepared = {**self.prepared, 'directory': str(destination)}
        for key in ('subtitle', 'bilingual', 'timed_source'):
            prepared[key] = str(destination / Path(prepared[key]).name)
        (destination / 'result.json').write_text(json.dumps(prepared))
        response = asyncio.run(video_action({'action': 'nas_adopt', 'database': str(self.queue.path),
            'prepared': prepared, 'mappings': [self.mapping], 'processing': self.config}, timeout=15))
        self.assertIn('nas_result', response, response)
        self.assertEqual(self.queue.job(self.jid)['state'], 'generated')


if __name__ == '__main__':
    unittest.main()
