"""Lyric-only typography and lossless, repeatable stylesheet installation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import waybar_theme


class WaybarThemeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-waybar-theme-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.style = self.root / 'style.css'
        self.original = '/* User styles are preserved. */\n#custom-existing { color: #123456; }\n'
        self.style.write_text(self.original)

    def test_only_lyric_font_follows_app_typography_in_both_modes(self):
        tokens = json.loads((waybar_theme.ROOT / 'design/material-theme.json').read_text())
        for name in ('light', 'dark'):
            css = waybar_theme.build_css(name)
            for family in tokens['fontFamilies']:
                self.assertIn(family, css)
            self.assertIn('window#waybar #custom-media {', css)
            self.assertIn('font-size: 13px', css)
            self.assertEqual(css.count('{'), 1)
            self.assertIn('background: transparent;', css)
            self.assertIn('min-width: 0;', css)
            self.assertIn('padding: 0 4px;', css)
            for forbidden in ('#workspaces', '#clock', 'tooltip', '@define-color'):
                self.assertNotIn(forbidden, css)

    def test_first_install_preserves_all_old_css_and_creates_one_backup(self):
        generated = waybar_theme.sync_theme('light', self.root)
        self.assertEqual(generated, self.root / 'qwen-theme.css')
        self.assertEqual((self.root / waybar_theme.BACKUP_NAME).read_text(), self.original)
        self.assertTrue(self.style.read_text().startswith(self.original))
        self.assertEqual(self.style.read_text().count(waybar_theme.START), 1)
        self.assertIn(generated.read_text().strip(), self.style.read_text())
        self.assertNotIn('@import', self.style.read_text())

    def test_appearance_font_and_explicit_color_are_scoped_to_lyric_label(self):
        css = waybar_theme.build_css('dark', {'font_family': '思源黑体', 'size': 22,
                                              'foreground': '#12aBcD', 'background': '#FF0000'})
        self.assertIn('font-family: "思源黑体"', css)
        self.assertIn('font-size: 13px;', css)
        self.assertIn('color: #12aBcD;', css)
        self.assertNotIn('#FF0000', css)
        self.assertEqual(css.count('{'), 1)

    def test_appearance_rejects_css_injection_and_color_is_opt_in(self):
        for appearance in (None, {}, {'foreground': ''}, {'foreground': 'red; background: black'},
                           {'font_family': 'Bad"; } tooltip { color:red', 'foreground': '#ffffff;'}):
            css = waybar_theme.build_css('dark', appearance)
            self.assertIn('color: inherit;', css)
            self.assertNotIn('color: #', css)
            self.assertNotIn('tooltip', css)
            self.assertEqual(css.count('{'), 1)
            self.assertIn('"Noto Sans CJK SC"', css)

    def test_caption_font_sizes_are_scaled_and_clamped(self):
        for value, expected in ((22, 13), (44, 20), (14, 10), (-10, 10),
                                ('33', 20), (float('nan'), 13), (None, 13), (True, 13)):
            with self.subTest(value=value):
                self.assertIn(f'font-size: {expected}px;', waybar_theme.build_css('dark', {'size': value}))

    def test_sync_updates_only_typography_when_appearance_changes(self):
        waybar_theme.sync_theme('dark', self.root, {'font_family': 'Cascadia Code', 'size': 28})
        text = self.style.read_text()
        self.assertTrue(text.startswith(self.original))
        self.assertIn('font-family: "Cascadia Code"', text)
        self.assertIn('font-size: 17px;', text)
        self.assertEqual((self.root / waybar_theme.BACKUP_NAME).read_text(), self.original)

    def test_mode_change_replaces_managed_block_and_keeps_original_backup(self):
        waybar_theme.sync_theme('light', self.root)
        light = self.style.read_text()
        waybar_theme.sync_theme('dark', self.root)
        dark = self.style.read_text()
        self.assertNotEqual(light, dark)
        self.assertIn('Qwen lyric typography: dark', dark)
        self.assertEqual(dark.count(waybar_theme.START), 1)
        self.assertEqual((self.root / waybar_theme.BACKUP_NAME).read_text(), self.original)
        self.assertTrue(dark.startswith(self.original))

    def test_old_whole_bar_managed_style_is_replaced_by_lyric_font_only(self):
        old = (self.original + waybar_theme.START + '\nwindow#waybar {background: #000000;}\n'
               '#clock {color: #00ffaa;}\n' + waybar_theme.END + '\n/* other user rules */')
        self.style.write_text(old)
        waybar_theme.sync_theme('dark', self.root)
        updated = self.style.read_text()
        self.assertNotIn('window#waybar {background:', updated)
        self.assertNotIn('#clock {color:', updated)
        self.assertTrue(updated.startswith(self.original))
        self.assertTrue(updated.endswith('\n/* other user rules */'))
        self.assertEqual(updated.count(waybar_theme.START), 1)

    def test_same_theme_does_not_rewrite_any_files(self):
        waybar_theme.sync_theme('dark', self.root)
        before = {path.name: (path.stat().st_mtime_ns, path.stat().st_ino) for path in self.root.iterdir()}
        with patch.object(waybar_theme.tempfile, 'mkstemp', wraps=waybar_theme.tempfile.mkstemp) as temporary:
            waybar_theme.sync_theme('dark', self.root)
        temporary.assert_not_called()
        after = {path.name: (path.stat().st_mtime_ns, path.stat().st_ino) for path in self.root.iterdir()}
        self.assertEqual(before, after)

    def test_css_after_managed_block_and_no_final_newline_survive_updates(self):
        waybar_theme.sync_theme('light', self.root)
        outside = '\n/* Later customization */\n#mine { padding: 2px; }'
        self.style.write_text(self.style.read_text() + outside)
        waybar_theme.sync_theme('dark', self.root)
        self.assertTrue(self.style.read_text().endswith(outside))

    def test_missing_configuration_directory_is_created(self):
        directory = self.root / 'new-waybar'
        result = waybar_theme.sync_theme('light', directory)
        self.assertTrue(result.is_file())
        self.assertTrue((directory / 'style.css').is_file())
        self.assertFalse((directory / waybar_theme.BACKUP_NAME).exists())

    def test_xdg_configuration_directory_is_honored(self):
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root / 'xdg')}):
            result = waybar_theme.sync_theme('light')
        self.assertEqual(result, self.root / 'xdg/waybar/qwen-theme.css')

    def test_invalid_theme_or_unfinished_managed_block_leaves_styles_untouched(self):
        with self.assertRaises(ValueError):
            waybar_theme.sync_theme('neon', self.root)
        self.assertEqual(self.style.read_text(), self.original)
        broken = self.original + waybar_theme.START + '\n/* incomplete */'
        self.style.write_text(broken)
        with self.assertRaises(ValueError):
            waybar_theme.sync_theme('dark', self.root)
        self.assertEqual(self.style.read_text(), broken)
        self.assertFalse((self.root / 'qwen-theme.css').exists())

    def test_original_file_mode_is_preserved_and_temporary_files_cleaned(self):
        self.style.chmod(0o640)
        waybar_theme.sync_theme('dark', self.root)
        self.assertEqual(self.style.stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.root / waybar_theme.BACKUP_NAME).stat().st_mode & 0o777, 0o640)
        self.assertFalse(list(self.root.glob('.qwen-theme-*')))

    def test_stylesheet_passes_real_gtk3_parser_when_available(self):
        try:
            import gi
            gi.require_version('Gtk', '3.0')
            from gi.repository import Gtk
        except (ImportError, ValueError):
            self.skipTest('GTK 3 Python bindings unavailable')
        for name, options in (('light', None), ('dark', None),
                              ('dark', {'font_family': '思源黑体', 'size': 28, 'foreground': '#7ED8C3'})):
            provider = Gtk.CssProvider()
            errors = []
            provider.connect('parsing-error', lambda provider, section, error: errors.append(str(error)))
            provider.load_from_data(waybar_theme.build_css(name, options).encode())
            self.assertFalse(errors, errors)


if __name__ == '__main__':
    unittest.main()
