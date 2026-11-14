"""ASR checkpoints resume work; explicit regeneration discards only ASR progress."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from video_jobs import asr_file, prepare
from video_subtitles import Cue, compose


class ASRCheckpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.starts = []
        self.fail_at = None
        self.output = 'original'

        async def command(args, **kwargs):
            if args[0] == 'ffmpeg':
                at = float(args[args.index('-ss') + 1])
                self.starts.append(at)
                if at == self.fail_at:
                    raise RuntimeError('interrupted')
                return b''
            return compose([Cue(0, 800, self.output)]).encode()

        for patcher in (
            patch('video_jobs.asr_available', return_value=True),
            patch('video_jobs.file_identity', side_effect=lambda path: [str(path), 1, 1]),
            patch('video_jobs.tool', side_effect=lambda name: name),
            patch('video_jobs.command', side_effect=command),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def recognize(self, reset=False):
        return await asr_file('media.mkv', 241, None, self.root, AsyncMock(), reset_cache=reset)

    async def test_resume_skips_completed_chunks_but_reset_reprocesses_all(self):
        original = await self.recognize()
        self.assertEqual(self.starts, [0, 120, 240])
        self.starts.clear()
        self.assertEqual(await self.recognize(), original)
        self.assertEqual(self.starts, [])
        product = self.root / 'prepared' / 'original.srt'
        product.parent.mkdir()
        product.write_text(compose(original))
        self.output = 'corrected'
        regenerated = await self.recognize(reset=True)
        self.assertEqual(self.starts, [0, 120, 240])
        self.assertEqual([c.text for c in regenerated], ['corrected'] * 3)
        self.assertEqual(product.read_text(), compose(original))

    async def test_interrupted_reset_does_not_restore_old_completed_checkpoint(self):
        await self.recognize()
        self.starts.clear()
        self.fail_at = 0
        with self.assertRaises(RuntimeError):
            await self.recognize(reset=True)
        checkpoint = next(self.root.glob('asr-*.json'))
        self.assertEqual(json.loads(checkpoint.read_text()), {'next': 0, 'cues': []})
        self.fail_at = None
        self.starts.clear()
        self.output = 'fresh'
        result = await self.recognize()
        self.assertEqual(self.starts, [0, 120, 240])
        self.assertEqual([c.text for c in result], ['fresh'] * 3)

    async def test_interrupted_run_resumes_at_first_unfinished_chunk(self):
        self.fail_at = 120
        with self.assertRaises(RuntimeError):
            await self.recognize()
        self.fail_at = None
        self.starts.clear()
        result = await self.recognize()
        self.assertEqual(self.starts, [120, 240])
        self.assertEqual([c.start for c in result], [0, 120000, 240000])

    async def test_corrupt_checkpoint_starts_over(self):
        await self.recognize()
        checkpoint = next(self.root.glob('asr-*.json'))
        for corrupt in ('{', 'null', '[]', '{"next":999,"cues":[]}',
                        '{"next":NaN,"cues":[]}', '{"next":120,"cues":{}}',
                        '{"next":120,"cues":[{"start":0,"end":800,"text":null}]}',
                        '{"next":120,"cues":[{"start":0,"end":121000,"text":"bad"}]}'):
            with self.subTest(checkpoint=corrupt):
                checkpoint.write_text(corrupt)
                self.starts.clear()
                result = await self.recognize()
                self.assertEqual(self.starts, [0, 120, 240])
                self.assertEqual(len(result), 3)

    async def test_prepare_reset_also_ignores_existing_subtitles(self):
        source = self.root / 'video.mkv'
        source.touch()
        info = {'title': 'video', 'audio': [], 'subtitles': [{'path': 'existing.srt'}]}
        with patch('video_jobs.directory', return_value=self.root), \
                patch('video_jobs.inspect', AsyncMock(return_value=info)), \
                patch('video_jobs.probe', AsyncMock(return_value=({}, 241))), \
                patch('video_jobs.asr_file', AsyncMock(return_value=[Cue(0, 800, 'fresh')])) as recognize, \
                patch('video_jobs.load_subtitle', AsyncMock()) as load:
            await prepare({'source': str(source), 'reset_asr_cache': True,
                           'subtitle_path': 'provided.srt', 'make_chinese': False, 'sync_mode': 'none'}, AsyncMock())
        load.assert_not_awaited()
        self.assertTrue(recognize.await_args.kwargs['reset_cache'])


if __name__ == '__main__':
    unittest.main()
