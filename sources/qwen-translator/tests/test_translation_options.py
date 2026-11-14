import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from text_translation import SubtitleTranslator, TRANSLATION_INSTRUCTIONS, translate_codex


class TranslationOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_effort_and_short_instructions_reach_cli_without_transcript_on_disk(self):
        paths = []
        async def process(args, *, data, cwd, **kwargs):
            self.assertIn('model_reasoning_effort="medium"', args)
            override = next(arg for arg in args if arg.startswith('model_instructions_file='))
            path = Path(json.loads(override.split('=', 1)[1]))
            paths.append(path)
            self.assertEqual(path.read_text(), TRANSLATION_INSTRUCTIONS)
            self.assertEqual(list(Path(cwd).iterdir()), [path])
            self.assertNotIn('private-transcript', ' '.join(args))
            self.assertIn('private-transcript', data.decode())
            self.assertNotIn(TRANSLATION_INSTRUCTIONS, data.decode())
            events = [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '译文'}},
                      {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'output_tokens': 2}}]
            return 0, '\n'.join(json.dumps(event) for event in events).encode(), b''
        with patch('text_translation.codex_executable', return_value='codex'), patch('text_translation.run_process', process):
            result = await translate_codex('private-transcript', {'subtitle_language': 'en-zh', 'codex_reasoning_effort': 'medium'})
        self.assertEqual(result['usage']['input_tokens'], 100)
        self.assertFalse(paths[0].exists())

    async def test_default_effort_omits_override(self):
        from text_translation import codex_command
        with patch('text_translation.codex_executable', return_value='codex'):
            self.assertFalse(any('model_reasoning_effort=' in arg for arg in codex_command('test', 'default')))
            with self.assertRaises(ValueError):
                codex_command('test', 'invalid')

    async def test_cache_is_exact_bounded_and_local_to_session(self):
        adapter = AsyncMock(return_value={'text': '你好', 'usage': {'input_tokens': 30}})
        config = {'subtitle_language': 'en-zh', 'text_provider': 'api'}
        translator = SubtitleTranslator(config, cache_size=2)
        config['subtitle_language'] = 'zh-en'
        with patch('text_translation.translate_text', adapter):
            first = await translator.translate('Hello')
            second = await translator.translate('Hello')
            self.assertEqual(adapter.await_count, 1)
            self.assertEqual(first['usage']['input_tokens'], 30)
            self.assertEqual(second, {'text': '你好', 'usage': {}, 'cache_hit': True})
            self.assertEqual(adapter.call_args.args[1]['subtitle_language'], 'en-zh')
            await translator.translate('hello')
            await translator.translate('Hello!')
            await translator.translate('Hello')  # Evicted, not a fuzzy match.
            self.assertEqual(adapter.await_count, 4)
            await SubtitleTranslator(config).translate('Hello')
            self.assertEqual(adapter.await_count, 5)

    async def test_failed_translation_is_never_cached(self):
        adapter = AsyncMock(side_effect=[RuntimeError('offline'), {'text': '你好', 'usage': {}}])
        translator = SubtitleTranslator({'text_provider': 'api'})
        with patch('text_translation.translate_text', adapter):
            with self.assertRaises(RuntimeError):
                await translator.translate('Hello')
            self.assertEqual((await translator.translate('Hello'))['text'], '你好')
            self.assertEqual(adapter.await_count, 2)


if __name__ == '__main__':
    unittest.main()
