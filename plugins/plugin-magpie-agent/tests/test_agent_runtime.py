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


def test_agents_only_exposes_codex_and_agy(monkeypatch):
    import agent_runtime

    monkeypatch.setattr(agent_runtime, 'codex_binary', lambda _: '/bin/codex')
    monkeypatch.setattr(agent_runtime, 'agy_binary', lambda _: '/bin/agy')
    ctx = SimpleNamespace(get_config=lambda _k, default='': default)
    listed = agent_runtime.agents(ctx)
    assert [a['id'] for a in listed] == ['codex', 'agy']
    assert all(a['tools'] is True and a['available'] is True for a in listed)


@pytest.mark.asyncio
async def test_agy_runtime_configures_omnidesk_mcp_and_parses_result(tmp_path, monkeypatch):
    import agent_runtime

    monkeypatch.setattr(agent_runtime, 'agy_binary', lambda _: '/fake/agy')

    class Context:
        def get_config(self, _key, default=''):
            return default

        async def exec_subprocess(self, args, **kwargs):
            cwd = Path(kwargs['cwd'])
            mcp_cfg = json.loads((cwd / '.agents/plugins/omnidesk-mcp/mcp_config.json').read_text(encoding='utf-8'))
            mcp_args = mcp_cfg['mcpServers']['omnidesk']['args']
            assert '--tools' in mcp_args
            assert 'omnidesk_collect_context' in mcp_args[mcp_args.index('--tools') + 1]
            assert (cwd / '.agents/agents/omnidesk-agent/agent.md').is_file()
            assert '--agent' in args and 'omnidesk-agent' in args
            return SimpleNamespace(
                timed_out=False,
                cancelled=False,
                returncode=0,
                stdout=json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'AGY 答复', 'usage': {'input_tokens': 10}}}),
            )

    result = await chat({'messages': [{'role': 'user', 'content': 'hello'}]}, ctx=Context(), agent_id='agy')
    assert result.ok and result.data['reply'] == 'AGY 答复' and result.data['agent_id'] == 'agy'
