import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from antigravity_session import AntigravitySession, list_antigravity_models
from translation_pool import TranslationPool, RateLimitedError, QuotaExceededError, gather_cancel_on_error
from text_translation import SubtitleTranslator

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
if args == ['models']:
    print('Fetching available models...')
    print('flash-low\tFlash (Low)')
    print('sonnet\tSonnet')
    sys.exit()
payload = json.loads(sys.stdin.readline())
text = payload['message']['content']
assert 'private-transcript' not in ' '.join(args)
assert '--continue' not in args and '--conversation' not in args
assert '--disable-slash-commands' in args
assert 'GEMINI_API_KEY' not in os.environ
agent = open('.agents/agents/qwen-translator/agent.md').read()
assert 'tools: [finish]' in agent
assert 'private-transcript' not in agent
if 'slow' in text:
    time.sleep(30)
if 'reverse-order' in text:
    time.sleep(.1)
error = '429 rate limit' if 'rate-limited' in text else 'quota exceeded' if 'no-quota' in text else ''
schema = json.loads(args[args.index('--json-schema') + 1])
structured = {'segments': {'a': {'0': '你好'}}} if 'segments' in schema.get('properties', {}) else {'text': '你好'}
if 'subtitles' in schema.get('properties', {}):
    structured = {'subtitles': [{'id': 0, 'text': '你好'}]}
result = {'status': 'ERROR' if error else 'SUCCESS', 'error': error,
          'response': json.dumps(structured), 'structured_output': structured,
          'usage': {'input_tokens': 20, 'output_tokens': 2, 'thinking_tokens': 1, 'cache_read_tokens': 5}}
print(json.dumps({'event': 'result', 'result': result}), flush=True)
'''


class AntigravityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.executable = Path(self.directory.name) / 'agy'
        self.executable.write_text(FAKE)
        self.executable.chmod(0o700)
        self.patch = patch('antigravity_session.antigravity_executable', return_value=str(self.executable))
        self.patch.start()
        self.config = {'text_provider': 'antigravity', 'antigravity_model': 'flash-low', 'subtitle_language': 'en-zh'}

    def tearDown(self):
        self.patch.stop()
        self.directory.cleanup()

    async def test_model_catalog_and_subtitle_usage(self):
        self.assertEqual([x['model'] for x in (await list_antigravity_models())['models']], ['flash-low', 'sonnet'])
        translator = SubtitleTranslator(self.config)
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'must-not-be-sent'}):
            first = await translator.translate('private-transcript')
            cached = await translator.translate('private-transcript')
        self.assertEqual(first['text'], '你好')
        self.assertEqual(first['usage'], {'input_tokens': 20, 'output_tokens': 2, 'reasoning_output_tokens': 1, 'cached_input_tokens': 5})
        self.assertTrue(cached['cache_hit'])
        await translator.close()

    async def test_schema_and_provider_errors(self):
        session = AntigravitySession(self.config)
        result = await session.translate('{}', instructions='Translate only', output_schema={
            'type': 'object', 'properties': {'segments': {'type': 'object'}}})
        self.assertEqual(json.loads(result['text']), {'segments': {'a': {'0': '你好'}}})
        for text, error in [('rate-limited', RateLimitedError), ('no-quota', QuotaExceededError)]:
            with self.assertRaises(error):
                await session.translate(text)
        await session.close()

    async def test_video_object_schema_unwraps_to_existing_cue_array(self):
        session = AntigravitySession({**self.config, 'video_batch': True})
        result = await session.translate('[{"id": 0, "text": "Hello"}]')
        self.assertEqual(json.loads(result['text']), [{'id': 0, 'text': '你好'}])
        await session.close()

    async def test_timeout_cancellation_and_recovery(self):
        session = AntigravitySession(self.config, timeout=.1)
        with self.assertRaisesRegex(RuntimeError, '超时'):
            await session.translate('slow')
        session.timeout = 5
        task = asyncio.create_task(session.translate('slow'))
        await asyncio.sleep(.05)
        await session.close()
        self.assertTrue(task.cancelled())
        self.assertEqual((await session.translate('next'))['text'], '你好')

    async def test_four_and_eight_independent_sessions_do_not_share_workdirs(self):
        for size in (4, 8):
            entered, peak = 0, 0
            directories = set()
            ready = asyncio.Event()
            release = asyncio.Event()
            async def process(args, *, data, cwd, **kwargs):
                nonlocal entered, peak
                directories.add(cwd)
                entered += 1
                peak = max(peak, entered)
                if entered == size:
                    ready.set()
                await release.wait()
                await asyncio.sleep(.001)
                entered -= 1
                result = {'status': 'SUCCESS', 'structured_output': {'text': json.loads(data)['message']['content']}}
                return 0, json.dumps({'event': 'result', 'result': result}).encode(), b''
            pool = TranslationPool(lambda: AntigravitySession(self.config), size)
            with patch('antigravity_session.run_process', process):
                task = asyncio.create_task(gather_cancel_on_error(pool.translate(str(i)) for i in range(size * 2)))
                await asyncio.wait_for(ready.wait(), 2)
                self.assertEqual(peak, size)
                release.set()
                results = await task
            self.assertEqual(len(directories), size * 2)
            self.assertTrue(all(not Path(path).exists() for path in directories))
            self.assertEqual(len(results), size * 2)
            for i, result in enumerate(results):
                self.assertIn(json.dumps(str(i)), result['text'])
            await pool.close()

    async def test_malformed_output_and_missing_model_are_actionable(self):
        with patch('antigravity_session.run_process', AsyncMock(return_value=(0, b'not-json', b'secret'))):
            with self.assertRaisesRegex(RuntimeError, 'Antigravity 翻译失败'):
                await AntigravitySession(self.config).translate('text')
        with self.assertRaisesRegex(ValueError, '选择 Antigravity 模型'):
            await AntigravitySession({}).translate('text')
