"""MCP integration contracts: book workflow, explicit writes, and real image content."""
import io
import json
from pathlib import Path

import pytest

from omnidesk_core.cli import serve_mcp_stdio
from omnidesk_core.paths import OmnideskPaths


@pytest.mark.asyncio
async def test_book_workflow_preserves_provider_and_download_receipt(tmp_path, monkeypatch):
    import omnidesk_core.cli as cli
    captured = []
    class Client:
        def __init__(self, **kwargs):
            self.socket_path = tmp_path / 'control.sock'
        async def request(self, request, **kwargs):
            captured.append(request)
            payload = request['payload']
            data = {'request': payload}
            if payload['action'] == 'download':
                data = {'file_path': '/library/book.epub', 'sha256': 'verified-sha', 'size_bytes': 123}
            return {'ok': True, 'provider_plugin_id': request.get('provider_plugin_id', ''), 'data': data}
    monkeypatch.setattr(cli, 'ControlClient', Client)
    calls = [
        ('omnidesk_search_books', {'query': '书名', 'provider_plugin_id': 'plugin-annas-archive'}),
        ('omnidesk_book_detail', {'item_id': 'abc', 'provider_plugin_id': 'plugin-annas-archive'}),
        ('omnidesk_download_book', {'item_id': 'abc', 'provider_plugin_id': 'plugin-annas-archive'}),
        ('omnidesk_download_book', {'item_id': 'abc', 'provider_plugin_id': 'plugin-annas-archive', 'confirm': True, 'reason': '用户请求下载'}),
        ('omnidesk_open_downloaded_book', {'file_path': '/library/book.epub', 'target': 'ebook', 'provider_plugin_id': 'plugin-annas-archive', 'confirm': True, 'reason': '用户请求阅读'}),
        ('omnidesk_write_recommendations', {'bundle': {'action': 'push_interactive_set'}}),
    ]
    requests = [{'jsonrpc': '2.0', 'id': i, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}} for i, (name, args) in enumerate(calls)]
    output = io.StringIO()
    paths = OmnideskPaths.from_env({'XDG_DATA_HOME': str(tmp_path/'data'), 'XDG_CONFIG_HOME': str(tmp_path/'config'), 'XDG_RUNTIME_DIR': str(tmp_path/'runtime')})
    await serve_mcp_stdio(paths=paths, plugin_dirs=[], socket_path=str(tmp_path/'socket'), stdin=io.StringIO('\n'.join(json.dumps(req) for req in requests)), stdout=output)
    results = [json.loads(line)['result'] for line in output.getvalue().splitlines()]
    assert [result['isError'] for result in results] == [False, False, True, False, False, True]
    receipt = json.loads(results[3]['content'][0]['text'])
    assert receipt['data']['file_path'] == '/library/book.epub'
    assert [call['payload']['action'] for call in captured] == ['search', 'detail', 'download', 'open_in_ebook']
    assert all(call['provider_plugin_id'] == 'plugin-annas-archive' for call in captured)


@pytest.mark.asyncio
async def test_mcp_context_returns_image_content_and_accepts_cursor_only(tmp_path, monkeypatch):
    import omnidesk_core.cli as cli
    class Client:
        def __init__(self, **kwargs): self.socket_path = tmp_path/'socket'
        async def request(self, req, **kwargs):
            assert req['payload']['cursor'] == 'snapshot:20'
            return {'ok': True, 'data': {'context': 'page', 'images': [{'url': 'data:image/png;base64,aW1n'}]}}
    monkeypatch.setattr(cli, 'ControlClient', Client)
    request = {'id': 1, 'method': 'tools/call', 'params': {'name': 'omnidesk_collect_context', 'arguments': {'cursor': 'snapshot:20'}}}
    output = io.StringIO()
    paths = OmnideskPaths.from_env({'XDG_DATA_HOME': str(tmp_path/'data'), 'XDG_CONFIG_HOME': str(tmp_path/'config'), 'XDG_RUNTIME_DIR': str(tmp_path/'runtime')})
    await serve_mcp_stdio(paths=paths, plugin_dirs=[], socket_path=str(tmp_path/'socket'), stdin=io.StringIO(json.dumps(request)+'\n'), stdout=output)
    result = json.loads(output.getvalue())['result']
    assert result['content'][1] == {'type': 'image', 'mimeType': 'image/png', 'data': 'aW1n'}
    assert 'base64' not in result['content'][0]['text']


@pytest.mark.asyncio
async def test_mcp_allowed_tools_filters_list_and_blocks_disallowed_calls(tmp_path):
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'omnidesk_call_capability', 'arguments': {'capability_id': 'demo.echo.v1'}}},
    ]
    output = io.StringIO()
    paths = OmnideskPaths.from_env({'XDG_DATA_HOME': str(tmp_path/'data'), 'XDG_CONFIG_HOME': str(tmp_path/'config'), 'XDG_RUNTIME_DIR': str(tmp_path/'runtime')})
    await serve_mcp_stdio(
        paths=paths,
        plugin_dirs=[],
        socket_path=str(tmp_path/'socket'),
        allowed_tools=['omnidesk_list_plugins', 'omnidesk_collect_context'],
        stdin=io.StringIO('\n'.join(json.dumps(req) for req in requests)),
        stdout=output,
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [t['name'] for t in responses[0]['result']['tools']] == ['omnidesk_list_plugins', 'omnidesk_collect_context']
    assert responses[1]['error']['code'] == -32602
    assert 'not enabled' in responses[1]['error']['message']
