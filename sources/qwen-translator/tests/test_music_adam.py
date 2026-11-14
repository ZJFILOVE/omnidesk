"""Native lyric access stays on the same-user local engine and verifies TLS."""
import http.client
import io
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import music_adam


STATE = {'backend': 'mpris', 'player': 'org.mpris.MediaPlayer2.adam'}
TRACK = {'id': '/com/apple/music/track/123456', 'title': 'Song'}
LRC = '[00:01.00]中文原生翻译\n[00:01.00]Original line'


class FakeResponse:
    status = 200

    def __init__(self, data=LRC.encode(), *, status=200, length=None):
        self.data = io.BytesIO(data)
        self.status, self.length = status, length

    def getheader(self, name):
        return self.length

    def read1(self, size):
        return self.data.read(size)


class FakeConnection:
    def __init__(self, response=None, cert=b'certificate'):
        self.response = response or FakeResponse()
        self.sock = MagicMock()
        self.sock.getpeercert.return_value = cert
        self.requests, self.closed = [], False

    def connect(self):
        pass

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class MusicAdamTests(unittest.TestCase):
    def setUp(self):
        music_adam._library_cache = {}
        music_adam._library_cache_until = 0

    def test_library_track_uses_exact_catalog_url_and_keeps_mpris_seek_id(self):
        track = {**TRACK, 'id': '/com/apple/music/track/i_aJGY33YF3Z1Vkmg',
                 'url': 'https://music.apple.com/cn/song/123456'}
        with patch.object(music_adam, '_fetch_lrc', return_value=LRC) as fetch:
            result = music_adam.enrich_track(track, STATE)
        fetch.assert_called_once_with('123456')
        self.assertEqual(result['id'], track['id'])
        self.assertEqual(result['lyrics'], LRC)
        self.assertNotIn('lyrics', track)

    def test_catalog_song_urls_accept_album_selection_but_never_album_ids_or_other_hosts(self):
        urls = {
            'https://music.apple.com/song/123456': '123456',
            'https://music.apple.com/jp/song/title/123456': '123456',
            'https://music.apple.com/cn/album/title/999?i=123456': '123456',
            'https://music.apple.com/cn/album/title/999': None,
            'https://music.apple.com/cn/playlist/123456': None,
            'https://other.test/cn/song/123456': None,
            'https://music.apple.com.evil.test/song/123456': None,
            'https://user:secret@music.apple.com/song/123456': None,
            'https://music.apple.com/song/123456?i=1&i=2': None,
            'https://music.apple.com/song/i.library': None,
            'https://[': None,
            '': None,
        }
        for url, expected in urls.items():
            with self.subTest(url=url):
                self.assertEqual(music_adam._catalog_id_from_url(url), expected)

    def test_library_without_catalog_identity_does_not_guess_or_connect(self):
        with patch.object(music_adam, '_fetch_lrc') as fetch, \
                patch.object(music_adam, '_library_catalog_id', return_value=None):
            track = {**TRACK, 'id': '/com/apple/music/track/i_library'}
            self.assertEqual(music_adam.enrich_track(track, STATE), track)
        fetch.assert_not_called()

    def test_old_adam_library_mapping_is_exact_cached_and_uses_verified_local_requests(self):
        responses = [
            '{"playlists":[{"lid":"p.example"}]}',
            '{"tracks":[{"lid":"i.library","cid":"123456"},{"lid":"i.upload"}]}',
        ]
        with patch.object(music_adam, '_fetch_text', side_effect=responses) as read, \
                patch.object(music_adam, '_fetch_lrc', return_value=LRC) as fetch:
            track = {**TRACK, 'id': '/com/apple/music/track/i_library'}
            self.assertEqual(music_adam.enrich_track(track, STATE)['lyrics'], LRC)
            self.assertEqual(music_adam.enrich_track(track, STATE)['lyrics'], LRC)
            self.assertIsNone(music_adam._library_catalog_id('i.upload'))
            self.assertIsNone(music_adam._library_catalog_id('i_upload'))
        self.assertEqual(read.call_count, 2)
        self.assertEqual([c.args[0] for c in read.call_args_list],
                         ['/api/v1/library/playlists', '/api/v1/library/playlists/p.example/tracks'])
        self.assertTrue(all(c.args == ('123456',) for c in fetch.call_args_list))

    def test_library_mapping_rejects_unsafe_paths_ambiguous_ids_and_retries_failures(self):
        with patch.object(music_adam, '_fetch_text', side_effect=OSError('offline')):
            self.assertNotIn('lyrics', music_adam.enrich_track({**TRACK, 'id': '/com/apple/music/track/i_library'}, STATE))
        self.assertEqual(music_adam._library_cache_until, 0)
        responses = ['{"playlists":[{"lid":"../secret"},{"lid":"p.ok"}]}',
                     '{"tracks":[{"lid":"i.library","cid":"123"},{"lid":"i.library","cid":"456"}]}']
        with patch.object(music_adam, '_fetch_text', side_effect=responses) as read:
            self.assertIsNone(music_adam._library_catalog_id('i_library'))
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args.args[0], '/api/v1/library/playlists/p.ok/tracks')

    def test_library_song_uses_catalog_region_fallback(self):
        track = {**TRACK, 'id': '/com/apple/music/track/i_library',
                 'url': 'https://music.apple.com/jp/album/title/999?i=123456'}
        with patch.object(music_adam, '_fetch_lrc', side_effect=[music_adam.LyricsUnavailable('404'), LRC]) as fetch:
            result = music_adam.enrich_track(track, STATE)
        self.assertEqual(result['lyrics'], LRC)
        self.assertEqual(fetch.call_args_list[1].args, ('123456', 'jp'))

    def test_missing_default_region_retries_china_and_preserves_timed_lyrics(self):
        with patch.object(music_adam, '_fetch_lrc', side_effect=[music_adam.LyricsUnavailable('404'), LRC]) as fetch:
            result = music_adam.enrich_track(TRACK, STATE)
        self.assertEqual(result['lyrics'], LRC)
        self.assertEqual(fetch.call_args_list[0].args, ('123456',))
        self.assertEqual(fetch.call_args_list[1].args, ('123456', 'cn'))

    def test_missing_default_region_prefers_song_url_country(self):
        with patch.object(music_adam, '_fetch_lrc', side_effect=[music_adam.LyricsUnavailable('404'), LRC]) as fetch:
            music_adam.enrich_track({**TRACK, 'url': 'https://music.apple.com/jp/song/123456'}, STATE)
        self.assertEqual(fetch.call_args_list[1].args, ('123456', 'jp'))

    def test_storefront_is_validated_and_sent_to_local_endpoint(self):
        connection = self.fetch()
        self.assertEqual(music_adam._fetch_lrc('123456', 'cn'), LRC)
        self.assertEqual(connection.requests[0][0], ('GET', '/api/v1/lyrics/123456?format=lrc&sf=cn'))
        for region in ('../secret', 'cn&format=audio', 'CN', '143465'):
            with self.assertRaises(ValueError):
                music_adam._fetch_lrc('123456', region)

    def fetch(self, connection=None):
        connection = connection or FakeConnection()
        ownership = patch.object(music_adam, '_listener_owned_by_user', return_value=True)
        capture = patch.object(music_adam, '_capture_certificate', return_value=b'certificate')
        context = patch.object(music_adam, '_trusted_context', return_value='verified-context')
        factory = patch.object(music_adam.http.client, 'HTTPSConnection', return_value=connection)
        for item in (ownership, capture, context, factory):
            item.start()
            self.addCleanup(item.stop)
        return connection

    def test_enrichment_preserves_native_chinese_and_never_changes_original_track(self):
        with patch.object(music_adam, '_fetch_lrc', return_value=LRC) as fetch:
            result = music_adam.enrich_track(TRACK, STATE)
        fetch.assert_called_once_with('123456')
        self.assertEqual(result['lyrics'], LRC)
        self.assertEqual(result['lyrics_source'], 'Apple Music 原生歌词')
        self.assertNotIn('lyrics', TRACK)

    def test_non_adam_or_non_mpris_and_untrusted_track_ids_never_connect(self):
        with patch.object(music_adam, '_fetch_lrc') as fetch:
            for state in (None, {'backend': 'browser', 'player': STATE['player']},
                          {'backend': 'mpris', 'player': 'org.mpris.MediaPlayer2.spotify'},
                          {'backend': 'mpris', 'player': 'org.mpris.MediaPlayer2.adam_evil'}):
                self.assertEqual(music_adam.enrich_track(TRACK, state), TRACK)
            for track_id in ('123', '/com/apple/music/track/../credentials',
                             '/com/apple/music/track/1?audio=true', 'https://other.test/123'):
                track = {**TRACK, 'id': track_id}
                self.assertEqual(music_adam.enrich_track(track, STATE), track)
        fetch.assert_not_called()

    def test_existing_lyrics_win_and_expected_engine_failure_is_nonfatal(self):
        with patch.object(music_adam, '_fetch_lrc', side_effect=OSError('offline')) as fetch:
            existing = {**TRACK, 'lyrics': LRC}
            self.assertEqual(music_adam.enrich_track(existing, STATE), existing)
            fetch.assert_not_called()
            result = music_adam.enrich_track(TRACK, STATE)
            self.assertEqual(result['title'], TRACK['title'])
            self.assertIn('offline', result['lyrics_error'])
            self.assertNotIn('lyrics', result)

    def test_listener_ownership_requires_loopback_listening_and_current_user(self):
        def table(address='0100007F', state='0A', uid=1000):
            return f'header\n0: {address}:{music_adam.PORT:04X} 00000000:0000 {state} 0:0 0:0 0 {uid} 0 123\n'
        with patch.object(music_adam.os, 'geteuid', return_value=1000):
            for value, expected in ((table(), True), (table(uid=1001), False),
                                    (table(address='00000000'), False), (table(state='01'), False),
                                    ('header\ninvalid', False)):
                with patch.object(Path, 'read_text', return_value=value):
                    self.assertEqual(music_adam._listener_owned_by_user(), expected)

    def test_ownership_failure_does_not_attempt_tls(self):
        with patch.object(music_adam, '_listener_owned_by_user', return_value=False), \
                patch.object(music_adam, '_capture_certificate') as capture:
            result = music_adam.enrich_track(TRACK, STATE)
        capture.assert_not_called()
        self.assertNotIn('lyrics', result)

    def test_fixed_path_direct_https_and_chinese_language_header(self):
        connection = self.fetch()
        self.assertEqual(music_adam._fetch_lrc('123456'), LRC)
        music_adam.http.client.HTTPSConnection.assert_called_once_with(
            '127.0.0.1', 20025, timeout=music_adam.TIMEOUT, context='verified-context')
        args, kwargs = connection.requests[0]
        self.assertEqual(args, ('GET', '/api/v1/lyrics/123456?format=lrc'))
        self.assertTrue(kwargs['headers']['Accept-Language'].startswith('zh-CN'))
        self.assertNotIn('Authorization', kwargs['headers'])
        self.assertTrue(connection.closed)

    def test_pin_mismatch_sends_no_http_request_and_closes_socket(self):
        connection = self.fetch(FakeConnection(cert=b'changed certificate'))
        with self.assertRaises(ssl.SSLError):
            music_adam._fetch_lrc('123456')
        self.assertEqual(connection.requests, [])
        self.assertTrue(connection.closed)

    def test_redirects_are_errors_and_never_followed(self):
        connection = self.fetch(FakeConnection(FakeResponse(status=302)))
        result = music_adam.enrich_track(TRACK, STATE)
        self.assertNotIn('lyrics', result)
        self.assertIn('302', result['lyrics_error'])
        self.assertEqual(len(connection.requests), 1)

    def test_oversize_body_or_declared_length_is_rejected(self):
        for response in (FakeResponse(data=b'x' * (music_adam.MAX_LYRICS_BYTES + 1)),
                         FakeResponse(length=str(music_adam.MAX_LYRICS_BYTES + 1))):
            with self.subTest(length=response.length):
                connection = self.fetch(FakeConnection(response))
                with self.assertRaises(ValueError):
                    music_adam._fetch_lrc('123456')
                self.assertTrue(connection.closed)

    def test_trusted_context_has_certificate_and_hostname_verification(self):
        fake_context = SimpleNamespace(minimum_version=None, verify_mode=ssl.CERT_REQUIRED,
                                       check_hostname=True, verify_flags=ssl.VERIFY_X509_STRICT,
                                       load_verify_locations=MagicMock())
        with patch.object(music_adam.ssl, 'SSLContext', return_value=fake_context):
            context = music_adam._trusted_context(b'certificate')
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertFalse(context.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertTrue(context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN)
        context.load_verify_locations.assert_called_once_with(cadata=ssl.DER_cert_to_PEM_cert(b'certificate'))

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL needed for local TLS verification fixture')
    def test_real_verified_https_accepts_fresh_nonca_adam_style_leaf(self):
        # A local ephemeral server verifies the same self-signed, non-CA leaf
        # shape Adam generates, including its absent authority key identifier.
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                            'ec_paramgen_curve:P-256', '-nodes', '-days', '1', '-subj', '/CN=localhost',
                            '-addext', 'subjectAltName=IP:127.0.0.1',
                            '-addext', 'basicConstraints=critical,CA:FALSE',
                            '-addext', 'authorityKeyIdentifier=none',
                            '-keyout', str(key), '-out', str(cert)], check=True,
                           capture_output=True, timeout=5)
            requests = []
            class Handler(BaseHTTPRequestHandler):
                def handle(self):
                    try:
                        super().handle()
                    except (BrokenPipeError, ConnectionResetError):
                        # The certificate-only bootstrap intentionally closes
                        # without sending an HTTP request.
                        pass
                def do_GET(self):
                    requests.append(self.path)
                    body = LRC.encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(music_adam, 'PORT', server.server_port), \
                        patch.object(music_adam, '_listener_owned_by_user', return_value=True):
                    self.assertEqual(music_adam._fetch_lrc('123456'), LRC)
                self.assertEqual(requests, ['/api/v1/lyrics/123456?format=lrc'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
