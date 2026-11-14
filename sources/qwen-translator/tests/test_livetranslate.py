"""Qwen 3.8 protocol regressions; no credentials or real audio are used."""
import base64
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

import check_access
from duplex import translation_session
from server import MODEL, WS_URLS, build_session, normalize_event


class LiveTranslateTests(unittest.TestCase):
    def test_all_regions_and_access_check_use_new_model(self):
        self.assertEqual(MODEL, 'qwen3.8-livetranslate-flash-realtime')
        for url in WS_URLS.values():
            self.assertTrue(url.endswith('?model=' + MODEL))
        self.assertEqual(check_access.URLS, WS_URLS)

    def test_audio_session_uses_nested_formats_and_voice(self):
        session, details = build_session({'source_language': 'zh', 'voice': 'Ethan'})
        self.assertEqual(session['output_modalities'], ['text', 'audio'])
        self.assertEqual(session['translation'], {'language': 'en'})
        self.assertEqual(session['audio']['input']['format'], {'type': 'pcm', 'sample_rate': 16000})
        self.assertEqual(session['audio']['output'],
                         {'format': {'type': 'pcm', 'sample_rate': 24000}, 'voice': 'Ethan'})
        self.assertEqual(details['voice'], 'Ethan')
        self.assertEqual(details['source_language'], 'auto')
        for legacy in ('modalities', 'input_audio_format', 'output_audio_format',
                       'input_audio_transcription', 'voice', 'sample_rate'):
            self.assertNotIn(legacy, session)

    def test_text_only_languages_and_disabled_audio_do_not_enable_clone(self):
        for config in ({'audio_enabled': False}, {'target_language': 'yue'}):
            with self.subTest(config=config):
                session, details = build_session({**config, 'voice_clone_mode': 'always'})
                self.assertEqual(session['output_modalities'], ['text'])
                self.assertNotIn('voice', session['audio']['output'])
                self.assertNotIn('enable_voice_clone', session)
                self.assertFalse(details['audio_enabled'])
                self.assertEqual(details['voice_clone_mode'], 'off')

    def test_duplex_directions_preserve_target_voice_and_clone_choice(self):
        for direction, source, target in [('inbound', 'en', 'zh'), ('outbound', 'zh', 'en')]:
            for voice in ('Ethan', 'clone_once', 'clone_always'):
                with self.subTest(direction=direction, voice=voice):
                    session = translation_session((direction, 'input', 'output', source, target),
                                                  {f'{direction}_voice': voice})
                    self.assertEqual(session['translation']['language'], target)
                    clone = voice.removeprefix('clone_')
                    self.assertEqual(session['audio']['output']['voice'],
                                     'default' if voice.startswith('clone_') else voice)
                    if voice.startswith('clone_'):
                        self.assertTrue(session['enable_voice_clone'])
                        self.assertEqual(session['voice_clone_options'], {'frequency': clone})

    def test_disabled_voice_options_report_effective_configuration(self):
        for config in ({'minimal_session': True}, {'voice_config_enabled': False},
                       {'voice_clone_config_enabled': False}):
            with self.subTest(config=config):
                session, details = build_session({**config, 'voice_clone_mode': 'always'})
                self.assertNotIn('enable_voice_clone', session)
                self.assertEqual(details['voice_clone_mode'], 'off')
                self.assertEqual(details['voice'], 'Tina')
        minimal, _ = build_session({'minimal_session': True})
        self.assertEqual(minimal, {'output_modalities': ['text', 'audio'], 'translation': {'language': 'en'}})

    def test_new_delta_events_preserve_whitespace_and_repeated_words(self):
        for event_type, key in [
            ('conversation.item.input_audio_transcription.delta', 'source_delta'),
            ('response.text.delta', 'translation_delta'),
            ('response.audio_transcript.delta', 'translation_delta'),
        ]:
            with self.subTest(event_type=event_type):
                chunks = [normalize_event({'type': event_type, 'delta': text,
                                          'item_id': 'item1', 'speaker_id': 0})
                          for text in ['Very', ' ', 'very', ' good.']]
                self.assertEqual(''.join(chunk[key] for chunk in chunks), 'Very very good.')
                self.assertEqual(chunks[0]['item_id'], 'item1')
                self.assertEqual(chunks[0]['speaker_id'], 0)

    def test_final_captions_audio_and_usage_still_reach_clients(self):
        for event_type, field, key in [
            ('conversation.item.input_audio_transcription.completed', 'transcript', 'source_final'),
            ('response.text.done', 'text', 'translation_final'),
            ('response.audio_transcript.done', 'transcript', 'translation_final'),
        ]:
            self.assertEqual(normalize_event({'type': event_type, field: 'Hello.'})[key], 'Hello.')
        pcm = base64.b64encode(b'\x00\x00\x01\x00').decode()
        audio = normalize_event({'type': 'response.audio.delta', 'delta': pcm})
        self.assertEqual(audio, {'type': 'model_audio', 'audio': pcm, 'sample_rate': 24000, 'format': 'pcm'})
        done = normalize_event({'type': 'response.done', 'response': {'usage': {'total_tokens': 4}}})
        self.assertTrue(done['response_done'])
        self.assertEqual(done['usage']['total_tokens'], 4)


class AccessCheckTests(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, events):
        ws = AsyncMock()
        ws.recv.side_effect = [json.dumps(event) for event in events]
        connection = AsyncMock()
        connection.__aenter__.return_value = ws
        with patch('check_access.websockets.connect', return_value=connection), \
             patch.dict('os.environ', {'DASHSCOPE_API_KEY': 'test-key'}), \
             patch('sys.argv', ['check_access.py']), redirect_stdout(io.StringIO()):
            result = await check_access.main()
        return result, ws

    async def test_connection_created_is_not_configuration_success(self):
        result, ws = await self.run_check([
            {'type': 'session.created'},
            {'type': 'error', 'error': {'message': 'invalid configuration'}},
        ])
        self.assertEqual(result, 1)
        self.assertEqual(ws.recv.await_count, 2)

    async def test_waits_for_updated_and_uses_production_text_session(self):
        result, ws = await self.run_check([{'type': 'session.created'}, {'type': 'session.updated'}])
        self.assertEqual(result, 0)
        self.assertEqual(ws.recv.await_count, 2)
        session = json.loads(ws.send.call_args.args[0])['session']
        expected, _ = build_session({'region': 'mainland', 'audio_enabled': False})
        self.assertEqual(session, expected)


if __name__ == '__main__':
    unittest.main()
