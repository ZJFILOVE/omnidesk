import importlib.util
import json
from pathlib import Path

import pytest
from omnidesk_sdk import PluginContext


@pytest.mark.parametrize('folder,auth_name', [('plugin-zlib', 'zlib_auth'), ('plugin-annas-archive', 'annas_auth')])
@pytest.mark.asyncio
async def test_mirror_switch_persists_clears_cache_and_does_not_forward_vault_secrets(tmp_path, monkeypatch, folder, auth_name):
    directory = Path(__file__).resolve().parents[1] / 'plugins' / folder
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location(folder.replace('-', '_') + '_mirrors', directory / 'plugin.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = json.loads((directory / 'plugin.json').read_text())
    ctx = PluginContext(folder, plugin_dir=directory, config_dir=tmp_path/'config', data_dir=tmp_path/'data', config_schema=manifest['config_schema'])
    ctx.set_config({'base_url': 'https://old.example.com', 'chrome_cookie_source': 'vault'})
    ctx.set_secret('api_key', 'old-secret')
    ctx.set_secret('cookie_header', 'session=old-secret')
    plugin = module.create_plugin()
    await plugin.on_load(ctx)
    try:
        plugin._search_cache['old'] = {}
        result = await plugin.handle_book_search({'action': 'configure_mirror', 'mirror_url': 'https://new.example.com'})
        assert result.ok and result.data['mirror_url'] == 'https://new.example.com'
        assert not plugin._search_cache
        auth = importlib.import_module(auth_name)
        material = await auth.resolve_auth_material(ctx, 'https://new.example.com')
        assert not material.api_key and not material.cookie_header
        original = await auth.resolve_auth_material(ctx, 'https://old.example.com')
        assert original.api_key == 'old-secret'
        bad = await plugin.handle_book_search({'action': 'configure_mirror', 'mirror_url': 'https://user:pass@example.com'})
        assert not bad.ok and ctx.get_config('mirror_url') == 'https://new.example.com'
    finally:
        await plugin.on_unload()
        await ctx.acleanup()
