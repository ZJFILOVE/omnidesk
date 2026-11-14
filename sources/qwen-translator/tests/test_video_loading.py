"""Load already matched subtitles with no media analysis or model access."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from video_jobs import load_existing, prepare
from video_subtitles import Cue, compose, parse_srt, translate_cues


class ExistingSubtitleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video = self.root / 'episode.mkv'
        self.video.write_bytes(b'placeholder, not valid media')
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        patcher = patch('video_jobs.directory', return_value=self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('asr_file', 'probe', 'inspect', 'translate_cues', 'align'):
            patcher = patch('video_jobs.' + name, AsyncMock(side_effect=AssertionError(name + ' must not run')))
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_srt_is_copied_to_cache_without_touching_original(self):
        subtitle = self.root / 'provided.srt'
        original = compose([Cue(100, 900, 'English subtitle')]).encode('utf-16')
        subtitle.write_bytes(original)
        result = await load_existing({'source': str(self.video), 'subtitle_path': str(subtitle),
                                      'make_chinese': True, 'sync_mode': 'auto', 'offset_ms': 800}, AsyncMock())
        self.assertEqual(Path(result['subtitle']).read_text(), compose([Cue(100, 900, 'English subtitle')]))
        self.assertNotEqual(Path(result['subtitle']), subtitle)
        self.assertEqual(subtitle.read_bytes(), original)
        self.assertEqual(result['media'], str(self.video))
        self.assertTrue(result['loaded'])
        self.assertTrue((self.cache / 'result.json').is_file())

    async def test_ass_is_converted_before_loading(self):
        subtitle = self.root / 'provided.ass'
        original = '[Script Info]\nTitle: placeholder\n'
        subtitle.write_text(original)
        body = compose([Cue(100, 900, 'converted')])
        with patch('video_jobs.tool', return_value='ffmpeg'), \
                patch('video_jobs.command', AsyncMock(return_value=body.encode())) as command:
            result = await load_existing({'source': str(self.video), 'subtitle_path': str(subtitle)}, AsyncMock())
        self.assertEqual(command.await_count, 1)
        self.assertEqual(command.await_args.args[0][-3:], ['-f', 'srt', 'pipe:1'])
        self.assertEqual(Path(result['subtitle']).read_text(), body)
        self.assertEqual(subtitle.read_text(), original)

    async def test_selected_embedded_subtitle_can_be_loaded(self):
        cues = [Cue(100, 900, 'embedded')]
        with patch('video_jobs.extract_subtitle', AsyncMock(return_value=(cues, self.cache / 'embedded.srt'))) as extract:
            result = await load_existing({'source': str(self.video), 'subtitle_stream': 2}, AsyncMock())
        extract.assert_awaited_once_with(str(self.video), 2, self.cache)
        self.assertEqual(Path(result['subtitle']).read_text(), compose(cues))

    async def test_missing_selection_is_an_error_not_an_asr_fallback(self):
        with self.assertRaises(ValueError):
            await load_existing({'source': str(self.video)}, AsyncMock())

    async def test_english_subtitle_aligns_then_translates_preserving_original(self):
        subtitle = self.root / 'english.srt'
        original = [Cue(100, 900, 'English sentence')]
        subtitle.write_text(compose(original))
        aligned = [Cue(2100, 2900, 'English sentence')]
        info = {'title': self.video.name, 'audio': [], 'subtitles': []}
        # Exercise the real numbered-batch translator, replacing only the Codex call.
        with patch('video_jobs.inspect', AsyncMock(return_value=info)), \
                patch('video_jobs.align', AsyncMock(return_value=(aligned, {'offset_seconds': 2}))) as align, \
                patch('video_jobs.translate_cues', translate_cues), \
                patch('codex_session.CodexSession.translate', AsyncMock(return_value={
                    'text': '[{"id":0,"text":"英文句子"}]'})) as translate, \
                patch('codex_session.CodexSession.close', AsyncMock()):
            result = await prepare({'source': str(self.video), 'subtitle_path': str(subtitle),
                                    'text_provider': 'codex', 'make_chinese': True, 'sync_mode': 'auto'}, AsyncMock())
        self.assertEqual(result['origin'], 'external')
        self.assertEqual(align.await_count, 1)
        self.assertEqual(translate.await_count, 1)
        self.assertEqual(parse_srt(Path(result['original']).read_text()), original)
        for field in ('timed_source', 'subtitle', 'bilingual'):
            cues = parse_srt(Path(result[field]).read_text())
            self.assertEqual([(c.start, c.end) for c in cues], [(2100, 2900)])
        self.assertEqual(parse_srt(Path(result['subtitle']).read_text())[0].text, '英文句子')
        self.assertEqual(subtitle.read_text(), compose(original))

    async def test_nas_auto_return_records_video_content_and_rejects_change_during_translation(self):
        from nas_io import fingerprint
        subtitle = self.root / 'english.srt'
        subtitle.write_text(compose([Cue(100, 900, 'Hello world.')]))
        info = {'title': self.video.name, 'audio': [], 'subtitles': []}
        config = {'source': str(self.video), 'subtitle_path': str(subtitle),
                  'make_chinese': True, 'sync_mode': 'none', 'nas_writeback': True}
        with patch('video_jobs.inspect', AsyncMock(return_value=info)), \
                patch('video_jobs.translate_cues', AsyncMock(return_value=[Cue(100, 900, '你好。')])):
            result = await prepare(config, AsyncMock())
        self.assertEqual(result['source_fingerprint'], fingerprint(self.video))

        async def replace_video(*args):
            self.video.write_bytes(b'a replaced video')
            return [Cue(100, 900, '你好。')]
        with patch('video_jobs.inspect', AsyncMock(return_value=info)), \
                patch('video_jobs.translate_cues', side_effect=replace_video), self.assertRaises(ValueError):
            await prepare(config, AsyncMock())


if __name__ == '__main__':
    unittest.main()
