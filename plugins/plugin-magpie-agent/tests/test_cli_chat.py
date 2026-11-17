import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_llm_capabilities import _load_plugin_module


@pytest.mark.parametrize('backend', ['codex', 'agy'])
@pytest.mark.asyncio
async def test_direct_cli_uses_stdin_isolated_directory_and_requested_model(tmp_path, monkeypatch, backend):
    module = _load_plugin_module()._cli_mod
    monkeypatch.setattr(module, 'binary', lambda *a: '/fake/' + backend)
    monkeypatch.setenv('OPENAI_API_KEY', 'do-not-forward')
    monkeypatch.setenv('GEMINI_API_KEY', 'do-not-forward')
    directories = []
    class Context:
        def get_config(self, key, default=''):
            return default
        async def exec_subprocess(self, args, **kwargs):
            directories.append(kwargs['cwd'])
            assert Path(kwargs['cwd']).is_dir()
            assert 'private source' not in ' '.join(args)
            assert 'private source' in kwargs['stdin_data']
            assert args[args.index('--model') + 1] == 'chosen-model'
            if backend == 'codex':
                assert '--ignore-user-config' in args
                assert 'features.shell_tool=false' in args
                assert 'OPENAI_API_KEY' not in kwargs['env']
                output = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '<translation>你好</translation>'}}
            else:
                agent = Path(kwargs['cwd']) / '.agents/agents/omnidesk-text/agent.md'
                assert 'tools: [finish]' in agent.read_text() and 'mcpServers: []' in agent.read_text()
                assert 'GEMINI_API_KEY' not in kwargs['env']
                assert json.loads(kwargs['stdin_data'])['event'] == 'user'
                output = {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '<translation>你好</translation>'}}
            return SimpleNamespace(timed_out=False, cancelled=False, returncode=0, stdout=json.dumps(output), stderr='')
    result = await module.chat({'messages': [{'role': 'user', 'content': 'private source'}], 'model': 'chosen-model'}, ctx=Context(), backend=backend, translation=True)
    assert result.ok and result.data['reply'] == '<translation>你好</translation>'
    assert all(not Path(directory).exists() for directory in directories)


@pytest.mark.asyncio
async def test_cli_failure_is_redacted_and_rate_limit_is_retryable(monkeypatch):
    module = _load_plugin_module()._cli_mod
    monkeypatch.setattr(module, 'binary', lambda *a: '/fake/agy')
    class Context:
        def get_config(self, key, default=''):
            return default
        async def exec_subprocess(self, *args, **kwargs):
            return SimpleNamespace(timed_out=False, cancelled=False, returncode=1, stdout='', stderr='429 secret private source')
    result = await module.chat({}, ctx=Context(), backend='agy')
    assert not result.ok and result.data['error_code'] == 'rate_limited'
    assert 'private source' not in str(result) and 'secret' not in str(result)


@pytest.mark.asyncio
async def test_translation_routes_directly_and_keeps_backend_models_separate(tmp_path, monkeypatch):
    from omnidesk_sdk import PluginContext, CapabilityResult
    module = _load_plugin_module()
    directory = Path(__file__).resolve().parents[1]
    manifest = json.loads((directory / 'plugin.json').read_text())
    ctx = PluginContext(manifest['id'], plugin_dir=directory, data_dir=tmp_path/'data', config_dir=tmp_path/'config', config_schema=manifest['config_schema'])
    plugin = module.MagpieAgentPlugin()
    await plugin.on_load(ctx)
    seen = []
    async def direct(req, **kwargs):
        seen.append((req, kwargs))
        return CapabilityResult.ok({'reply': '<translation>你好</translation>'})
    monkeypatch.setattr(module._cli_mod, 'chat', direct)
    try:
        result = await plugin.handle_translate({'action': 'configure', 'translation_backend': 'agy', 'translation_agy_models': 'agy-model', 'translation_models': 'gateway-model'})
        assert result.ok
        result = await plugin.handle_translate({'text': 'Hello'})
        assert result.ok and seen[0][0]['model'] == 'agy-model' and seen[0][1]['backend'] == 'agy'
        assert ctx.get_config('translation_models') == 'gateway-model'
    finally:
        await plugin.on_unload()
        await ctx.acleanup()
