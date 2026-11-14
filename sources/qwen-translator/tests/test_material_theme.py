"""Cross-platform colors are generated together and remain readable in both themes."""
import importlib.util
import json
from pathlib import Path
import unittest

from ui_tokens import THEMES

ROOT = Path(__file__).resolve().parent.parent


def luminance(color):
    values = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
    return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


class MaterialThemeTests(unittest.TestCase):
    def test_generated_android_and_desktop_tokens_have_not_drifted(self):
        spec = importlib.util.spec_from_file_location('sync_ui_theme', ROOT / 'scripts/sync_ui_theme.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for path, content in module.outputs().items():
            self.assertEqual(path.read_text(), content, str(path))
        source = json.loads((ROOT / 'design/material-theme.json').read_text())
        self.assertEqual(THEMES, {key: source[key] for key in ('light', 'dark')})

    def test_text_and_semantic_states_meet_normal_text_contrast(self):
        for name, palette in THEMES.items():
            pairs = [(fg, bg) for fg in ('text', 'secondary', 'muted') for bg in ('base', 'surface', 'soft')]
            pairs += [('on_accent', 'accent'), ('accent', 'tint'), ('success', 'success_bg'),
                      ('error', 'error_bg'), ('warning', 'warning_bg')]
            for foreground, background in pairs:
                with self.subTest(theme=name, foreground=foreground, background=background):
                    a, b = sorted([luminance(palette[foreground]), luminance(palette[background])])
                    self.assertGreaterEqual((b + 0.05) / (a + 0.05), 4.5)


if __name__ == '__main__':
    unittest.main()
