"""Adam engine integration, ported from v1's same-user, pinned-TLS client."""
from __future__ import annotations

import asyncio
import base64
import hmac
import http.client
import json
import os
import re
import socket
import ssl
from pathlib import Path
from urllib.parse import urlencode

from omnidesk_sdk import CapabilityResult, MediaItem
if __package__:
    from .apple_client import AppleMusicApiError
    from .state_store import GateError
else:
    from apple_client import AppleMusicApiError
    from state_store import GateError

CATEGORIES = ['recommendations', 'heavy-rotation', 'recently-played', 'library', 'search']
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')


def artwork(value: str) -> str:
    return str(value or '').replace('{w}', '600').replace('{h}', '600').replace('{f}', 'jpg').replace('{c}', 'bb')


def resolver(song_id: str) -> dict:
    return {'capability': 'media.adam.v1', 'provider_plugin_id': 'plugin-music',
            'payload': {'action': 'resolve_stream', 'item_id': song_id},
            'quality_options': ['hires', 'lossless', 'aac', 'atmos']}


class AdamError(RuntimeError):
    def __init__(self, message: str, code: str = 'service_offline', status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class AdamService:
    def __init__(self, ctx, catalog_client):
        self.ctx, self.catalog_client = ctx, catalog_client
        self._start_lock = asyncio.Lock()
        self._engine_task = None
        self.sessions: list[str] = []
        self._covers: dict[str, str] = {}

    def owned_listener(self) -> bool:
        expected = '0100007F:4E39'  # 127.0.0.1:20025
        for row in Path('/proc/net/tcp').read_text().splitlines()[1:]:
            fields = row.split()
            if len(fields) > 7 and fields[1].upper() == expected and fields[3] == '0A':
                return int(fields[7]) == os.geteuid()
        return False

    async def ensure_engine(self):
        if self.owned_listener():
            return
        async with self._start_lock:
            if self.owned_listener():
                return
            binary = Path(str(self.ctx.get_config('adam_engine_bin') or '/home/zhu/Projects/apple-music-online/electron/dist/resources/engine')).expanduser()
            directory = Path(str(self.ctx.get_config('adam_data_dir') or '~/.config/adam/engine-data')).expanduser()
            if not binary.is_file() or not (directory / 'config.yaml').is_file():
                raise AdamError('Adam 引擎或配置不存在，请在设置中配置 v1 使用的引擎路径和数据目录', 'not_configured')
            if self._engine_task is None or self._engine_task.done():
                env = dict(os.environ)
                env['LD_LIBRARY_PATH'] = str(binary.parent) + ':' + env.get('LD_LIBRARY_PATH', '')
                self._engine_task = self.ctx.spawn_task(self.ctx.exec_subprocess([str(binary), '--api', '20025'], cwd=directory, env=env), name='adam-engine')
            for _ in range(50):
                if self.owned_listener():
                    return
                if self._engine_task.done():
                    raise AdamError('Adam 引擎启动失败，请检查引擎配置')
                await asyncio.sleep(.1)
            raise AdamError('Adam 引擎启动超时', 'timeout')

    def _request(self, path: str, method: str, body, image: bool = False):
        if not self.owned_listener():
            raise AdamError('Adam 本机服务未运行或不属于当前用户')
        # Bootstrap transmits no credentials; the actual request pins this leaf.
        bootstrap = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        bootstrap.check_hostname = False
        bootstrap.verify_mode = ssl.CERT_NONE
        with socket.create_connection(('127.0.0.1', 20025), timeout=5) as raw:
            with bootstrap.wrap_socket(raw, server_hostname='127.0.0.1') as tls:
                cert = tls.getpeercert(binary_form=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_flags |= getattr(ssl, 'VERIFY_X509_PARTIAL_CHAIN', 0)
        context.verify_flags &= ~getattr(ssl, 'VERIFY_X509_STRICT', 0)
        context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert))
        conn = http.client.HTTPSConnection('127.0.0.1', 20025, context=context, timeout=30)
        try:
            conn.connect()
            if not hmac.compare_digest(cert, conn.sock.getpeercert(binary_form=True)):
                raise AdamError('Adam 证书已改变，请重试')
            conn.request(method, path, body=json.dumps(body) if body is not None else None,
                         headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'Accept-Language': 'zh-CN', 'Connection': 'close'})
            response = conn.getresponse()
            raw = response.read(8 * 1024 * 1024 + 1)
            if not 200 <= response.status < 300:
                # Do not echo upstream bodies: auth errors can contain account data.
                raise AdamError(f'Adam 请求失败（HTTP {response.status}）', 'unauthorized' if response.status in (401, 403) else 'upstream_error', response.status)
            if len(raw) > 8 * 1024 * 1024:
                raise AdamError('Adam 响应过大', 'invalid_response')
            if image:
                mime = response.getheader('Content-Type', '').split(';')[0].strip().lower()
                if mime not in {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}:
                    raise AdamError('Adam 封面响应不是图片', 'invalid_response')
                return 'data:' + mime + ';base64,' + base64.b64encode(raw).decode('ascii')
            return json.loads(raw) if raw else {}
        finally:
            conn.close()

    async def request(self, path, method='GET', body=None, *, image=False):
        await self.ensure_engine()
        return await asyncio.to_thread(self._request, path, method, body, image)

    def map_nodes(self, payload) -> list[MediaItem]:
        result, seen = [], set()
        def visit(node):
            if isinstance(node, list):
                for n in node: visit(n)
            elif isinstance(node, dict):
                typ, ident = node.get('type', ''), str(node.get('id') or '')
                attrs = node.get('attributes') or {}
                if typ in ('songs', 'music-videos', 'albums', 'playlists', 'artists', 'library-playlists') and ID.fullmatch(ident):
                    if (typ, ident) in seen: return
                    seen.add((typ, ident))
                    collection = typ not in ('songs', 'music-videos')
                    relationships = node.get('relationships') or {}
                    artists = (relationships.get('artists') or {}).get('data') or []
                    extra = {'apple_music_song_id': ident}
                    if collection:
                        extra['collection'] = {'id': ident, 'type': typ}
                    else:
                        extra['stream_resolver'] = resolver(ident)
                        if typ == 'music-videos':
                            extra['stream_resolver']['payload']['video'] = True
                    if artists:
                        extra['creator_id'] = str(artists[0].get('id') or '')
                    result.append(MediaItem(id=f'adam:{typ}:{ident}', source_id='plugin-music', kind='video' if typ == 'music-videos' else 'music', title=attrs.get('name') or ident, creator=attrs.get('artistName') or '', subtitle=attrs.get('albumName') or typ, cover_url=artwork((attrs.get('artwork') or {}).get('url', '')), play_url=attrs.get('url') or f'https://music.apple.com/cn/song/{ident}', duration_sec=float(attrs.get('durationInMillis') or 0)/1000, aspect='1:1', quality_label='Apple Music', source_badge='Apple Music', extra=extra))
                    return
                for value in node.values():
                    if isinstance(value, (dict, list)): visit(value)
        visit(payload)
        return result

    async def feed(self, category, query='', page=1):
        if category == 'search':
            if not query.strip(): return []
            client = self.catalog_client()
            _, data = await client.request('/v1/catalog/cn/search?' + urlencode({'term': query, 'types': 'songs,albums,artists,playlists,music-videos', 'limit': 25, 'offset': (page-1)*25}))
        elif category == 'library':
            if page > 1: return []
            data = await self.request('/api/v1/library/playlists')
            playlists = data.get('playlists') or []
            if not playlists:
                _, data = await self.catalog_client().request('/v1/me/library/playlists?limit=100')
            else:
                data = [{'id': p.get('lid'), 'type': 'library-playlists', 'attributes': {'name': p.get('name'), 'artwork': {'url': p.get('artworkUrl')}}} for p in playlists]
        else:
            suffix = '' if category == 'recommendations' else '/' + category
            data = await self.request('/api/v1/recommendations' + suffix + '?' + urlencode({'limit': 10, 'offset': (page-1)*10}))
        return self.map_nodes(data)

    async def sync_library(self):
        client = self.catalog_client()
        body = {'playlists': [], 'songs': [], 'albums': []}
        for typ in ('playlists', 'songs'):
            path = f'/v1/me/library/{typ}?limit=100'
            for _ in range(20):
                _, data = await client.request(path)
                for node in data.get('data') or []:
                    a = node.get('attributes') or {}
                    row = {'lid': node.get('id'), 'name': a.get('name', '')}
                    if typ == 'playlists':
                        row.update(trackCount=a.get('trackCount', 0), artworkUrl=artwork((a.get('artwork') or {}).get('url', '')))
                    else:
                        row.update(cid=(a.get('playParams') or {}).get('catalogId', ''), artist=a.get('artistName', ''), album=a.get('albumName', ''), ms=a.get('durationInMillis', 0))
                    body[typ].append(row)
                path = str(data.get('next') or '')
                if not path: break
                if not path.startswith(f'/v1/me/library/{typ}?'):
                    raise AdamError('资料库分页地址无效', 'invalid_response')
            if path:
                raise AdamError('资料库超过本次同步上限，未覆盖本地缓存', 'limit_exceeded')
        await self.request('/api/v1/library/ingest', 'POST', body)
        return {'playlists': len(body['playlists']), 'songs': len(body['songs'])}

    async def handle(self, req):
        try:
            action = req.get('action', 'status')
            if action == 'status':
                return CapabilityResult.ok(await self.request('/api/v1/drm/status'))
            if action == 'sync_library':
                return CapabilityResult.ok(await self.sync_library())
            if action in ('authenticate', 'challenge', 'resend', 'logout'):
                paths = {'authenticate': 'drm/authenticate', 'challenge': 'drm/challenge', 'resend': 'drm/challenge/resend', 'logout': 'drm/logout', 'sync_library': 'library/sync'}
                body = {'email': str(req.get('email') or ''), 'password': str(req.get('password') or '')} if action == 'authenticate' else {'reply': str(req.get('code') or '')} if action == 'challenge' else {}
                return CapabilityResult.ok(await self.request('/api/v1/' + paths[action], 'POST', body))
            if action == 'fetch_items':
                category = str(req.get('category') or 'recommendations')
                if category not in CATEGORIES: raise AdamError('未知 Apple Music 分类', 'invalid_argument')
                items = await self.feed(category, str(req.get('query') or ''), max(1, int(req.get('page') or 1)))
                return CapabilityResult.ok({'items': [i.model_dump() for i in items]})
            if action == 'collection':
                ident, typ = str(req.get('id') or ''), str(req.get('type') or '')
                if not ID.fullmatch(ident) or typ not in ('albums', 'playlists', 'artists', 'library-playlists'):
                    raise AdamError('无效的 Apple Music 条目', 'invalid_argument')
                page = max(1, min(int(req.get('page') or 1), 100))
                offset = (page - 1) * 100
                has_more = False
                if typ == 'library-playlists':
                    # The AMP response carries artwork and catalog IDs which
                    # the older engine's lid/cid-only cache cannot supply.
                    try:
                        _, data = await self.catalog_client().request(f'/v1/me/library/playlists/{ident}/tracks?include=catalog&limit=100&offset={offset}')
                        has_more = bool(data.get('next'))
                        songs = []
                        for n in data.get('data') or []:
                            a = n.get('attributes') or {}
                            catalog = ((n.get('relationships') or {}).get('catalog') or {}).get('data') or []
                            if catalog:
                                songs.extend(catalog)
                            else:
                                cid = (a.get('playParams') or {}).get('catalogId')
                                if cid: songs.append({'id': cid, 'type': 'songs', 'attributes': a})
                        data = songs
                    except (AppleMusicApiError, GateError):
                        cached = await self.request(f'/api/v1/library/playlists/{ident}/tracks')
                        tracks = cached.get('tracks') or []
                        has_more = len(tracks) > offset + 100
                        data = [{'id': t.get('cid') or t.get('lid'), 'type': 'songs', 'attributes': {'name': t.get('name'), 'artistName': t.get('artist'), 'albumName': t.get('album'), 'durationInMillis': t.get('ms'), 'artwork': {'url': t.get('artworkUrl')}}} for t in tracks[offset:offset+100]]
                elif typ in ('albums', 'playlists'):
                    _, data = await self.catalog_client().request(f'/v1/catalog/cn/{typ}/{ident}/tracks?limit=100&offset={offset}&include=artists')
                    has_more = bool(data.get('next'))
                else:
                    data = await self.request(f'/api/v1/catalog/artists/{ident}?sf=cn')
                    data = [node.get('relationships', {}) for node in data.get('data', [])]
                return CapabilityResult.ok({'items': [i.model_dump() for i in self.map_nodes(data)], 'has_more': has_more})
            if action == 'cover':
                ident = str(req.get('item_id') or '')
                sf = str(req.get('storefront') or 'cn').lower()
                if not ID.fullmatch(ident) or not re.fullmatch(r'[a-z]{2}', sf):
                    raise AdamError('无效歌曲 ID 或地区', 'invalid_argument')
                key = sf + ':' + ident
                if key not in self._covers:
                    value = await self.request(f'/api/v1/artwork/{ident}?sf={sf}&size=600', image=True)
                    if len(self._covers) >= 64:
                        self._covers.pop(next(iter(self._covers)))
                    self._covers[key] = value
                return CapabilityResult.ok({'data_url': self._covers[key]})
            if action == 'resolve_stream':
                ident = str(req.get('item_id') or '')
                if not ID.fullmatch(ident): raise AdamError('无效歌曲 ID', 'invalid_argument')
                quality = str(req.get('quality') or 'hires')
                if quality not in ('hires', 'lossless', 'aac', 'atmos'): raise AdamError('无效音质', 'invalid_argument')
                body = {'assetId': ident, 'storefront': 'cn', 'capabilities': {'lossless': quality in ('hires', 'lossless'), 'hiRes': quality == 'hires', 'atmos': quality == 'atmos', 'video': bool(req.get('video'))}}
                try:
                    data = await self.request('/api/v1/playback', 'POST', body)
                except AdamError as exc:
                    if exc.status == 401 and self.catalog_client is not None:
                        creds = getattr(self.catalog_client(), '_creds', None)
                        dev = str(getattr(creds, 'developer_token', '') or '').strip()
                        mut = str(getattr(creds, 'user_token', '') or '').strip()
                        if dev and mut:
                            data = await self.request(
                                '/api/v1/playback',
                                'POST',
                                {**body, 'token': dev, 'mediaUserToken': mut},
                            )
                        else:
                            raise
                    elif exc.status in (500, 502, 503):
                        # The engine returns these statuses before exposing a session.
                        await asyncio.sleep(.5)
                        data = await self.request('/api/v1/playback', 'POST', body)
                    else:
                        # Never retry authentication failures or ambiguous timeouts.
                        raise
                session = str(data.get('sessionId') or '')
                if not ID.fullmatch(session): raise AdamError('Adam 没有返回播放会话', 'invalid_response')
                self.sessions.append(session)
                if len(self.sessions) > 4:
                    old = self.sessions.pop(0)
                    try: await self.request('/api/v1/playback/' + old, 'DELETE')
                    except (AdamError, OSError): pass
                base = f'https://127.0.0.1:20025/api/v1/playback/{session}'
                video = data.get('type') == 'mv'
                return CapabilityResult.ok({'play_url': base + ('/vseg/manifest' if video else '/audio'), 'audio_url': base + '/audio' if video else '', 'source': 'adam', 'quality': f"{data.get('codec', quality)} · {data.get('bitDepth', 0)}bit / {data.get('sampleRate', 0)}Hz", 'duration_sec': float(data.get('durationMs') or 0)/1000})
            raise AdamError('未知 Adam 动作', 'invalid_argument')
        except AdamError as exc:
            return CapabilityResult.fail(str(exc), error_code=exc.code)
        except (AppleMusicApiError, GateError) as exc:
            return CapabilityResult.fail('Apple Music 曲库或账号暂不可用，请检查登录状态', error_code=exc.error_code)
        except (OSError, ValueError, TypeError, http.client.HTTPException):
            return CapabilityResult.fail('Adam 连接或响应无效，请检查引擎及登录状态', error_code='service_offline')
