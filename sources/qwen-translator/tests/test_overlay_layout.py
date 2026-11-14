"""Transparent raster output and screen-aware sizing using real Qt widgets."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QEvent, QRect, QSettings, Qt
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from caption_style import CaptionAppearance, resolve_appearance
from caption_window import CaptionWindow, fitted_overlay_geometry
from music_caption_window import LyricsCaptionWindow
from ui_theme import apply_theme


class OverlayLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_geometry_stays_in_work_area_with_scaling_portrait_and_negative_origin(self):
        for area in (QRect(2560, 45, 2560, 1395), QRect(-1280, 30, 1280, 770),
                     QRect(0, 45, 720, 1235), QRect(0, 0, 360, 640)):
            with self.subTest(area=area):
                rect = fitted_overlay_geometry(area, 3000)
                self.assertTrue(area.contains(rect))
                self.assertLessEqual(abs(rect.center().x() - area.center().x()), 1)
                self.assertLessEqual(rect.height(), area.height() * .65)
                self.assertLessEqual(rect.width(), 1600)
        self.assertEqual(fitted_overlay_geometry(QRect(2560, 45, 2560, 1395), 188),
                         QRect(3072, 1196, 1536, 188))

    def test_input_passthrough_is_reversible_without_hiding_or_losing_lyrics(self):
        with patch.object(CaptionWindow, 'apply_desktop_policy'):
            window = LyricsCaptionWindow()
            window.appearance.update(click_through=True)
            window.show()
            self.app.processEvents()
            spy = QSignalSpy(window.visibilityChanged)
            text = window.surfaces['lyrics'].translation.text()
            try:
                for enabled in (True, False, True):
                    window.appearance.update(click_through=enabled)
                    self.app.processEvents()
                    self.assertTrue(window.isVisible())
                    self.assertEqual(bool(window.windowHandle().flags() & Qt.WindowType.WindowTransparentForInput), enabled)
                    self.assertEqual(bool(window.windowHandle().flags() & Qt.WindowType.WindowDoesNotAcceptFocus), enabled)
                    self.assertEqual(window.toolbar.isHidden(), enabled)
                    self.assertEqual(window.grip.isHidden(), enabled)
                    self.assertEqual(window.surfaces['lyrics'].translation.text(), text)
                self.assertEqual(len(spy), 0)
                window.appearance.apply_preset('clear')
                self.assertTrue(window.appearance.options['click_through'])
                window.hide()
                window.show()
                self.app.processEvents()
                self.assertTrue(window.windowHandle().flags() & Qt.WindowType.WindowTransparentForInput)
                self.assertTrue(window.toolbar.isHidden())
                # QWidget flag changes recreate its native window. The input
                # policy must survive a pin toggle as well as hide/show.
                window.set_pinned(False)
                self.app.processEvents()
                self.assertTrue(window.windowHandle().flags() & Qt.WindowType.WindowTransparentForInput)
                self.assertTrue(window.windowHandle().flags() & Qt.WindowType.WindowDoesNotAcceptFocus)
            finally:
                window.close()
                window.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_growing_lyrics_keep_the_bottom_inside_the_screen(self):
        with patch.object(CaptionWindow, 'apply_desktop_policy'):
            window = LyricsCaptionWindow()
            window.show()
            self.app.processEvents()
            old_height = window.height()
            try:
                window.surfaces['lyrics'].translation.setText('长歌词需要换行并保持完整可见。' * 25)
                window.fit_height()
                self.app.processEvents()
                self.assertGreater(window.height(), old_height)
                self.assertTrue(window.screen().availableGeometry().contains(QRect(*window._pending_geometry)))
                self.assertLess(window._pending_geometry[1], window.screen().availableGeometry().height() - old_height)
            finally:
                window.close()
                window.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_transparency_restores_background_and_survives_presets_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(directory + '/settings.ini', QSettings.Format.IniFormat)
            appearance = CaptionAppearance(settings, 'music_caption_')
            self.assertTrue(appearance.options['auto_fit_screen'])
            appearance.update(background_opacity=63, transparent_background=True, auto_fit_screen=False)
            self.assertEqual(resolve_appearance(appearance.options)['background_opacity'], 0)
            appearance.update(transparent_background=False)
            self.assertEqual(resolve_appearance(appearance.options)['background_opacity'], 63)
            appearance.update(transparent_background=True)
            appearance.apply_preset('paper')
            reloaded = CaptionAppearance(settings, 'music_caption_')
            self.assertTrue(reloaded.options['transparent_background'])
            self.assertFalse(reloaded.options['auto_fit_screen'])
            self.assertEqual(resolve_appearance(reloaded.options)['background_opacity'], 0)
            reloaded.update(transparent_background=False)
            self.assertEqual(resolve_appearance(reloaded.options)['background_opacity'], 95)
            self.assertFalse(CaptionAppearance(settings).options['auto_fit_screen'])

    def test_full_overlay_pixels_are_transparent_and_saved_width_is_overridden(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(CaptionWindow, 'apply_desktop_policy'):
            settings = QSettings(directory + '/settings.ini', QSettings.Format.IniFormat)
            old = LyricsCaptionWindow(settings)
            old.resize(720, 190)
            settings.setValue('music_caption_geometry', old.saveGeometry())
            old.deleteLater()
            window = LyricsCaptionWindow(settings)
            window.appearance.update(transparent_background=True)
            try:
                for theme in ('light', 'dark'):
                    apply_theme(self.app, theme)
                    window.show()
                    self.app.processEvents()
                    window.toolbar.hide()
                    window.grip.hide()
                    self.app.processEvents()
                    area = window.screen().availableGeometry()
                    self.assertEqual(window.width(), fitted_overlay_geometry(area, window.height()).width())
                    rendered = window.grab().toImage()
                    self.assertEqual(rendered.pixelColor(2, 2).alpha(), 0)
                    surface = window.surfaces['lyrics']
                    point = surface.mapTo(window, surface.rect().topLeft())
                    self.assertEqual(rendered.pixelColor(point.x() + 10, point.y() + 10).alpha(), 0)
                    # Content remains visible; full transparency must never
                    # be implemented by making the entire window invisible.
                    self.assertTrue(any(rendered.pixelColor(x, y).alpha() > 0
                                        for y in range(rendered.height()) for x in range(rendered.width())))
                    window.hide()
            finally:
                window.close()
                window.deleteLater()
                self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
