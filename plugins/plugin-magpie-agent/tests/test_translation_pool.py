import asyncio
import json
from pathlib import Path

import pytest
from omnidesk_sdk import CapabilityResult, PluginContext
from test_llm_capabilities import _load_plugin_module


@pytest.mark.asyncio
async def test_parallel_batches_preserve_order_models_context_and_usage():
    module = _load_plugin_module()
    active = peak = 0
    calls = []
    async def translate(req):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(req)
        try:
            await asyncio.sleep(0.03 if req['segments'][0]['id'] == '0' else 0.001)
            return CapabilityResult.ok({'results': [dict(segment, source=segment['text'], translation='译文:' + segment['text']) for segment in req['segments']],
                'served_model': req['model'], 'usage': {'total_tokens': 2}})
        finally:
            active -= 1
    result = await module._translation_mod.translate_with_pool({
        'texts': [f'line {i}' for i in range(7)], 'concurrency': 2, 'batch_size': 2, 'models': ['provider/a', 'provider/b'],
    }, translate=translate, normalize=module._chat_mod._normalize_translate_segments)
    assert result.ok
    assert peak == 2 and active == 0
    assert result.data['translations'] == [f'译文:line {i}' for i in range(7)]
    assert [item['id'] for item in result.data['results']] == [str(i) for i in range(7)]
    assert result.data['served_models'] == ['provider/a', 'provider/b']
    assert result.data['usage']['total_tokens'] == 8
    assert calls[0]['segments'][-1]['context_after'] == 'line 2'
    assert calls[1]['segments'][0]['context_before'] == 'line 1'


@pytest.mark.asyncio
async def test_quota_failure_cancels_siblings_and_queued_batches():
    module = _load_plugin_module()
    started, cancelled = [], []
    async def translate(req):
        text = req['segments'][0]['text']
        started.append(text)
        if text == 'a':
            await asyncio.sleep(0.01)
            return CapabilityResult.fail('quota exhausted', error_code='quota_exceeded')
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(text)
    result = await module._translation_mod.translate_with_pool({'texts': list('abcdef'), 'concurrency': 2, 'batch_size': 1},
        translate=translate, normalize=module._chat_mod._normalize_translate_segments)
    assert not result.ok and result.data['error_code'] == 'quota_exceeded'
    assert set(cancelled) == set(started) - {'a'}
    assert len(started) <= 3


@pytest.mark.asyncio
async def test_translation_settings_are_persisted_and_do_not_change_chat_route(tmp_path):
    module = _load_plugin_module()
    directory = Path(__file__).resolve().parents[1]
    manifest = json.loads((directory / 'plugin.json').read_text())
    ctx = PluginContext(manifest['id'], plugin_dir=directory, data_dir=tmp_path / 'data', config_dir=tmp_path / 'config', config_schema=manifest['config_schema'])
    plugin = module.MagpieAgentPlugin()
    await plugin.on_load(ctx)
    try:
        result = await plugin.handle_translate({'action': 'configure', 'translation_models': 'provider/translator', 'translation_concurrency': 3, 'translation_batch_size': 2})
        assert result.ok and result.data['translation_concurrency'] == 3
        assert ctx.get_config('default_group') == 'omnidesk-active'
        assert (await plugin.handle_translate({'action': 'settings'})).data['translation_models'] == 'provider/translator'
        bad = await plugin.handle_translate({'action': 'configure', 'translation_models': 'changed', 'translation_concurrency': 99})
        assert not bad.ok and ctx.get_config('translation_models') == 'provider/translator'
        seen = []
        async def chat(req, stream):
            seen.append(req)
            return CapabilityResult.ok({'reply': '<translation>你好</translation>', 'model': req['model']})
        plugin._call_chat_via_ctx = chat
        assert (await plugin.handle_translate({'text': 'Hello'})).data['translation'] == '你好'
        assert seen[0]['model'] == 'provider/translator'
    finally:
        await plugin.on_unload()
        await ctx.acleanup()
