"""PDF input guards, model routing and cancellation, without a real LLM."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pdf_translation import INSTRUCTIONS, SCHEMA, TextBridge, validate_config
from text_translation import translate_api_sync


class PdfConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'paper.pdf'
        self.path.write_bytes(b'%PDF-test')
        self.config = {'source': str(self.path), 'text_provider': 'codex'}

    def test_ranges_and_default_model(self):
        config = validate_config({**self.config, 'pages': '1, 3-5'})
        self.assertEqual(config['pages'], '1,3-5')
        self.assertEqual(config['codex_model'], 'gpt-6-luna')
        self.assertEqual(validate_config({**self.config, 'codex_model': 'selected-model'})['codex_model'], 'selected-model')

    def test_reject_bad_source_languages_ranges_and_provider(self):
        for fields in ({'source': '/missing.pdf'}, {'lang_out': 'en'}, {'lang_in': 'xx'},
                       {'pages': '0'}, {'pages': '5-2'}, {'pages': '1,,2'}, {'pages': '-3'},
                       {'text_provider': 'none'}, {'text_provider': 'antigravity'},
                       {'text_provider': 'api', 'text_endpoint': 'file:///secret'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                validate_config({**self.config, **fields})

    def test_api_receives_document_prompt_not_subtitle_prompt(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            'choices': [{'message': {'content': '{"text":"你好 {v1}"}'}}]}).encode()
        opener = unittest.mock.MagicMock()
        opener.open.return_value = response
        with patch('urllib.request.build_opener', return_value=opener):
            result = translate_api_sync('document request', {'text_model': 'model',
                'text_endpoint': 'http://localhost:1234/v1', 'document_instructions': INSTRUCTIONS})
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertIn('document request', body['messages'][0]['content'])
        self.assertNotIn('Translate speech', body['messages'][0]['content'])
        self.assertEqual(body['max_tokens'], 16384)
        self.assertEqual(json.loads(result['text'])['text'], '你好 {v1}')


class PdfBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_engine_json_and_placeholders(self):
        expected = '[{"id": 0, "output": "能量 {v1}<style id=\'2\'>铜</style>"}]'
        pool = AsyncMock()
        pool.translate.return_value = {'text': json.dumps({'text': expected})}
        bridge = TextBridge({'text_provider': 'codex', 'codex_model': 'gpt-6-luna'}, pool)
        try:
            actual = await asyncio.to_thread(bridge.request, 'BabelDOC prompt')
            self.assertEqual(actual, expected)
            pool.translate.assert_awaited_once_with('BabelDOC prompt', instructions=INSTRUCTIONS, output_schema=SCHEMA)
            self.assertEqual(bridge.calls, 1)
        finally:
            await bridge.close()
        pool.close.assert_awaited_once()

    async def test_failed_provider_cannot_be_published_as_success(self):
        pool = AsyncMock()
        pool.translate.side_effect = RuntimeError('quota exhausted')
        bridge = TextBridge({}, pool)
        with self.assertRaisesRegex(RuntimeError, 'quota'):
            await asyncio.to_thread(bridge.request, 'text')
        self.assertIsNotNone(bridge.error)
        with self.assertRaises(RuntimeError):
            await asyncio.to_thread(bridge.request, 'next')
        self.assertEqual(pool.translate.await_count, 1)
        await bridge.close()

    async def test_malformed_and_empty_responses_fail(self):
        for text in ('not json', '{}', '{"text":""}', '{"text":3}', '{"text":"ok","extra":1}'):
            pool = AsyncMock()
            pool.translate.return_value = {'text': text}
            bridge = TextBridge({}, pool)
            with self.subTest(text=text), self.assertRaises(ValueError):
                await asyncio.to_thread(bridge.request, 'text')
            self.assertIsNotNone(bridge.error)
            await bridge.close()

    async def test_close_cancels_active_model_request(self):
        entered = asyncio.Event()
        async def block(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        pool = AsyncMock()
        pool.translate.side_effect = block
        bridge = TextBridge({}, pool)
        pending = asyncio.create_task(asyncio.to_thread(bridge.request, 'text'))
        await entered.wait()
        await bridge.close()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(bridge.pending)


if __name__ == '__main__':
    unittest.main()
