import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from video_subtitles import (Cue, batch_result, chinese, compose, decode, parse_srt,
                             transform, translate_cues, two_anchors, translation_batches)
from subtitle_sources import (SubHDSearch, episode, search_name, subtitle_files, validate_url)


class SubtitleTests(unittest.TestCase):
    def test_srt_encoding_markup_multiline_and_order(self):
        text = '1\r\n00:00:02,000 --> 00:00:04,100\r\n<i>Hello</i> &amp; welcome\r\n第二行\r\n\r\n'
        cues = parse_srt(decode(text.encode('utf-16')))
        self.assertEqual(cues, [Cue(2000, 4100, 'Hello & welcome\n第二行')])
        self.assertEqual(parse_srt(compose(cues)), cues)
        self.assertTrue(chinese(cues))
        self.assertEqual(decode('中文字幕'.encode('gb18030')), '中文字幕')

    def test_invalid_html_is_not_subtitle(self):
        with self.assertRaises(ValueError):
            parse_srt('<html>Please log in</html>')

    def test_offset_and_drift(self):
        scale, offset = two_anchors(1000, 2200, 101000, 106200)
        self.assertAlmostEqual(scale, 1.04)
        cues = transform([Cue(1000, 2000, 'one'), Cue(101000, 102000, 'two')], scale, offset)
        self.assertEqual([c.start for c in cues], [2200, 106200])
        self.assertEqual(transform([Cue(0, 1000, 'one')], 1, -500), [Cue(0, 500, 'one')])
        with self.assertRaises(ValueError):
            two_anchors(1000, 0, 500, 100)

    def test_numbered_batches_reject_missing_and_duplicates(self):
        self.assertEqual(batch_result('[{"id":2,"text":"二"},{"id":1,"text":"一"}]', [1, 2]), {1:'一',2:'二'})
        for bad in ('[]', '[{"id":1,"text":""}]', '[{"id":3,"text":"三"}]',
                    '[{"id":true,"text":"一"}]'):
            with self.assertRaises(ValueError):
                batch_result(bad, [1])
        with self.assertRaises(ValueError):
            batch_result('[{"id":1,"text":"一"},{"id":1,"text":"一"}]', [1, 2])

    def test_ten_minute_windows_and_dense_caption_guard(self):
        cues = [Cue(t, t+1000, 'text') for t in (0, 599_000, 600_000, 1_199_000, 1_800_000)]
        self.assertEqual(list(translation_batches(cues, range(5))), [[0,1], [2,3], [4]])
        self.assertEqual(list(translation_batches(cues, [1,3,4])), [[1], [3], [4]])
        dense = [Cue(i, i+1, 'a'*13_000) for i in range(3)]
        self.assertEqual(list(translation_batches(dense, range(3))), [[0], [1], [2]])

    def test_episode_search_preserves_episode(self):
        self.assertEqual(search_name('Stranger.Things.S02E01.Chapter.One.1080p.mkv'), 'Stranger Things S02E01')
        self.assertEqual(episode('Stranger Things S02E01'), (2, 1))
        parser = SubHDSearch()
        parser.feed("<a href='/a/abc'>剧名</a><a href='/a/abc'>Show.S02E01</a><a href='/login'>login</a>")
        self.assertEqual(parser.items, {'/a/abc': ['剧名', 'Show.S02E01']})

    def test_archives_never_extract_paths_and_ignore_code(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('../../outside.srt', 'subtitle')
            archive.writestr('run.py', 'raise Exception()')
        self.assertEqual(subtitle_files(data.getvalue(), 'subs.zip'), [('outside.srt', b'subtitle')])
        with self.assertRaises(ValueError):
            subtitle_files(b'Rar!content', 'subs.rar')
        for url in ('file:///etc/passwd', 'http://localhost/a', 'http://127.0.0.1/a', 'https://user:pass@site.com/a'):
            with self.assertRaises(ValueError):
                validate_url(url)


class TranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_cue_invalid_response_reaches_provider_on_retry(self):
        for malformed in ('not JSON', '[]', '[{"id":1,"text":"错误编号"}]'):
            with self.subTest(response=malformed), tempfile.TemporaryDirectory() as tmp:
                provider = AsyncMock(side_effect=[{'text': malformed}, {'text': '[{"id":0,"text":"译文"}]'}])
                with patch('text_translation.translate_text', provider):
                    cues = [Cue(0, 100, 'source')]
                    first = await translate_cues(cues, {'text_provider': 'api'}, tmp, AsyncMock())
                    resumed = await translate_cues(cues, {'text_provider': 'api'}, tmp, AsyncMock())
                self.assertEqual(provider.await_count, 2)
                self.assertEqual(provider.await_args_list[0].args[0], provider.await_args_list[1].args[0])
                self.assertEqual(first, [Cue(0, 100, '译文')])
                self.assertEqual(resumed, first)

    async def test_single_cue_tail_retries_and_preserves_completed_batch(self):
        first_batch = json.dumps([{'id': i, 'text': f'译文{i}'} for i in range(6)])
        provider = AsyncMock(side_effect=[{'text': first_batch}, {'text': '[]'},
                                         {'text': '[{"id":6,"text":"译文6"}]'}])
        with tempfile.TemporaryDirectory() as tmp, patch('text_translation.translate_text', provider):
            cues = [Cue(i * 100_000, i * 100_000 + 800, str(i)) for i in range(7)]
            result = await translate_cues(cues, {'text_provider': 'api'}, tmp, AsyncMock())
        self.assertEqual(provider.await_count, 3)
        self.assertEqual([cue.text for cue in result], [f'译文{i}' for i in range(7)])

    async def test_corrupt_translation_checkpoint_is_retranslated(self):
        with tempfile.TemporaryDirectory() as tmp:
            for corrupt in ('[]', 'null', '{', '{"100":"越界","0":null}'):
                with self.subTest(checkpoint=corrupt):
                    provider = AsyncMock(return_value={'text': '[{"id":0,"text":"译文"}]'})
                    with patch('text_translation.translate_text', provider):
                        cues = [Cue(0, 100, 'source')]
                        await translate_cues(cues, {'text_provider': 'api'}, tmp, AsyncMock())
                        next(Path(tmp).glob('translation-*.json')).write_text(corrupt)
                        provider.reset_mock()
                        result = await translate_cues(cues, {'text_provider': 'api'}, tmp, AsyncMock())
                    self.assertEqual(provider.await_count, 1)
                    self.assertEqual(result, [Cue(0, 100, '译文')])

    async def test_translation_resumes_without_repeating_completed_cues(self):
        calls = []
        class Fake:
            def __init__(self, config):
                self.config = config
            async def translate(self, text):
                rows = json.loads(text)
                calls.append([r['id'] for r in rows])
                return {'text': json.dumps([{'id': r['id'], 'text': '译文' + str(r['id'])} for r in rows])}
            async def close(self):
                pass
        async def emit(event):
            pass
        cues = [Cue(i*1000, i*1000+800, 'source '+str(i)) for i in range(8)]
        with tempfile.TemporaryDirectory() as tmp:
            first = await translate_cues(cues, {'text_provider': 'api'}, tmp, emit, Fake)
            second = await translate_cues(cues, {'text_provider': 'api'}, tmp, emit, Fake)
        self.assertEqual(calls, [[0,1,2,3,4,5,6,7]])
        self.assertEqual(first, second)
        self.assertEqual([(c.start,c.end) for c in first], [(c.start,c.end) for c in cues])

    async def test_split_retry_checkpoints_success_before_later_failure(self):
        cues = [Cue(i*1000, i*1000+800, str(i)) for i in range(8)]
        calls = []
        fail = True
        class Fake:
            def __init__(self, config): pass
            async def close(self): pass
            async def translate(self, text):
                ids = [row['id'] for row in json.loads(text)]
                calls.append(ids)
                if len(ids) > 4:
                    return {'text': '[]'}
                if fail and ids[0] == 4:
                    raise RuntimeError('connection lost')
                return {'text': json.dumps([{'id': i, 'text': f'译文{i}'} for i in reversed(ids)])}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, 'connection lost'):
                await translate_cues(cues, {'text_provider':'api'}, tmp, AsyncMock(), Fake)
            saved = json.loads(next(Path(tmp).glob('translation-*.json')).read_text())
            self.assertEqual(set(saved), {'0','1','2','3'})
            fail = False
            result = await translate_cues(cues, {'text_provider':'api'}, tmp, AsyncMock(), Fake)
        self.assertEqual(calls, [list(range(8)), list(range(4)), list(range(4,8)), list(range(4,8))])
        self.assertEqual([c.text for c in result], [f'译文{i}' for i in range(8)])
        self.assertEqual([(c.start,c.end) for c in result], [(c.start,c.end) for c in cues])

    async def test_malformed_batch_retries_individually(self):
        class Fake:
            def __init__(self, config):
                pass
            async def translate(self, text):
                rows = json.loads(text)
                return {'text': '[]' if len(rows)>1 else json.dumps([{'id':rows[0]['id'], 'text':'译文'}])}
            async def close(self):
                pass
        async def emit(event):
            pass
        with tempfile.TemporaryDirectory() as tmp:
            result = await translate_cues([Cue(0,100,'a'), Cue(200,300,'b')], {'text_provider':'api'}, tmp, emit, Fake)
        self.assertEqual([c.text for c in result], ['译文','译文'])


class AntigravityCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_switching_antigravity_model_does_not_reuse_other_model_translation(self):
        calls = []
        class Translator:
            def __init__(self, config):
                self.model = config['antigravity_model']
            async def translate(self, text):
                calls.append(self.model)
                return {'text': json.dumps([{'id': item['id'], 'text': self.model}
                                           for item in json.loads(text)])}
            async def close(self):
                pass
        async def emit(event):
            pass
        with tempfile.TemporaryDirectory() as directory:
            for model in ('first', 'second', 'first'):
                result = await translate_cues([Cue(0, 1000, 'Hello')],
                    {'text_provider': 'antigravity', 'antigravity_model': model}, directory, emit, Translator)
                self.assertEqual(result[0].text, model)
            self.assertEqual(calls, ['first', 'second'])


if __name__ == '__main__':
    unittest.main()
