from unittest.mock import AsyncMock
from types import SimpleNamespace
import pytest
from adam import AdamService, artwork

@pytest.mark.asyncio
async def test_cover_uses_playback_engine_and_caches_image():
    service = AdamService(SimpleNamespace(), None)
    service.request = AsyncMock(return_value='data:image/jpeg;base64,/9j/')
    first = await service.handle({'action': 'cover', 'item_id': '123'})
    second = await service.handle({'action': 'cover', 'item_id': '123'})
    assert first.ok and second.data == first.data
    service.request.assert_awaited_once_with('/api/v1/artwork/123?sf=cn&size=600', image=True)
    assert not (await service.handle({'action': 'cover', 'item_id': '../bad'})).ok

@pytest.mark.asyncio
async def test_adam_playback_returns_engine_stream_not_web_page():
    service = AdamService(SimpleNamespace(), None)
    service.request = AsyncMock(return_value={'sessionId': 'session-test', 'codec': 'alac', 'bitDepth': 24, 'sampleRate': 192000, 'durationMs': 180000})
    res = await service.handle({'action': 'resolve_stream', 'item_id': '12345', 'quality': 'hires'})
    assert res.ok
    assert res.data['play_url'] == 'https://127.0.0.1:20025/api/v1/playback/session-test/audio'
    assert res.data['duration_sec'] == 180
    body = service.request.call_args.args[2]
    assert body['capabilities']['lossless'] and body['capabilities']['hiRes']
    bad = await service.handle({'action': 'resolve_stream', 'item_id': '../../etc/passwd'})
    assert not bad.ok
    assert service.request.await_count == 1


def test_recommendations_preserve_collections_and_playable_song_resolvers():
    service = AdamService(SimpleNamespace(), None)
    payload = {'data': [{'type': 'personal-recommendation', 'relationships': {'contents': {'data': [
        {'id': '123', 'type': 'albums', 'attributes': {'name': 'Album', 'artwork': {'url': 'https://a.test/{w}x{h}.{f}'}}},
        {'id': '456', 'type': 'songs', 'attributes': {'name': 'Song'}, 'relationships': {'artists': {'data': [{'id': '789'}]}}},
    ]}}}]}
    album, song = service.map_nodes(payload)
    assert album.extra['collection'] == {'id': '123', 'type': 'albums'}
    assert album.cover_url == 'https://a.test/600x600.jpg'
    assert song.extra['stream_resolver']['payload']['item_id'] == '456'
    assert song.extra['creator_id'] == '789'

@pytest.mark.asyncio
async def test_empty_engine_library_uses_authenticated_catalog():
    client = SimpleNamespace(request=AsyncMock(return_value=(200, {'data': [{'id': 'p.test', 'type': 'library-playlists', 'attributes': {'name': 'My playlist'}}]})))
    service = AdamService(SimpleNamespace(), lambda: client)
    service.request = AsyncMock(return_value={'playlists': None})
    items = await service.feed('library')
    assert items[0].title == 'My playlist'
    assert items[0].extra['collection']['type'] == 'library-playlists'

@pytest.mark.asyncio
async def test_library_playlist_pagination_preserves_catalog_and_artwork():
    client = SimpleNamespace(request=AsyncMock(return_value=(200, {'next': '/next', 'data': [
        {'id': 'i.local', 'type': 'library-songs', 'relationships': {'catalog': {'data': [
            {'id': '123', 'type': 'songs', 'attributes': {'name': 'Song', 'artwork': {'url': 'https://a.test/{w}x{h}.{f}'}}}
        ]}}}
    ]})))
    service = AdamService(SimpleNamespace(), lambda: client)
    result = await service.handle({'action': 'collection', 'id': 'p.test', 'type': 'library-playlists', 'page': 2})
    assert result.ok and result.data['has_more']
    assert 'offset=100' in client.request.call_args.args[0]
    song = result.data['items'][0]
    assert song['cover_url'] == 'https://a.test/600x600.jpg'
    assert song['extra']['stream_resolver']['payload']['item_id'] == '123'

@pytest.mark.asyncio
async def test_transient_playback_failure_retries_once_but_auth_failure_does_not():
    from adam import AdamError
    service = AdamService(SimpleNamespace(), None)
    transient = AdamError('temporary', 'upstream_error', 500)
    service.request = AsyncMock(side_effect=[transient, {'sessionId': 'recovered'}])
    result = await service.handle({'action': 'resolve_stream', 'item_id': '123'})
    assert result.ok and service.request.await_count == 2
    service.request = AsyncMock(side_effect=transient)
    result = await service.handle({'action': 'resolve_stream', 'item_id': '123'})
    assert not result.ok and service.request.await_count == 2
    service.request = AsyncMock(side_effect=AdamError('login', 'unauthorized', 401))
    result = await service.handle({'action': 'resolve_stream', 'item_id': '123'})
    assert not result.ok and service.request.await_count == 1


@pytest.mark.asyncio
async def test_playback_401_retries_with_vault_tokens_when_available():
    from adam import AdamError
    client = SimpleNamespace(_creds=SimpleNamespace(developer_token='dev-tok', user_token='user-tok'))
    service = AdamService(SimpleNamespace(), lambda: client)
    service.request = AsyncMock(side_effect=[AdamError('login', 'unauthorized', 401), {'sessionId': 'sess-ok'}])
    result = await service.handle({'action': 'resolve_stream', 'item_id': '123'})
    assert result.ok and service.request.await_count == 2
    retry_body = service.request.call_args_list[1].args[2]
    assert retry_body['token'] == 'dev-tok'
    assert retry_body['mediaUserToken'] == 'user-tok'
