"""Read native lyrics through the already-running, same-user Adam engine.

The engine creates a new loopback certificate on each launch. Capture that leaf
only after checking the listening socket's Linux UID, then perform a second,
verified TLS connection with that exact leaf as its sole trust anchor. No
credentials, external hosts, redirects, or audio endpoints are accessed here.
"""
from __future__ import annotations

import hmac
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit


HOST = '127.0.0.1'
PORT = 20025
MAX_LYRICS_BYTES = 1024 * 1024
TIMEOUT = 2.5
_PLAYER = re.compile(r'^org\.mpris\.MediaPlayer2\.adam(?:\.[A-Za-z0-9_.-]+)?$')
_TRACK = re.compile(r'^/com/apple/music/track/([0-9]{1,24}|i_[A-Za-z0-9_]{1,128})$')
_library_lock = threading.Lock()
_library_cache = {}
_library_cache_until = 0


class LyricsUnavailable(ValueError):
    """A missing catalog result permits a bounded region fallback."""



def _listener_owned_by_user():
    """Accept an IPv4 loopback listener belonging to this effective user only."""
    expected = f'0100007F:{PORT:04X}'
    try:
        # Reading /proc does not need process enumeration, privileges, or ps.
        for row in Path('/proc/net/tcp').read_text(encoding='ascii').splitlines()[1:]:
            fields = row.split()
            if len(fields) >= 10 and fields[1].upper() == expected and fields[3] == '0A':
                return int(fields[7]) == os.geteuid()
    except (OSError, ValueError):
        pass
    return False


def _capture_certificate():
    # This unauthenticated handshake transmits no HTTP request or credentials.
    # The captured leaf is subsequently validated and pinned on the real request.
    bootstrap = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    bootstrap.check_hostname = False
    bootstrap.verify_mode = ssl.CERT_NONE
    bootstrap.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection((HOST, PORT), timeout=TIMEOUT) as raw:
        with bootstrap.wrap_socket(raw, server_hostname=HOST) as tls:
            cert = tls.getpeercert(binary_form=True)
    if not cert or len(cert) > 65536:
        raise ValueError('Adam 本机证书不可用')
    return cert


def _trusted_context(cert):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    # Adam's generated leaf is intentionally not a CA and has no AKI. Keep
    # date/hostname/signature checks, treating this exact leaf as the trust anchor.
    context.verify_flags |= getattr(ssl, 'VERIFY_X509_PARTIAL_CHAIN', 0)
    context.verify_flags &= ~getattr(ssl, 'VERIFY_X509_STRICT', 0)
    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(cert))
    return context


def _fetch_lrc(catalog_id, storefront=None):
    if not re.fullmatch(r'[0-9]{1,24}', str(catalog_id)):
        raise ValueError('Adam 歌曲编号无效')
    if storefront is not None and not re.fullmatch(r'[a-z]{2}', str(storefront)):
        raise ValueError('Apple Music 歌词地区无效')
    query = 'format=lrc' + (f'&sf={storefront}' if storefront else '')
    return _fetch_text(f'/api/v1/lyrics/{catalog_id}?{query}')


def _fetch_text(path, *, accept='text/plain', deadline=None):
    timeout = TIMEOUT if deadline is None else min(TIMEOUT, deadline - time.monotonic())
    if timeout <= 0:
        raise TimeoutError('Adam 本机歌词服务响应超时')
    if not _listener_owned_by_user():
        raise ValueError('Adam 本机歌词服务未运行或不属于当前用户')
    cert = _capture_certificate()
    connection = http.client.HTTPSConnection(HOST, PORT, timeout=timeout,
                                              context=_trusted_context(cert))
    try:
        connection.connect()
        actual = connection.sock.getpeercert(binary_form=True)
        if not hmac.compare_digest(cert, actual or b''):
            raise ssl.SSLError('Adam 本机证书已改变，请重试')
        connection.request('GET', path,
                           headers={'Accept': accept, 'Accept-Language': 'zh-CN,zh;q=0.9',
                                    'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = connection.getresponse()
        if response.status == 404:
            raise LyricsUnavailable('Apple Music 当前默认地区未返回歌词（404）')
        if response.status != 200:
            raise ValueError(f'Apple Music 暂无原生歌词（{response.status}）')
        length = response.getheader('Content-Length')
        if length is not None and (int(length) < 0 or int(length) > MAX_LYRICS_BYTES):
            raise ValueError('Adam 原生歌词过大')
        # read1 returns after a single buffered/network read so the monotonic
        # deadline also bounds a response that keeps dripping small chunks.
        end = min(time.monotonic() + TIMEOUT, deadline) if deadline is not None else time.monotonic() + TIMEOUT
        parts, size = [], 0
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Adam 本机歌词服务响应超时')
            if connection.sock is not None:
                connection.sock.settimeout(min(TIMEOUT, remaining))
            chunk = response.read1(min(65536, MAX_LYRICS_BYTES + 1 - size))
            if not chunk:
                break
            parts.append(chunk)
            size += len(chunk)
            if size > MAX_LYRICS_BYTES:
                raise ValueError('Adam 原生歌词过大')
        text = b''.join(parts).decode('utf-8-sig').strip()
        if not text or '\x00' in text:
            raise ValueError('Adam 原生歌词为空或格式无效')
        return text
    finally:
        connection.close()


def _library_catalog_id(track_id):
    """Compatibility with running Adam versions that omit xesam:url.

    Use its existing same-user library API's exact lid→cid pairs. Only metadata
    is read, with bounded requests and a short in-memory cache; no credentials,
    encrypted library files, catalog searches or audio are involved.
    """
    global _library_cache, _library_cache_until
    if not re.fullmatch(r'i_[A-Za-z0-9_]{1,128}', track_id):
        return None
    with _library_lock:
        if time.monotonic() >= _library_cache_until:
            deadline = time.monotonic() + 8
            def read(path):
                value = json.loads(_fetch_text(path, accept='application/json', deadline=deadline))
                if not isinstance(value, dict):
                    raise ValueError('Adam 资料库响应格式无效')
                return value
            playlists = read('/api/v1/library/playlists').get('playlists') or []
            if not isinstance(playlists, list) or len(playlists) > 128:
                raise ValueError('Adam 资料库歌单列表过大或无效')
            mapping = {}
            for playlist in playlists:
                pid = playlist.get('lid') if isinstance(playlist, dict) else None
                if not isinstance(pid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]{0,127}', pid):
                    continue
                tracks = read(f'/api/v1/library/playlists/{pid}/tracks').get('tracks') or []
                if not isinstance(tracks, list):
                    raise ValueError('Adam 资料库歌曲列表无效')
                for item in tracks:
                    if not isinstance(item, dict):
                        continue
                    lid, cid = str(item.get('lid', '')), str(item.get('cid', ''))
                    if re.fullmatch(r'i\.[A-Za-z0-9]{1,128}', lid) and re.fullmatch(r'[0-9]{1,24}', cid):
                        key = lid.replace('.', '_')
                        # Conflicting mappings are ambiguous: never pick a version.
                        mapping[key] = cid if key not in mapping or mapping[key] == cid else None
                if len(mapping) > 50_000:
                    raise ValueError('Adam 资料库歌曲列表过大')
            _library_cache, _library_cache_until = mapping, time.monotonic() + 60
        return _library_cache.get(track_id)


def enrich_track(track, state=None):
    """Return a copied track with native LRC when this is an Adam MPRIS song.

    Apple Chinese translations may precede the original at identical timestamps;
    preserve the LRC exactly so the lyrics repository can pair those lines.
    Existing lyrics always take precedence, and a missing engine is harmless.
    """
    result = dict(track)
    state = state if isinstance(state, dict) else {}
    if result.get('lyrics') or state.get('backend') != 'mpris' or not _PLAYER.fullmatch(str(state.get('player', ''))):
        return result
    match = _TRACK.fullmatch(str(result.get('id', '')))
    if not match:
        return result
    try:
        catalog_id = match.group(1)
        if not catalog_id.isdigit():
            catalog_id = _catalog_id_from_url(result.get('url')) or _library_catalog_id(catalog_id)
            if not catalog_id:
                return result
        try:
            result['lyrics'] = _fetch_lrc(catalog_id)
        except LyricsUnavailable:
            # Adam's default can be a numeric DRM storefront, while its
            # browser requests use two-letter catalog regions. Prefer an
            # explicit track region; this Chinese UI has one bounded CN
            # fallback for tracks without a region in MPRIS metadata.
            region = str(result.get('storefront') or state.get('storefront') or '').lower()
            if not re.fullmatch(r'[a-z]{2}', region):
                url = urlsplit(str(result.get('url') or ''))
                region = url.path.strip('/').split('/')[0].lower() if url.hostname == 'music.apple.com' else ''
            result['lyrics'] = _fetch_lrc(catalog_id, region if re.fullmatch(r'[a-z]{2}', region) else 'cn')
        result['lyrics_source'] = 'Apple Music 原生歌词'
        result.pop('lyrics_error', None)
    except (OSError, ValueError, UnicodeError, http.client.HTTPException) as exc:
        result['lyrics_error'] = str(exc)[:200]
    return result


def _catalog_id_from_url(value):
    try:
        url = urlsplit(str(value or ''))
        if url.scheme != 'https' or url.hostname != 'music.apple.com' or url.username or url.password:
            return None
        parts = url.path.strip('/').split('/')
        if parts and re.fullmatch(r'[a-z]{2}', parts[0]):
            parts = parts[1:]
        if len(parts) < 2 or parts[0] not in {'song', 'album'}:
            return None
        selected = parse_qs(url.query).get('i', [])
        # Album URLs identify the song via ?i=; the path's ID is the album.
        value = selected[0] if len(selected) == 1 else parts[-1] if not selected and parts[0] == 'song' else ''
        return value if re.fullmatch(r'[0-9]{1,24}', value) else None
    except ValueError:
        return None
