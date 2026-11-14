"""Waybar consumes JSON safely and controls the existing local music service."""
import contextlib
import importlib.util
import io
import html
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('waybar_music', Path(__file__).parents[1] / 'scripts' / 'waybar_music.py')
waybar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(waybar)


def state(**extra):
    return {'available': True, 'playing': True, 'position': 30, 'volume': .4,
            'track': {'title': '<Song & Name>', 'artist': 'Singer', 'duration': 120},
            'lyrics': {'current': {'text': 'Original line', 'translation': '中文翻译'}},
            'devices': [{'id': '20', 'description': 'Headphones', 'selected': True}], **extra}


class WaybarMusicTests(unittest.TestCase):
    def test_json_text_uses_translation_and_escapes_tooltip_markup(self):
        rendered = waybar.render(state())
        self.assertEqual(rendered['text'], '♫ &lt;Song &amp; Name&gt; · 中文翻译 [0:30/2:00]')
        self.assertIn('&lt;Song &amp; Name&gt;', rendered['tooltip'])
        self.assertIn('Original line', rendered['tooltip'])
        self.assertIn('0:30 / 2:00', rendered['tooltip'])
        self.assertIn('Headphones', rendered['tooltip'])
        self.assertIn('中键上一首', rendered['tooltip'])
        self.assertIn('右键下一首', rendered['tooltip'])
        self.assertNotIn('滚轮', rendered['tooltip'])
        self.assertEqual(rendered['percentage'], 25)
        self.assertEqual(json.loads(json.dumps(rendered, ensure_ascii=False)), rendered)

    def test_paused_wrapped_state_and_plain_track_fallback(self):
        rendered = waybar.render({'state': state(playing=False, lyrics={})})
        self.assertEqual(rendered['class'], 'paused')
        self.assertIn('Song', rendered['text'])
        self.assertTrue(rendered['text'].startswith('♫ Ⅱ'))
        idle = waybar.render({'available': False})
        self.assertEqual(idle['class'], 'idle')
        self.assertEqual(idle['text'], '♫')
        self.assertIn('等待', idle['tooltip'])
        self.assertEqual(waybar.render(state(available=False))['text'], '♫')

    def test_display_length_is_bounded_and_control_characters_removed(self):
        rendered = waybar.render(state(lyrics='a\n' * 100), max_length=10)
        self.assertNotIn('\n', rendered['text'])
        self.assertIn('…', rendered['text'])
        self.assertTrue(rendered['text'].endswith('[0:30/2:00]'))
        self.assertLessEqual(waybar._width(html.unescape(rendered['text'])), waybar.MIN_DISPLAY_WIDTH)

    def test_original_mode_and_lyric_only_options_are_independent(self):
        original = waybar.render(state(waybar={'display': 'original'}))
        self.assertIn('Original line', original['text'])
        self.assertNotIn('中文翻译', original['text'])
        for mode, expected in (('translation', '中文翻译'), ('original', 'Original line')):
            rendered = waybar.render(state(waybar={'display': mode, 'show_title': False, 'show_progress': False}))
            self.assertEqual(rendered['text'], '♫ ' + expected)
            self.assertIn('Song', rendered['tooltip'])
            self.assertIn('Original line', rendered['tooltip'])
            self.assertIn('中文翻译', rendered['tooltip'])

    def test_bilingual_mode_has_only_one_safe_markup_line_break_and_single_line_json(self):
        snapshot = state(waybar={'display': 'bilingual', 'show_title': False},
                         lyrics={'current': {'text': '<b>A & B</b>\nnext', 'translation': '<i>中文</i> & 后续'}})
        rendered = waybar.render(snapshot)
        self.assertEqual(rendered['text'], '<span size="small">&lt;b&gt;A &amp; B&lt;/b&gt; next</span>\n'
                                          '♫ &lt;i&gt;中文&lt;/i&gt; &amp; 后续 [0:30/2:00]')
        self.assertEqual(rendered['text'].count('\n'), 1)
        self.assertNotIn('<b>', rendered['text'])
        self.assertNotIn('<i>', rendered['text'])
        encoded = json.dumps(rendered, ensure_ascii=False)
        self.assertNotIn('\n', encoded)
        self.assertEqual(json.loads(encoded), rendered)
        self.assertEqual(rendered['percentage'], 25)

    def test_bilingual_falls_back_to_one_line_when_translation_absent_or_equal(self):
        for current in ({'text': 'English only'}, {'text': '中文原文', 'translation': '中文原文'},
                        {'translation': '只有中文'}):
            rendered = waybar.render(state(waybar={'display': 'bilingual'}, lyrics={'current': current}))
            self.assertNotIn('\n', rendered['text'])
            self.assertNotIn('<span', rendered['text'])
        fallback = waybar.render(state(waybar={'display': 'original', 'show_title': False},
                                       lyrics={'current': {'translation': '只有中文'}}))
        self.assertIn('只有中文', fallback['text'])

    def test_default_escapes_markup_and_never_expands_bar_to_two_lines(self):
        snapshot = state(lyrics={'current': {'text': 'Original\r\nline',
                                             'translation': '中文\x00<script>\t& \"引号\"'}})
        rendered = waybar.render(snapshot)
        self.assertNotIn('\n', rendered['text'])
        self.assertNotIn('\x00', rendered['text'])
        self.assertNotIn('<script>', rendered['text'])
        self.assertIn('&lt;script&gt;', rendered['text'])
        self.assertIn('&quot;引号&quot;', rendered['text'])
        self.assertNotIn('<span', rendered['text'])

    def test_long_title_and_lyrics_truncate_separately_keep_timer_and_full_tooltip(self):
        title, original, translated = 'A very long song title ' * 20, 'Original text ' * 70, '中文歌词' * 150
        snapshot = state(track={'title': title, 'artist': 'Artist', 'duration': 3600}, position=600,
                         lyrics={'current': {'text': original, 'translation': translated}})
        rendered = waybar.render(snapshot, max_length=50)
        visible = html.unescape(rendered['text'])
        self.assertTrue(visible.endswith('[10:00/60:00]'))
        self.assertIn(' · ', visible)
        self.assertGreaterEqual(visible.count('…'), 2)
        self.assertLessEqual(waybar._width(visible), 50)
        self.assertIn(original.strip(), rendered['tooltip'])
        self.assertIn(translated, rendered['tooltip'])

    def test_bilingual_each_line_is_width_bounded_without_truncating_markup_entities(self):
        snapshot = state(waybar={'display': 'bilingual'},
                         lyrics={'current': {'text': '<原文&>' * 50, 'translation': '<译文&>' * 50}})
        rendered = waybar.render(snapshot, max_length=35)
        upper, lower = rendered['text'].split('\n')
        self.assertTrue(upper.startswith('<span size="small">'))
        self.assertTrue(upper.endswith('</span>'))
        upper = upper.removeprefix('<span size="small">').removesuffix('</span>')
        for line in (upper, lower):
            self.assertLessEqual(waybar._width(html.unescape(line)), 35)
            self.assertNotIn('<原文', line)
            self.assertNotIn('<译文', line)

    def test_invalid_times_are_finite_and_position_clamps_to_duration(self):
        for position, duration, expected_time, percentage in (
                (-20, 120, '[0:00/2:00]', 0), (999, 120, '[2:00/2:00]', 100),
                (float('nan'), 120, '[0:00/2:00]', 0),
                (float('inf'), float('nan'), '[0:00/0:00]', 0), ('bad', None, '[0:00/0:00]', 0)):
            rendered = waybar.render(state(position=position, track={'title': 'Song', 'duration': duration}))
            self.assertTrue(rendered['text'].endswith(expected_time))
            self.assertEqual(rendered['percentage'], percentage)
        for width in (-1, float('inf'), None, 999999):
            rendered = waybar.render(state(position=1e100, track={'title': 'Song', 'duration': 1e101}), max_length=width)
            self.assertTrue(rendered['text'].endswith('[5999:59/5999:59]'))
            self.assertLessEqual(waybar._width(html.unescape(rendered['text'])), 240)
            self.assertEqual(rendered['percentage'], 10)

    def test_empty_current_line_retains_title_and_progress_and_unknown_preferences_fall_back(self):
        for options in ({'display': 'unknown'}, {'display': []}, None, 'bad'):
            rendered = waybar.render(state(waybar=options, lyrics={'current': {'text': '', 'translation': ''}}))
            self.assertIn('Song', rendered['text'])
            self.assertTrue(rendered['text'].endswith('[0:30/2:00]'))

    def test_bilingual_cli_prints_one_json_record_with_original_and_translation(self):
        stdout = io.StringIO()
        with patch.object(waybar, 'request', return_value=state(waybar={'display': 'bilingual'})), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(waybar.main([]), 0)
        output = stdout.getvalue()
        self.assertEqual(len(output.splitlines()), 1)
        rendered = json.loads(output)
        self.assertIn('<span size="small">Original line</span>\n', rendered['text'])
        self.assertIn('中文翻译', rendered['text'])

    def test_scroll_volume_reads_current_value_and_clamps(self):
        calls = []
        def requester(path, payload=None):
            calls.append((path, payload))
            return state(volume=.98) if payload is None else {'ok': True}
        self.assertTrue(waybar.control('volume_up', requester=requester)['ok'])
        self.assertEqual(calls[-1], ('/music/control', {'action': 'volume', 'value': 1.0}))
        calls.clear()
        def unavailable(path, payload=None):
            calls.append((path, payload))
            return state(volume=None)
        self.assertFalse(waybar.control('volume_down', requester=unavailable)['ok'])
        self.assertEqual(len(calls), 1)

    def test_device_selector_returns_allowlisted_selection(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout=kwargs['input'])
        selected = waybar.choose_device(state(), finder=lambda name: '/usr/bin/rofi' if name == 'rofi' else None,
                                       runner=run)
        self.assertEqual(selected, '20')
        self.assertEqual(calls[0][0:2], ['/usr/bin/rofi', '-dmenu'])
        self.assertIsNone(waybar.choose_device(state(), finder=lambda _: '/usr/bin/rofi',
                          runner=lambda *a, **kw: SimpleNamespace(returncode=0, stdout='injected device')))

    def test_http_client_is_loopback_and_sends_no_credentials(self):
        calls = []
        class Opener:
            def open(self, req, timeout):
                calls.append((req, timeout))
                return io.BytesIO(b'{"ok":true}')
        self.assertTrue(waybar.request('/music/control', {'action': 'next'}, opener=Opener())['ok'])
        req, timeout = calls[0]
        self.assertEqual(req.full_url, 'http://127.0.0.1:18765/music/control')
        self.assertEqual(req.method, 'POST')
        self.assertEqual(json.loads(req.data), {'action': 'next'})
        self.assertNotIn('Authorization', req.headers)
        self.assertLessEqual(timeout, 3)

    def test_http_client_rejects_unbounded_payloads(self):
        class Opener:
            def open(self, *args, **kwargs):
                return io.BytesIO(b'x' * (waybar.MAX_RESPONSE + 1))
        with self.assertRaises(ValueError):
            waybar.request('/music/state', opener=Opener())

    def test_service_unavailable_still_outputs_valid_waybar_json(self):
        stdout = io.StringIO()
        with patch.object(waybar, 'request', side_effect=OSError('offline')), contextlib.redirect_stdout(stdout):
            self.assertEqual(waybar.main([]), 0)
        rendered = json.loads(stdout.getvalue())
        self.assertEqual(rendered['class'], 'offline')
        self.assertEqual(rendered['text'], '♫')
        self.assertIn('未启动', rendered['tooltip'])

    def test_control_failure_reports_nonzero_instead_of_waybar_text(self):
        stderr = io.StringIO()
        with patch.object(waybar, 'control', return_value={'ok': False, 'error': 'unsupported'}), contextlib.redirect_stderr(stderr):
            self.assertEqual(waybar.main(['--action', 'next']), 1)
        self.assertIn('unsupported', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
