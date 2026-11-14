import json
from pathlib import Path
import unittest

from scripts.setup_music_waybar import configure


class WaybarSetupTests(unittest.TestCase):
    def test_only_media_changes_and_commands_are_quoted(self):
        source = '{"modules-left":["clock","custom/media"],"custom/media":{"exec":"old {value}"},"clock":{"format":"keep"}}'
        result = configure(source, Path('/tmp/path with spaces'), '/tmp/python bin')
        parsed = json.loads(result)
        self.assertEqual(parsed['clock'], {'format': 'keep'})
        self.assertEqual(parsed['modules-left'], ['clock', 'custom/media'])
        self.assertEqual(parsed['custom/media']['return-type'], 'json')
        self.assertEqual(parsed['custom/media']['on-click'], 'playerctl play-pause')
        self.assertEqual(parsed['custom/media']['on-click-middle'], 'playerctl previous')
        self.assertEqual(parsed['custom/media']['on-click-right'], 'playerctl next')
        self.assertNotIn('on-scroll-up', parsed['custom/media'])
        self.assertNotIn('on-scroll-down', parsed['custom/media'])
        self.assertNotIn('max-length', parsed['custom/media'])
        self.assertIn("'/tmp/path with spaces/scripts/waybar_music.py'", parsed['custom/media']['exec'])
        self.assertEqual(configure(result, Path('/tmp/path with spaces'), '/tmp/python bin'), result)

    def test_jsonc_comments_and_trailing_commas_survive(self):
        source = '// keep\n{\n "custom/media": { /* { unmatched */ "exec":"old \\\" }" },\n "clock": {"format":"keep"},\n}\n'
        result = configure(source)
        self.assertTrue(result.startswith('// keep\n'))
        self.assertTrue(result.endswith(' "clock": {"format":"keep"},\n}\n'))
        self.assertIn('"on-click-right": "playerctl next"', result)
        self.assertNotIn('"on-scroll-up"', result)

    def test_missing_or_malformed_module_is_rejected(self):
        for source in ('{}', '{"custom/media":{"exec":"x"'):
            with self.assertRaises(ValueError):
                configure(source)
