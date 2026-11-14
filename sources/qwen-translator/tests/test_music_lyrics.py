import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.error

from music_lyrics import (LYRICS_BATCH_INSTRUCTIONS, LyricsRepository, _LyricsTranslator,
                          compose_lrc, has_chinese, parse_lyrics)
from video_subtitles import Cue


TRACK = {'title': 'Hello', 'artist': 'An Artist', 'album': 'An Album', 'duration': 210.25}


class LyricsParsingTests(unittest.TestCase):
    def test_timestamps_offsets_repeated_lines_and_bilingual_pairing(self):
        lines = parse_lyrics('[ar:Artist]\n[offset:-250]\n[00:01.125][00:20.5]Hello\n'
                             '[00:01.125][00:20.5]你好\n[00:25.000]\n')
        self.assertEqual([(line['start'], line['text'], line['translation']) for line in lines],
                         [(.875, 'Hello', '你好'), (20.25, 'Hello', '你好'), (24.75, '', '')])
        self.assertEqual(lines, parse_lyrics('[ar:Artist]\n[offset:-250]\n'
                                            '[00:01.125][00:20.5]Hello\n'
                                            '[00:01.125][00:20.5]你好\n[00:25.000]\n'))
        self.assertNotEqual(lines[0]['id'], lines[1]['id'])
        self.assertIn('[00:00.875]Hello\n[00:00.875]你好', compose_lrc(lines, 'bilingual'))
        self.assertTrue(compose_lrc(lines, 'bilingual').endswith('[00:24.750]\n'))

    def test_plain_and_same_line_bilingual_lyrics_keep_alignment(self):
        plain = parse_lyrics('Hello\n你好\nGoodbye\n再见')
        self.assertEqual([(line['text'], line['translation']) for line in plain],
                         [('Hello', '你好'), ('Goodbye', '再见')])
        self.assertTrue(all(line['start'] is None for line in plain))
        self.assertEqual(compose_lrc(plain, 'translation'), '你好\n再见\n')
        line = parse_lyrics('[01:03.009]Hello / 你好')[0]
        self.assertEqual((line['start'], line['text'], line['translation']), (63.009, 'Hello', '你好'))

    def test_short_chinese_and_kana_have_distinct_detection(self):
        self.assertTrue(has_chinese('爱'))
        self.assertTrue(has_chinese('你好'))
        self.assertFalse(has_chinese('君の名は'))
        self.assertFalse(has_chinese('あなた'))
        self.assertFalse(has_chinese('Hello'))

    def test_hours_enhanced_timestamps_and_invalid_seconds(self):
        lines = parse_lyrics('[01:02:03.001]<00:01.00>Hello\n[00:75.1]Invalid')
        self.assertEqual([(line['start'], line['text']) for line in lines], [(3723.001, 'Hello')])


class LyricsRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.repository = LyricsRepository(self.directory)
        self.config = {'text_provider': 'codex', 'music_lyrics_online': False}

    async def test_supplied_bilingual_never_calls_network_or_model_and_exports(self):
        with patch('music_lyrics.translate_cues', AsyncMock()) as translate, \
                patch.object(self.repository, '_fetch', AsyncMock()) as fetch:
            result = await self.repository.get({**TRACK, 'lyrics': '[00:02.001]Hello\n[00:02.001]你好'},
                                               self.config)
        fetch.assert_not_awaited()
        translate.assert_not_awaited()
        self.assertTrue(result['synced'])
        self.assertTrue(result['translated'])
        self.assertEqual(result['translation_status'], 'existing')
        self.assertEqual(Path(result['paths']['original']).read_text(), '[00:02.001]Hello\n')
        self.assertEqual(Path(result['paths']['translation']).read_text(), '[00:02.001]你好\n')
        self.assertEqual(result['source'], 'player')

    async def test_chinese_original_needs_no_model_even_single_character(self):
        with patch('music_lyrics.translate_cues', AsyncMock()) as translate:
            result = await self.repository.get({**TRACK, 'lyrics': '[00:01]爱\n[00:02]你好'}, self.config)
        translate.assert_not_awaited()
        self.assertEqual(result['translation_status'], 'original_chinese')
        self.assertEqual(Path(result['paths']['translation']).read_text(), '[00:01.000]爱\n[00:02.000]你好\n')

    async def test_non_latin_lyrics_are_translated_rather_than_treated_as_chinese(self):
        for text in ('Привет', 'مرحبا', '안녕하세요'):
            with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '你好')])) as translate:
                result = await self.repository.get({**TRACK, 'title': text, 'lyrics': text}, self.config)
            translate.assert_awaited_once()
            self.assertEqual(result['translation_status'], 'translated')

    async def test_japanese_context_translates_kanji_only_lines_and_preserves_timing_ids(self):
        original = '[00:01.123]空\n[00:03.456]君がいる'
        calls = []
        async def translator(cues, config, directory, emit, **kwargs):
            calls.append((cues, config))
            return [Cue(9000, 9999, '天空'), Cue(8888, 9999, '有你在')]
        with patch('music_lyrics.translate_cues', translator):
            result = await self.repository.get({**TRACK, 'lyrics': original}, self.config)
        self.assertEqual([cue.text for cue in calls[0][0]], ['空', '君がいる'])
        self.assertEqual(calls[0][1]['codex_model'], 'gpt-6-luna')
        self.assertEqual([line['start'] for line in result['lines']], [1.123, 3.456])
        self.assertEqual([line['id'] for line in result['lines']], [line['id'] for line in parse_lyrics(original)])
        self.assertEqual([line['translation'] for line in result['lines']], ['天空', '有你在'])

    async def test_existing_provider_and_model_settings_pass_through(self):
        with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '你好')])) as translate:
            await self.repository.get({**TRACK, 'lyrics': 'Hello'}, {
                **self.config, 'text_provider': 'antigravity', 'antigravity_model': 'chosen',
                'codex_model': 'user-model', 'text_api_key': 'secret',
            })
        passed = translate.await_args.args[1]
        self.assertEqual((passed['text_provider'], passed['antigravity_model'], passed['codex_model']),
                         ('antigravity', 'chosen', 'user-model'))
        for path in self.directory.rglob('*'):
            if path.is_file():
                self.assertNotIn('secret', path.read_text())
                self.assertNotIn('text_api_key', path.read_text())

    async def test_translated_cache_survives_repository_restart_without_network_or_model(self):
        with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '你好')])) as translate:
            first = await self.repository.get({**TRACK, 'lyrics': '[00:01]Hello'}, self.config)
        restarted = LyricsRepository(self.directory)
        with patch('music_lyrics.translate_cues', AsyncMock()) as translate, \
                patch.object(restarted, '_fetch', AsyncMock()) as fetch:
            second = await restarted.get(TRACK, {**self.config, 'music_lyrics_online': True})
        translate.assert_not_awaited()
        fetch.assert_not_awaited()
        self.assertEqual(second['lines'], first['lines'])
        self.assertTrue(second['cache_hit'])

    async def test_new_native_chinese_replaces_cached_model_translation(self):
        with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '模型译文')])):
            await self.repository.get({**TRACK, 'lyrics': '[00:01]Hello'}, self.config)
        with patch('music_lyrics.translate_cues', AsyncMock()) as translate:
            result = await self.repository.get({**TRACK, 'lyrics': '[00:01]Hello',
                                                'translated_lyrics': '[00:01]原生中文'}, self.config)
        translate.assert_not_awaited()
        self.assertEqual(result['lines'][0]['translation'], '原生中文')

    async def test_native_chinese_first_bilingual_japanese_never_uses_model(self):
        with patch('music_lyrics.translate_cues', AsyncMock()) as translate:
            result = await self.repository.get({**TRACK, 'lyrics':
                '[00:01.234]有你在\n[00:01.234]君がいる\n[00:02.345]永远\n[00:02.345]Forever'}, self.config)
        translate.assert_not_awaited()
        self.assertEqual([(line['text'], line['translation']) for line in result['lines']],
                         [('君がいる', '有你在'), ('Forever', '永远')])

    async def test_lyrics_adapter_gives_account_sessions_lyrics_instructions_and_closes(self):
        session = Mock()
        session.translate = AsyncMock(return_value={'text': '[{"id":0,"text":"你好"}]'})
        session.close = AsyncMock()
        with patch('codex_session.CodexSession', Mock(return_value=session)) as constructor:
            adapter = _LyricsTranslator({'text_provider': 'codex', 'codex_model': 'gpt-6-luna', 'video_batch': True})
            result = await adapter.translate('[{"id":0,"text":"Hello"}]')
            await adapter.close()
        self.assertIn('你好', result['text'])
        self.assertEqual(constructor.call_args.args[0]['codex_model'], 'gpt-6-luna')
        self.assertEqual(session.translate.await_args.kwargs['instructions'], LYRICS_BATCH_INSTRUCTIONS)
        session.close.assert_awaited_once()

    async def test_real_batch_validation_with_lyrics_factory_and_persistent_checkpoint(self):
        session = Mock()
        session.translate = AsyncMock(return_value={'text': '[{"id":0,"text":"你好"},{"id":1,"text":"再见"}]'})
        session.close = AsyncMock()
        with patch('codex_session.CodexSession', Mock(return_value=session)):
            result = await self.repository.get({**TRACK, 'lyrics': '[00:01.234]Hello\n[00:03.456]Goodbye'}, self.config)
        self.assertEqual(result['translation_status'], 'translated')
        self.assertEqual([line['translation'] for line in result['lines']], ['你好', '再见'])
        self.assertTrue(list(self.directory.rglob('translation-*.json')))
        session.close.assert_awaited_once()

    async def test_disabled_translation_does_not_prevent_later_retry(self):
        track = {**TRACK, 'lyrics': 'Hello'}
        for config in ({**self.config, 'music_translation_enabled': False},
                       {**self.config, 'text_provider': 'none'}):
            with patch('music_lyrics.translate_cues', AsyncMock()) as translate:
                result = await self.repository.get(track, config)
            translate.assert_not_awaited()
            self.assertEqual(result['translation_status'], 'disabled')
        with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '你好')])) as translate:
            result = await self.repository.get(TRACK, self.config)
        translate.assert_awaited_once()
        self.assertEqual(result['translation_status'], 'translated')

    async def test_failure_keeps_original_hides_secrets_and_is_retryable(self):
        track = {**TRACK, 'lyrics': '[00:01.111]Hello'}
        with patch('music_lyrics.translate_cues', AsyncMock(side_effect=RuntimeError('secret sk-private-abc'))):
            failed = await self.repository.get(track, {**self.config, 'text_api_key': 'secret'})
        self.assertEqual(failed['translation_status'], 'error')
        self.assertNotIn('secret', json.dumps(failed))
        self.assertEqual(Path(failed['paths']['original']).read_text(), '[00:01.111]Hello\n')
        self.assertFalse(any('.partial' in path.name for path in self.directory.rglob('*')))
        with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '你好')])) as translate:
            retried = await self.repository.get(TRACK, self.config)
        translate.assert_awaited_once()
        self.assertEqual(retried['translation_status'], 'translated')
        self.assertEqual(failed['lines'][0]['id'], retried['lines'][0]['id'])

    async def test_original_snapshot_is_saved_and_visible_before_model_finishes(self):
        started, release = asyncio.Event(), asyncio.Event()
        updates = []
        async def translator(cues, config, directory, emit, **kwargs):
            started.set()
            await release.wait()
            return [Cue(0, 1000, '你好')]
        async def updated(result):
            updates.append(result)
            self.assertEqual(Path(result['paths']['original']).read_text(), '[00:01.234]Hello\n')
        with patch('music_lyrics.translate_cues', translator):
            task = asyncio.create_task(self.repository.get({**TRACK, 'lyrics': '[00:01.234]Hello'},
                                                           self.config, on_update=updated))
            await started.wait()
            self.assertFalse(task.done())
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0]['translation_status'], 'translating')
            self.assertEqual(updates[0]['lines'][0]['text'], 'Hello')
            self.assertFalse(updates[0]['translated'])
            release.set()
            completed = await task
        self.assertEqual(completed['translation_status'], 'translated')
        self.assertEqual(completed['lines'][0]['translation'], '你好')
        self.assertEqual(updates[0]['lines'][0]['translation'], '')

    async def test_observer_mutation_and_failure_do_not_corrupt_translation_or_checkpoints(self):
        def updated(result):
            result['lines'][0]['text'] = 'observer mutation'
            raise RuntimeError('observer failed')
        with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '你好')])) as translate:
            result = await self.repository.get({**TRACK, 'lyrics': 'Hello'}, self.config, on_update=updated)
        self.assertEqual(translate.await_args.args[0][0].text, 'Hello')
        self.assertEqual(result['lines'][0]['text'], 'Hello')
        self.assertEqual(result['translation_status'], 'translated')
        self.assertNotIn('on_update', Path(result['paths']['original']).parent.joinpath('lyrics.json').read_text())

    async def test_cancellation_propagates_without_poisoning_original_cache(self):
        with patch('music_lyrics.translate_cues', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.repository.get({**TRACK, 'lyrics': 'Hello'}, self.config)
        with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '你好')])):
            result = await self.repository.get(TRACK, self.config)
        self.assertTrue(result['translated'])

    async def test_local_original_and_chinese_sidecar_do_not_use_model(self):
        original = self.directory / 'my-song.lrc'
        chinese = self.directory / 'my-song.zh.lrc'
        original.write_text('[00:01.009]Hello')
        chinese.write_text('[00:01.009]你好')
        with patch('music_lyrics.translate_cues', AsyncMock()) as translate:
            result = await self.repository.get({**TRACK, 'lyrics_path': str(original)}, self.config)
        translate.assert_not_awaited()
        self.assertEqual(result['source'], 'local')
        self.assertEqual(result['lines'][0]['translation'], '你好')

    async def test_local_legacy_chinese_encoding_is_not_misread_as_utf16(self):
        path = self.directory / 'legacy.lrc'
        path.write_bytes('[00:01]你好世界'.encode('gb18030'))
        with patch('music_lyrics.translate_cues', AsyncMock()) as translate:
            result = await self.repository.get({**TRACK, 'lyrics_path': str(path)}, self.config)
        translate.assert_not_awaited()
        self.assertEqual(result['lines'][0]['text'], '你好世界')

    async def test_partial_native_translation_only_translates_missing_lines(self):
        with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '再见')])) as translate:
            result = await self.repository.get({**TRACK, 'lyrics': '[00:01]Hello\n[00:02]Goodbye',
                                                'translated_lyrics': '[00:01]你好'}, self.config)
        self.assertEqual([cue.text for cue in translate.await_args.args[0]], ['Goodbye'])
        self.assertEqual([line['translation'] for line in result['lines']], ['你好', '再见'])

    async def test_mismatched_timestamps_are_not_arbitrarily_paired(self):
        with patch('music_lyrics.translate_cues', AsyncMock()) as translate:
            result = await self.repository.get({**TRACK, 'lyrics': '[00:01]Hello',
                                                'translated_lyrics': '[00:50]错误配对'},
                                               {**self.config, 'music_translation_enabled': False})
        translate.assert_not_awaited()
        self.assertFalse(result['translated'])

    async def test_fetch_validates_exact_signature_and_duration(self):
        record = {'trackName': 'Hello', 'artistName': 'An Artist', 'albumName': 'An Album',
                  'duration': 210, 'syncedLyrics': '[00:01]你好', 'instrumental': False}
        for changes, accepted in (({}, True), ({'trackName': 'Hello (Live)'}, False),
                                   ({'artistName': 'Other Artist'}, False), ({'duration': 220}, False),
                                   ({'albumName': 'Other Album'}, False)):
            self.repository._next_request = 0
            with patch.object(self.repository, '_fetch_sync', Mock(return_value={**record, **changes})):
                result = await self.repository._fetch(TRACK)
            self.assertEqual(result is not None, accepted)
        with patch.object(self.repository, '_fetch_sync', Mock()) as fetch:
            self.assertIsNone(await self.repository._fetch({**TRACK, 'duration': 0}))
            self.assertIsNone(await self.repository._fetch({**TRACK, 'artist': ''}))
        fetch.assert_not_called()

    async def test_online_instrumental_cache_and_transient_not_found(self):
        with patch.object(self.repository, '_fetch', AsyncMock(return_value={'instrumental': True})) as fetch, \
                patch('music_lyrics.translate_cues', AsyncMock()) as translate:
            result = await self.repository.get(TRACK, {**self.config, 'music_lyrics_online': True})
            second = await self.repository.get(TRACK, {**self.config, 'music_lyrics_online': True})
        fetch.assert_awaited_once()
        translate.assert_not_awaited()
        self.assertEqual(result['translation_status'], 'instrumental')
        self.assertTrue(second['instrumental'])
        other = {**TRACK, 'title': 'Unknown'}
        with patch.object(self.repository, '_fetch', AsyncMock(return_value=None)) as fetch:
            await self.repository.get(other, {**self.config, 'music_lyrics_online': True})
            await self.repository.get(other, {**self.config, 'music_lyrics_online': True})
        self.assertEqual(fetch.await_count, 2)

    async def test_concurrent_same_track_only_translates_once(self):
        async def translate(*args, **kwargs):
            await asyncio.sleep(.01)
            return [Cue(0, 1000, '你好')]
        with patch('music_lyrics.translate_cues', AsyncMock(side_effect=translate)) as mocked:
            first, second = await asyncio.gather(*[
                self.repository.get({**TRACK, 'lyrics': 'Hello'}, self.config) for _ in range(2)])
        mocked.assert_awaited_once()
        self.assertEqual(first['lines'], second['lines'])

    async def test_corrupt_cache_rebuilds_from_player_and_wrong_schema_is_ignored(self):
        result = await self.repository.get({**TRACK, 'lyrics': '你好'}, self.config)
        record = Path(result['paths']['original']).parent / 'lyrics.json'
        for content in ('not json', '{"version":1,"lines":[{"text":"a","translation":"b","start":"bad"}]}'):
            record.write_text(content)
            retried = await self.repository.get({**TRACK, 'lyrics': '你好'}, self.config)
            self.assertEqual(retried['lines'][0]['text'], '你好')

    def test_http_request_is_lyrics_only_bounded_and_honors_rate_limit(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        response.read.return_value = b'{"instrumental":true}'
        with patch('urllib.request.urlopen', Mock(return_value=response)) as opener:
            self.repository._fetch_sync(TRACK)
        request = opener.call_args.args[0]
        self.assertTrue(request.full_url.startswith('https://lrclib.net/api/get?'))
        self.assertIn('track_name=Hello', request.full_url)
        self.assertIn('duration=210.25', request.full_url)
        self.assertIn('QwenTranslator', request.headers['User-agent'])
        self.assertEqual(opener.call_args.kwargs['timeout'], 8)
        response.read.assert_called_once_with(1024 * 1024 + 1)
        limited = urllib.error.HTTPError(request.full_url, 429, 'rate limited', {'Retry-After': '100'}, io.BytesIO())
        with patch('urllib.request.urlopen', Mock(side_effect=limited)), patch('music_lyrics.time.monotonic', return_value=1000):
            with self.assertRaises(urllib.error.HTTPError):
                self.repository._fetch_sync(TRACK)
        self.assertEqual(self.repository._next_request, 1110)


if __name__ == '__main__':
    unittest.main()
