#!/usr/bin/env python3
"""OmniDesk Apple Music write-back bridge & CLI directly reusing ZJFILOVE/music-daily scripts.

Reuses `apple_music_sync.py` and `daily_playlists_sync.py` from `ZJFILOVE/music-daily`
(preferring `<music_daily_dir>/scripts/` when available, falling back to the verbatim
copies bundled alongside this worker) while enforcing OmniDesk's dual-mode guardrails:
  - `daily_immutable`: authorized cumulative sync (`p.YJXV7dEIeb3GzZX`) + dated playlists (`ChatGPT Daily 5 · YYYY-MM-DD`)
  - `interactive_ephemeral`: user-prompted repeatable playlists (default `"OmniDesk AI 灵感电台"`), hard-blocked from `p.YJXV7dEIeb3GzZX`
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
import io
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
sys.dont_write_bytecode = True
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

PROTECTED_DAILY_PLAYLIST_ID = 'p.YJXV7dEIeb3GzZX'
PROTECTED_DAILY_PLAYLIST_NAME = 'ChatGPT Daily 5'
DEFAULT_INTERACTIVE_PLAYLIST = 'OmniDesk AI 灵感电台'
LOOPBACK_RE = re.compile(r'^http://127\.0\.0\.1:([0-9]{1,5})$')


class WritebackError(Exception):
    def __init__(self, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False, encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write('\n')
        tmp_name = fh.name
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, path)


DEFAULT_MUSIC_REPO = Path.home() / '.local/share/omnidesk/curation/repos/music-daily'


def load_upstream_modules(music_daily_dir=None):
    """Require the real upstream checkout, including its complete authorization helpers."""
    repo = Path(music_daily_dir or DEFAULT_MUSIC_REPO).expanduser().resolve()
    scripts = repo / 'scripts'
    required = ('apple_music_sync', 'daily_playlists_sync', 'local_sync_gate',
                'check_consistency', 'publication_gate', 'recording_history', 'recording_withdrawal')
    if not all((scripts / (name + '.py')).is_file() for name in required):
        raise WritebackError('upstream_dependency_missing', 'Complete music-daily scripts checkout is required')
    # This is a one-request CLI worker. Never import a stale module from a previous checkout.
    for name in required:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(scripts))
    try:
        for name in required:
            importlib.import_module(name)
        common = sys.modules['apple_music_sync']
        dated = sys.modules['daily_playlists_sync']
    finally:
        sys.path.remove(str(scripts))
    return common, dated


def is_protected_playlist(playlist_id: str, playlist_name: str, protected_ids: set[str] | None = None) -> bool:
    pid = (playlist_id or '').strip()
    name_low = (playlist_name or '').strip().lower()
    if pid == PROTECTED_DAILY_PLAYLIST_ID:
        return True
    if protected_ids and pid in protected_ids:
        return True
    if name_low == PROTECTED_DAILY_PLAYLIST_NAME.lower() or name_low.startswith('chatgpt daily 5'):
        return True
    return False


def collect_protected_ids(daily_state: dict | None) -> set[str]:
    out = {PROTECTED_DAILY_PLAYLIST_ID}
    if not isinstance(daily_state, dict):
        return out
    cum_id = daily_state.get('cumulative_playlist_id')
    if isinstance(cum_id, str) and cum_id.strip():
        out.add(cum_id.strip())
    dated = daily_state.get('dated_playlists')
    if isinstance(dated, dict):
        for item in dated.values():
            if isinstance(item, dict):
                did = item.get('playlist_id')
                if isinstance(did, str) and did.strip():
                    out.add(did.strip())
    return out


def resolve_auth(common, payload: dict, require_mut: bool) -> dict:
    dev = (payload.get('dev_token') or os.environ.get('APPLE_MUSIC_DEVELOPER_TOKEN') or '').strip()
    mut = (payload.get('music_user_token') or os.environ.get('APPLE_MUSIC_USER_TOKEN') or '').strip()
    no_wrapper = bool(payload.get('no_wrapper', False))

    if require_mut:
        if dev and mut:
            return {'dev_token': dev, 'music_user_token': mut}
        if not no_wrapper:
            try:
                return common.load_auth(no_wrapper=False)
            except Exception as exc:
                raise WritebackError('auth_required', 'Apple Music credentials are unavailable') from None
        raise WritebackError(
            'auth_required',
            'Apple Music developer token and Music-User-Token are required for cloud sync',
        )

    if dev:
        return {'dev_token': dev, 'music_user_token': mut}
    if not no_wrapper:
        try:
            return common.load_auth(no_wrapper=False)
        except Exception as exc:
            raise WritebackError('auth_required', 'Apple Music credentials are unavailable') from None
    raise WritebackError('auth_required', 'auth_required: Developer token required for live catalog verification')


def build_client(common, base_url_or_host: str, auth: dict, proxy: str = ''):
    target = (base_url_or_host or 'https://amp-api.music.apple.com').strip().rstrip('/')
    if LOOPBACK_RE.match(target):
        class LoopbackClient:
            is_loopback = True

            def __init__(self, base_url: str, auth_obj: dict):
                self.base_url = base_url
                self.host = '127.0.0.1'
                self.auth = auth_obj
                self.opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}),
                    common.NoRedirect(),
                )

            def request(self, path: str, method: str = 'GET', body=None):
                if not path.startswith('/v1/') or '..' in path or any(c in path for c in '\r\n#\\'):
                    raise RuntimeError('Refusing unexpected pagination path')
                url = self.base_url + path
                headers = {
                    'Authorization': 'Bearer ' + self.auth['dev_token'],
                    'Origin': 'https://music.apple.com',
                    'Accept': 'application/json',
                }
                if method == 'POST' or body is not None:
                    headers['Content-Type'] = 'application/json'
                mut = self.auth.get('music_user_token', '')
                if mut:
                    headers['Music-User-Token'] = mut
                    headers['Media-User-Token'] = mut
                    headers['Cookie'] = 'media-user-token=' + mut
                data = json.dumps(body).encode('utf-8') if body is not None else None
                req = urllib.request.Request(url, headers=headers, data=data, method=method)
                try:
                    with self.opener.open(req, timeout=12) as resp:
                        raw = resp.read()
                        return resp.status, json.loads(raw) if raw else {}
                except urllib.error.HTTPError as err:
                    raise common.ApiError(err.code) from None

            def collection(self, path: str):
                items = []
                visited = set()
                while path:
                    if path in visited:
                        raise RuntimeError('Pagination repeated')
                    visited.add(path)
                    _, data = self.request(path)
                    if not isinstance(data.get('data'), list) or data.get('errors'):
                        raise RuntimeError('Invalid Apple Music collection response')
                    items.extend(data['data'])
                    path = data.get('next')
                return items

            def playlist_tracks(self, playlist_id: str):
                return self.collection(f'/v1/me/library/playlists/{playlist_id}/tracks?limit=100&include=catalog')

        return LoopbackClient(target, auth)

    if target.startswith('https://'):
        parsed = urllib.parse.urlparse(target)
        host = parsed.hostname or ''
    else:
        host = target
    client = common.Client(host, auth)
    client.is_loopback = False
    if proxy:
        client.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy, 'https': proxy}),
            common.NoRedirect(),
        )
    return client


def verify_readback(common, client, playlist_id: str, expected: set[str]):
    if not getattr(client, 'is_loopback', False):
        return common.verify(client, playlist_id, expected)
    for attempt in range(3):
        try:
            tracks = client.playlist_tracks(playlist_id)
            ids = common.catalog_ids(tracks)
            if expected <= ids:
                return tracks, ids
        except common.ApiError as error:
            if error.status not in (404, 429, 500, 502, 503, 504):
                raise
        if attempt < 2:
            time.sleep(0.15)
    raise RuntimeError('Write not confirmed by readback; stopped without resending')


def list_library_playlists_safe(common, client):
    # A failed list (including 404) is never evidence that a playlist does not exist.
    return client.collection('/v1/me/library/playlists?limit=100')


def fetch_playlist_meta(common, client, playlist_id: str) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9._-]+', playlist_id or ''):
        raise WritebackError('apple_music_playlist_verification_failed', 'Invalid Apple Music library playlist ID')
    try:
        _, body = client.request(f'/v1/me/library/playlists/{playlist_id}')
    except common.ApiError as error:
        raise WritebackError('apple_music_playlist_verification_failed', f'AMP API HTTP {error.status}') from None
    data = body.get('data') if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != 1 or body.get('errors'):
        raise WritebackError('apple_music_playlist_verification_failed', 'Invalid playlist metadata response')
    node = data[0]
    attrs = node.get('attributes') if isinstance(node, dict) else None
    if (
        not isinstance(node, dict)
        or node.get('id') != playlist_id
        or node.get('type') != 'library-playlists'
        or not isinstance(attrs, dict)
        or not isinstance(attrs.get('name'), str)
        or not attrs.get('name').strip()
        or attrs.get('canEdit') is not True
    ):
        raise WritebackError(
            'apple_music_playlist_verification_failed',
            'Live playlist identity, name or edit permission could not be verified',
        )
    return {'id': playlist_id, 'name': attrs['name'], 'canEdit': True}


def format_quality_label(audio_traits) -> str:
    traits = set(audio_traits) if isinstance(audio_traits, list) else set()
    parts = []
    if 'hi-res-lossless' in traits:
        parts.append('Hi-Res Lossless')
    elif 'lossless' in traits:
        parts.append('ALAC')
    else:
        parts.append('AAC')
    if 'atmos' in traits:
        parts.append('Dolby Atmos')
    return ' · '.join(parts)


def preflight_catalog(common, client, track_ids: list[str], storefront: str = 'cn') -> dict:
    sf = (storefront or 'cn').strip().lower()
    if len(sf) != 2 or not sf.isalpha() or not all(isinstance(t, str) and t.isdigit() for t in track_ids):
        raise WritebackError('invalid_argument', 'Invalid storefront or numeric catalog song ID')
    if not track_ids:
        return {
            'storefront': sf,
            'requested_count': 0,
            'verified_count': 0,
            'all_playable': True,
            'verified_ids': [],
            'unplayable_ids': [],
            'tracks': [],
            'verification_source': 'apple_music_live_catalog',
            'writeback_backend': 'music_daily_python',
        }

    found: dict[str, dict] = {}
    for offset in range(0, len(track_ids), 100):
        batch = track_ids[offset:offset + 100]
        query = urllib.parse.urlencode({'ids': ','.join(batch)})
        try:
            _, body = client.request(f'/v1/catalog/{sf}/songs?{query}')
        except common.ApiError as error:
            raise WritebackError('catalog_preflight_failed', f'AMP API HTTP {error.status}') from None
        if not isinstance(body, dict) or body.get('errors') or not isinstance(body.get('data'), list):
            raise WritebackError('catalog_preflight_failed', 'Invalid live catalog response')
        for node in body['data']:
            if not isinstance(node, dict) or node.get('type') != 'songs':
                continue
            attrs = node.get('attributes')
            if not isinstance(attrs, dict):
                continue
            play_params = attrs.get('playParams')
            sid = str(node.get('id', ''))
            if (
                not isinstance(play_params, dict)
                or str(play_params.get('id', '')) != sid
                or play_params.get('kind') != 'song'
                or attrs.get('isStreamable', True) is False
            ):
                continue
            title = attrs.get('name', '')
            if not sid or not title:
                continue
            artwork_url = ''
            if isinstance(attrs.get('artwork'), dict):
                raw_url = attrs['artwork'].get('url', '')
                if isinstance(raw_url, str) and raw_url:
                    artwork_url = raw_url.replace('{w}', '500').replace('{h}', '500')
            traits = attrs.get('audioTraits') if isinstance(attrs.get('audioTraits'), list) else []
            found[sid] = {
                'id': sid,
                'title': title,
                'artist': attrs.get('artistName', ''),
                'album': attrs.get('albumName', ''),
                'storefront': sf,
                'duration_sec': float(attrs.get('durationInMillis', 0) or 0) / 1000.0,
                'has_lyrics': bool(attrs.get('hasLyrics', False)),
                'cover_url': artwork_url,
                'is_hires': 'hi-res-lossless' in traits,
                'is_lossless': ('lossless' in traits) or ('hi-res-lossless' in traits),
                'is_atmos': 'atmos' in traits,
                'quality_label': format_quality_label(traits),
                'url': f'https://music.apple.com/{sf}/song/{sid}',
            }

    verified_ids: list[str] = []
    unplayable_ids: list[str] = []
    tracks: list[dict] = []
    for sid in track_ids:
        tr = found.get(sid)
        if tr is None:
            unplayable_ids.append(sid)
        else:
            verified_ids.append(sid)
            tracks.append(tr)

    return {
        'storefront': sf,
        'requested_count': len(track_ids),
        'verified_count': len(tracks),
        'all_playable': len(tracks) == len(track_ids) and not unplayable_ids,
        'verified_ids': verified_ids,
        'unplayable_ids': unplayable_ids,
        'tracks': tracks,
        'verification_source': 'apple_music_live_catalog',
        'writeback_backend': 'music_daily_python',
    }


def private_state_path(raw, repo=None):
    path = Path(raw).expanduser().resolve()
    forbidden = [Path(__file__).resolve().parents[3], DEFAULT_MUSIC_REPO.resolve()]
    if repo is not None:
        forbidden.append(Path(repo).resolve())
    if any(path.is_relative_to(root) for root in forbidden):
        raise WritebackError('invalid_state_path', 'Private runtime checkpoints must be outside repositories')
    if path.exists() and not path.is_file():
        raise WritebackError('invalid_state_path', 'Private checkpoint is not a regular file')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def read_state(path):
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise WritebackError('invalid_checkpoint', 'Private checkpoint is invalid')
    return data


@contextmanager
def upstream_transport(common, client, auth):
    # Adapt only authentication and transport. Authorization remains the real upstream gate.
    with patch.object(common, 'load_auth', lambda no_wrapper=False: auth), \
         patch.object(common, 'Client', lambda host, credentials: client), \
         patch.object(common, 'verify', lambda c, pid, expected: verify_readback(common, c, pid, expected)):
        yield


def handle_sync_daily_cloud(common, dated_mod, payload):
    if payload.get('sync_all'):
        raise WritebackError('unsupported_sync_all', 'Live writes require one explicit authorized date')
    if payload.get('cumulative_playlist_id', PROTECTED_DAILY_PLAYLIST_ID) != PROTECTED_DAILY_PLAYLIST_ID:
        raise WritebackError('invalid_target_playlist', 'Daily sync requires the protected cumulative playlist')
    if payload.get('dated_playlist_prefix', dated_mod.PREFIX) != dated_mod.PREFIX:
        raise WritebackError('invalid_target_playlist', 'Daily playlist prefix must match the upstream workflow')
    days = payload.get('days')
    if not isinstance(days, list) or len(days) != 1 or not isinstance(days[0], dict):
        raise WritebackError('publication_not_authorized', 'Exactly one authorized daily selection is required')
    day = days[0]
    repo = Path(payload['music_repo']).expanduser().resolve()
    state_file = private_state_path(payload['state_file'], repo)
    total_file = private_state_path(str(state_file) + '.upstream-total.json', repo)
    days_file = private_state_path(str(state_file) + '.upstream-days.json', repo)
    report_file = private_state_path(str(state_file) + '.upstream-report.json', repo)
    args = SimpleNamespace(repo=repo, date=day.get('date'), music_head=payload.get('music_head'),
                           execute=True, replace=[], no_wrapper=bool(payload.get('no_wrapper')),
                           api_host='amp-api.music.apple.com', playlist_id=PROTECTED_DAILY_PLAYLIST_ID,
                           create=False, state_file=days_file, report=report_file)
    gate = importlib.import_module('local_sync_gate')
    with common.state_lock(state_file), common.state_lock(total_file), common.state_lock(days_file):
        try:
            authorization = gate.authorize_args(args)
        except Exception:
            raise WritebackError('publication_not_authorized', 'Full local music history and snapshot authorization failed') from None
        if (authorization['song_ids'] != payload.get('targets')
                or authorization['song_ids'] != day.get('song_ids')
                or authorization['manifest_path'] != day.get('rel_path')
                or authorization['music_head'] != payload.get('music_head')):
            raise WritebackError('publication_not_authorized', 'Requested songs/date/path differ from the authorized snapshot')
        state = read_state(state_file)
        if not total_file.exists() and (state.get('pending_song_ids') or
                any(v.get('creation_pending') or v.get('pending_append')
                    for v in state.get('dated_playlists', {}).values() if isinstance(v, dict))):
            raise WritebackError('pending_write_unconfirmed', 'Legacy pending cloud write requires manual reconciliation')
        auth = resolve_auth(common, payload, require_mut=True)
        client = build_client(common, payload.get('base_url', 'https://amp-api.music.apple.com'), auth, payload.get('proxy', ''))
        pre = preflight_catalog(common, client, authorization['song_ids'], 'cn')
        if not pre['all_playable']:
            raise WritebackError('catalog_preflight_failed', 'Authorized songs must all be playable in CN catalog')
        meta = fetch_playlist_meta(common, client, PROTECTED_DAILY_PLAYLIST_ID)
        if meta['name'] != PROTECTED_DAILY_PLAYLIST_NAME:
            raise WritebackError('apple_music_playlist_verification_failed', 'Protected cumulative playlist name differs')
        before = common.ordered_catalog_ids(client.playlist_tracks(PROTECTED_DAILY_PLAYLIST_ID))
        # Every upstream mutation rechecks its own real authorization. Also check live identities
        # at the transport boundary, including existing dated playlists before append.
        original_request = client.request
        def guarded_request(path, method='GET', body=None):
            if method != 'GET':
                gate.recheck(authorization)
                if method != 'POST':
                    raise WritebackError('invalid_argument', 'Only missing-song append and dated creation are allowed')
                if path == '/v1/me/library/playlists':
                    if not payload.get('create_dated_playlist') or body.get('attributes', {}).get('name') != dated_mod.PREFIX + authorization['date']:
                        raise WritebackError('invalid_target_playlist', 'Unexpected upstream playlist creation')
                else:
                    match = re.fullmatch(r'/v1/me/library/playlists/([A-Za-z0-9._-]+)/tracks', path)
                    if not match:
                        raise WritebackError('invalid_target_playlist', 'Unexpected upstream playlist write')
                    live = fetch_playlist_meta(common, client, match[1])
                    expected_name = PROTECTED_DAILY_PLAYLIST_NAME if match[1] == PROTECTED_DAILY_PLAYLIST_ID else dated_mod.PREFIX + authorization['date']
                    if live['name'] != expected_name:
                        raise WritebackError('invalid_target_playlist', 'Live daily playlist identity differs')
                ids = [entry.get('id') for entry in body.get('data', body.get('relationships', {}).get('tracks', {}).get('data', []))]
                if not ids or any(sid not in authorization['song_ids'] for sid in ids):
                    raise WritebackError('publication_not_authorized', 'Upstream write differs from authorized recordings')
            return original_request(path, method, body)
        with upstream_transport(common, client, auth), patch.object(common, 'STATE', total_file), \
             patch.object(client, 'request', guarded_request), redirect_stdout(io.StringIO()):
            # The actual Action functions own missing-only append, pending checkpoints and readback.
            common.sync(args)
            if payload.get('create_dated_playlist'):
                dated_mod.sync(args)
        after_tracks, _ = verify_readback(common, client, PROTECTED_DAILY_PLAYLIST_ID, set(authorization['song_ids']))
        after = common.ordered_catalog_ids(after_tracks)
        dated_state = read_state(days_file).get('days', {})
        dated_record = dated_state.get(authorization['date'])
        if payload.get('create_dated_playlist'):
            if not dated_record or common.ordered_catalog_ids(client.playlist_tracks(dated_record['playlist_id'])) != authorization['song_ids']:
                raise WritebackError('readback_verification_failed', 'Dated playlist exact order is unconfirmed')
            state.setdefault('dated_playlists', {})[authorization['date']] = {**dated_record, 'track_ids': authorization['song_ids'], 'cloud_synced': True}
        state.setdefault('synced_dates', {})[authorization['date']] = {
            'business_date': authorization['date'], 'operation_id': 'music:' + authorization['date'],
            'music_head': authorization['music_head'], 'track_ids': authorization['song_ids'],
            'cloud_synced': True, 'verified_at': utc_now()}
        state.update(cumulative_track_ids=after, last_verified_music_head=authorization['music_head'], last_updated_at=utc_now())
        atomic_write_json(state_file, state)
        added = [sid for sid in authorization['song_ids'] if sid not in set(before)]
        return dict(mode='daily_immutable', cumulative_playlist_id=PROTECTED_DAILY_PLAYLIST_ID,
                    cumulative_playlist_name=PROTECTED_DAILY_PLAYLIST_NAME, business_date=authorization['date'],
                    music_head=authorization['music_head'], dates_synced=1, synced_dates=[authorization['date']],
                    added_ids=added, added_count=len(added), already_present_count=len(authorization['song_ids'])-len(added),
                    verified_count=len(authorization['song_ids']), total_cumulative_tracks=len(after),
                    readback_verified=True, cloud_synced=True, live_apple_music_synced=True, dry_run=False,
                    state_file=str(state_file), history_anomalies=payload.get('history_anomalies', []), writeback_backend='music_daily_python')


def handle_write_interactive_cloud(common, payload):
    requested_ids = payload['requested_ids']
    if not isinstance(requested_ids, list) or not requested_ids or len(requested_ids) != len(set(requested_ids)) or any(not isinstance(s, str) or not s.isdigit() for s in requested_ids):
        raise WritebackError('invalid_argument', 'Unique numeric catalog song IDs are required')
    name = (payload.get('playlist_name') or DEFAULT_INTERACTIVE_PLAYLIST).strip()
    playlist_id = (payload.get('apple_playlist_id') or '').strip()
    protected = collect_protected_ids(payload.get('daily_state'))
    if is_protected_playlist(playlist_id, name, protected):
        raise WritebackError('protected_daily_playlist', 'Interactive writes cannot target a daily playlist')
    # Lock by target, not selection: a retry with different songs cannot overwrite uncertainty.
    target_key = hashlib.sha256((playlist_id or name).encode()).hexdigest()
    default_root = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'omnidesk/curation/apple-music-interactive'
    root = Path(payload['checkpoint_dir']).expanduser().resolve() if payload.get('checkpoint_dir') else default_root
    checkpoint = private_state_path(root / (target_key + '.json'), payload.get('music_repo'))
    with common.state_lock(checkpoint):
        state = read_state(checkpoint)
        auth = resolve_auth(common, payload, require_mut=True)
        client = build_client(common, payload.get('base_url', 'https://amp-api.music.apple.com'), auth, payload.get('proxy', ''))
        pre = preflight_catalog(common, client, requested_ids, 'cn')
        if not pre['all_playable']:
            raise WritebackError('catalog_preflight_failed', 'Every interactive song must be playable in CN catalog')
        if not playlist_id:
            matches = [p for p in list_library_playlists_safe(common, client) if p.get('attributes', {}).get('name') == name]
            if len(matches) > 1:
                raise WritebackError('apple_music_playlist_verification_failed', 'Multiple matching playlists')
            playlist_id = str(matches[0].get('id', '')) if matches else ''
        if state.get('playlist_id'):
            if playlist_id != state['playlist_id']:
                raise WritebackError('pending_write_unconfirmed', 'Checkpoint playlist identity differs from live target')
        if state.get('pending'):
            if not playlist_id:
                raise WritebackError('pending_write_unconfirmed', 'Previous playlist creation is unresolved; refusing to resend')
            pending_tracks, _ = verify_readback(common, client, playlist_id, set(state['pending']))
            if state.get('creation_pending') and common.ordered_catalog_ids(pending_tracks) != state['pending']:
                raise WritebackError('pending_write_unconfirmed', 'Previous creation exact order is unconfirmed')
            state.update(pending=[], creation_pending=False, playlist_id=playlist_id)
            atomic_write_json(checkpoint, state)
        if playlist_id:
            meta = fetch_playlist_meta(common, client, playlist_id)
            if is_protected_playlist(meta['id'], meta['name'], protected):
                raise WritebackError('protected_daily_playlist', 'Live target is a protected daily playlist')
            remote = common.ordered_catalog_ids(client.playlist_tracks(playlist_id))
            missing = [sid for sid in requested_ids if sid not in set(remote)]
        else:
            state = {'pending': requested_ids[:25], 'creation_pending': True, 'playlist_name': name}
            atomic_write_json(checkpoint, state)
            _, response = client.request('/v1/me/library/playlists', 'POST', {
                'attributes': {'name': name, 'description': 'OmniDesk AI 灵感电台'},
                'relationships': {'tracks': {'data': [{'id': sid, 'type': 'songs'} for sid in requested_ids[:25]]}}})
            playlist_id = response['data'][0]['id']
            state['playlist_id'] = playlist_id
            atomic_write_json(checkpoint, state)
            verify_readback(common, client, playlist_id, set(requested_ids[:25]))
            state.update(pending=[], creation_pending=False)
            atomic_write_json(checkpoint, state)
            missing = requested_ids[25:]
        for offset in range(0, len(missing), 25):
            batch = missing[offset:offset+25]
            meta = fetch_playlist_meta(common, client, playlist_id)
            if is_protected_playlist(meta['id'], meta['name'], protected):
                raise WritebackError('protected_daily_playlist', 'Live target is a protected daily playlist')
            state.update(playlist_id=playlist_id, pending=batch)
            atomic_write_json(checkpoint, state)
            client.request(f'/v1/me/library/playlists/{playlist_id}/tracks', 'POST', {'data': [{'id': sid, 'type': 'songs'} for sid in batch]})
            verify_readback(common, client, playlist_id, set(batch))
            state['pending'] = []
            atomic_write_json(checkpoint, state)
        tracks, _ = verify_readback(common, client, playlist_id, set(requested_ids))
        return dict(apple_music_playlist_id=playlist_id, playlist_name=name,
                    remote_track_ids=common.ordered_catalog_ids(tracks), verified_tracks=pre['tracks'],
                    verified_count=len(requested_ids), readback_verified=True, cloud_synced=True,
                    live_apple_music_synced=True, writeback_backend='music_daily_python')


def dispatch_rpc(payload: dict) -> dict:
    common, dated_mod = load_upstream_modules(payload.get('music_repo'))
    op = payload.get('op', '')
    if op == 'preflight_cn_catalog':
        auth = resolve_auth(common, payload, require_mut=False)
        client = build_client(common, payload.get('base_url', 'https://amp-api.music.apple.com'), auth, payload.get('proxy', ''))
        return {'ok': True, 'data': preflight_catalog(common, client, list(payload['track_ids']), payload.get('storefront', 'cn'))}
    if op == 'sync_daily_cloud':
        return {'ok': True, 'data': handle_sync_daily_cloud(common, dated_mod, payload)}
    if op == 'write_interactive_cloud':
        return {'ok': True, 'data': handle_write_interactive_cloud(common, payload)}
    raise WritebackError('invalid_argument', f'Unsupported writeback op: {op}')


def run_workflow_cli(args) -> int:
    """Directly execute upstream apple_music_sync.py + daily_playlists_sync.py like apple-music-playlists.yml."""
    repo = args.repo.expanduser().resolve()
    common, dated_mod = load_upstream_modules(repo)
    state_dir = args.state_dir.expanduser().resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    head = args.music_head
    if not head:
        head = subprocess.check_output(['/usr/bin/git', 'rev-parse', 'HEAD'], cwd=str(repo), text=True).strip()

    cum_argv = [
        '--repo', str(repo),
        '--state-file', str(state_dir / 'total.json'),
        '--playlist-id', PROTECTED_DAILY_PLAYLIST_ID,
        '--api-host', args.api_host,
        '--date', args.date,
        '--music-head', head,
    ]
    if args.no_wrapper:
        cum_argv.append('--no-wrapper')
    if args.execute:
        cum_argv.append('--execute')
    common.main(cum_argv)

    if args.create_dated:
        dated_argv = [
            '--repo', str(repo),
            '--state-file', str(state_dir / 'days.json'),
            '--report', str(state_dir / 'dated-playlists.json'),
            '--api-host', args.api_host,
            '--date', args.date,
            '--music-head', head,
        ]
        if args.no_wrapper:
            dated_argv.append('--no-wrapper')
        if args.execute:
            dated_argv.append('--execute')
        dated_mod.main(dated_argv)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdin-rpc', action='store_true', help='Read JSON RPC request from stdin and emit JSON on stdout')
    sub = parser.add_subparsers(dest='cmd')

    p_pre = sub.add_parser('preflight', help='Verify Apple Music CN catalog playability')
    p_pre.add_argument('--ids', required=True, help='Comma-separated numeric song IDs')
    p_pre.add_argument('--storefront', default='cn')
    p_pre.add_argument('--api-host', default=os.environ.get('APPLE_MUSIC_API_HOST', 'amp-api.music.apple.com'))
    p_pre.add_argument('--no-wrapper', action='store_true')

    p_ix = sub.add_parser('write-interactive', help='Append repeatable recommendations to an interactive playlist')
    p_ix.add_argument('--playlist-name', default=DEFAULT_INTERACTIVE_PLAYLIST)
    p_ix.add_argument('--playlist-id', default='')
    p_ix.add_argument('--ids', required=True, help='Comma-separated numeric song IDs')
    p_ix.add_argument('--api-host', default=os.environ.get('APPLE_MUSIC_API_HOST', 'amp-api.music.apple.com'))
    p_ix.add_argument('--no-wrapper', action='store_true')
    p_ix.add_argument('--execute', action='store_true')

    p_wf = sub.add_parser('sync-workflow', help='Run upstream apple-music-playlists.yml workflow steps directly')
    p_wf.add_argument('--repo', type=Path, required=True, help='Path to clean ZJFILOVE/music-daily checkout')
    p_wf.add_argument('--date', required=True, help='Authorized publication date YYYY-MM-DD')
    p_wf.add_argument('--music-head', default='', help='Expected full commit SHA (defaults to HEAD)')
    p_wf.add_argument(
        '--state-dir',
        type=Path,
        default=Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'omnidesk/curation/apple-music-workflow-state',
    )
    p_wf.add_argument('--api-host', default=os.environ.get('APPLE_MUSIC_API_HOST', 'amp-api.music.apple.com'))
    p_wf.add_argument('--create-dated', action='store_true', default=True)
    p_wf.add_argument('--no-wrapper', action='store_true')
    p_wf.add_argument('--execute', action='store_true')

    args = parser.parse_args(argv)

    if args.stdin_rpc:
        try:
            req = json.load(sys.stdin)
            out = dispatch_rpc(req)
        except WritebackError as werr:
            out = {'ok': False, 'error_code': werr.code, 'error': werr.message, 'extra': werr.extra}
        except Exception as exc:
            msg = 'Apple Music write-back stopped; check private checkpoint, authorization and connectivity'
            out = {'ok': False, 'error_code': 'python_writeback_failed', 'error': msg, 'extra': {}}
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write('\n')
        sys.stdout.flush()
        return 0

    if args.cmd == 'preflight':
        ids = [s.strip() for s in args.ids.split(',') if s.strip()]
        res = dispatch_rpc({
            'op': 'preflight_cn_catalog',
            'track_ids': ids,
            'storefront': args.storefront,
            'base_url': f'https://{args.api_host}' if not args.api_host.startswith('http') else args.api_host,
            'no_wrapper': args.no_wrapper,
        })
        print(json.dumps(res['data'], ensure_ascii=False, indent=2))
        return 0 if res['data']['all_playable'] else 1

    if args.cmd == 'write-interactive':
        ids = [s.strip() for s in args.ids.split(',') if s.strip()]
        if is_protected_playlist(args.playlist_id, args.playlist_name):
            raise WritebackError(
                'protected_daily_playlist',
                f'Interactive mode cannot target protected playlist {PROTECTED_DAILY_PLAYLIST_ID}',
            )
        if not args.execute:
            print(json.dumps({
                'dry_run': True,
                'mode': 'interactive_ephemeral',
                'playlist_name': args.playlist_name,
                'planned_track_ids': ids,
            }, ensure_ascii=False, indent=2))
            return 0
        res = dispatch_rpc({
            'op': 'write_interactive_cloud',
            'playlist_name': args.playlist_name,
            'apple_playlist_id': args.playlist_id,
            'requested_ids': ids,
            'base_url': f'https://{args.api_host}' if not args.api_host.startswith('http') else args.api_host,
            'no_wrapper': args.no_wrapper,
        })
        print(json.dumps(res['data'], ensure_ascii=False, indent=2))
        return 0

    if args.cmd == 'sync-workflow':
        return run_workflow_cli(args)

    parser.print_help()
    return 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except WritebackError as werr:
        print(f'Stopped: {werr.code}: {werr.message}', file=sys.stderr, flush=True)
        sys.exit(1)
    except Exception as exc:
        # Never log tokens, cookies, or raw network payloads.
        message = 'Check configuration, manifests, connectivity, and private checkpoint'
        print(f'Stopped: {type(exc).__name__}: {message}', file=sys.stderr, flush=True)
        sys.exit(1)
