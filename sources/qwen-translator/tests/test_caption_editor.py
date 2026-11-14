"""User interactions in the desktop caption editor, without external services."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QColor, QPalette, QWheelEvent
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QDialog, QPushButton, QScrollArea, QVBoxLayout, QWidget

import caption_settings
from caption_settings import CaptionAppearanceEditor, appearance_dialog
from caption_style import CaptionAppearance, PRESETS, STYLES
from music_ui import MusicPage
from ui_theme import apply_theme


class CaptionEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qwen-caption-editor-')
        self.settings_path = Path(self.temp.name) / 'settings.ini'
        self.settings = QSettings(str(self.settings_path), QSettings.Format.IniFormat)
        self.live = CaptionAppearance(self.settings)
        self.music = CaptionAppearance(self.settings, 'music_caption_')
        self.music.update(size=34, foreground='#31648a', background_opacity=43)
        self.widgets = []
        self.music_pages = []
        apply_theme(self.app, 'light')

    def tearDown(self):
        for page in self.music_pages:
            page.shutdown()
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.settings.sync()
        self.temp.cleanup()

    def keep(self, widget):
        self.widgets.append(widget)
        return widget

    def editor(self, appearance=None):
        editor = self.keep(CaptionAppearanceEditor(appearance or self.live))
        editor.resize(560, 760)
        editor.show()
        self.app.processEvents()
        return editor

    def reveal(self, editor, control):
        for index in range(editor.tabs.count()):
            page = editor.pages.widget(index)
            if page is control or page.isAncestorOf(control):
                editor.tabs.setCurrentIndex(index)
                break
        ancestor = control.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea):
                ancestor.ensureWidgetVisible(control)
            ancestor = ancestor.parentWidget()
        self.app.processEvents()

    def click(self, editor, control):
        self.reveal(editor, control)
        self.assertTrue(control.isVisible())
        QTest.mouseClick(control, Qt.MouseButton.LeftButton)
        self.app.processEvents()

    def assert_music_unchanged(self, expected):
        self.assertEqual(self.music.options, expected)
        saved = QSettings(str(self.settings_path), QSettings.Format.IniFormat)
        self.assertEqual(CaptionAppearance(saved, 'music_caption_').options, expected)

    def assert_editor_fits_horizontally(self, editor):
        for scroll in editor.scroll_areas:
            if scroll.isVisible():
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                self.assertLessEqual(scroll.widget().width(), scroll.viewport().width())
        controls = [editor.reset_button, editor.background_button, editor.tabs,
                    *editor.controls.values(), *editor.colors.values(),
                    *editor.preset_buttons.values(), *editor.cards]
        for control in controls:
            if control.isVisible():
                left = control.mapTo(editor, QPoint()).x()
                self.assertGreaterEqual(left, 0, control.accessibleName())
                self.assertLessEqual(left + control.width(), editor.width(), control.accessibleName())

    def test_preset_cards_apply_full_style_and_reselecting_restores_it(self):
        editor = self.editor()
        untouched = self.music.options
        self.assertEqual(set(editor.preset_buttons), set(STYLES))
        for style, control in editor.preset_buttons.items():
            with self.subTest(style=style):
                self.click(editor, control)
                self.assertEqual(self.live.options['style'], style)
                self.assertEqual(editor.style_combo.currentData(), style)
                self.assertEqual(editor.preview.style, style)
                self.assertTrue(control.isChecked())
                self.assertEqual(sum(button.isChecked() for button in editor.preset_buttons.values()), 1)
        self.click(editor, editor.preset_buttons['paper'])
        self.live.update(foreground='#abcdef', background_opacity=20, blur_translation=True)
        self.click(editor, editor.preset_buttons['paper'])
        self.assertEqual(self.live.options['foreground'], '')
        self.assertEqual(self.live.options['background_opacity'], PRESETS['paper']['background_opacity'])
        self.assertFalse(self.live.options['blur_translation'])
        self.assert_music_unchanged(untouched)

    def test_preset_cards_are_keyboard_operable(self):
        editor = self.editor()
        untouched = self.music.options
        for style, key in (('green', Qt.Key.Key_Space), ('outline', Qt.Key.Key_Return)):
            with self.subTest(style=style):
                button = editor.preset_buttons[style]
                self.reveal(editor, button)
                button.setFocus(Qt.FocusReason.TabFocusReason)
                self.assertTrue(button.hasFocus())
                QTest.keyClick(button, key)
                self.app.processEvents()
                self.assertEqual(self.live.options['style'], style)
                self.assertTrue(button.isChecked())
        self.assert_music_unchanged(untouched)

    def test_color_cancel_changes_nothing_and_confirmation_syncs_only_current_profile(self):
        editor = self.editor()
        second = self.keep(CaptionAppearanceEditor(self.live))
        untouched = self.music.options
        before = self.live.options
        settings_before = {key: self.settings.value(key) for key in self.settings.allKeys()}
        changes = QSignalSpy(self.live.changed)
        with patch.object(caption_settings.QColorDialog, 'getColor', return_value=QColor()):
            self.click(editor, editor.colors['foreground'])
        self.assertEqual(len(changes), 0)
        self.assertEqual(self.live.options, before)
        self.assertEqual({key: self.settings.value(key) for key in self.settings.allKeys()}, settings_before)
        with patch.object(caption_settings.QColorDialog, 'getColor', return_value=QColor('#3175a2')):
            self.click(editor, editor.colors['foreground'])
        self.assertEqual(len(changes), 1)
        self.assertEqual(self.live.options['foreground'], '#3175a2')
        for view in (editor, second):
            view.preview.translation.ensurePolished()
            self.assertEqual(view.preview.translation.palette().color(QPalette.ColorRole.WindowText).name(), '#3175a2')
        self.assert_music_unchanged(untouched)

    def test_opacity_sliders_and_number_fields_sync_with_keyboard_without_crosstalk(self):
        editor = self.editor()
        second = self.keep(CaptionAppearanceEditor(self.live))
        untouched = self.music.options
        for key, minimum in (('background_opacity', 0), ('text_opacity', 10)):
            with self.subTest(control=key):
                self.live.update(**{key: 40})
                slider = editor.sliders[key]
                self.reveal(editor, slider)
                slider.setFocus(Qt.FocusReason.TabFocusReason)
                changes = QSignalSpy(self.live.changed)
                QTest.keyClick(slider, Qt.Key.Key_Right)
                self.assertEqual(len(changes), 1)
                expected = 40 + slider.singleStep()
                self.assertEqual(self.live.options[key], expected)
                self.assertEqual(editor.controls[key].value(), expected)
                self.assertEqual(second.sliders[key].value(), expected)
                QTest.mouseClick(slider, Qt.MouseButton.LeftButton,
                                 pos=QPoint(slider.width() - 8, slider.height() // 2))
                self.assertGreater(self.live.options[key], expected)
                QTest.keyClick(slider, Qt.Key.Key_Home)
                self.assertEqual(self.live.options[key], minimum)
                QTest.keyClick(slider, Qt.Key.Key_End)
                self.assertEqual(self.live.options[key], 100)
                editor.controls[key].setValue(37)
                self.assertEqual(slider.value(), 37)
                self.assertEqual(second.controls[key].value(), 37)
        self.assert_music_unchanged(untouched)

    def test_wheel_over_controls_does_not_mutate_saved_appearance(self):
        editor = self.editor()
        untouched_live, untouched_music = self.live.options, self.music.options
        controls = [editor.controls['font_family'], editor.controls['size'].spin,
                    editor.controls['background_opacity'].spin, editor.sliders['background_opacity']]
        for control in controls:
            with self.subTest(control=control.accessibleName()):
                self.reveal(editor, control)
                control.setFocus(Qt.FocusReason.MouseFocusReason)
                position = control.rect().center()
                event = QWheelEvent(QPointF(position), QPointF(control.mapToGlobal(position)), QPoint(),
                                    QPoint(0, -120), Qt.MouseButton.NoButton,
                                    Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                QApplication.sendEvent(control, event)
                self.app.processEvents()
                self.assertEqual(self.live.options, untouched_live)
        self.assert_music_unchanged(untouched_music)

    def test_reset_button_restores_only_its_bound_profile_and_saved_defaults(self):
        editor = self.editor()
        music_editor = self.editor(self.music)
        self.live.update(size=51, foreground='#abcdef', blur_translation=True)
        untouched_music = self.music.options
        self.click(editor, editor.reset_button)
        self.assertEqual(self.live.options['size'], 22)
        self.assertFalse(self.live.options['translation_first'])
        self.assertFalse(self.live.options['blur_translation'])
        self.assert_music_unchanged(untouched_music)
        untouched_live = self.live.options
        self.click(music_editor, music_editor.reset_button)
        self.assertEqual(self.music.options['size'], 24)
        self.assertTrue(self.music.options['translation_first'])
        self.assertEqual(self.live.options, untouched_live)
        self.assertEqual(CaptionAppearance(QSettings(str(self.settings_path), QSettings.Format.IniFormat)).options,
                         untouched_live)

    def test_each_group_reset_preserves_other_groups_and_the_music_profile(self):
        editor = self.editor()
        untouched_music = self.music.options
        groups = (
            ('布局与背景', ('background', 'background_opacity', 'radius', 'spacing', 'show_source', 'translation_first')),
            ('文字样式', ('font_family', 'size', 'foreground', 'secondary', 'text_opacity', 'shadow')),
            ('阅读效果', ('blur_translation', 'dashed_translation', 'dim_original')),
        )
        for title, fields in groups:
            with self.subTest(group=title):
                self.live.update(style='outline', background='#234567', background_opacity=34, radius=9,
                                 spacing=20, show_source=False, translation_first=True, font_family='DejaVu Sans',
                                 size=37, foreground='#345678', secondary='#456789', text_opacity=67, shadow=True,
                                 blur_translation=True, dashed_translation=True, dim_original=True)
                expected = self.live.options
                expected.update({key: self.live.defaults[key] for key in fields})
                reset = next(button for button in editor.findChildren(QPushButton)
                             if button.accessibleName() == '恢复' + title + '默认设置')
                self.click(editor, reset)
                self.assertEqual(self.live.options, expected)
                self.assert_music_unchanged(untouched_music)

    def test_editor_opens_separately_without_expanding_the_music_page(self):
        window = self.keep(QWidget())
        window.settings = self.settings
        page = MusicPage(window)
        self.music_pages.append(page)
        QVBoxLayout(window).addWidget(page)
        window.resize(680, 900)
        window.show()
        page.appearance_toggle.setChecked(True)
        page.caption_appearance.update(size=64)
        editor = page.appearance_editor
        for theme in ('light', 'dark'):
            apply_theme(self.app, theme)
            for index in range(editor.tabs.count()):
                with self.subTest(theme=theme, tab=index):
                    editor.tabs.setCurrentIndex(index)
                    self.app.processEvents()
                    self.assertLessEqual(window.width(), 680)
                    self.assertTrue(page.appearance_dialog.isVisible())
                    self.assertTrue(page.appearance_dialog.isWindow())
                    self.assertGreaterEqual(editor.pages.height(), 80)
                    for scroll in page.findChildren(QScrollArea):
                        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                    self.assert_editor_fits_horizontally(editor)

    def test_tiny_dialog_scrolls_instead_of_growing_off_screen(self):
        dialog = self.keep(appearance_dialog(None, self.live))
        dialog.resize(420, 320)
        dialog.show()
        self.app.processEvents()
        self.assertLessEqual(dialog.height(), 320)
        self.assertGreater(dialog.editor_scroll.verticalScrollBar().maximum(), 0)
        done = next(b for b in dialog.findChildren(QPushButton) if b.text() == '完成')
        self.assertTrue(dialog.rect().contains(done.mapTo(dialog, done.rect().center())))

    def test_dialog_keeps_preview_visible_across_tabs_and_closes_without_resetting(self):
        dialog = self.keep(appearance_dialog(None, self.live))
        self.assertIsInstance(dialog, QDialog)
        dialog.resize(560, 700)
        dialog.show()
        self.app.processEvents()
        self.assertLessEqual(dialog.height(), 700)
        editor = dialog.findChild(CaptionAppearanceEditor)
        self.assertIsNotNone(editor)
        initial_preview_top = editor.preview.mapTo(dialog, QPoint()).y()
        for index in range(editor.tabs.count()):
            with self.subTest(tab=index):
                editor.tabs.setCurrentIndex(index)
                self.app.processEvents()
                self.assertFalse(editor.preview.visibleRegion().isEmpty())
                self.assertEqual(editor.preview.mapTo(dialog, QPoint()).y(), initial_preview_top)
                self.assertLessEqual(dialog.width(), 560)
                self.assert_editor_fits_horizontally(editor)
                for scroll in dialog.findChildren(QScrollArea):
                    self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
                    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                self.app.processEvents()
                self.assertFalse(editor.preview.visibleRegion().isEmpty())
                self.assertEqual(editor.preview.mapTo(dialog, QPoint()).y(), initial_preview_top)
        self.live.update(size=39, background_opacity=61)
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertFalse(dialog.isVisible())
        reopened = CaptionAppearance(QSettings(str(self.settings_path), QSettings.Format.IniFormat))
        self.assertEqual(reopened.options['size'], 39)
        self.assertEqual(reopened.options['background_opacity'], 61)

    def test_themed_number_fields_have_room_to_display_values_and_suffixes(self):
        editor = self.editor()
        self.live.update(size=64, background_opacity=100, text_opacity=100, radius=40, spacing=30)
        for theme in ('light', 'dark'):
            apply_theme(self.app, theme)
            self.app.processEvents()
            for key in ('size', 'background_opacity', 'text_opacity', 'radius', 'spacing'):
                with self.subTest(theme=theme, control=key):
                    control = editor.controls[key]
                    self.reveal(editor, control)
                    line_edit = control.spin.lineEdit()
                    required = line_edit.fontMetrics().horizontalAdvance(line_edit.text()) + 4
                    self.assertGreaterEqual(line_edit.width(), required)
                    self.assertTrue(control.spin.rect().contains(line_edit.geometry()))

    def test_maximum_font_preview_is_not_clipped_or_overlapped_in_a_small_dialog(self):
        dialog = self.keep(appearance_dialog(None, self.live))
        self.live.update(size=64, spacing=30, dashed_translation=True)
        dialog.resize(560, 660)
        dialog.show()
        self.app.processEvents()
        editor = dialog.findChild(CaptionAppearanceEditor)
        self.assertLessEqual(dialog.height(), 660)
        for text in (editor.preview.source, editor.preview.translation):
            self.assertTrue(text.isVisible())
            self.assertTrue(editor.preview.rect().contains(text.geometry()))
            origin = text.mapTo(editor.preview_stage, QPoint())
            self.assertGreaterEqual(origin.y(), 0)
            self.assertLessEqual(origin.y() + text.height(), editor.preview_stage.height())
        stage_bottom = editor.preview_stage.mapTo(editor, QPoint()).y() + editor.preview_stage.height()
        meta_top = editor.current_preset_label.mapTo(editor, QPoint()).y()
        meta_bottom = meta_top + editor.current_preset_label.height()
        self.assertGreaterEqual(meta_top, stage_bottom)
        self.assertGreaterEqual(editor.tabs.y(), meta_bottom)
        self.assertGreaterEqual(editor.pages.y(), editor.tabs.y() + editor.tabs.height())
        self.assert_editor_fits_horizontally(editor)

    def test_slider_has_no_outer_frame_and_focus_changes_only_its_handle(self):
        editor = self.editor()
        slider = editor.sliders['background_opacity']
        self.reveal(editor, slider)
        editor.background_button.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        self.assertFalse(slider.hasFocus())
        unfocused = slider.grab().toImage()
        self.assertEqual(unfocused.pixelColor(unfocused.width() // 4, 0).alpha(), 0)
        slider.setFocus(Qt.FocusReason.TabFocusReason)
        self.app.processEvents()
        focused = slider.grab().toImage()
        self.assertTrue(slider.hasFocus())
        self.assertEqual(focused.pixelColor(focused.width() // 4, 0).alpha(), 0)
        self.assertNotEqual(unfocused, focused, 'keyboard focus must retain a visible handle indicator')


if __name__ == '__main__':
    unittest.main()
