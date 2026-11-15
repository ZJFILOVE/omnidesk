#!/usr/bin/env python3
"""Unit tests for plugins/omnidesk-plugin-music/workers/apple_music_writeback.py."""
from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = PLUGIN_ROOT / 'workers' / 'apple_music_writeback.py'
UPSTREAM_SCRIPTS = Path.home() / '.local/share/omnidesk/curation/repos/music-daily/scripts'


class _MockAmpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, handler_cls):
        super().__init__(('127.0.0.1', 0), handler_cls)
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.catalog_songs: dict[str, dict] = {}
        self.playlists: dict[str, dict] = {}
        self.list_playlists_status: int = 200
        self.next_playlist_seq = 1


def _make_handler(server_ref: _MockAmpServer):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, format, *args):
            return

        def _send_json(self, status: int, body: dict):
            raw = json.dumps(body).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _record(self, method: str, path: str, body=None):
            with server_ref.lock:
                server_ref.requests.append({
                    'method': method,
                    'path': path,
                    'authorization': self.headers.get('Authorization', ''),
                    'music_user_token': self.headers.get('Music-User-Token', ''),
                    'media_user_token': self.headers.get('Media-User-Token', ''),
                    'cookie': self.headers.get('Cookie', ''),
                    'body': body,
                })

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            self._record('GET', self.path)

            if parsed.path == '/v1/catalog/cn/songs':
                qs = urllib.parse.parse_qs(parsed.query)
                raw_ids = qs.get('ids', [''])[0]
                ids = [s for s in raw_ids.split(',') if s]
                data = []
                with server_ref.lock:
                    for sid in ids:
                        if sid in server_ref.catalog_songs:
                            data.append(server_ref.catalog_songs[sid])
                self._send_json(200, {'data': data})
                return

            if parsed.path == '/v1/me/library/playlists':
                with server_ref.lock:
                    if server_ref.list_playlists_status != 200:
                        self._send_json(server_ref.list_playlists_status, {'errors': [{'status': str(server_ref.list_playlists_status)}]})
                        return
                    data = [
                        {
                            'id': pid,
                            'type': 'library-playlists',
                            'attributes': {'name': p['name'], 'canEdit': p.get('canEdit', True)},
                        }
                        for pid, p in server_ref.playlists.items()
                    ]
                self._send_json(200, {'data': data})
                return

            if parsed.path.startswith('/v1/me/library/playlists/') and parsed.path.endswith('/tracks'):
                pid = parsed.path[len('/v1/me/library/playlists/'):-len('/tracks')]
                with server_ref.lock:
                    p = server_ref.playlists.get(pid)
                    if p is None:
                        self._send_json(404, {'errors': [{'status': '404'}]})
                        return
                    data = [
                        {
                            'id': f'i.{idx}.{sid}',
                            'type': 'library-songs',
                            'attributes': {'playParams': {'id': sid, 'catalogId': sid, 'kind': 'song'}},
                            'relationships': {
                                'catalog': {'data': [{'id': sid, 'type': 'songs'}]}
                            },
                        }
                        for idx, sid in enumerate(p['tracks'])
                    ]
                self._send_json(200, {'data': data})
                return

            if parsed.path.startswith('/v1/me/library/playlists/'):
                pid = parsed.path[len('/v1/me/library/playlists/'):]
                with server_ref.lock:
                    p = server_ref.playlists.get(pid)
                    if p is None:
                        self._send_json(404, {'errors': [{'status': '404'}]})
                        return
                    self._send_json(200, {
                        'data': [{
                            'id': pid,
                            'type': 'library-playlists',
                            'attributes': {'name': p['name'], 'canEdit': p.get('canEdit', True)},
                        }]
                    })
                return

            self._send_json(404, {'errors': [{'status': '404'}]})

        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0') or '0')
            raw = self.rfile.read(length) if length > 0 else b'{}'
            body = json.loads(raw.decode('utf-8'))
            self._record('POST', self.path, body)

            if self.path == '/v1/me/library/playlists':
                name = body.get('attributes', {}).get('name', '')
                track_entries = body.get('relationships', {}).get('tracks', {}).get('data', [])
                track_ids = [str(item['id']) for item in track_entries]
                with server_ref.lock:
                    pid = f'p.mock{server_ref.next_playlist_seq}'
                    server_ref.next_playlist_seq += 1
                    server_ref.playlists[pid] = {
                        'name': name,
                        'canEdit': True,
                        'tracks': list(track_ids),
                        'description': body.get('attributes', {}).get('description', ''),
                    }
                self._send_json(201, {
                    'data': [{
                        'id': pid,
                        'type': 'library-playlists',
                        'attributes': {'name': name, 'canEdit': True},
                    }]
                })
                return

            if self.path.startswith('/v1/me/library/playlists/') and self.path.endswith('/tracks'):
                pid = self.path[len('/v1/me/library/playlists/'):-len('/tracks')]
                track_ids = [str(item['id']) for item in body.get('data', [])]
                with server_ref.lock:
                    p = server_ref.playlists.get(pid)
                    if p is None:
                        self._send_json(404, {'errors': [{'status': '404'}]})
                        return
                    p['tracks'].extend(track_ids)
                self._send_json(204, {})
                return

            self._send_json(404, {'errors': [{'status': '404'}]})

    return Handler


def _song_node(sid: str, title: str, streamable: bool = True) -> dict:
    return {
        'id': sid,
        'type': 'songs',
        'attributes': {
            'name': title,
            'artistName': 'Test Artist',
            'albumName': 'Test Album',
            'durationInMillis': 210000,
            'hasLyrics': True,
            'isStreamable': streamable,
            'audioTraits': ['lossless'],
            'artwork': {'url': 'https://example.com/{w}x{h}.jpg'},
            'playParams': {'id': sid, 'kind': 'song'},
        },
    }


def _run_rpc(payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(WORKER_PATH), '--stdin-rpc'],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def _create_music_repo(root: Path, day: str = '2026-11-10', song_ids: list[str] | None = None) -> str:
    song_ids = song_ids or ['1001', '1002', '1003', '1004', '1005']
    shutil.copytree(UPSTREAM_SCRIPTS, root / 'scripts', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

    tracks = []
    recommended = {}
    for pos, sid in enumerate(song_ids, start=1):
        artist = f'Artist {pos}'
        title = f'Track {sid}'
        cid = f'{artist}::{title}'
        tracks.append({
            'position': pos,
            'canonical_id': cid,
            'artist': artist,
            'title': title,
            'apple_music': {
                'storefront': 'cn',
                'song_id': sid,
                'url': f'https://music.apple.com/cn/song/{sid}',
            },
        })
        recommended[cid] = {
            'count': 1,
            'first_recommended': day,
            'last_recommended': day,
        }

    manifest_path = root / 'recommendations' / day[:4] / day[5:7] / f'{day}.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({
        'schema_version': 1,
        'provider': 'apple_music',
        'date': day,
        'tracks': tracks,
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    state_path = root / 'state' / 'tracks.json'
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        'schema_version': 1,
        'provider': 'apple_music',
        'recommended': recommended,
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    _git(root, 'init', '-b', 'main')
    _git(root, 'config', 'user.name', 'Test User')
    _git(root, 'config', 'user.email', 'test@example.com')
    _git(root, 'add', '.')
    _git(root, 'commit', '-m', 'Initial music snapshot')
    return _git(root, 'rev-parse', 'HEAD')


@unittest.skipUnless(UPSTREAM_SCRIPTS.is_dir(), 'Complete upstream music-daily/scripts checkout required')
class AppleMusicWritebackTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix='am_wb_test_'))
        self.server = _MockAmpServer(None)
        self.server.RequestHandlerClass = _make_handler(self.server)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f'http://127.0.0.1:{self.server.server_address[1]}'

        for sid in ('1001', '1002', '1003', '1004', '1005', '2001', '2002', '2003'):
            self.server.catalog_songs[sid] = _song_node(sid, f'Song {sid}', streamable=True)
        self.server.catalog_songs['9999'] = _song_node('9999', 'Blocked Song', streamable=False)

        self.server.playlists['p.YJXV7dEIeb3GzZX'] = {
            'name': 'ChatGPT Daily 5',
            'canEdit': True,
            'tracks': ['1001', '1002'],
        }
        self.repo = self.tmpdir / 'music-daily'
        self.repo.mkdir()
        self.head = _create_music_repo(self.repo, day='2026-11-10', song_ids=['1001', '1002', '1003', '1004', '1005'])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_preflight_cn_catalog_checks_headers_and_streamability(self):
        res = _run_rpc({
            'op': 'preflight_cn_catalog',
            'music_repo': str(self.repo),
            'base_url': self.base_url,
            'dev_token': 'dev-tok-123',
            'music_user_token': 'mut-tok-456',
            'track_ids': ['1001', '9999', '8888'],
            'storefront': 'cn',
            'no_wrapper': True,
        })
        self.assertTrue(res['ok'], res)
        data = res['data']
        self.assertFalse(data['all_playable'])
        self.assertEqual(data['verified_ids'], ['1001'])
        self.assertEqual(data['unplayable_ids'], ['9999', '8888'])
        self.assertEqual(data['writeback_backend'], 'music_daily_python')

        req = self.server.requests[-1]
        self.assertEqual(req['authorization'], 'Bearer dev-tok-123')
        self.assertEqual(req['music_user_token'], 'mut-tok-456')
        self.assertEqual(req['cookie'], 'media-user-token=mut-tok-456')

    def test_sync_daily_cloud_executes_upstream_and_is_idempotent(self):
        state_file = self.tmpdir / 'private' / 'daily-state.json'
        payload = {
            'op': 'sync_daily_cloud',
            'music_repo': str(self.repo),
            'base_url': self.base_url,
            'dev_token': 'dev-tok-123',
            'music_user_token': 'mut-tok-456',
            'no_wrapper': True,
            'state_file': str(state_file),
            'music_head': self.head,
            'cumulative_playlist_id': 'p.YJXV7dEIeb3GzZX',
            'dated_playlist_prefix': 'ChatGPT Daily 5 · ',
            'create_dated_playlist': True,
            'targets': ['1001', '1002', '1003', '1004', '1005'],
            'days': [{
                'date': '2026-11-10',
                'rel_path': 'recommendations/2026/11/2026-11-10.json',
                'song_ids': ['1001', '1002', '1003', '1004', '1005'],
            }],
        }

        res1 = _run_rpc(payload)
        self.assertTrue(res1['ok'], res1)
        d1 = res1['data']
        self.assertEqual(d1['writeback_backend'], 'music_daily_python')
        self.assertEqual(d1['added_ids'], ['1003', '1004', '1005'])
        self.assertEqual(d1['added_count'], 3)
        self.assertEqual(d1['total_cumulative_tracks'], 5)
        self.assertEqual(self.server.playlists['p.YJXV7dEIeb3GzZX']['tracks'], ['1001', '1002', '1003', '1004', '1005'])

        dated_playlists = [p for p in self.server.playlists.values() if p['name'] == 'ChatGPT Daily 5 · 2026-11-10']
        self.assertEqual(len(dated_playlists), 1)
        self.assertEqual(dated_playlists[0]['tracks'], ['1001', '1002', '1003', '1004', '1005'])
        self.assertIn('2026-11-10', dated_playlists[0]['description'])

        post_count_after_first = sum(1 for r in self.server.requests if r['method'] == 'POST')
        res2 = _run_rpc(payload)
        self.assertTrue(res2['ok'], res2)
        d2 = res2['data']
        self.assertEqual(d2['added_count'], 0)
        self.assertEqual(d2['already_present_count'], 5)
        post_count_after_second = sum(1 for r in self.server.requests if r['method'] == 'POST')
        self.assertEqual(post_count_after_first, post_count_after_second)

    def test_sync_daily_cloud_blocks_sync_all_dirty_repo_and_2026_11_06(self):
        state_file = self.tmpdir / 'private' / 'daily-state.json'
        base_payload = {
            'op': 'sync_daily_cloud',
            'music_repo': str(self.repo),
            'base_url': self.base_url,
            'dev_token': 'dev-tok-123',
            'music_user_token': 'mut-tok-456',
            'no_wrapper': True,
            'state_file': str(state_file),
            'music_head': self.head,
            'cumulative_playlist_id': 'p.YJXV7dEIeb3GzZX',
            'dated_playlist_prefix': 'ChatGPT Daily 5 · ',
            'targets': ['1001', '1002', '1003', '1004', '1005'],
            'days': [{
                'date': '2026-11-10',
                'rel_path': 'recommendations/2026/11/2026-11-10.json',
                'song_ids': ['1001', '1002', '1003', '1004', '1005'],
            }],
        }

        res_all = _run_rpc({**base_payload, 'sync_all': True})
        self.assertFalse(res_all['ok'])
        self.assertEqual(res_all['error_code'], 'unsupported_sync_all')

        res_bad_head = _run_rpc({**base_payload, 'music_head': '0' * 40})
        self.assertFalse(res_bad_head['ok'])
        self.assertEqual(res_bad_head['error_code'], 'publication_not_authorized')

        res_withdrawn = _run_rpc({
            **base_payload,
            'days': [{
                'date': '2026-11-06',
                'rel_path': 'recommendations/2026/11/2026-11-06.json',
                'song_ids': ['1001', '1002', '1003', '1004', '1005'],
            }],
        })
        self.assertFalse(res_withdrawn['ok'])
        self.assertEqual(res_withdrawn['error_code'], 'publication_not_authorized')

        (self.repo / 'dirty.txt').write_text('uncommitted\n', encoding='utf-8')
        res_dirty = _run_rpc(base_payload)
        self.assertFalse(res_dirty['ok'])
        self.assertEqual(res_dirty['error_code'], 'publication_not_authorized')

    def test_interactive_blocks_protected_daily_playlist_and_alias(self):
        cp_dir = self.tmpdir / 'checkpoints'
        self.server.playlists['p.alias99'] = {'name': 'ChatGPT Daily 5', 'canEdit': True, 'tracks': []}

        for bad in (
            {'playlist_name': 'OmniDesk AI 灵感电台', 'apple_playlist_id': 'p.YJXV7dEIeb3GzZX'},
            {'playlist_name': 'ChatGPT Daily 5', 'apple_playlist_id': ''},
            {'playlist_name': 'ChatGPT Daily 5 · 2026-11-10', 'apple_playlist_id': ''},
            {'playlist_name': 'Custom Name', 'apple_playlist_id': 'p.alias99'},
        ):
            res = _run_rpc({
                'op': 'write_interactive_cloud',
                'music_repo': str(self.repo),
                'base_url': self.base_url,
                'dev_token': 'dev-tok-123',
                'music_user_token': 'mut-tok-456',
                'no_wrapper': True,
                'checkpoint_dir': str(cp_dir),
                'requested_ids': ['2001', '2002'],
                **bad,
            })
            self.assertFalse(res['ok'], bad)
            self.assertEqual(res['error_code'], 'protected_daily_playlist')

    def test_interactive_creates_appends_and_fails_closed_on_404_list(self):
        cp_dir = self.tmpdir / 'checkpoints'
        base = {
            'op': 'write_interactive_cloud',
            'music_repo': str(self.repo),
            'base_url': self.base_url,
            'dev_token': 'dev-tok-123',
            'music_user_token': 'mut-tok-456',
            'no_wrapper': True,
            'checkpoint_dir': str(cp_dir),
            'playlist_name': 'OmniDesk AI 灵感电台',
        }

        self.server.list_playlists_status = 404
        res_404 = _run_rpc({**base, 'requested_ids': ['2001', '2002']})
        self.assertFalse(res_404['ok'])
        self.assertEqual(res_404['error_code'], 'python_writeback_failed')
        self.server.list_playlists_status = 200

        res1 = _run_rpc({**base, 'requested_ids': ['2001', '2002']})
        self.assertTrue(res1['ok'], res1)
        pid = res1['data']['apple_music_playlist_id']
        self.assertEqual(res1['data']['remote_track_ids'], ['2001', '2002'])
        self.assertEqual(res1['data']['writeback_backend'], 'music_daily_python')

        res2 = _run_rpc({**base, 'requested_ids': ['2002', '2003']})
        self.assertTrue(res2['ok'], res2)
        self.assertEqual(res2['data']['apple_music_playlist_id'], pid)
        self.assertEqual(res2['data']['remote_track_ids'], ['2001', '2002', '2003'])


if __name__ == '__main__':
    unittest.main()
