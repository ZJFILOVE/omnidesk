"""Music page controls and process lifecycle, using only a local fake worker."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QProcess, QSettings, Qt, QUrl
from PyQt6.QtGui import QPalette, QTextDocument
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QGraphicsBlurEffect, QScrollArea, QVBoxLayout, QWidget

import music_ui
from ui_theme import apply_theme


def state(**overrides):
    return {'available': True, 'playing': True, 'identity': 'Adam',
            'track': {'id': '/com/apple/music/track/123', 'title': 'Test Song',
                      'artist': 'Singer', 'album': 'Album', 'duration': 180},
            'position': 45.25, 'volume': .37,
            'capabilities': {key: True for key in ('play_pause', 'previous', 'next', 'seek', 'volume', 'device')},
            'devices': [{'id': '10', 'description': 'Speakers'},
                        {'id': '20', 'description': 'Headphones', 'selected': True}],
            'lyrics': {'status_text': '已有中文', 'translation_status': 'existing', 'current_index': 1,
                       'lines': [{'id': 'first', 'start': 0, 'text': '<b>Original</b>', 'translation': '中文第一行'},
                                 {'id': 'second', 'start': 40, 'text': 'Original two', 'translation': '中文第二行'}]},
            **overrides}


WORKER = '''
import json, sys, time
from pathlib import Path
def log(data):
    with Path('events.jsonl').open('a') as handle:
        handle.write(json.dumps(data) + '\\n')
line = sys.stdin.readline()
if not line:
    sys.exit(0)
config = json.loads(line)
log({'config': config})
print(json.dumps({'state': INITIAL}), flush=True)
for line in sys.stdin:
    data = json.loads(line)
    log(data)
    if data['action'] == 'bad_output':
        print('[]', flush=True)
        print('null', flush=True)
        print('invalid JSON', flush=True)
        print(json.dumps({'state': 123}), flush=True)
        print(json.dumps({'state': {**INITIAL, 'position': 60}}), flush=True)
delay = Path('eof_delay')
if delay.exists():
    time.sleep(float(delay.read_text()))
'''.replace('INITIAL', repr(state()))


class MusicUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-music-ui-')
        self.root = Path(self.tmp.name)
        (self.root / '.venv/bin').mkdir(parents=True)
        (self.root / '.venv/bin/python').symlink_to(sys.executable)
        (self.root / 'music_service.py').write_text(WORKER)
        self.root_patch = patch.object(music_ui, 'ROOT', self.root)
        self.root_patch.start()
        self.settings = QSettings(str(self.root / 'settings.ini'), QSettings.Format.IniFormat)
        self.window = QWidget()
        self.window.settings = self.settings
        self.page = music_ui.MusicPage(self.window)
        QVBoxLayout(self.window).addWidget(self.page)
        self.window.resize(680, 900)
        apply_theme(self.app, 'light')
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.shutdown()
        for process in self.page.findChildren(QProcess):
            if not sip.isdeleted(process) and process.state() != QProcess.ProcessState.NotRunning:
                process.kill()
                process.waitForFinished(2000)
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.root_patch.stop()
        self.tmp.cleanup()

    def wait_for(self, predicate, timeout=4000):
        end = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < end:
            QTest.qWait(10)
        self.assertTrue(predicate(), 'music page operation timed out')

    def events(self):
        try:
            return [json.loads(line) for line in (self.root / 'events.jsonl').read_text().splitlines() if line]
        except (OSError, ValueError):
            return []

    def commands(self):
        return [event for event in self.events() if 'action' in event]

    def launch(self):
        self.page.activate()
        self.wait_for(lambda: self.page.state.get('available'))
        return self.page.process

    def test_state_populates_actual_controls_without_sending_commands(self):
        self.launch()
        self.assertEqual(self.page.title.text(), 'Test Song')
        self.assertEqual(self.page.artist.text(), 'Singer · Album')
        self.assertEqual(self.page.play.text(), '暂停')
        self.assertEqual(self.page.progress.maximum(), 180000)
        self.assertEqual(self.page.progress.value(), 45250)
        self.assertEqual(self.page.time.text(), '0:45 / 3:00')
        self.assertEqual(self.page.volume.value(), 37)
        self.assertEqual(self.page.devices.currentData(), '20')
        self.assertEqual(self.commands(), [])
        self.page.apply_state(state(volume=.62, position=50))
        self.app.processEvents()
        self.assertEqual(self.page.volume.value(), 62)
        self.assertEqual(self.commands(), [])

    def test_native_playback_buttons_dispatch_displayed_track_id(self):
        self.launch()
        QTest.mouseClick(self.page.play, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.page.next, Qt.MouseButton.LeftButton)
        QTest.mouseClick(self.page.previous, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: len(self.commands()) == 3)
        self.assertEqual([event['action'] for event in self.commands()], ['play_pause', 'next', 'previous'])
        self.assertTrue(all(event['track_id'] == '/com/apple/music/track/123' for event in self.commands()))

    def test_keyboard_and_page_click_seek_and_keyboard_volume(self):
        self.launch()
        self.page.progress.setFocus()
        QTest.keyClick(self.page.progress, Qt.Key.Key_Right)
        self.wait_for(lambda: any(event['action'] == 'seek' for event in self.commands()))
        seek = [event for event in self.commands() if event['action'] == 'seek'][-1]
        self.assertAlmostEqual(seek['value'], self.page.progress.value() / 1000)
        count = len(self.commands())
        QTest.mouseClick(self.page.progress, Qt.MouseButton.LeftButton,
                         pos=QPoint(self.page.progress.width() - 15, self.page.progress.height() // 2))
        self.wait_for(lambda: len(self.commands()) > count)
        self.assertEqual(self.commands()[-1]['action'], 'seek')
        self.page.volume.setFocus()
        QTest.keyClick(self.page.volume, Qt.Key.Key_Right)
        self.wait_for(lambda: self.commands()[-1]['action'] == 'volume')
        self.assertAlmostEqual(self.commands()[-1]['value'], .38)

    def test_device_activated_and_lyric_anchor_seek(self):
        self.launch()
        self.page.devices.setCurrentIndex(0)
        self.page.devices.activated.emit(0)
        self.page.reading.anchorClicked.emit(QUrl('seek:40'))
        self.wait_for(lambda: len(self.commands()) == 2)
        self.assertEqual(self.commands()[0]['action'], 'device')
        self.assertEqual(self.commands()[0]['value'], '10')
        self.assertEqual(self.commands()[1]['value'], 40)

    def test_polling_does_not_overwrite_sliders_during_drag(self):
        self.launch()
        self.page.progress.setSliderDown(True)
        self.page.volume.setSliderDown(True)
        self.page.progress.setValue(90000)
        self.page.volume.setValue(80)
        self.page.apply_state(state(volume=.2, position=15))
        self.assertEqual(self.page.progress.value(), 90000)
        self.assertEqual(self.page.volume.value(), 80)
        self.page.progress.setSliderDown(False)
        self.page.volume.setSliderDown(False)
        self.wait_for(lambda: len(self.commands()) == 2)
        self.assertEqual([event['value'] for event in self.commands()], [90, .8])

    def test_lyrics_highlight_escapes_markup_and_changes_with_current_line(self):
        self.page.apply_state(state())
        document = self.page.reading.document()
        self.assertIn('<b>Original</b>', self.page.reading.toPlainText())
        active = document.find('中文第二行').charFormat()
        self.assertEqual(active.foreground().color().name(), self.page.palette().color(QPalette.ColorRole.Highlight).name())
        self.assertEqual(active.fontWeight(), 700)
        value = state()
        value['lyrics']['current_index'] = 0
        self.page.apply_state(value)
        self.assertEqual(document.find('中文第一行').charFormat().fontWeight(), 700)
        self.assertEqual(document.find('中文第二行').charFormat().fontWeight(), 400)

    def test_follow_scroll_can_be_disabled_for_manual_reading(self):
        lines = [{'id': str(index), 'start': index * 5, 'text': f'Line {index}',
                  'translation': f'中文 {index}'} for index in range(60)]
        value = state(lyrics={'lines': lines, 'current_index': 40})
        self.page.apply_state(value)
        self.app.processEvents()
        scroll = self.page.reading.verticalScrollBar()
        self.assertGreater(scroll.value(), 0)
        self.page.follow.setChecked(False)
        scroll.setValue(30)
        value = copy.deepcopy(value)
        value['lyrics']['current_index'] = 50
        self.page.apply_state(value)
        self.assertEqual(scroll.value(), 30)

    def test_utf8_bom_import_dispatches_lyrics_and_track_id(self):
        self.launch()
        path = self.root / '中文歌词.lrc'
        path.write_text('[00:01.00]你好\n', encoding='utf-8-sig')
        with patch.object(music_ui, 'choose_file', return_value=str(path)):
            self.page.import_lyrics()
        self.wait_for(lambda: len(self.commands()) == 1)
        self.assertEqual(self.commands()[0], {'action': 'import_lyrics', 'value': None,
                         'lyrics': '[00:01.00]你好\n', 'track_id': '/com/apple/music/track/123'})

    def test_song_change_inside_file_picker_cannot_attach_lyrics_to_new_song(self):
        self.launch()
        path = self.root / 'lyrics.lrc'
        path.write_text('[00:01.00]Wrong song')
        def choose(*args):
            value = state()
            value['track']['id'] = '/com/apple/music/track/999'
            self.page.apply_state(value)
            return str(path)
        with patch.object(music_ui, 'choose_file', side_effect=choose):
            self.page.import_lyrics()
        self.app.processEvents()
        self.assertEqual(self.commands(), [])
        self.assertIn('歌曲已切换', self.page.error.text())

    def test_invalid_utf8_and_oversize_import_are_read_errors(self):
        self.launch()
        path = self.root / 'invalid.lrc'
        for data in (b'\xff\xff', b'x' * (1024 * 1024 + 1)):
            path.write_bytes(data)
            with patch.object(music_ui, 'choose_file', return_value=str(path)):
                self.page.import_lyrics()
            self.assertIn('UTF-8', self.page.error.text())
        self.assertEqual(self.commands(), [])

    def test_automatic_mode_default_config_and_manual_switch_restart(self):
        old = self.launch()
        self.assertEqual(self.page.translation_mode.currentData(), 'automatic')
        self.assertTrue(self.events()[0]['config']['music_translation_enabled'])
        self.page.translation_mode.setCurrentIndex(self.page.translation_mode.findData('manual'))
        self.wait_for(lambda: self.page.process is not None and self.page.process is not old and self.page.state.get('available'))
        configs = [event['config'] for event in self.events() if 'config' in event]
        self.assertEqual(len(configs), 2)
        self.assertFalse(configs[-1]['music_translation_enabled'])
        self.assertEqual(configs[-1]['music_translation_mode'], 'manual')
        self.assertEqual(self.settings.value('music/translation_mode'), 'manual')
        self.assertIn('点击', self.page.translation_hint.text())
        QTest.mouseClick(self.page.translate_button, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: len(self.commands()) == 1)
        self.assertEqual(self.commands()[0]['action'], 'translate_lyrics')

    def test_legacy_translation_preference_migrates_to_manual_mode(self):
        settings = QSettings(str(self.root / 'legacy.ini'), QSettings.Format.IniFormat)
        settings.setValue('music/translate', False)
        window = QWidget()
        window.settings = settings
        page = music_ui.MusicPage(window)
        try:
            self.assertEqual(page.translation_mode.currentData(), 'manual')
            self.assertEqual(settings.value('music/translation_mode'), 'manual')
        finally:
            page.shutdown()
            window.deleteLater()

    def test_stop_and_late_state_keep_controls_disabled_until_process_exits(self):
        process = self.launch()
        (self.root / 'eof_delay').write_text('.25')
        self.page.stop()
        self.assertIs(self.page.process, process)
        self.page.apply_state(state())
        self.assertFalse(self.page.play.isEnabled())
        self.assertFalse(self.page.translate_button.isEnabled())
        self.assertFalse(self.page.progress.isEnabled())
        self.wait_for(lambda: self.page.process is None)
        self.assertFalse(self.page.state.get('available'))
        self.assertFalse(self.page.next.isEnabled())
        self.assertIn('已停止', self.page.status.text())
        self.assertNotIn('中文第一行', self.page.reading.toPlainText())

    def test_disable_stops_and_shutdown_prevents_restart(self):
        self.launch()
        self.page.enabled.setChecked(False)
        self.wait_for(lambda: self.page.process is None)
        self.assertFalse(self.settings.value('music/enabled', True, type=bool))
        self.page.enabled.setChecked(True)
        self.wait_for(lambda: self.page.state.get('available'))
        self.assertFalse(self.page.shutdown())
        self.page.translation_mode.setCurrentIndex(1)
        self.wait_for(lambda: self.page.process is None)
        QTest.qWait(30)
        self.assertIsNone(self.page.process)
        self.assertTrue(self.page.shutdown())
        self.assertFalse(self.page.play.isEnabled())

    def test_failed_start_releases_process_and_close_during_start_is_safe(self):
        (self.root / '.venv/bin/python').unlink()
        self.page.activate()
        self.wait_for(lambda: self.page.process is None)
        self.assertTrue(self.page.connect_button.isEnabled())
        self.assertIn('重试', self.page.status.text())
        (self.root / '.venv/bin/python').symlink_to(sys.executable)
        self.page.activate()
        self.assertFalse(self.page.shutdown())
        self.wait_for(lambda: self.page.process is None)
        self.assertTrue(self.page.shutdown())

    def test_malformed_worker_output_does_not_crash_and_later_state_is_read(self):
        self.launch()
        self.page.command('bad_output')
        self.wait_for(lambda: self.page.state.get('position') == 60)
        self.assertEqual(self.page.progress.value(), 60000)

    def test_palette_changes_refresh_lyrics_and_layout_fits_680_pixels(self):
        self.page.apply_state(state())
        scroll = self.page.findChild(QScrollArea)
        for theme in ('dark', 'light'):
            apply_theme(self.app, theme)
            self.app.processEvents()
            QTest.qWait(10)
            active = self.page.reading.document().find('中文第二行').charFormat()
            self.assertEqual(active.foreground().color().name(), self.page.palette().color(QPalette.ColorRole.Highlight).name())
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0, f'{theme} page overflows narrow layout')
            self.assertLessEqual(self.window.width(), 680)

    def test_floating_lyrics_use_existing_state_without_starting_a_worker(self):
        self.page.apply_state(state())
        self.page.toggle_caption_window()
        floating = self.page.caption_window
        surface = floating.surfaces['lyrics']
        self.assertTrue(floating.isVisible())
        self.assertEqual(floating.windowTitle(), 'Qwen 悬浮歌词')
        self.assertEqual(surface.translation.text(), '中文第二行')
        self.assertEqual(surface.source.text(), 'Original two')
        self.assertIsNone(self.page.process)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.page.caption_button.text(), '隐藏悬浮歌词')
        floating.hide()
        self.assertEqual(self.page.caption_button.text(), '打开悬浮歌词')
        self.page.toggle_caption_window()
        self.assertIs(self.page.caption_window, floating)
        self.assertTrue(floating.isVisible())

    def test_floating_lyrics_follow_worker_index_and_pause_without_own_clock(self):
        worker = self.launch()
        self.page.toggle_caption_window()
        floating = self.page.caption_window
        surface = floating.surfaces['lyrics']
        paused = state(playing=False, position=90)
        self.page.apply_state(paused)
        self.assertEqual(surface.translation.text(), '中文第二行')
        self.assertIn('已暂停', surface.notice.text())
        earlier = state(position=10)
        earlier['lyrics']['current_index'] = 0
        self.page.apply_state(earlier)
        self.assertEqual(surface.translation.text(), '中文第一行')
        self.assertNotIn('已暂停', surface.notice.text())
        floating.close()
        self.assertIs(self.page.process, worker)
        self.assertEqual(self.commands(), [])
        self.assertFalse(self.page.shutdown())
        self.assertIsNone(self.page.caption_window)

    def test_floating_lyrics_clear_old_song_on_track_change_disconnect_and_stop(self):
        self.launch()
        self.page.toggle_caption_window()
        surface = self.page.caption_window.surfaces['lyrics']
        new_track = state(track={'id': 'new', 'title': 'Next song'}, lyrics={'status_text': '正在读取歌词…'})
        self.page.apply_state(new_track)
        self.assertEqual(surface.source.text(), '')
        self.assertEqual(surface.translation.text(), '正在读取歌词…')
        self.assertIn('Next song', surface.notice.text())
        self.page.apply_state(state(available=False))
        self.assertEqual(surface.translation.text(), '等待 Apple Music 播放…')
        self.assertEqual(surface.source.text(), '')
        self.page.apply_state(state())
        self.page.stop()
        self.assertEqual(surface.translation.text(), '等待 Apple Music 播放…')

    def test_untimed_and_chinese_lyrics_are_not_falsely_synchronized_or_duplicated(self):
        self.page.toggle_caption_window()
        surface = self.page.caption_window.surfaces['lyrics']
        self.page.apply_state(state(lyrics={'lines': [{'text': 'No timestamp', 'translation': '没有时间轴'}],
                                           'current_index': 0}))
        self.assertEqual(surface.translation.text(), '当前歌词没有时间轴')
        self.assertIn('没有时间轴', self.page.reading.toPlainText())
        self.page.apply_state(state(lyrics={'lines': [{'start': 0, 'text': '中文本来如此', 'translation': '中文本来如此'}],
                                           'current_index': 0}))
        self.assertEqual(surface.source.text(), '')
        self.assertEqual(surface.translation.text(), '中文本来如此')
        self.assertEqual(self.page.reading.toPlainText().count('中文本来如此'), 1)
        self.page.caption_appearance.update(show_source=False)
        self.page.apply_state(state(lyrics={'lines': [{'start': 0, 'text': 'Original only'}], 'current_index': 0}))
        self.assertEqual(surface.translation.text(), 'Original only')
        self.assertIn('Original only', self.page.reading.toPlainText())

    def test_lyrics_appearance_updates_both_views_and_has_independent_persistence(self):
        self.settings.setValue('caption_size', 39)
        self.page.apply_state(state())
        self.page.toggle_caption_window()
        surface = self.page.caption_window.surfaces['lyrics']
        self.page.caption_appearance.update(size=30, foreground='#123456', secondary='#654321',
                                            background='#f0f1f2', background_opacity=40,
                                            text_opacity=75, font_family='DejaVu Sans', show_source=False)
        self.assertNotIn('Original two', self.page.reading.toPlainText())
        self.assertTrue(surface.source.isHidden())
        translated = self.page.reading.document().find('中文第二行').charFormat()
        self.assertEqual(translated.foreground().color().name(), '#123456')
        self.assertAlmostEqual(translated.foreground().color().alphaF(), .75, delta=.01)
        self.assertEqual(self.page.reading.document().defaultFont().family(), 'DejaVu Sans')
        self.assertIn('30px', surface.translation.styleSheet())
        self.assertIn('40', self.page.reading.styleSheet())
        self.assertEqual(int(self.settings.value('caption_size')), 39)
        self.assertEqual(int(self.settings.value('music_caption_size')), 30)
        self.page.caption_appearance.update(show_source=True, translation_first=False)
        plain = self.page.reading.toPlainText()
        self.assertLess(plain.index('Original two'), plain.index('中文第二行'))
        self.page.caption_appearance.update(translation_first=True)
        plain = self.page.reading.toPlainText()
        self.assertLess(plain.index('中文第二行'), plain.index('Original two'))
        self.assertEqual(surface.layout.indexOf(surface.translation), 0)

    def test_expanded_lyrics_appearance_fits_narrow_page(self):
        before = self.page.sizeHint()
        self.page.appearance_toggle.setChecked(True)
        self.app.processEvents()
        scroll = self.page.findChild(QScrollArea)
        self.assertTrue(self.page.appearance_editor.isVisible())
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        self.assertEqual(self.page.sizeHint(), before)
        self.assertTrue(self.page.appearance_dialog.isWindow())

    def test_click_through_and_reset_are_accessible_outside_the_overlay(self):
        self.page.toggle_caption_window()
        overlay = self.page.caption_window
        self.page.click_through.setChecked(True)
        self.assertTrue(overlay.windowHandle().flags() & Qt.WindowType.WindowTransparentForInput)
        self.page.caption_appearance.update(click_through=False, size=40)
        self.assertFalse(self.page.click_through.isChecked())
        self.page.reset_appearance_button.click()
        self.assertEqual(self.page.caption_appearance.options['size'], 24)
        self.assertFalse(self.page.click_through.isChecked())
        overlay.edit_appearance()
        self.app.processEvents()
        self.assertTrue(self.page.appearance_dialog.isVisible())
        self.assertIsNone(overlay.settings_dialog)

    def test_blurred_translation_reveals_on_hover_without_blurring_original_or_moving_scroll(self):
        self.launch()
        self.page.caption_appearance.update(style='blur')
        scroll = self.page.findChild(QScrollArea)
        scroll.ensureWidgetVisible(self.page.reading)
        self.app.processEvents()
        viewport = self.page.reading.viewport()
        target = next((QPoint(x, y) for y in range(0, viewport.height(), 4)
                       for x in range(0, viewport.width(), 8)
                       if self.page.reading.anchorAt(QPoint(x, y)) == 'lyric:1'), None)
        self.assertIsNotNone(target, 'the current translation should have a hover target')
        original = self.page.reading.document().find('Original two')
        self.assertFalse(original.isNull(), 'the original should remain native selectable text')
        before = viewport.grab().toImage()
        position = self.page.reading.verticalScrollBar().value()
        QTest.mouseMove(viewport, target)
        self.app.processEvents()
        self.assertEqual(self.page._lyric_hover_index, 1)
        self.assertEqual(self.page.reading.verticalScrollBar().value(), position)
        self.assertNotEqual(before, viewport.grab().toImage())
        self.assertIn('中文第二行', self.page.reading.accessibleDescription())
        self.page.reading.anchorClicked.emit(QUrl('lyric:1'))
        self.wait_for(lambda: len(self.commands()) == 1)
        self.assertEqual(self.commands()[0]['value'], 40)
        self.app.sendEvent(viewport, QEvent(QEvent.Type.Leave))
        self.assertEqual(self.page._lyric_hover_index, -1)

    def test_combined_subtitle_templates_and_reset_preserve_music_defaults(self):
        self.page.apply_state(state())
        self.page.toggle_caption_window()
        self.page.caption_appearance.update(blur_translation=True, dashed_translation=True, dim_original=True)
        original = self.page.reading.document().find('Original two').charFormat()
        self.assertAlmostEqual(original.foreground().color().alphaF(), .6, delta=.01)
        surface = self.page.caption_window.surfaces['lyrics']
        self.assertAlmostEqual(surface.translation.graphicsEffect().blurRadius(), 6)
        self.assertTrue(surface.translation.underline_color)
        self.assertIn('lyric-image:', self.page.reading.toHtml())
        self.page.caption_appearance.reset()
        self.assertEqual(self.page.caption_appearance.options['size'], 24)
        self.assertTrue(self.page.caption_appearance.options['translation_first'])
        self.assertIn('中文第二行', self.page.reading.toPlainText())
        self.assertNotIn('lyric-image:', self.page.reading.toHtml())

    def test_large_blurred_lyrics_render_visible_rows_and_reuse_bounded_resources(self):
        lines = [{'start': index * 3, 'text': f'Original {index}', 'translation': f'中文歌词第 {index} 行'}
                 for index in range(200)]
        value = state(lyrics={'lines': lines, 'current_index': 100})
        self.page.apply_state(value)
        self.page.caption_appearance.update(style='blur')
        self.app.processEvents()
        document = self.page.reading.document()
        def resource(index):
            return document.resource(QTextDocument.ResourceType.ImageResource, QUrl(f'lyric-image:{index}'))
        self.assertGreater(resource(100).width(), 1)
        self.assertEqual(resource(0).width(), 1)
        self.assertLess(sum(resource(index).width() > 1 for index in range(200)), 30)
        with patch.object(music_ui, 'QGraphicsScene', wraps=music_ui.QGraphicsScene) as renderer:
            value['lyrics']['current_index'] = 101
            self.page.apply_state(value)
            self.app.processEvents()
            self.assertLess(renderer.call_count, 12, 'advancing one lyric must not redraw the entire song')
        self.page.follow.setChecked(False)
        self.page.reading.verticalScrollBar().setValue(self.page.reading.verticalScrollBar().maximum())
        self.app.processEvents()
        self.assertGreater(resource(199).width(), 1)
        self.assertEqual(resource(100).width(), 1)
        self.assertLessEqual(self.page._lyric_image_bytes, 64 * 1024 * 1024)
        self.page.apply_state(state())
        self.app.processEvents()
        self.assertEqual(resource(199).width(), 1, 'switching songs releases old document image data')

    def test_original_only_and_waiting_remain_readable_with_translation_blur_enabled(self):
        self.page.caption_appearance.update(style='blur', blur_translation=True, dashed_translation=True)
        self.page.toggle_caption_window()
        surface = self.page.caption_window.surfaces['lyrics']
        self.assertNotIsInstance(surface.translation.graphicsEffect(), QGraphicsBlurEffect)
        value = state(lyrics={'lines': [{'start': 0, 'text': 'Original only'}], 'current_index': 0})
        self.page.apply_state(value)
        self.assertEqual(surface.translation.text(), 'Original only')
        self.assertNotIsInstance(surface.translation.graphicsEffect(), QGraphicsBlurEffect)
        self.assertIn('Original only', self.page.reading.toPlainText())
        self.assertNotIn('<img', self.page.reading.toHtml())
        value['lyrics']['lines'][0]['translation'] = '现在有译文'
        self.page.apply_state(value)
        self.assertIsInstance(surface.translation.graphicsEffect(), QGraphicsBlurEffect)
        self.assertIn('<img', self.page.reading.toHtml())


if __name__ == '__main__':
    unittest.main()
