"""YouTube source captions and ASR workflows with entirely mocked network/media."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from video_jobs import load_subtitle, obtain_original, prepare
from video_subtitles import Cue, compose, parse_srt


class YouTubeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = 'https://www.youtube.com/watch?v=example'
        self.info = {'title': 'Example lesson', 'subtitles': {'en': [{}]}}
        self.cues = [Cue(100, 900, 'An English sentence')]
        self.commands = []
        self.fail_download = False
        self.converted_srt = compose(self.cues)

        async def command(args, **kwargs):
            self.commands.append(args)
            if '--dump-single-json' in args:
                return json.dumps(self.info).encode()
            if '--write-subs' in args or '--write-auto-subs' in args:
                if self.fail_download:
                    raise RuntimeError('download failed')
                language = args[args.index('--sub-langs') + 1]
                (self.root / f'youtube.{language}.vtt').write_text('WEBVTT\n')
                return b''
            if '-f' in args and args[0] == 'yt-dlp':
                self.assertIn('--progress-template', args)
                await kwargs['on_line'](b'QWEN_DOWNLOAD:{"status":"downloading","downloaded":50,"total":100}\n')
                media = self.root / ('video.mp4' if args[args.index('-f') + 1] != 'ba/b' else 'audio.m4a')
                media.touch()
                return str(media).encode()
            if args[0] == 'ffmpeg':
                return self.converted_srt.encode()
            raise AssertionError(args)

        for patcher in (patch('video_jobs.directory', return_value=self.root),
                        patch('video_jobs.tool', side_effect=lambda name: name),
                        patch('video_jobs.command', side_effect=command),
                        patch('video_jobs.align', AsyncMock(side_effect=AssertionError('no alignment for source captions')))):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_manual_and_auto_captions_are_exportable_without_models_or_asr(self):
        for kind, origin in (('subtitles', 'youtube_manual'), ('automatic_captions', 'youtube_auto')):
            with self.subTest(origin=origin):
                self.info = {'title': 'Example lesson', kind: {'en': [{}]}}
                self.commands.clear()
                with patch('video_jobs.asr_file', AsyncMock(side_effect=AssertionError('ASR must not run'))), \
                        patch('video_jobs.translate_cues', AsyncMock(side_effect=AssertionError('translation must not run'))):
                    result = await obtain_original({'source': self.source, 'download_video': False,
                                                    'make_chinese': True, 'sync_mode': 'auto', 'offset_ms': 700}, AsyncMock())
                self.assertEqual(result['origin'], origin)
                self.assertTrue(result['original_only'])
                self.assertEqual(parse_srt(Path(result['original']).read_text()), self.cues)
                self.assertEqual(parse_srt(Path(result['subtitle']).read_text()), self.cues)
                self.assertEqual(result['media'], self.source)
                self.assertFalse(any('--print' in args for args in self.commands))

    async def test_english_manual_and_auto_captions_translate_without_asr(self):
        for kind, origin in (('subtitles', 'youtube_manual'), ('automatic_captions', 'youtube_auto')):
            with self.subTest(origin=origin):
                self.info = {'title': 'Example lesson', kind: {'en': [{}]}}
                translated = [Cue(100, 900, '一句英文')]
                with patch('video_jobs.asr_file', AsyncMock(side_effect=AssertionError('ASR must not run'))), \
                        patch('video_jobs.translate_cues', AsyncMock(return_value=translated)) as translate:
                    result = await prepare({'source': self.source, 'download_video': False,
                                            'text_provider': 'codex', 'make_chinese': True}, AsyncMock())
                self.assertEqual(result['origin'], origin)
                translate.assert_awaited_once()
                self.assertEqual(translate.await_args.args[0], self.cues)
                self.assertEqual(translate.await_args.args[1]['text_provider'], 'codex')
                self.assertEqual(parse_srt(Path(result['subtitle']).read_text()), translated)
                self.assertEqual(parse_srt(Path(result['original']).read_text()), self.cues)
                self.assertEqual(parse_srt(Path(result['bilingual']).read_text())[0].start, 100)

    async def test_absent_captions_download_audio_then_export_asr_original(self):
        self.info = {'title': 'No captions'}
        with patch('video_jobs.probe', AsyncMock(return_value=({}, 1))), \
                patch('video_jobs.asr_file', AsyncMock(return_value=self.cues)) as asr, \
                patch('video_jobs.translate_cues', AsyncMock(side_effect=AssertionError('no translation'))):
            result = await obtain_original({'source': self.source, 'download_video': False}, AsyncMock())
        self.assertEqual(result['origin'], 'asr')
        self.assertEqual(asr.await_args.args[0], str(self.root / 'audio.m4a'))
        self.assertEqual(parse_srt(Path(result['original']).read_text()), self.cues)
        self.assertEqual(result['media'], self.source)

    async def test_explicit_asr_bypasses_available_auto_captions(self):
        self.info = {'title': 'Rolling captions', 'language': 'en',
                     'automatic_captions': {'en-orig': [{}]}}
        self.fail_download = True
        with patch('video_jobs.probe', AsyncMock(return_value=({}, 1))), \
                patch('video_jobs.asr_file', AsyncMock(return_value=self.cues)) as asr:
            result = await obtain_original({'source': self.source, 'download_video': False,
                                            'force_asr': True}, AsyncMock())
        self.assertEqual(result['origin'], 'asr')
        self.assertEqual(asr.await_args.args[0], str(self.root / 'audio.m4a'))
        self.assertFalse(any('--write-auto-subs' in args for args in self.commands))

    async def test_download_video_option_produces_local_playable_media(self):
        emit = AsyncMock()
        with patch('video_jobs.asr_file', AsyncMock(side_effect=AssertionError('ASR must not run'))):
            result = await obtain_original({'source': self.source, 'download_video': True}, emit)
        self.assertEqual(result['media'], str(self.root / 'video.mp4'))
        self.assertTrue(Path(result['media']).is_file())
        self.assertTrue(any(call.args[0].get('progress') == .5 for call in emit.await_args_list))

    async def test_auto_captions_with_blank_placeholder_export_without_asr(self):
        self.info = {'title': 'Rolling captions', 'language': 'en',
                     'automatic_captions': {'en-orig': [{}]}}
        self.converted_srt = '1\n00:00:00,100 --> 00:00:00,900\n \nAn English sentence\n \n\n'
        with patch('video_jobs.asr_file', AsyncMock(side_effect=AssertionError('ASR must not run'))):
            result = await obtain_original({'source': self.source, 'download_video': False}, AsyncMock())
        self.assertEqual(result['origin'], 'youtube_auto')
        self.assertEqual(parse_srt(Path(result['original']).read_text()), self.cues)

    async def test_caption_download_failure_does_not_fall_back_to_asr(self):
        self.fail_download = True
        with patch('video_jobs.asr_file', AsyncMock()) as asr, self.assertRaisesRegex(RuntimeError, 'download failed'):
            await obtain_original({'source': self.source, 'download_video': False}, AsyncMock())
        asr.assert_not_awaited()
        self.assertFalse(any('--print' in args for args in self.commands))

    async def test_platform_request_failure_does_not_fall_back_to_asr(self):
        with patch('video_jobs.command', AsyncMock(side_effect=RuntimeError('platform unavailable'))), \
                patch('video_jobs.asr_file', AsyncMock()) as asr, self.assertRaisesRegex(RuntimeError, 'platform unavailable'):
            await obtain_original({'source': self.source, 'download_video': False}, AsyncMock())
        asr.assert_not_awaited()


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required for real VTT conversion')
class YouTubeCaptionConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ffmpeg_rolling_captions_preserve_text_and_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'youtube.en-orig.vtt'
            source.write_text(
                'WEBVTT\nKind: captions\nLanguage: en\n\n'
                '00:00:00.080 --> 00:00:01.910 align:start position:0%\n'
                ' \nHello<00:00:00.320><c> everyone.</c>\n\n'
                '00:00:01.910 --> 00:00:01.920 align:start position:0%\n'
                'Hello everyone.\n \n\n'
                '00:00:01.920 --> 00:00:05.430 align:start position:0%\n'
                'Hello everyone.\nWelcome<00:00:02.159><c> back.</c>\n\n', encoding='utf-8')
            cues = await load_subtitle(source, root)
        self.assertEqual(cues, [Cue(80, 1910, 'Hello everyone.'),
                                Cue(1910, 1920, 'Hello everyone.'),
                                Cue(1920, 5430, 'Hello everyone.\nWelcome back.')])
        self.assertEqual(parse_srt(compose(cues)), cues)


if __name__ == '__main__':
    unittest.main()
