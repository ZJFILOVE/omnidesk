import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runtime import chat, command


def test_command_preserves_model_config_but_limits_mcp_tools(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    (tmp_path / 'config.toml').write_text('[mcp_servers.other]\ncommand="other"\n')
    args, env = command('/bin/codex', str(tmp_path), model='my-model')
    assert '--sandbox' in args and 'read-only' in args
    assert 'mcp_servers.other.enabled=false' in args
    assert 'mcp_servers.omnidesk.enabled=true' in args
    assert 'my-model' in args and '--ignore-user-config' not in args
    tools = next(arg for arg in args if arg.startswith('mcp_servers.omnidesk.enabled_tools='))
    assert 'omnidesk_search_books' in tools and 'omnidesk_call_capability' not in tools
    assert 'core' in env['PYTHONPATH']


@pytest.mark.asyncio
async def test_harness_receives_history_images_and_reports_real_output(tmp_path, monkeypatch):
    import agent_runtime
    monkeypatch.setattr(agent_runtime, 'codex_binary', lambda _: '/fake/codex')
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    class Context:
        def get_config(self, key, default=''):
            return default
        async def exec_subprocess(self, args, **kwargs):
            assert args[-1] == '-' and 'last request' in kwargs['stdin_data']
            assert '--image' in args
            assert Path(args[args.index('--image') + 1]).read_bytes() == b'img'
            return SimpleNamespace(timed_out=False, cancelled=False, returncode=0,
                stdout=json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '实际答复'}}))
    result = await chat({'messages': [{'role': 'user', 'content': 'last request'}, {'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aW1n'}}]}]}, ctx=Context())
    assert result.ok and result.data['reply'] == '实际答复' and result.data['agent_id'] == 'codex'
