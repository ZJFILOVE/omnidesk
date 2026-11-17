"""Contract fixtures derived from Anna's official templates, not Z-Library cards."""
import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from annas_html import page_state
from annas_parser import parse_search_page, parse_annas_detail_response, parse_annas_item_id, extract_slow_download_link, AnnasPageError
from annas_auth import AuthMaterial
from annas_test_helpers import MD5_SICP, MockAnnasState, run_mock_annas_server
from test_plugin_annas_archive import _load_plugin_module, _make_ctx, PLUGIN_DIR


def card(md5, title):
    return f'''<div class="flex pt-3 border-b">
      <a href="/md5/{md5}"><div><img src="cover.jpg"><br></div></a>
      <div><div><a href="/md5/{md5}" class="font-semibold text-lg">{title} <em>Nested</em></a>
      <a href="/search?q=Author"><span class="icon-[mdi--user-edit]"></span>Author One</a>
      <a href="/search?q=Press"><span class="icon-[mdi--company]"></span>Press 2020</a></div>
      <div class="text-gray-800">English [en] · PDF · 2.3 MB · 2020</div></div></div>'''


def test_current_sibling_cards_comments_void_tags_and_pagination():
    other = 'f' * 32
    html = card(MD5_SICP, 'First') + '<!--' + card(other, 'Second') + '-->' + '<a href="/search?q=test&amp;page=2">2</a>'
    page = parse_search_page(html.encode(), 'text/html', 'https://annas-archive.gl')
    assert [i['title'] for i in page['results']] == ['First Nested', 'Second Nested']
    assert all(i['author'] == 'Author One' and i['format'] == 'pdf' and not i['download_token'] for i in page['results'])
    assert page['has_more'] and page['next_page'] == 2
    assert not parse_annas_item_id('zlib123456')


def test_detail_one_md5_one_format_and_actual_source_indices():
    html = f'''<div class="font-semibold text-2xl">Book<span class="select-none"><a href="/search">🔍</a></span></div>
    <a href="/search?q=A"><span class="icon-[mdi--user-edit]"></span>A Writer</a>
    <div class="text-gray-800">English [en] · EPUB · 4 MB · 2024</div>
    <div class="js-md5-top-box-description"><div>Summary</div><div>Nested <b>description</b></div></div>
    <a class="js-download-link" href="/fast_download/{MD5_SICP}/3/2">Fast partner #3</a>
    <a class="js-download-link" href="/slow_download/{MD5_SICP}/1/4">Slow partner</a>
    <a class="js-download-link" rel="noopener noreferrer nofollow" href="https://libgen.example/ads.php?md5={MD5_SICP}">Library Genesis</a>'''
    detail = parse_annas_detail_response(html.encode(), 'text/html', 'https://annas-archive.gl', requested_id=MD5_SICP, detail_url='detail')
    assert detail['title'] == 'Book' and detail['author'] == 'A Writer'
    assert detail['description'] == 'Summary Nested description'
    assert detail['available_formats'] == ['epub']
    assert [o['kind'] for o in detail['download_options']] == ['fast', 'slow', 'external']
    assert detail['download_options'][0]['path_index'] == 3
    assert detail['download_options'][0]['domain_index'] == 2


@pytest.mark.parametrize('body,code', [(b'<title>Just a moment</title>', 'verification_required'), (b'<html>Other library catalog</html>', 'unsupported_response'), (b'{broken', 'parse_error'), (b'{"error":"denied"}', 'unsupported_response')])
def test_unknown_or_challenge_pages_never_look_empty(body, code):
    with pytest.raises(AnnasPageError) as exc:
        parse_search_page(body, 'text/html', 'https://annas-archive.gl')
    assert exc.value.code == code


def test_slow_wait_and_copy_only_links():
    assert page_state(b'<script>let waitSeconds = 120;</script>')['wait_seconds'] == 120
    html = b'''<button onclick='navigator.clipboard.writeText("https://cdn.example/file?token=abc")'>Copy</button>'''
    assert extract_slow_download_link(html) == 'https://cdn.example/file?token=abc'
    assert 'X-Api-Key' not in AuthMaterial(api_key='member-secret').build_headers()


@pytest.mark.asyncio
async def test_slow_wait_and_checksum_mismatch_leave_no_files(tmp_path):
    plugin = _load_plugin_module().create_plugin()
    manifest = json.loads((PLUGIN_DIR/'plugin.json').read_text())
    ctx = _make_ctx(tmp_path, manifest)
    state = MockAnnasState()
    with run_mock_annas_server(state) as url:
        ctx.set_config({'mirror_url': url, 'download_root': str(tmp_path/'downloads')})
        await plugin.on_load(ctx)
        try:
            request = {'action': 'download', 'item_id': MD5_SICP, 'format': 'pdf', 'download_token': f'/slow_download/{MD5_SICP}/0/0'}
            state.partner_html = '<script>let waitSeconds = 30;</script>'
            result = await plugin.handle_book_search(request)
            assert not result.ok and result.data['error_code'] == 'waiting' and result.data['wait_seconds'] == 30
            state.partner_html = None
            state.pdf_bytes = b'%PDF-1.7\nDifferent file'
            result = await plugin.handle_book_search(request)
            assert not result.ok and result.data['error_code'] == 'checksum_mismatch'
            assert not list((tmp_path/'downloads').glob('*'))
        finally:
            await plugin.on_unload(); await ctx.acleanup()

@pytest.mark.asyncio
async def test_fast_api_passes_real_indices_and_keeps_key_out_of_headers(tmp_path):
    plugin = _load_plugin_module().create_plugin()
    manifest = json.loads((PLUGIN_DIR/'plugin.json').read_text())
    ctx = _make_ctx(tmp_path, manifest)
    state = MockAnnasState()
    with run_mock_annas_server(state) as url:
        ctx.set_config({'mirror_url': url, 'download_root': str(tmp_path/'downloads')})
        ctx.set_secret('api_key', 'secret-member')
        await plugin.on_load(ctx)
        try:
            from annas_security import parse_origin
            result = await plugin._resolve_fast_api_download(url, MD5_SICP, auth=AuthMaterial(api_key='secret-member'),
                origins=frozenset({parse_origin(url)}), option={'path_index': 3, 'domain_index': 2})
            assert result.endswith('/pdf')
            request = state.requests[-1]
            assert 'path_index=3' in request['query'] and 'domain_index=2' in request['query']
            assert request['api_key'] == ''
        finally:
            await plugin.on_unload(); await ctx.acleanup()

@pytest.mark.asyncio
async def test_member_api_redirects_are_not_followed(tmp_path):
    from annas_http import fetch_with_chrome_fallback, HttpFetchError
    from annas_security import parse_origin
    state = MockAnnasState()
    with run_mock_annas_server(state) as url:
        with pytest.raises(HttpFetchError) as exc:
            await fetch_with_chrome_fallback(None, url+'/dl/redirect-ok?key=secret', auth=AuthMaterial(api_key='secret'),
                timeout_sec=2, max_bytes=10000, configured_origins=frozenset({parse_origin(url)}), allow_chrome=False, allow_json_errors=True)
        assert exc.value.error_code == 'unsafe_url'
        assert len(state.requests) == 1


def test_djvu_is_a_supported_anna_file_format(tmp_path):
    from annas_magic import validate_downloaded_file
    path = tmp_path/'file.djvu'
    path.write_bytes(b'AT&TFORM\x00\x00\x00\x10DJVU' + b'\x00'*16)
    assert validate_downloaded_file(path, 'djvu', 1024) == 'djvu'


def test_verification_help_link_is_not_a_challenge_page():
    assert page_state(b'<h1>Book title</h1><a href="/browser_verification">Browser verification</a>')['state'] == 'ready'

@pytest.mark.asyncio
async def test_small_ui_pages_do_not_skip_remaining_server_results(tmp_path):
    plugin = _load_plugin_module().create_plugin()
    manifest = json.loads((PLUGIN_DIR/'plugin.json').read_text())
    ctx = _make_ctx(tmp_path, manifest)
    state = MockAnnasState()
    with run_mock_annas_server(state) as url:
        ctx.set_config({'mirror_url': url})
        await plugin.on_load(ctx)
        try:
            first = await plugin.handle_book_search({'action': 'search', 'query': 'test', 'limit': 1})
            assert first.ok and first.data['next_cursor'] == {'page': 1, 'offset': 1}
            second = await plugin.handle_book_search({'action': 'search', 'query': 'test', 'limit': 1, 'cursor': first.data['next_cursor']})
            assert second.ok and second.data['results'][0]['item_id'] != first.data['results'][0]['item_id']
            assert not second.data['has_more']
        finally:
            await plugin.on_unload(); await ctx.acleanup()
