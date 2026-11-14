"""Exercise caption styling through real Qt widgets and isolated saved profiles."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QCoreApplication, QEvent, QPointF, QSettings, Qt
from PyQt6.QtGui import QColor, QEnterEvent, QPalette
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QGraphicsBlurEffect, QPushButton

from caption_settings import CaptionAppearanceEditor
from caption_style import CaptionAppearance, CaptionSurface
from caption_window import CaptionWindow


class CaptionAppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qwen-caption-appearance-')
        self.path = Path(self.temp.name) / 'settings.ini'
        self.settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        self.widgets = []
        # These are rendering/settings tests and must never change desktop rules.
        self.desktop_patch = patch.object(CaptionWindow, 'apply_desktop_policy')
        self.desktop_patch.start()

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.desktop_patch.stop()
        self.settings.sync()
        self.temp.cleanup()

    def keep(self, widget):
        self.widgets.append(widget)
        return widget

    def surface(self, appearance):
        surface = self.keep(CaptionSurface())
        surface.resize(560, 180)
        surface.source.setText('Original words remain readable.')
        surface.translation.setText('译文清楚显示，样式改变不丢失内容。')
        appearance.changed.connect(surface.apply_appearance)
        surface.apply_appearance(appearance.options)
        surface.ensurePolished()
        return surface

    def color(self, widget, role=QPalette.ColorRole.WindowText):
        widget.ensurePolished()
        self.app.processEvents()
        return widget.palette().color(role)

    def enter(self, widget):
        point = QPointF(5, 5)
        QApplication.sendEvent(widget, QEnterEvent(point, point, point))

    def leave(self, widget):
        QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))

    def choose(self, combo, key):
        index = combo.findData(key)
        self.assertGreaterEqual(index, 0, f'Missing selectable preset: {key}')
        combo.setCurrentIndex(index)
        combo.activated.emit(index)
        self.app.processEvents()

    def dashed_rows(self, widget, color):
        """Find repeated 4-pixel ink / 2-pixel gap patterns in the actual raster."""
        if isinstance(widget.parentWidget(), CaptionSurface):
            widget.parentWidget().layout.activate()
        widget.ensurePolished()
        rendered = widget.grab().toImage()
        expected = QColor(color).getRgb()[:3]
        rows = []
        for y in range(rendered.height()):
            pixels = []
            for x in range(rendered.width()):
                value = rendered.pixelColor(x, y)
                if value.alpha() > 50 and all(abs(actual - target) <= 2
                        for actual, target in zip(value.getRgb()[:3], expected)):
                    pixels.append(x)
            if len(pixels) < 20:
                continue
            width = pixels[-1] - pixels[0] + 1
            in_pattern = sum((x - pixels[0]) % 6 < 4 for x in pixels)
            if in_pattern / len(pixels) > .94 and .55 < len(pixels) / width < .8:
                rows.append((y, pixels[0], pixels[-1], rendered.pixelColor(pixels[0], y).alpha()))
        return rows

    def test_every_preset_remains_selectable_and_renders_bilingual_content(self):
        appearance = CaptionAppearance(self.settings)
        editor = self.keep(CaptionAppearanceEditor(appearance))
        overlay = self.keep(CaptionWindow(self.settings, appearance=appearance))
        overlay.pending('inbound', '<Original & source>', segment_id=7)
        overlay.caption('inbound', '你好，双语字幕。', segment_id=7)
        source = overlay.source_lines['inbound']
        translated = overlay.lines['inbound']
        original = source.text(), translated.text()
        overlay.show()
        # Explicit list is a product completeness check: three existing appearances,
        # plus all eight Read Frog presets, including "none" and hover-to-reveal.
        presets = ('cinema', 'paper', 'clear', 'none', 'blur', 'quote',
                   'weaken', 'dashed', 'outline', 'green', 'highlight')
        for preset in presets:
            with self.subTest(preset=preset):
                self.choose(editor.style_combo, preset)
                self.assertEqual(overlay.style_combo.currentData(), preset)
                self.assertEqual((source.text(), translated.text()), original)
                self.assertEqual(source.textFormat(), Qt.TextFormat.PlainText)
                self.assertEqual(translated.textFormat(), Qt.TextFormat.PlainText)
                for surface in (*overlay.surfaces.values(), editor.preview):
                    self.assertEqual(surface.style, preset)
                    self.assertFalse(surface.grab().isNull())
        self.assertEqual(editor.style_combo.count(), len(presets))

    def test_presets_change_visible_ink_and_background_and_clear_previous_decoration(self):
        appearance = CaptionAppearance(self.settings)
        surface = self.surface(appearance)
        appearance.apply_preset('green')
        self.assertEqual(self.color(surface.translation).name(), '#00bb7c')
        appearance.apply_preset('weaken')
        self.assertEqual(self.color(surface.translation).name(), '#a1a1a1')
        appearance.apply_preset('highlight')
        background = self.color(surface, QPalette.ColorRole.Window)
        self.assertEqual(background.name(), '#d8a94b')
        self.assertEqual(background.alpha(), 38)
        for preset in ('quote', 'outline', 'dashed'):
            with self.subTest(preset=preset):
                appearance.apply_preset(preset)
                appearance.update(shadow=False)
                surface.layout.activate()
                rendered = surface.translation.grab().toImage()
                # A one-pixel dashed rule is antialiased over two pixel rows;
                # unpremultiplying its alpha can round an RGB component by one.
                gold_pixels = 0
                for y in range(rendered.height()):
                    for x in range(rendered.width()):
                        color = rendered.pixelColor(x, y)
                        if color.alpha() > 80 and all(abs(actual - expected) <= 2
                                for actual, expected in zip(color.getRgb()[:3], (216, 169, 75))):
                            gold_pixels += 1
                self.assertGreater(gold_pixels, 20, 'The decoration must be visibly painted')
        appearance.apply_preset('none')
        self.assertEqual(self.color(surface, QPalette.ColorRole.Window).alpha(), 0)
        self.assertFalse(self.dashed_rows(surface.translation, '#D8A94B'))
        self.assertNotIsInstance(surface.translation.graphicsEffect(), QGraphicsBlurEffect)

    def test_background_opacity_preserves_text_opacity(self):
        appearance = CaptionAppearance(self.settings)
        surface = self.surface(appearance)
        editor = self.keep(CaptionAppearanceEditor(appearance))
        initial_source = self.color(surface.source).getRgb()
        initial_translation = self.color(surface.translation).getRgb()
        for value, alpha in ((0, 0), (25, 64), (100, 255)):
            with self.subTest(opacity=value):
                editor.controls['background_opacity'].setValue(value)
                self.assertEqual(self.color(surface, QPalette.ColorRole.Window).alpha(), alpha)
                self.assertEqual(self.color(surface.source).getRgb(), initial_source)
                self.assertEqual(self.color(surface.translation).getRgb(), initial_translation)
                self.assertEqual(self.color(editor.preview.translation).alpha(), 255)
        editor.controls['text_opacity'].setValue(40)
        self.assertEqual(self.color(surface.translation).alpha(), 102)
        self.assertEqual(self.color(surface.source).alpha(), 102)
        self.assertEqual(self.color(surface, QPalette.ColorRole.Window).alpha(), 255)

    def test_three_subtitle_effects_stack_without_overriding_custom_colors_or_font(self):
        appearance = CaptionAppearance(self.settings)
        editor = self.keep(CaptionAppearanceEditor(appearance))
        surface = self.surface(appearance)
        appearance.update(size=32, foreground='#31A3CF', secondary='#AABBCC', text_opacity=80)
        surface.translation.setText('Translated words')
        for key in ('blur_translation', 'dashed_translation', 'dim_original'):
            editor.controls[key].setChecked(True)
        effect = surface.translation.graphicsEffect()
        self.assertIsInstance(effect, QGraphicsBlurEffect)
        self.assertEqual(effect.blurRadius(), 6)
        self.assertEqual(surface.translation.blur_animation.duration(), 150)
        self.assertEqual(self.color(surface.translation).name(), '#31a3cf')
        self.assertEqual(self.color(surface.translation).alpha(), 204)
        self.assertEqual(self.color(surface.source).name(), '#aabbcc')
        self.assertEqual(self.color(surface.source).alpha(), 122)
        self.assertNotIsInstance(surface.source.graphicsEffect(), QGraphicsBlurEffect)
        self.assertEqual(surface.translation.font().pixelSize(), 32)
        self.enter(surface.translation)
        QTest.qWait(180)
        self.assertTrue(self.dashed_rows(surface.translation, '#31A3CF'))
        self.assertFalse(self.dashed_rows(surface.source, '#AABBCC'))
        # Removing one effect must not accidentally remove either of the others.
        editor.controls['blur_translation'].setChecked(False)
        self.assertNotIsInstance(surface.translation.graphicsEffect(), QGraphicsBlurEffect)
        self.assertTrue(self.dashed_rows(surface.translation, '#31A3CF'))
        self.assertEqual(self.color(surface.source).alpha(), 122)
        editor.controls['dashed_translation'].setChecked(False)
        self.assertFalse(self.dashed_rows(surface.translation, '#31A3CF'))
        self.assertEqual(self.color(surface.source).alpha(), 122)
        editor.controls['dim_original'].setChecked(False)
        self.assertEqual(self.color(surface.source).alpha(), 204)

    def test_web_blur_only_reveals_on_translation_hover_and_returns_on_leave(self):
        appearance = CaptionAppearance(self.settings)
        surface = self.surface(appearance)
        appearance.apply_preset('blur')
        source_color = self.color(surface.source).getRgb()
        source_effect = surface.source.graphicsEffect()
        self.assertNotIsInstance(source_effect, QGraphicsBlurEffect)
        self.assertEqual(surface.translation.graphicsEffect().blurRadius(), 4)
        self.assertEqual(surface.translation.blur_animation.duration(), 100)
        self.assertEqual(self.color(surface.translation).alpha(), 191)
        for target in (surface, surface.source):
            self.enter(target)
            QTest.qWait(120)
            self.assertEqual(surface.translation.graphicsEffect().blurRadius(), 4)
            self.assertEqual(self.color(surface.translation).alpha(), 191)
        self.enter(surface.translation)
        surface.translation.blur_animation.setCurrentTime(50)
        self.assertGreater(self.color(surface.translation).alpha(), 191)
        self.assertLess(self.color(surface.translation).alpha(), 255)
        QTest.qWait(130)
        self.assertAlmostEqual(surface.translation.graphicsEffect().blurRadius(), 0)
        self.assertEqual(self.color(surface.translation).alpha(), 255)
        self.assertEqual(self.color(surface.source).getRgb(), source_color)
        self.assertIs(surface.source.graphicsEffect(), source_effect)
        self.leave(surface.translation)
        QTest.qWait(130)
        self.assertAlmostEqual(surface.translation.graphicsEffect().blurRadius(), 4)
        self.assertEqual(self.color(surface.translation).alpha(), 191)

    def test_underlines_follow_each_centered_line_including_automatic_wraps(self):
        appearance = CaptionAppearance(self.settings)
        surface = self.surface(appearance)
        appearance.apply_preset('dashed')
        appearance.update(font_family='DejaVu Sans', shadow=False)
        surface.resize(560, 280)
        surface.translation.setText('Short\nA much longer translated line')
        rows = self.dashed_rows(surface.translation, '#D8A94B')
        self.assertEqual(len(rows), 2)
        center = surface.translation.contentsRect().center().x()
        for _, left, right, _ in rows:
            self.assertAlmostEqual((left + right) / 2, center, delta=3)
            self.assertGreater(left, 5)
            self.assertLess(right, surface.translation.width() - 5)
        short_width = surface.translation.fontMetrics().horizontalAdvance('Short')
        self.assertAlmostEqual(rows[0][2] - rows[0][1] + 1, short_width, delta=6)
        self.assertLess(rows[0][2] - rows[0][1], (rows[1][2] - rows[1][1]) * .4)
        surface.resize(280, 440)
        surface.translation.setText('A translated sentence that wraps onto several lines while '
                                    'each underline stays with its own text.')
        wrapped = self.dashed_rows(surface.translation, '#D8A94B')
        self.assertGreaterEqual(len(wrapped), 3)
        center = surface.translation.contentsRect().center().x()
        for _, left, right, _ in wrapped:
            self.assertAlmostEqual((left + right) / 2, center, delta=3)
        self.assertLess(wrapped[-1][2] - wrapped[-1][1], surface.translation.width() * .9)

    def test_template_underline_respects_opacity_and_quote_or_outline_padding(self):
        appearance = CaptionAppearance(self.settings)
        surface = self.surface(appearance)
        surface.resize(560, 280)
        surface.translation.setText('Short\nA longer second line')
        for preset in ('quote', 'outline'):
            with self.subTest(preset=preset):
                appearance.apply_preset(preset)
                appearance.update(dashed_translation=True, foreground='#31A3CF',
                                  text_opacity=40, shadow=False)
                rows = self.dashed_rows(surface.translation, '#31A3CF')
                self.assertEqual(len(rows), 2)
                center = surface.translation.contentsRect().center().x()
                for y, left, right, alpha in rows:
                    self.assertAlmostEqual((left + right) / 2, center, delta=3)
                    self.assertEqual(alpha, 102)
                    self.assertGreater(left, surface.translation.contentsRect().left())
                    self.assertLess(right, surface.translation.contentsRect().right())
                    self.assertLess(y, surface.translation.height() - 2)

    def test_untranslated_primary_text_skips_translation_learning_effects(self):
        appearance = CaptionAppearance(self.settings)
        surface = self.surface(appearance)
        surface.translation.setText('Original waiting for translation')
        appearance.apply_preset('blur')
        appearance.update(blur_translation=True, dashed_translation=True, foreground='#31A3CF',
                          shadow=False)
        self.assertIsInstance(surface.translation.graphicsEffect(), QGraphicsBlurEffect)
        surface.set_primary_is_source(True)
        self.assertNotIsInstance(surface.translation.graphicsEffect(), QGraphicsBlurEffect)
        self.assertEqual(self.color(surface.translation).alpha(), 255)
        self.assertFalse(self.dashed_rows(surface.translation, '#31A3CF'))
        appearance.update(size=34)
        self.assertNotIsInstance(surface.translation.graphicsEffect(), QGraphicsBlurEffect)
        self.assertEqual(self.color(surface.translation).name(), '#31a3cf')
        self.assertEqual(surface.translation.font().pixelSize(), 34)
        self.assertEqual(surface.translation.text(), 'Original waiting for translation')
        surface.set_primary_is_source(False)
        self.assertEqual(surface.translation.graphicsEffect().blurRadius(), 6)
        self.assertEqual(surface.translation.font().pixelSize(), 34)
        self.enter(surface.translation)
        QTest.qWait(180)
        self.assertTrue(self.dashed_rows(surface.translation, '#31A3CF'))
        appearance.update(blur_translation=False)
        self.assertEqual(surface.translation.graphicsEffect().blurRadius(), 4)
        surface.set_primary_is_source(True)
        self.assertNotIsInstance(surface.translation.graphicsEffect(), QGraphicsBlurEffect)
        self.assertEqual(self.color(surface.translation).alpha(), 255)

    def test_subtitle_blur_hover_preserves_the_chosen_text_opacity(self):
        appearance = CaptionAppearance(self.settings)
        surface = self.surface(appearance)
        appearance.update(blur_translation=True, text_opacity=60, dim_original=True)
        original_alpha = self.color(surface.source).alpha()
        self.enter(surface.translation)
        QTest.qWait(180)
        self.assertEqual(surface.translation.graphicsEffect().blurRadius(), 0)
        self.assertEqual(self.color(surface.translation).alpha(), 153)
        self.assertEqual(self.color(surface.source).alpha(), original_alpha)
        self.leave(surface.translation)
        QTest.qWait(180)
        self.assertEqual(surface.translation.graphicsEffect().blurRadius(), 6)
        self.assertEqual(self.color(surface.translation).alpha(), 153)

    def test_saved_profile_reopens_with_custom_appearance_and_all_three_effects(self):
        appearance = CaptionAppearance(self.settings)
        appearance.apply_preset('outline')
        appearance.update(size=41, font_family='DejaVu Sans', background_opacity=37,
                          text_opacity=81, foreground='#123ABC', secondary='#FEDCBA',
                          background='#273849', radius=13, spacing=17, show_source=False,
                          translation_first=True, shadow=False, blur_translation=True,
                          dashed_translation=True, dim_original=True)
        self.settings.sync()
        reopened_settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        reopened = CaptionAppearance(reopened_settings)
        self.assertEqual(reopened.options, appearance.options)
        editor = self.keep(CaptionAppearanceEditor(reopened))
        self.assertEqual(editor.style_combo.currentData(), 'outline')
        self.assertEqual(editor.controls['size'].value(), 41)
        self.assertEqual(editor.controls['background_opacity'].value(), 37)
        self.assertFalse(editor.controls['show_source'].isChecked())
        self.assertTrue(editor.preview.source.isHidden())
        self.assertIs(editor.preview.layout.itemAt(0).widget(), editor.preview.translation)
        for key in ('blur_translation', 'dashed_translation', 'dim_original'):
            self.assertTrue(editor.controls[key].isChecked())
        self.assertEqual(editor.preview.translation.graphicsEffect().blurRadius(), 6)

    def test_live_and_music_profiles_are_independent_and_reset_to_their_own_defaults(self):
        live = CaptionAppearance(self.settings)
        music = CaptionAppearance(self.settings, 'music_caption_')
        initial_live, initial_music = live.options, music.options
        music.update(style='green', size=38, background_opacity=21, dim_original=True)
        self.assertEqual(live.options, initial_live)
        music_custom = music.options
        live.update(style='highlight', size=47, foreground='#abcdef', blur_translation=True)
        self.assertEqual(music.options, music_custom)
        live_editor = self.keep(CaptionAppearanceEditor(live))
        music_editor = self.keep(CaptionAppearanceEditor(music))
        for editor, profile, expected in ((live_editor, live, initial_live),
                                          (music_editor, music, initial_music)):
            reset = next(button for button in editor.findChildren(QPushButton)
                         if button.text() == '恢复默认外观')
            reset.click()
            self.assertEqual(profile.options, expected)
        reopened_settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        self.assertEqual(CaptionAppearance(reopened_settings).options, initial_live)
        self.assertEqual(CaptionAppearance(reopened_settings, 'music_caption_').options, initial_music)
        self.assertEqual(live_editor.controls['size'].value(), 22)
        self.assertEqual(music_editor.controls['size'].value(), 24)
        self.assertFalse(live_editor.controls['translation_first'].isChecked())
        self.assertTrue(music_editor.controls['translation_first'].isChecked())

    def test_overlay_toolbar_and_multiple_editors_sync_in_both_directions_once(self):
        appearance = CaptionAppearance(self.settings)
        first = self.keep(CaptionAppearanceEditor(appearance))
        second = self.keep(CaptionAppearanceEditor(appearance))
        overlay = self.keep(CaptionWindow(self.settings, appearance=appearance))
        overlay.pending('inbound', 'Source', segment_id=2)
        overlay.caption('inbound', '译文', segment_id=2)
        changes = QSignalSpy(appearance.changed)
        first.controls['size'].setValue(35)
        self.assertEqual(len(changes), 1)
        self.assertEqual(second.controls['size'].value(), 35)
        self.assertEqual(overlay.size_control.value(), 35)
        self.choose(first.style_combo, 'green')
        self.assertEqual(overlay.style_combo.currentData(), 'green')
        self.assertEqual(second.style_combo.currentData(), 'green')
        before = len(changes)
        overlay.size_control.setValue(49)
        self.assertEqual(len(changes), before + 1)
        self.assertEqual(first.controls['size'].value(), 49)
        self.assertEqual(second.controls['size'].value(), 49)
        for surface in (*overlay.surfaces.values(), first.preview, second.preview):
            surface.translation.ensurePolished()
            self.assertEqual(surface.translation.font().pixelSize(), 49)
        overlay.style_combo.setCurrentIndex(overlay.style_combo.findData('paper'))
        self.assertEqual(first.style_combo.currentData(), 'paper')
        self.assertEqual(second.style_combo.currentData(), 'paper')
        self.assertEqual(first.controls['background_opacity'].value(), 95)
        self.assertEqual(first.controls['size'].value(), overlay.size_control.value())
        self.assertEqual(second.controls['size'].value(), overlay.size_control.value())
        for surface in (*overlay.surfaces.values(), first.preview, second.preview):
            self.assertEqual(self.color(surface.translation).name(), '#17201c')
            self.assertEqual(surface.translation.font().pixelSize(), overlay.size_control.value())
        self.assertEqual(overlay.source_lines['inbound'].text(), 'Source')
        self.assertEqual(overlay.lines['inbound'].text(), '译文')

    def test_hide_source_and_reorder_keep_current_words_when_restored(self):
        appearance = CaptionAppearance(self.settings)
        surface = self.surface(appearance)
        original = surface.source.text(), surface.translation.text()
        appearance.update(show_source=False, translation_first=True, spacing=23)
        self.assertTrue(surface.source.isHidden())
        self.assertIs(surface.layout.itemAt(0).widget(), surface.translation)
        self.assertEqual(surface.layout.spacing(), 23)
        appearance.update(show_source=True, translation_first=False)
        self.assertFalse(surface.source.isHidden())
        self.assertIs(surface.layout.itemAt(0).widget(), surface.source)
        self.assertEqual((surface.source.text(), surface.translation.text()), original)

    def test_legacy_settings_are_kept_and_bad_values_do_not_prevent_opening_editor(self):
        self.settings.setValue('caption_style', 'clear')
        self.settings.setValue('caption_size', 31)
        appearance = CaptionAppearance(self.settings)
        self.assertEqual(appearance.options['style'], 'clear')
        self.assertEqual(appearance.options['size'], 31)
        self.assertEqual(appearance.options['background_opacity'], 0)
        self.settings.setValue('caption_size', 'invalid')
        self.settings.setValue('caption_foreground', 'not a colour')
        self.settings.setValue('caption_background_opacity', -200)
        self.settings.setValue('caption_text_opacity', 9999)
        self.settings.setValue('caption_font_family', 'Sans"; color:red; {\n}')
        self.settings.setValue('caption_show_source', 'false')
        repaired = CaptionAppearance(self.settings)
        editor = self.keep(CaptionAppearanceEditor(repaired))
        self.assertGreaterEqual(editor.controls['size'].value(), 14)
        self.assertLessEqual(editor.controls['size'].value(), 64)
        self.assertEqual(self.color(editor.preview, QPalette.ColorRole.Window).alpha(), 0)
        self.assertEqual(self.color(editor.preview.translation).alpha(), 255)
        self.assertTrue(QColor(self.color(editor.preview.translation)).isValid())
        self.assertTrue(editor.preview.source.isHidden())
        for forbidden in (';', '{', '}', '"', '\n'):
            self.assertNotIn(forbidden, repaired.options['font_family'])


if __name__ == '__main__':
    unittest.main()
