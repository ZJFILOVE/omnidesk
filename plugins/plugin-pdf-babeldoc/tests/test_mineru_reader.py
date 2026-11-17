import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mineru_reader import MineruReader, image_data, json_blocks, parse_result, source_hash
from test_plugin import _load_plugin_module
from pdf_fixture import write_pdf
from omnidesk_sdk import PluginContext

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


@pytest.mark.asyncio
async def test_single_page_wrapper_protocol_and_cache(tmp_path):
    python = Path.home() / 'Projects/qwen-translator/runtime/pdf-venv/bin/python'
    if not python.is_file():
        pytest.skip('Qwen PDF Python required for subset integration check')
    source = tmp_path / 'source.pdf'
    subprocess.run([str(python), '-c',
        "import pymupdf,sys; d=pymupdf.open(); d.new_page().insert_text((72,72),'First page'); d.new_page().insert_text((72,72),'Second page'); d.save(sys.argv[1])",
        str(source)], check=True)
    wrapper = tmp_path / 'wrapper.py'
    wrapper.write_text('''import json, os, sys
from pathlib import Path
import pymupdf
action = sys.argv[1]
task = Path(sys.argv[sys.argv.index('--output') + 1])
task.mkdir(parents=True, exist_ok=True)
assert os.environ['MINERU_API_TOKEN'] == 'test-private-token'
with (task / 'calls').open('a') as log:
    log.write(action + '\\n')
if action == 'submit':
    with pymupdf.open(sys.argv[sys.argv.index('--file') + 1]) as doc:
        assert len(doc) == 1 and 'Second page' in doc[0].get_text()
    (task / 'task.json').write_text('{}')
elif action == 'download':
    result = task / 'result'
    result.mkdir()
    (result / 'content_list.json').write_text(json.dumps([{'type': 'text', 'page_idx': 0, 'text': 'Second page OCR'}]))
    (task / 'download.json').write_text('{}')
''')
    reader = MineruReader(tmp_path / 'data', lambda: {
        'mineru_wrapper': str(wrapper), 'mineru_python': str(python),
    }, lambda: 'test-private-token')
    parsed = await reader.parse(source, 2, upload=True)
    assert parsed['blocks'][0].original == 'Second page OCR'
    assert parsed['locations'][0]['page'] == 2
    cached = await reader.parse(source, 2, upload=False)
    assert cached['blocks'] == parsed['blocks']
    task = reader.directory(source, 2) / 'task'
    assert (task / 'calls').read_text().splitlines() == ['submit', 'poll', 'download']
    assert 'test-private-token' not in (task.parent / 'page.json').read_text()


def fixture(root):
    (root / 'images').mkdir()
    (root / 'images/figure.png').write_bytes(PNG)
    return root


def test_json_preserves_page_bbox_formula_figure_and_caption(tmp_path):
    fixture(tmp_path)
    raw = [
        {'type': 'text', 'page_idx': 0, 'text': 'Before', 'bbox': [1, 2, 30, 40]},
        {'type': 'image', 'page_idx': 0, 'img_path': 'images/figure.png', 'image_caption': ['Figure 1']},
        {'type': 'equation', 'page_idx': 0, 'text': 'x^2'},
        {'type': 'text', 'page_idx': 1, 'text': 'Different page'},
    ]
    (tmp_path / 'test_content_list.json').write_text(json.dumps(raw))
    (tmp_path / 'full.md').write_text('WRONG FALLBACK')
    result = parse_result(tmp_path, 7)
    assert result['format'] == 'content_list'
    assert [b['kind'] for b in result['blocks']] == ['paragraph', 'image', 'caption', 'formula']
    assert result['locations'][0]['page'] == 7
    assert result['locations'][0]['bbox'] == [1, 2, 30, 40]
    image = result['blocks'][1]
    assert image['image_url'] == image['translated_image_url']
    assert base64.b64decode(image['image_url'].split(',')[1]) == PNG
    assert result['blocks'][-1]['original'] == '$$\nx^2\n$$'
    assert 'Different page' not in str(result)
    nested, _ = json_blocks([[raw[0]], [raw[-1]]], tmp_path, 9, 1)
    assert nested[0]['original'] == 'Different page'


def test_md_fallback_preserves_image_order_and_references(tmp_path):
    fixture(tmp_path)
    (tmp_path / 'test_content_list.json').write_text('{"unknown":"schema"}')
    (tmp_path / 'full.md').write_text('Before\n\n![Plot](images/figure.png)\n\nAfter\n\n![Again][fig]\n\n[fig]: images/figure.png\n')
    result = parse_result(tmp_path, 3)
    assert result['format'] == 'markdown'
    assert [b['kind'] for b in result['blocks']] == ['paragraph', 'image', 'caption', 'paragraph', 'image', 'caption']
    assert [b['original'] for b in result['blocks'] if b['kind'] != 'image'] == ['Before', 'Plot', 'After', 'Again']


@pytest.mark.parametrize('reference', ['../private.png', '/etc/passwd', 'https://example.com/a.png', 'images/missing.png', '%2e%2e/private.png'])
def test_image_paths_fail_closed(tmp_path, reference):
    fixture(tmp_path)
    with pytest.raises(ValueError):
        image_data(tmp_path, reference)


def test_symlink_image_rejected(tmp_path):
    fixture(tmp_path)
    (tmp_path / 'images/link.png').symlink_to(tmp_path / 'images/figure.png')
    with pytest.raises(ValueError):
        image_data(tmp_path, 'images/link.png')


@pytest.mark.asyncio
async def test_cached_scan_is_available_to_book_and_agent_without_upload(tmp_path):
    module = _load_plugin_module()
    directory = Path(__file__).resolve().parents[1]
    manifest = json.loads((directory / 'plugin.json').read_text())
    ctx = PluginContext(manifest['id'], plugin_dir=directory, config_dir=tmp_path / 'cfg', data_dir=tmp_path / 'data', config_schema=manifest['config_schema'])
    source = write_pdf(tmp_path / 'library/scan.pdf', pages=2)
    ctx.set_config({'pdf_library_roots': str(source.parent), 'output_root': str(tmp_path / 'out')})
    plugin = module.create_plugin()
    await plugin.on_load(ctx)
    try:
        book = (await plugin.handle_book_source({'action': 'list_books'})).data['books'][0]
        reader = plugin._book_source.mineru
        cache = reader.directory(source, 2)
        cache.mkdir(parents=True)
        (cache / 'page.json').write_text(json.dumps({'source_sha256': source_hash(source), 'page': 2, 'format': 'content_list',
            'blocks': [{'id': 'p2', 'kind': 'paragraph', 'original': 'Actual OCR text'}], 'locations': [{'page': 2}]}))
        spread = await plugin.handle_book_source({'action': 'fetch_spread', 'book_id': book['id'], 'page_number': 2})
        assert spread.ok and spread.data['spread']['blocks'][0]['original'] == 'Actual OCR text'
        assert spread.data['spread']['extra']['parser'] == 'mineru'
        reuse = await plugin.handle_bilingual_pdf({'action': 'parse_page', 'book_id': book['id'], 'page_number': 2})
        assert reuse.ok
        refused = await plugin.handle_bilingual_pdf({'action': 'parse_page', 'book_id': book['id'], 'page_number': 1})
        assert not refused.ok and '上传当前页' in refused.error
        source.write_bytes(source.read_bytes() + b'\n%changed')
        assert reader.cached(source, 2) is None
    finally:
        await plugin.on_unload()
        await ctx.acleanup()


@pytest.mark.asyncio
async def test_settings_token_is_private_and_not_echoed(tmp_path):
    module = _load_plugin_module()
    directory = Path(__file__).resolve().parents[1]
    manifest = json.loads((directory / 'plugin.json').read_text())
    ctx = PluginContext(manifest['id'], plugin_dir=directory, config_dir=tmp_path / 'cfg', data_dir=tmp_path / 'data', config_schema=manifest['config_schema'])
    plugin = module.create_plugin()
    await plugin.on_load(ctx)
    try:
        secret = 'test-mineru-private-token'
        result = await plugin.handle_bilingual_pdf({'action': 'configure_mineru', 'mineru_api_token': secret})
        assert result.ok and result.data['token_configured']
        assert secret not in str(result.data) and ctx.get_secret('mineru_api_token') == secret
        assert secret not in str(plugin._read_config())
        await plugin.handle_bilingual_pdf({'action': 'configure_mineru', 'mineru_api_token': ''})
        assert ctx.get_secret('mineru_api_token') == secret
        await plugin.handle_bilingual_pdf({'action': 'configure_mineru', 'clear_token': True})
        assert not ctx.get_secret('mineru_api_token')
    finally:
        await plugin.on_unload()
        await ctx.acleanup()
