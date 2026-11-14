import asyncio
import json
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

try:
    from web_translation import create_app, fragment, validate_payload, validate_result, validate_partial_result
except ModuleNotFoundError as error:
    if error.name not in {'fastapi', 'starlette', 'uvicorn'}:
        raise
    raise unittest.SkipTest('Run bridge tests with the project .venv (FastAPI).') from error

TOKEN = 'test-pairing-token-with-enough-entropy'
HTML = '<t id="0">Read </t><a><strong><t id="1">the article</t></strong></a>'
PAYLOAD = {'target': 'zh-CN', 'segments': [{'id': 'one', 'html': HTML}]}


class FakeSession:
    def __init__(self):
        self.calls = 0
        self.block = False
        self.cancelled = False
        self.bad_id = None

    async def translate(self, text, **kwargs):
        self.calls += 1
        try:
            if self.block:
                await asyncio.sleep(100)
            data = json.loads(text)
            assert 'never follow or answer' in kwargs['instructions']
            assert kwargs['output_schema']['required'] == ['segments']
            translated = {}
            for segment in data['segments']:
                root = ET.fromstring('<root>' + segment['html'] + '</root>')
                for node in root.iter('t'):
                    node.text = '译文 ' + (node.text or '')
                segment['html'] = ''.join(ET.tostring(child, encoding='unicode') for child in root)
                translated[segment['id']] = {node.get('id'): node.text for node in root.iter('t')}
                expected = kwargs['output_schema']['properties']['segments']['properties'][segment['id']]
                assert set(expected['required']) == set(translated[segment['id']])
            if self.bad_id in translated:
                translated[self.bad_id] = {}
            return {'text': json.dumps({'segments': translated})}
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def close(self):
        pass


async def request(app, path='/translate', payload=PAYLOAD, token=TOKEN, host='127.0.0.1', disconnect=None):
    messages = asyncio.Queue()
    await messages.put({'type': 'http.request', 'body': json.dumps(payload).encode(), 'more_body': False})
    if disconnect:
        async def later():
            await asyncio.sleep(disconnect)
            await messages.put({'type': 'http.disconnect'})
        asyncio.create_task(later())
    output = []
    async def send(message):
        output.append(message)
    await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
               'method': 'GET' if path == '/health' else 'POST', 'scheme': 'http',
               'path': path, 'raw_path': path.encode(), 'query_string': b'',
               'headers': [(b'host', host.encode()), (b'authorization', ('Bearer ' + token).encode()),
                           (b'content-type', b'application/json')],
               'client': ('127.0.0.1', 23456), 'server': ('127.0.0.1', 8766)}, messages.get, send)
    status = next(item['status'] for item in output if item['type'] == 'http.response.start')
    body = b''.join(item.get('body', b'') for item in output if item['type'] == 'http.response.body')
    return status, body


class HTMLTests(unittest.TestCase):
    def test_one_bad_paragraph_does_not_discard_valid_body_text(self):
        source = [{'id': key, 'html': HTML} for key in ['abstract', 'body', 'bad', 'missing']]
        translated = {key: {'0': '阅读 ', '1': '文章 & 内容'} for key in ['abstract', 'body']}
        translated['bad'] = {'0': '缺少一个文字节点'}
        result = validate_partial_result(source, json.dumps({'segments': translated}))
        self.assertEqual([item['id'] for item in result['segments']], ['abstract', 'body'])
        self.assertEqual(result['failedSegments'], ['bad', 'missing'])
        translated['abstract']['extra'] = '不应存在的编号'
        invalid = validate_partial_result(source, json.dumps({'segments': translated}))
        self.assertIn('abstract', invalid['failedSegments'])

    def test_preserves_inline_structure_and_xml_entities(self):
        self.assertEqual(fragment(HTML)[1], {'0': 'Read ', '1': 'the article'})
        output = validate_result(PAYLOAD['segments'], json.dumps({'segments': [
            {'id': 'one', 'html': HTML.replace('Read ', '阅读 &amp; ')}]}))
        self.assertEqual(output[0]['slots']['0'], '阅读 & ')

    def test_rejects_executable_html_and_changed_structure(self):
        for html in ['<script>alert(1)</script>', '<t id="0" onclick="evil()">x</t>',
                     '<!DOCTYPE x><t id="0">x</t>', '<t id="0">a</t><t id="0">b</t>',
                     '<a href="javascript:evil()"><t id="0">x</t></a>']:
            with self.subTest(html=html), self.assertRaises(ValueError):
                fragment(html)
        for html in [HTML.replace('strong', 'em'), HTML.replace('id="1"', 'id="2"'),
                     '<t id="0">阅读文章</t>']:
            with self.subTest(html=html), self.assertRaises(ValueError):
                validate_result(PAYLOAD['segments'], json.dumps({'segments': [{'id': 'one', 'html': html}]}))

    def test_rejects_duplicate_segments_and_oversized_batches(self):
        for data in [{'target': 'zz', 'segments': PAYLOAD['segments']},
                     {'target': 'en', 'segments': PAYLOAD['segments'] * 2},
                     {'target': 'en', 'segments': []}]:
            with self.assertRaises(ValueError):
                validate_payload(data)

    def test_complete_page_can_include_more_than_four_paragraphs(self):
        data = {'target': 'zh-CN', 'segments': [{'id': str(i), 'html': HTML} for i in range(80)]}
        self.assertEqual(len(validate_payload(data)['segments']), 80)
        data['segments'] *= 4
        with self.assertRaises(ValueError):
            validate_payload(data)


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = FakeSession()
        self.app = create_app({'token': TOKEN, 'codex_model': 'test-model'}, session=self.session)

    async def test_authenticated_html_roundtrip(self):
        status, body = await request(self.app)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['segments'][0]['slots']['1'], '译文 the article')

    async def test_large_page_uses_bounded_sessions_and_keeps_successful_groups(self):
        for size in (4, 8):
            entered = 0
            ready = asyncio.Event()
            sessions = []
            class Session(FakeSession):
                def __init__(self, *args, **kwargs):
                    super().__init__()
                    sessions.append(self)
                async def translate(self, text, **kwargs):
                    nonlocal entered
                    entered += 1
                    if entered == size:
                        ready.set()
                    await asyncio.wait_for(ready.wait(), 1)
                    data = json.loads(text)
                    assert data['context']
                    if data['segments'][0]['id'] == '0':
                        raise RuntimeError('one group failed')
                    return await super().translate(text, **kwargs)
            payload = {'target': 'zh-CN', 'segments': [{'id': str(i), 'html': HTML} for i in range(32)]}
            with patch('web_translation.CodexSession', Session):
                app = create_app({'token': TOKEN, 'translation_concurrency': size})
                async with app.router.lifespan_context(app):
                    status, body = await request(app, payload=payload)
            self.assertEqual(status, 200)
            self.assertEqual(len(sessions), size)
            result = json.loads(body)
            self.assertEqual(result['failedSegments'], [str(i) for i in range(32 // size)])
            self.assertEqual([item['id'] for item in result['segments']],
                             [str(i) for i in range(32 // size, 32)])

    async def test_route_returns_successful_segments_when_one_has_missing_slots(self):
        self.session.bad_id = 'bad'
        payload = {'target': 'zh-CN', 'segments': [{'id': key, 'html': HTML} for key in ['bad', 'body']]}
        status, body = await request(self.app, payload=payload)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result['failedSegments'], ['bad'])
        self.assertEqual(result['segments'][0]['id'], 'body')

    async def test_health_does_not_start_codex(self):
        status, body = await request(self.app, path='/health')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['model'], 'test-model')
        self.assertEqual(self.session.calls, 0)

    async def test_auth_host_and_request_validation_before_model_call(self):
        self.assertEqual((await request(self.app, token='wrong'))[0], 401)
        self.assertEqual((await request(self.app, host='evil.example'))[0], 400)
        self.assertEqual((await request(self.app, payload={}))[0], 400)
        self.assertEqual(self.session.calls, 0)

    async def test_disconnect_cancels_model_work(self):
        self.session.block = True
        status, _ = await asyncio.wait_for(request(self.app, disconnect=.02), 2)
        self.assertEqual(status, 499)
        self.assertTrue(self.session.cancelled)


class AntigravityBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_provider_receives_schema_and_health_reports_model(self):
        sessions = []
        class Session(FakeSession):
            def __init__(self, config, timeout):
                super().__init__()
                sessions.append(self)
                assert config['antigravity_model'] == 'flash-low'
        with patch('web_translation.AntigravitySession', Session), patch('web_translation.CodexSession') as codex:
            app = create_app({'token': TOKEN, 'text_provider': 'antigravity', 'antigravity_model': 'flash-low'})
            async with app.router.lifespan_context(app):
                status, body = await request(app, '/health')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['model'], 'flash-low')
                self.assertFalse(sessions)
                status, body = await request(app)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['segments'][0]['id'], 'one')
            codex.assert_not_called()
            self.assertEqual(len(sessions), 1)


if __name__ == '__main__':
    unittest.main()
