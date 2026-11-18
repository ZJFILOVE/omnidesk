from __future__ import annotations
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bilibili_impl.client import BilibiliApiError
from bilibili_impl.parsers import normalize_media_url
from bilibili_impl.streams import resolve_via_playurl
from bilibili_impl.browse import browse


@pytest.mark.asyncio
@pytest.mark.parametrize('web_error', [True, False])
async def test_bv_only_android_fallback_after_web_failure(monkeypatch, web_error):
    client = SimpleNamespace(allowed_loopback_origins=frozenset(), fetch_pagelist=AsyncMock(return_value=[{'cid': 99}]), fetch_view=AsyncMock(return_value={'aid': 42}), fetch_playurl=AsyncMock())
    if web_error:
        client.fetch_playurl.side_effect = BilibiliApiError('web failed', error_code='rate_limited')
    else:
        client.fetch_playurl.return_value = {'quality': 64, 'accept_quality': [64], 'dash': {'video': [{'id': 64, 'baseUrl': 'https://upos.bilivideo.com/720.m4s'}]}}
    plugin = SimpleNamespace(_build_client=lambda: client, resolve_cookie=AsyncMock(return_value=('session', 'vault')))
    android = AsyncMock(return_value={'qn': 120, 'quality': '4k', 'source': 'android_playview', 'video_url': 'https://upos.bilivideo.com/4k.m4s'})
    monkeypatch.setattr('bilibili_impl.streams.fetch_play_view_unite', android)
    result = await resolve_via_playurl(client, {'bvid': 'BV1xK4y1Z7aa'}, cookie='session', req_label='1080p+', req_qn=112, android_token='token', unlock_hd=True, plugin=plugin)
    assert result['qn'] == 120
    assert android.call_args.kwargs['aid'] == 42
    assert android.call_args.kwargs['cid'] == 99


def test_cover_upgrade_is_limited_to_actual_cdn():
    assert normalize_media_url('http://i1.hdslb.com/bfs/a.jpg') == 'https://i1.hdslb.com/bfs/a.jpg'
    assert normalize_media_url('http://hdslb.com.evil.test/a') == 'http://hdslb.com.evil.test/a'


@pytest.mark.asyncio
async def test_dynamic_keeps_text_and_uses_upstream_offset():
    client = SimpleNamespace(api_base='https://api.bilibili.com', get_json=AsyncMock(side_effect=[
        {'data': {'items': [{'id_str': '1', 'modules': {'module_author': {'mid': 42, 'name': 'UP'}, 'module_dynamic': {'desc': {'text': '图文动态'}, 'major': {'draw': {'items': [{'src': 'http://i1.hdslb.com/a.jpg'}]}}}}}], 'offset': 'cursor-2', 'has_more': True}},
        {'data': {'items': [], 'has_more': False}},
    ]))
    plugin = SimpleNamespace(_build_client=lambda: client, resolve_cookie=AsyncMock(return_value=('session', 'vault')), _browse_offsets={}, _default_quality_label=lambda: 'auto')
    first = await browse(plugin, {'action': 'dynamics'})
    assert first.data['cards'][0]['text'] == '图文动态'
    assert first.data['cards'][0]['video'] is None
    assert first.data['cards'][0]['images'] == ['https://i1.hdslb.com/a.jpg']
    await browse(plugin, {'action': 'dynamics', 'page': 2})
    assert client.get_json.call_args.kwargs['params']['offset'] == 'cursor-2'

@pytest.mark.asyncio
async def test_auto_mode_does_not_bypass_native_android_when_ytdlp_exists(monkeypatch):
    from plugin import BilibiliPlugin
    plugin = BilibiliPlugin()
    plugin.ctx = SimpleNamespace(get_config=lambda: {}, get_secret=lambda key: 'app-token' if key == 'android_access_token' else None)
    plugin.resolve_cookie = AsyncMock(return_value=('session', 'vault'))
    monkeypatch.setattr('plugin.resolve_ytdlp_mode', lambda *args, **kwargs: ('ytdlp', 'yt-dlp'))
    native = AsyncMock(return_value={'play_url': 'https://upos.bilivideo.com/4k.m4s', 'source': 'android_playview', 'android_media': True, 'quality': '4k'})
    extractor = AsyncMock()
    monkeypatch.setattr('plugin.resolve_via_playurl', native)
    monkeypatch.setattr('plugin.resolve_via_ytdlp', extractor)
    result = await plugin.action_resolve_stream({'item_id': 'BV1xK4y1Z7aa', 'emit_open': False})
    assert result.ok and result.data['source'] == 'android_playview'
    native.assert_awaited_once()
    extractor.assert_not_awaited()
    assert 'Referer' not in result.data['headers']


@pytest.mark.asyncio
async def test_android_path_is_used_by_default_before_web_playurl(monkeypatch):
    client = SimpleNamespace(
        allowed_loopback_origins=frozenset(),
        fetch_pagelist=AsyncMock(return_value=[{'cid': 99}]),
        fetch_view=AsyncMock(return_value={'aid': 42}),
        fetch_playurl=AsyncMock(),
    )
    plugin = SimpleNamespace(_build_client=lambda: client, resolve_cookie=AsyncMock(return_value=('session', 'vault')))
    android = AsyncMock(return_value={'qn': 80, 'quality': '1080p', 'source': 'android_playview', 'video_url': 'https://upos.bilivideo.com/1080.m4s'})
    monkeypatch.setattr('bilibili_impl.streams.fetch_play_view_unite', android)
    result = await resolve_via_playurl(
        client,
        {'bvid': 'BV1xK4y1Z7aa'},
        cookie='session',
        req_label='1080p',
        req_qn=80,
        android_token='token',
        unlock_hd=True,
        plugin=plugin,
    )
    assert result['source'] == 'android_playview'
    assert result['qn'] == 80
    android.assert_awaited_once()
    client.fetch_playurl.assert_not_awaited()
