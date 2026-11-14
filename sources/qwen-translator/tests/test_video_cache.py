import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from video_cache import cached_result, cached_videos, delete_cache, source_directory


class CacheDeletionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = self.root / 'cache'
        self.source = 'https://www.youtube.com/watch?v=test'
        self.entry = source_directory(self.cache, self.source)
        self.entry.mkdir(parents=True)
        for name in ('video.mp4', 'video.mp4.part', 'video-download.json', 'audio.webm',
                     'audio-download.json', 'youtube.en.vtt', 'asr-checkpoint.json', 'result.json'):
            (self.entry / name).write_text('cached')
        (self.entry / 'prepared').mkdir()
        (self.entry / 'prepared' / 'zh.srt').write_text('subtitle')

    def test_subtitles_preserve_media_and_external_symlink_target(self):
        original = self.root / 'original.srt'
        original.write_text('original')
        (self.entry / 'imports').symlink_to(self.root, target_is_directory=True)
        delete_cache(self.cache, 'subtitles', self.source)
        self.assertEqual(original.read_text(), 'original')
        self.assertTrue((self.entry / 'video.mp4').is_file())
        self.assertTrue((self.entry / 'audio.webm').is_file())
        self.assertFalse((self.entry / 'prepared').exists())
        self.assertFalse((self.entry / 'asr-checkpoint.json').exists())

    def test_video_removes_partial_download_and_record_preserves_subtitles(self):
        delete_cache(self.cache, 'video', self.source)
        self.assertFalse((self.entry / 'video.mp4').exists())
        self.assertFalse((self.entry / 'video.mp4.part').exists())
        self.assertFalse((self.entry / 'video-download.json').exists())
        self.assertTrue((self.entry / 'prepared' / 'zh.srt').exists())
        self.assertTrue((self.entry / 'audio.webm').exists())

    def test_current_and_all_scope_skip_symlink_entries(self):
        other = source_directory(self.cache, self.source + '2')
        other.mkdir()
        (other / 'audio.webm').write_text('other')
        (self.cache / ('a' * 24)).symlink_to(self.root, target_is_directory=True)
        (self.root / 'audio.webm').write_text('external')
        delete_cache(self.cache, 'audio', self.source)
        self.assertTrue((other / 'audio.webm').exists())
        delete_cache(self.cache, 'audio')
        self.assertFalse((other / 'audio.webm').exists())
        self.assertTrue((self.root / 'audio.webm').exists())

    def test_missing_cache_is_not_created(self):
        missing = self.root / 'missing'
        self.assertEqual(delete_cache(missing, 'subtitles', self.source), 0)
        self.assertFalse(missing.exists())


class CachedVideoTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.cache = self.root / 'cache'

    def result(self, source, title='视频标题'):
        root = source_directory(self.cache, source)
        root.mkdir(parents=True, exist_ok=True)
        subtitle = root / 'original.srt'
        subtitle.write_text('1\n00:00:00,000 --> 00:00:01,000\nHello\n')
        result = {'source': source, 'title': title, 'media': source,
                  'subtitle': str(subtitle), 'original': str(subtitle),
                  'timed_source': str(subtitle), 'loaded': True}
        (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False))
        return root, result

    def test_existing_results_keep_duplicate_titles_and_sort_newest_first(self):
        first, _ = self.result('https://youtu.be/first')
        second, _ = self.result('https://youtu.be/second')
        os.utime(first / 'result.json', ns=(100, 100))
        os.utime(second / 'result.json', ns=(200, 200))
        entries = cached_videos(self.cache)
        self.assertEqual([entry['source'] for entry in entries],
                         ['https://youtu.be/second', 'https://youtu.be/first'])
        self.assertEqual([entry['title'] for entry in entries], ['视频标题', '视频标题'])

    def test_listing_does_not_access_original_media_or_network_mounts(self):
        media = self.root / 'local.mkv'
        media.touch()
        root, result = self.result(str(media))
        result['source'] = result['media'] = '/mnt/unavailable/video.mkv'
        (root / 'result.json').write_text(json.dumps(result))
        original_stat = Path.stat
        def local_stat(path, *args, **kwargs):
            self.assertTrue(path.is_relative_to(self.cache), 'must only inspect local cache metadata')
            return original_stat(path, *args, **kwargs)
        with patch.object(Path, 'stat', local_stat):
            entries = cached_videos(self.cache)
        self.assertEqual(entries[0]['source'], '/mnt/unavailable/video.mkv')

    def test_broken_records_and_symlinks_do_not_break_the_list(self):
        _, good = self.result('https://youtu.be/good')
        for index, body in enumerate(('broken', '[]', '{}', '{"source":42}', '\xff')):
            root = self.cache / f'{index:024x}'
            root.mkdir()
            (root / 'result.json').write_bytes(body.encode('latin-1'))
        (self.cache / ('a' * 24)).symlink_to(self.root, target_is_directory=True)
        root = self.cache / ('b' * 24)
        root.mkdir()
        outside = self.root / 'result.json'
        outside.write_text(json.dumps(good))
        (root / 'result.json').symlink_to(outside)
        self.assertEqual([entry['source'] for entry in cached_videos(self.cache)], [good['source']])

    def test_missing_cache_is_not_created_by_listing(self):
        self.assertEqual(cached_videos(self.cache), [])
        self.assertFalse(self.cache.exists())

    def test_restore_rejects_missing_media_subtitles_and_mismatched_source(self):
        root, result = self.result('https://youtu.be/test')
        self.assertEqual(cached_result(root, result['source']), result)
        self.assertIsNone(cached_result(root, 'https://youtu.be/other'))
        result['media'] = str(root / 'missing.mp4')
        (root / 'result.json').write_text(json.dumps(result))
        self.assertIsNone(cached_result(root, result['source']))
        result['media'] = result['source']
        result['bilingual'] = str(root / 'missing.srt')
        (root / 'result.json').write_text(json.dumps(result))
        self.assertIsNone(cached_result(root, result['source']))
        result['bilingual'] = None
        (root / 'result.json').write_text(json.dumps(result))
        Path(result['subtitle']).unlink()
        self.assertIsNone(cached_result(root, result['source']))

    def test_subtitle_deletion_removes_record_but_video_deletion_keeps_it(self):
        root, result = self.result('https://youtu.be/test')
        media = root / 'video.mp4'
        media.touch()
        result['media'] = str(media)
        (root / 'result.json').write_text(json.dumps(result))
        delete_cache(self.cache, 'video')
        self.assertEqual(len(cached_videos(self.cache)), 1)
        self.assertIsNone(cached_result(root, result['source']))
        delete_cache(self.cache, 'subtitles')
        self.assertEqual(cached_videos(self.cache), [])


class CachedVideoWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_downloaded_video_without_inspection_download_asr_or_translation(self):
        from video_worker import action
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / 'video.mp4'
            media.touch()
            subtitle = root / 'original.srt'
            subtitle.write_text('cached subtitle')
            source = 'https://youtu.be/example'
            result = {'source': source, 'title': '标题', 'media': str(media),
                      'subtitle': str(subtitle), 'loaded': True}
            (root / 'result.json').write_text(json.dumps(result))
            with patch('video_jobs.directory', return_value=root), \
                    patch('video_jobs.inspect', AsyncMock()) as inspect, \
                    patch('video_jobs.command', AsyncMock()) as command, \
                    patch('video_jobs.asr_file', AsyncMock()) as asr, \
                    patch('video_jobs.translate_cues', AsyncMock()) as translate, \
                    patch('video_worker.output') as output:
                await action({'action': 'open_cached', 'source': source})
            self.assertEqual(output.call_args.args[0]['result'], result)
            for operation in (inspect, command, asr, translate):
                operation.assert_not_awaited()

    async def test_unavailable_local_video_reports_mount_instead_of_starting_processing(self):
        from video_worker import action
        with tempfile.TemporaryDirectory() as tmp, \
                patch('video_jobs.command', AsyncMock()) as command:
            with self.assertRaisesRegex(ValueError, '网盘挂载'):
                await action({'action': 'open_cached', 'source': str(Path(tmp) / 'missing.mkv')})
            command.assert_not_awaited()
