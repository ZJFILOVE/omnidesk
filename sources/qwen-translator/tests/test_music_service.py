import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from music_lyrics import LyricsRepository
from music_service import HOST, MAX_BODY, MusicService, current_line, music_config
from video_subtitles import Cue


def state(identifier='a', position=0, available=True):
    return {'available': available, 'track': {'id': identifier, 'title': 'Song ' + identifier,
            'artist': 'Artist', 'album': 'Album', 'duration': 180}, 'position': position,
            'playing': True, 'capabilities': {'play_pause': True, 'seek': True}}


def lyrics(text='Hello'):
    return {'lines': [{'id': 'first', 'start': 10, 'text': text, 'translation': '你好'},
                      {'id': 'gap', 'start': 20, 'text': '', 'translation': ''},
                      {'id': 'last', 'start': 30, 'text': 'Goodbye', 'translation': '再见'}],
            'translation_status': 'existing', 'synced': True}


class FakePlayback:
    def __init__(self):
        self.state = state()
        self.snapshots = 0
        self.commands = []

    def snapshot(self):
        self.snapshots += 1
        return copy.deepcopy(self.state)

    def command(self, action, value):
        self.commands.append((action, value))
        if action == 'seek':
            self.state['position'] = value
        elif action == 'next':
            self.state = state('b')
        elif action == 'play_pause':
            self.state['playing'] = not self.state['playing']
        elif action not in {'volume', 'device', 'previous'}:
            return {'ok': False, 'error': 'Unsupported'}
        return {'ok': True, 'state': copy.deepcopy(self.state)}


class CurrentLineTests(unittest.TestCase):
    def test_current_line_before_first_during_gap_and_after_backward_seek(self):
        content = lyrics()
        self.assertEqual(current_line(content, 9.999), (-1, None))
        self.assertEqual(current_line(content, 10)[0], 0)
        self.assertEqual(current_line(content, 19.999)[1]['text'], 'Hello')
        self.assertEqual(current_line(content, 20)[1]['text'], '')
        self.assertEqual(current_line(content, 29.99)[0], 1)
        self.assertEqual(current_line(content, 100)[0], 2)
        self.assertEqual(current_line(content, 12)[0], 0)
        self.assertEqual(current_line({'lines': [{'start': None, 'text': 'Untimed'}]}, 99), (-1, None))

    def test_config_forces_local_codex_without_mutating_other_translation_settings(self):
        config = {'text_provider': 'api', 'codex_model': 'wrong', 'codex_reasoning_effort': 'high',
                  'music_translation_enabled': False, 'other': 'preserved'}
        result = music_config(config)
        self.assertEqual((result['text_provider'], result['codex_model'], result['codex_reasoning_effort']),
                         ('codex', 'gpt-6-luna', 'low'))
        self.assertFalse(result['music_translation_enabled'])
        self.assertEqual(config['text_provider'], 'api')


class MusicServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playback = FakePlayback()
        self.repository = Mock()
        self.repository.get = AsyncMock(return_value=lyrics())
        self.emitted = []
        self.service = MusicService({'text_provider': 'api', 'music_translation_enabled': False},
                                    playback=self.playback, repository=self.repository, emit=self.emitted.append)
        self.addAsyncCleanup(self.service.close)

    async def test_missing_lyrics_retry_with_backoff_and_recover_without_track_change(self):
        self.repository.get.return_value = {'lines': [], 'translation_status': 'unavailable'}
        await self.load()
        self.assertEqual(self.service.lyrics['retry_after'], 5)
        for delay in (15, 30, 60, 60):
            calls = self.repository.get.await_count
            await self.service.poll()
            self.assertEqual(self.repository.get.await_count, calls)
            self.service.retry_at = 0
            await self.service.poll()
            await self.service.lyric_task
            self.assertEqual(self.repository.get.await_count, calls + 1)
            self.assertEqual(self.service.lyrics['retry_after'], delay)
        self.repository.get.return_value = lyrics('Recovered')
        self.service.retry_at = 0
        self.playback.state['position'] = 12
        await self.service.poll()
        await self.service.lyric_task
        self.assertEqual(self.service.state['lyrics']['current']['text'], 'Recovered')
        self.assertIsNone(self.service.retry_at)
        calls = self.repository.get.await_count
        await self.service.poll()
        self.assertEqual(self.repository.get.await_count, calls)

    async def test_retry_does_not_duplicate_pending_load_or_block_playback(self):
        self.repository.get.return_value = {'lines': [], 'translation_status': 'error'}
        await self.load()
        started, release = asyncio.Event(), asyncio.Event()
        async def get(*args, **kwargs):
            started.set()
            await release.wait()
            return lyrics()
        self.repository.get.side_effect = get
        self.service.retry_at = 0
        await self.service.poll()
        await started.wait()
        self.playback.state['position'] = 12
        await self.service.poll()
        self.assertEqual(self.repository.get.await_count, 2)
        self.assertEqual(self.service.state['position'], 12)
        release.set()
        await self.service.lyric_task

    async def test_retry_resets_on_new_song_and_stops_when_player_disappears(self):
        self.repository.get.return_value = {'lines': [], 'translation_status': 'unavailable'}
        await self.load()
        self.service.retry_attempt = 4
        await self.load(state('b'))
        self.assertEqual(self.service.lyrics['retry_after'], 5)
        await self.service._accept({'available': False, 'track': {}, 'position': 0})
        self.assertIsNone(self.service.retry_at)

    async def test_available_original_instrumental_and_manual_failures_are_not_retried(self):
        for result in (lyrics(), {**lyrics(), 'translation_status': 'error'},
                       {'lines': [], 'instrumental': True, 'translation_status': 'instrumental'}):
            self.repository.get.return_value = result
            await self.load(state(str(self.repository.get.await_count)))
            self.assertIsNone(self.service.retry_at)
        self.repository.get.side_effect = RuntimeError('offline')
        await self.service.command({'action': 'import_lyrics', 'lyrics': '[00:10]Imported'})
        await self.service.lyric_task
        self.assertIsNone(self.service.retry_at)

    async def test_catalog_url_arriving_later_reloads_same_library_song_immediately(self):
        snapshot = state()
        await self.load(snapshot)
        self.repository.get.reset_mock()
        snapshot['track']['url'] = 'https://music.apple.com/cn/song/123456'
        await self.load(snapshot)
        self.repository.get.assert_awaited_once()
        self.assertEqual(self.repository.get.await_args.args[0]['url'], snapshot['track']['url'])

    async def test_unexpected_observation_failure_clears_waybar_and_recovers(self):
        await self.service.poll()
        await self.service.lyric_task
        snapshot = self.playback.snapshot
        self.playback.snapshot = Mock(side_effect=RuntimeError('private backend detail'))
        await self.service.poll()
        self.assertFalse(self.service.state['available'])
        self.assertEqual(self.service.state['track'], {})
        self.assertIsNone(self.service.state['lyrics']['current'])
        self.assertNotIn('private backend detail', json.dumps(self.service.state))
        self.playback.snapshot = snapshot
        await self.service.poll()
        await self.service.lyric_task
        self.assertTrue(self.service.state['available'])

    async def test_waybar_display_preferences_are_global_and_do_not_request_translation(self):
        result = await self.service.command({'action': 'update_waybar', 'track_id': 'old-track',
            'value': {'display': 'bilingual', 'show_title': False, 'show_progress': True}})
        self.assertTrue(result['ok'])
        self.assertEqual(self.service.state['waybar'], {'display': 'bilingual', 'show_title': False, 'show_progress': True})
        self.repository.get.assert_not_awaited()
        for value in ({'display': 'bad'}, {'show_title': 'false'}, {'unknown': True}, []):
            self.assertFalse((await self.service.command({'action': 'update_waybar', 'value': value}))['ok'])
        self.assertEqual(self.service.state['waybar']['display'], 'bilingual')

    async def load(self, value=None):
        await self.service._accept(value or state())
        if self.service.lyric_task:
            await self.service.lyric_task

    async def request(self, path='/music/state', *, method='GET', data=None, extra_headers=(), raw=None):
        if not self.service.server:
            self.port = await self.service.serve(0)
        reader, writer = await asyncio.open_connection(HOST, self.port)
        if raw is None:
            body = json.dumps(data).encode() if data is not None else b''
            headers = [f'Host: {HOST}:{self.port}', *extra_headers]
            if body:
                headers += ['Content-Type: application/json', f'Content-Length: {len(body)}']
            raw = (f'{method} {path} HTTP/1.1\r\n' + '\r\n'.join(headers) + '\r\n\r\n').encode() + body
        writer.write(raw)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        await writer.wait_closed()
        header, body = response.split(b'\r\n\r\n', 1)
        return int(header.split(b' ')[1]), json.loads(body), header

    async def test_poll_fetches_once_per_track_and_tracks_progress_without_translation(self):
        await self.service.poll()
        await self.service.lyric_task
        self.playback.state['position'] = 12
        await self.service.poll()
        self.playback.state['position'] = 24
        await self.service.poll()
        self.repository.get.assert_awaited_once()
        self.assertEqual(self.service.state['lyrics']['current']['text'], '')
        self.playback.state['position'] = 11
        await self.service.poll()
        self.assertEqual(self.service.state['lyrics']['current_index'], 0)
        self.assertEqual(self.playback.snapshots, 4)
        passed = self.repository.get.await_args.args[1]
        self.assertEqual(passed['text_provider'], 'codex')
        self.assertEqual(passed['codex_model'], 'gpt-6-luna')
        self.assertFalse(passed['music_translation_enabled'])

    async def test_published_state_is_a_copy_and_model_completion_recomputes_current_line(self):
        await self.load(state(position=35))
        self.assertEqual(self.emitted[-1]['lyrics']['current_index'], 2)
        self.emitted[-1]['lyrics']['lines'][0]['text'] = 'mutated'
        self.assertEqual(self.service.lyrics['lines'][0]['text'], 'Hello')

    async def test_track_change_cancels_stale_lyrics_even_when_provider_ignores_cancellation(self):
        started = asyncio.Event()
        release = asyncio.Event()
        cancelled = asyncio.Event()
        async def get(track, config, **kwargs):
            if track['id'] == 'a':
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return lyrics('STALE')
            return lyrics('Current')
        self.repository.get.side_effect = get
        await self.service._accept(state('a'))
        old = self.service.lyric_task
        await started.wait()
        await self.service._accept(state('b', position=12))
        await self.service.lyric_task
        await cancelled.wait()
        release.set()
        await old
        self.assertEqual(self.service.state['track']['id'], 'b')
        self.assertEqual(self.service.state['lyrics']['current']['text'], 'Current')

    async def test_original_is_published_and_served_while_actual_repository_waits_for_model(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service.repository = LyricsRepository(Path(temporary.name))
        self.service.config.update(music_translation_enabled=True, music_lyrics_online=False)
        started, release = asyncio.Event(), asyncio.Event()
        async def translator(cues, config, directory, emit, **kwargs):
            started.set()
            await release.wait()
            return [Cue(0, 1000, '你好')]
        track_state = state(position=12)
        track_state['track']['lyrics'] = '[00:10.000]Original lyric'
        with patch('music_lyrics.translate_cues', translator):
            await self.service._accept(track_state)
            await started.wait()
            self.assertFalse(self.service.lyric_task.done())
            self.assertEqual(self.service.state['lyrics']['current']['text'], 'Original lyric')
            self.assertEqual(self.service.state['lyrics']['translation_status'], 'translating')
            status, response, _ = await self.request()
            self.assertEqual(status, 200)
            self.assertEqual(response['lyrics']['current']['text'], 'Original lyric')
            release.set()
            await self.service.lyric_task
        self.assertEqual(self.service.state['lyrics']['current']['translation'], '你好')

    async def test_stale_progress_is_ignored_after_track_changes(self):
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def get(track, config, *, on_update, **kwargs):
            if track['id'] == 'a':
                await on_update(lyrics('Original A'))
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                await on_update(lyrics('STALE A PROGRESS'))
                return lyrics('STALE A FINAL')
            await on_update(lyrics('Original B'))
            return lyrics('Current B')
        self.repository.get.side_effect = get
        await self.service._accept(state('a', position=12))
        old = self.service.lyric_task
        await started.wait()
        self.assertEqual(self.service.state['lyrics']['current']['text'], 'Original A')
        await self.service._accept(state('b', position=12))
        await self.service.lyric_task
        await cancelled.wait()
        published = len(self.emitted)
        release.set()
        await old
        self.assertEqual(len(self.emitted), published)
        self.assertEqual(self.service.state['lyrics']['current']['text'], 'Current B')

    async def test_switching_player_reloads_native_lyrics_even_with_identical_track_id(self):
        await self.load({**state(), 'backend': 'mpris', 'player': 'adam', 'identity': 'Adam'})
        self.repository.get.reset_mock()
        await self.load({**state(), 'backend': 'mpris', 'player': 'cider', 'identity': 'Cider'})
        self.repository.get.assert_awaited_once()
        self.assertEqual(self.service.state['player'], 'cider')

    async def test_player_disappearing_cancels_lyrics_and_clears_current_track(self):
        started = asyncio.Event()
        async def get(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.repository.get.side_effect = get
        await self.service._accept(state())
        old = self.service.lyric_task
        await started.wait()
        await self.service._accept({'available': False, 'track': {}, 'position': 0})
        await asyncio.gather(old, return_exceptions=True)
        self.assertTrue(old.cancelled())
        self.assertIsNone(self.service.key)
        self.assertIsNone(self.service.state['lyrics']['current'])

    async def test_enrichment_receives_track_and_snapshot_then_repository_receives_native_lyrics(self):
        def enrich(track, snapshot):
            self.assertEqual(track['id'], snapshot['track']['id'])
            return {**track, 'lyrics': '[00:10]原生歌词', 'translated_lyrics': '[00:10]已有中文'}
        self.service.enrich = enrich
        await self.load()
        self.assertEqual(self.repository.get.await_args.args[0]['lyrics'], '[00:10]原生歌词')

    async def test_retry_reloads_current_track_with_force_and_checks_stale_track_id(self):
        await self.load()
        self.repository.get.reset_mock()
        stale = await self.service.command({'action': 'retry_lyrics', 'track_id': 'stale'})
        self.assertFalse(stale['ok'])
        self.repository.get.assert_not_awaited()
        self.assertTrue((await self.service.command({'action': 'retry_lyrics', 'track_id': 'a'}))['ok'])
        await self.service.lyric_task
        self.assertTrue(self.repository.get.await_args.kwargs['force'])
        self.assertEqual(self.repository.get.await_args.args[0]['id'], 'a')

    async def test_manual_mode_only_translates_on_request_and_preserves_next_song_preference(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service.repository = LyricsRepository(Path(temporary.name))
        self.service.config.update(music_translation_enabled=False, music_lyrics_online=False)
        first = state('a', position=12)
        first['track']['lyrics'] = '[00:10.000]First original'
        started, release = asyncio.Event(), asyncio.Event()
        async def translator(cues, config, directory, emit, **kwargs):
            self.assertEqual(config['codex_model'], 'gpt-6-luna')
            self.assertEqual(config['text_provider'], 'codex')
            self.assertTrue(config['music_translation_enabled'])
            started.set()
            await release.wait()
            return [Cue(0, 1000, '第一首歌词')]
        with patch('music_lyrics.translate_cues', AsyncMock(side_effect=translator)) as translate:
            await self.load(first)
            translate.assert_not_awaited()
            self.assertEqual(self.service.state['lyrics']['current']['text'], 'First original')
            self.assertEqual(self.service.state['lyrics']['status_text'], '手动模式 · 点击翻译中文')
            self.service.enrich = Mock(side_effect=AssertionError('Saved originals must not be fetched again'))
            accepted = await self.service.command({'action': 'translate_lyrics', 'track_id': 'a'})
            self.assertTrue(accepted['ok'])
            self.assertEqual(self.service.state['lyrics']['current']['text'], 'First original')
            await started.wait()
            self.assertFalse(self.service.lyric_task.done())
            release.set()
            await self.service.lyric_task
            self.service.enrich.assert_not_called()
            self.assertEqual(self.service.state['lyrics']['current']['translation'], '第一首歌词')
            self.assertFalse(self.service.config['music_translation_enabled'])
            self.service.enrich = None
            second = state('b', position=12)
            second['track']['lyrics'] = '[00:10.000]Second original'
            await self.load(second)
            translate.assert_awaited_once()
            self.assertEqual(self.service.state['lyrics']['translation_status'], 'disabled')
            self.assertEqual(self.service.state['lyrics']['current']['text'], 'Second original')

    async def test_manual_translation_reuses_native_chinese_and_previously_translated_cache(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service.repository = LyricsRepository(Path(temporary.name))
        self.service.config.update(music_translation_enabled=False, music_lyrics_online=False)
        with patch('music_lyrics.translate_cues', AsyncMock()) as translate:
            for identifier, text in (('a', '[00:10]原文已经是中文'),
                                     ('b', '[00:10]Hello\n[00:10]你好')):
                snapshot = state(identifier, position=12)
                snapshot['track']['lyrics'] = text
                await self.load(snapshot)
                self.assertTrue((await self.service.command({'action': 'translate_lyrics'}))['ok'])
                await self.service.lyric_task
                self.assertFalse(self.service.config['music_translation_enabled'])
            translate.assert_not_awaited()

    async def test_manual_translate_uses_imported_original_instead_of_earlier_player_text(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service.repository = LyricsRepository(Path(temporary.name))
        self.service.config.update(music_translation_enabled=False, music_lyrics_online=False)
        snapshot = state(position=12)
        snapshot['track']['lyrics'] = '[00:10]Earlier player text'
        await self.load(snapshot)
        await self.service.command({'action': 'import_lyrics', 'lyrics': '[00:10]Imported original'})
        await self.service.lyric_task
        with patch('music_lyrics.translate_cues', AsyncMock(return_value=[Cue(0, 1000, '导入歌词译文')])) as translate:
            await self.service.command({'action': 'translate_lyrics'})
            await self.service.lyric_task
        self.assertEqual(translate.await_args.args[0][0].text, 'Imported original')
        self.assertEqual(self.service.state['lyrics']['current']['text'], 'Imported original')

    async def test_import_bypasses_native_enrichment_and_handles_repository_error(self):
        await self.load()
        self.service.enrich = Mock(side_effect=AssertionError('Imported lyrics must stay authoritative'))
        self.repository.get.side_effect = RuntimeError('private provider details')
        accepted = await self.service.command({'action': 'import_lyrics', 'lyrics': '[00:10]Hello'})
        self.assertTrue(accepted['ok'])
        await self.service.lyric_task
        self.service.enrich.assert_not_called()
        self.assertEqual(self.repository.get.await_args.args[0]['lyrics'], '[00:10]Hello')
        self.assertEqual(self.service.state['lyrics']['translation_status'], 'error')
        self.assertNotIn('private', json.dumps(self.service.state))

    async def test_new_import_same_track_fences_old_completion_and_next_track(self):
        await self.load()
        started, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def get(track, config, **kwargs):
            if track.get('lyrics') == 'old import':
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return lyrics('OLD IMPORT')
            return lyrics(track.get('lyrics') or 'NEW TRACK')
        self.repository.get.side_effect = get
        await self.service.command({'action': 'import_lyrics', 'lyrics': 'old import'})
        old = self.service.lyric_task
        await started.wait()
        await self.service.command({'action': 'import_lyrics', 'lyrics': 'new import'})
        await self.service.lyric_task
        await cancelled.wait()
        release.set()
        await old
        self.assertEqual(self.service.state['lyrics']['lines'][0]['text'], 'new import')
        await self.load(state('b'))
        self.assertEqual(self.service.state['lyrics']['lines'][0]['text'], 'NEW TRACK')

    async def test_no_track_empty_oversize_import_and_bad_command_are_rejected(self):
        for data in (None, [], {'action': 9}, {'action': 'retry_lyrics'}, {'action': 'translate_lyrics'},
                     {'action': 'import_lyrics', 'lyrics': 'Text'}):
            self.assertFalse((await self.service.command(data))['ok'])
        await self.load()
        for text in ('', ' ', 'x' * (MAX_BODY + 1)):
            self.assertFalse((await self.service.command({'action': 'import_lyrics', 'lyrics': text}))['ok'])
        self.assertEqual(self.playback.commands, [])

    async def test_state_http_get_is_read_only_and_control_requires_post(self):
        await self.load(state(position=12))
        self.repository.get.reset_mock()
        for _ in range(2):
            status, response, headers = await self.request()
            self.assertEqual(status, 200)
            self.assertEqual(response['lyrics']['current']['text'], 'Hello')
            self.assertIn(b'Cache-Control: no-store', headers)
        status, _, _ = await self.request('/music/control?action=next')
        self.assertEqual(status, 404)
        self.assertEqual(self.playback.commands, [])
        self.repository.get.assert_not_awaited()

    async def test_http_controls_dispatch_and_update_state_seek_volume_device_next(self):
        await self.load()
        for action, value in (('seek', 12), ('volume', .4), ('device', 'sink-name'), ('play_pause', None)):
            status, response, _ = await self.request('/music/control', method='POST',
                                                     data={'action': action, 'value': value, 'track_id': 'a'})
            self.assertEqual(status, 200)
            self.assertTrue(response['ok'])
        self.assertEqual(self.playback.commands[:3], [('seek', 12), ('volume', .4), ('device', 'sink-name')])
        self.assertEqual(self.service.state['lyrics']['current_index'], 0)
        status, _, _ = await self.request('/music/control', method='POST', data={'action': 'next', 'track_id': 'a'})
        self.assertEqual(status, 200)
        await self.service.lyric_task
        self.assertEqual(self.service.state['track']['id'], 'b')
        status, response, _ = await self.request('/music/control', method='POST',
                                                  data={'action': 'seek', 'value': 20, 'track_id': 'a'})
        self.assertEqual(status, 400)
        self.assertFalse(response['ok'])

    async def test_http_rejects_foreign_origin_host_fetch_site_and_transfer_encoding(self):
        await self.load()
        self.port = await self.service.serve(0)
        for headers in ([f'Host: {HOST}:{self.port}', 'Origin: https://evil.example'],
                        ['Host: evil.example'], [f'Host: {HOST}:{self.port}', 'Sec-Fetch-Site: cross-site'],
                        [f'Host: {HOST}:{self.port}', 'Transfer-Encoding: chunked']):
            raw = ('POST /music/control HTTP/1.1\r\n' + '\r\n'.join(headers) + '\r\n\r\n').encode()
            status, _, _ = await self.request(raw=raw)
            self.assertEqual(status, 403)
        self.assertEqual(self.playback.commands, [])

    async def test_http_rejects_duplicate_headers_oversize_body_headers_and_malformed_json(self):
        await self.load()
        self.port = await self.service.serve(0)
        base = f'POST /music/control HTTP/1.1\r\nHost: {HOST}:{self.port}\r\n'
        cases = [(base + f'Host: {HOST}:{self.port}\r\n\r\n', 400),
                 (base + 'Content-Length: 1\r\nContent-Length: 1\r\n\r\nx', 400),
                 (base + f'Content-Length: {MAX_BODY + 1}\r\nContent-Type: application/json\r\n\r\n', 413),
                 (base + 'Content-Length: 1\r\nContent-Type: application/json\r\n\r\nx', 400),
                 (base + 'Content-Length: -1\r\nContent-Type: application/json\r\n\r\n', 413),
                 (base + 'Content-Length: 1\r\nContent-Type: text/plain\r\n\r\nx', 413),
                 (base + 'X-Big: ' + 'x' * 17000 + '\r\n\r\n', 400)]
        for raw, expected in cases:
            status, _, _ = await self.request(raw=raw.encode())
            self.assertEqual(status, expected)
        self.assertEqual(self.playback.commands, [])

    async def test_close_stops_pending_lyrics_server_and_future_commands(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def pending(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.repository.get.side_effect = pending
        await self.service._accept(state())
        await started.wait()
        port = await self.service.serve(0)
        await self.service.close()
        self.assertTrue(cancelled.is_set())
        self.assertFalse((await self.service.command({'action': 'next'}))['ok'])
        self.assertEqual(self.playback.commands, [])
        with self.assertRaises(OSError):
            await asyncio.open_connection(HOST, port)


if __name__ == '__main__':
    unittest.main()
