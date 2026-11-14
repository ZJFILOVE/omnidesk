"""Exercise real Qt process signals with a local worker, without API or keychain access."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

import native_app


WORKER = r'''
import json, sys
config = json.loads(sys.stdin.readline())
action = config['action']
if action == 'devices':
    print(json.dumps({'sinks': [{'description':'Test headphones','name':'headphones','mute':False},
                              {'description':'meeting-remote','name':'meeting-remote','mute':False}],
                      'sources': [{'description':'Test microphone','name':'microphone','mute':False},
                                  {'description':'Monitor of Test headphones','name':'headphones.monitor','mute':False}],
                      'asr_ready': True, 'default_sink': 'headphones', 'voices': ['Tina','Cherry']}), flush=True)
elif action == 'lookup':
    print(json.dumps({'key':'test-credential'}), flush=True)
elif action in ('store','clear'):
    print(json.dumps({'key':''}), flush=True)
elif action == 'codex_models':
    print(json.dumps({'models': [
        {'model':'gpt-5.6-luna','displayName':'GPT-5.6-Luna','defaultReasoningEffort':'medium',
         'supportedReasoningEfforts':[{'reasoningEffort':'low'},{'reasoningEffort':'medium'},{'reasoningEffort':'high'}]},
        {'model':'test-model','displayName':'Test Model','description':'Test description',
         'supportedReasoningEfforts':[{'reasoningEffort':'high'}]}]}), flush=True)
elif action == 'antigravity_models':
    print(json.dumps({'models': [{'model': 'gemini-flash-low', 'displayName': 'Flash (Low)'},
                                {'model': 'sonnet', 'displayName': 'Sonnet'}]}), flush=True)
elif action == 'translate':
    if config.get('mode') == 'subtitles':
        assert 'api_key' not in config, 'Voice API credential must not be sent to subtitle worker'
    if config.get('api_key') == 'fail-credential':
        print(json.dumps({'error':'401 invalid API key fail-credential'}), flush=True)
        sys.exit(1)
    print(json.dumps({'direction':'outbound','status':'listening'}), flush=True)
    print(json.dumps({'direction':'outbound','source_final':'你好 <script>not markup</script>'}), flush=True)
    print(json.dumps({'direction':'outbound','translation_final':'Hello & welcome.'}), flush=True)
    sys.stdin.readline()
'''


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / '.venv/bin').mkdir(parents=True)
        (self.root / '.venv/bin/python').symlink_to(sys.executable)
        (self.root / 'native_worker.py').write_text(WORKER)
        self.patch = patch.object(native_app, 'APP_DIR', self.root)
        self.patch.start()
        self.settings = QSettings(str(self.root / 'preferences.ini'), QSettings.Format.IniFormat)
        self.settings.setValue('onboarded', True)
        self.settings.setValue('music/enabled', False)
        self.w = native_app.Translator(self.settings)
        self.w.show()
        self.wait_for(lambda: not self.w.jobs)

    def tearDown(self):
        if self.w.session:
            self.w.stop()
            self.wait_for(lambda: self.w.session is None)
        self.wait_for(lambda: not self.w.jobs)
        self.w.close()
        self.app.processEvents()
        self.patch.stop()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=3000):
        end = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < end:
            QTest.qWait(10)
        self.assertTrue(predicate(), 'Qt operation timed out')

    def test_success_lifecycle_and_metadata_privacy(self):
        self.assertTrue(self.w.start_button.isEnabled())
        self.assertEqual(self.w.fields['headphones'].count(), 1)
        self.w.start()
        self.assertFalse(self.w.fields['mode'].isEnabled())
        self.wait_for(lambda: len(self.w.transcripts['outbound'].entries) == 2)
        text = self.w.transcripts['outbound'].text.toPlainText()
        self.assertIn('<script>not markup</script>', text)
        self.assertIn('Hello & welcome.', text)
        self.assertIn('Hello & welcome.', self.w.overlay.lines['outbound'].text())
        self.assertEqual(self.w.badge.property('state'), 'active')
        self.w.set_theme('dark')
        self.assertEqual(self.w.transcripts['outbound'].text.toPlainText(), text)
        self.w.stop()
        self.wait_for(lambda: self.w.session is None)
        self.assertTrue(self.w.fields['mode'].isEnabled())
        self.assertTrue(self.w.start_button.isVisible())
        records = self.w.history_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], '完成')
        self.assertEqual(set(records[0]), {'started','mode','device','duration','status'})
        self.settings.sync()
        persisted = (self.root / 'preferences.ini').read_text()
        for secret in ('test-credential','Hello','script','你好'):
            self.assertNotIn(secret, persisted)
        self.w.navigate(1)
        self.assertEqual(self.w.history_table.rowCount(), 1)
        self.w.history_search.setText('no match')
        self.assertEqual(self.w.history_table.rowCount(), 0)

    def test_error_recovery_and_no_secret_in_error(self):
        self.w.key.setText('fail-credential')
        self.w.start()
        self.wait_for(lambda: self.w.session is None)
        self.assertFalse(self.w.error_card.isHidden())
        self.assertEqual(self.w.badge.property('state'), 'error')
        self.assertNotIn('fail-credential', self.w.error_detail.text())
        self.assertEqual(self.w.history_records()[0]['status'], '中断')
        self.w.recover()
        self.assertEqual(self.w.pages.currentIndex(), 3)
        self.w.key.setText('test-credential')
        self.w.start()
        self.wait_for(lambda: len(self.w.transcripts['outbound'].entries) == 2)
        self.assertTrue(self.w.error_card.isHidden())

    def test_close_ends_session_and_records_once(self):
        self.w.start()
        self.wait_for(lambda: len(self.w.transcripts['outbound'].entries) == 2)
        self.w.close()
        self.wait_for(lambda: self.w.session is None and not self.w.isVisible())
        self.assertEqual(len(self.w.history_records()), 1)

    def test_theme_navigation_resize_and_captions(self):
        for theme in ('light','dark'):
            self.w.set_theme(theme)
            for width, height in ((1380,900),(1000,780),(800,800),(680,660)):
                self.w.resize(width,height)
                QTest.qWait(5)
                self.assertEqual(self.w.width(), width)
                for index in range(4):
                    self.w.navigate(index)
                    self.app.processEvents()
                    self.assertEqual(self.w.pages.currentIndex(), index)
                for panel in self.w.transcripts.values():
                    self.assertGreaterEqual(panel.height(), 260)
        self.assertEqual(self.w.transcript_split.orientation(), Qt.Orientation.Vertical)
        self.assertEqual(self.settings.value('theme'), 'dark')
        self.w.toggle_captions()
        self.assertTrue(self.w.overlay.isVisible())
        self.w.overlay.compact_button.click()
        self.assertFalse(self.w.overlay.options.isHidden())
        self.w.overlay.compact_button.click()
        self.assertTrue(self.w.overlay.options.isHidden())
        self.w.overlay.pin.setChecked(False)
        self.assertFalse(bool(self.w.overlay.windowFlags() & Qt.WindowType.WindowStaysOnTopHint))

    def test_onboarding_and_mode_routes(self):
        self.w.show_onboarding()
        self.assertFalse(self.w.onboarding.isHidden())
        self.w.prepare_test()
        self.assertEqual(self.w.fields['mode'].currentData(), 'mic_test')
        self.assertIsNone(self.w.session)
        self.w.fields['mode'].setCurrentIndex(2)
        self.assertIn('不向耳机', self.w.route_hint.text())
        self.w.dismiss_onboarding()
        self.assertTrue(self.settings.value('onboarded', type=bool))

    def test_failed_worker_start_releases_controls(self):
        (self.root / '.venv/bin/python').unlink()
        self.w.remember.setChecked(False)
        self.w.start()
        self.wait_for(lambda: self.w.session is None)
        self.assertTrue(self.w.start_button.isEnabled())
        self.assertEqual(self.w.history_records()[0]['status'], '中断')

    def test_subtitle_mode_needs_no_voice_key_and_defaults_to_gpu(self):
        self.w.key.clear()
        self.w.fields['mode'].setCurrentIndex(self.w.fields['mode'].findData('subtitles'))
        self.assertEqual(self.w.fields['asr_backend'].currentData(), 'vulkan')
        self.assertTrue(self.w.start_button.isEnabled())
        self.assertTrue(self.w.subtitle_options.isVisible())
        self.w.start()
        self.wait_for(lambda: len(self.w.transcripts['outbound'].entries) == 2)
        self.assertIsNotNone(self.w.session)

    def test_late_translation_does_not_replace_newer_overlay(self):
        self.w.fields['mode'].setCurrentIndex(self.w.fields['mode'].findData('subtitles'))
        self.w.handle_event({'direction':'inbound','segment_id':1,'source_final':'First sentence'})
        self.w.handle_event({'direction':'inbound','segment_id':2,'source_final':'Second sentence'})
        self.w.handle_event({'direction':'inbound','segment_id':2,'translation_final':'第二句'})
        self.w.handle_event({'direction':'inbound','segment_id':1,'translation_final':'第一句'})
        self.assertIn('Second sentence',self.w.overlay.source_lines['inbound'].text())
        text=self.w.transcripts['inbound'].text.toPlainText()
        self.assertLess(text.index('第一句'),text.index('Second sentence'))
        self.assertIn('第二句',self.w.overlay.lines['inbound'].text())

    def test_overlay_keeps_readable_pair_while_new_source_waits(self):
        self.w.fields['subtitle_profile'].setCurrentIndex(self.w.fields['subtitle_profile'].findData('complete'))
        self.w.fields['mode'].setCurrentIndex(self.w.fields['mode'].findData('subtitles'))
        emit = self.w.handle_event
        emit({'direction':'inbound','segment_id':1,'source_final':'First sentence'})
        emit({'direction':'inbound','segment_id':2,'source_final':'Second sentence'})
        emit({'direction':'inbound','segment_id':1,'translation_final':'第一句'})
        self.assertEqual(self.w.overlay.lines['inbound'].text(), '第一句')
        self.assertEqual(self.w.overlay.source_lines['inbound'].text(), 'First sentence')
        emit({'direction':'inbound','segment_id':3,'source_final':'Third sentence'})
        self.assertEqual(self.w.overlay.lines['inbound'].text(), '第一句')
        emit({'direction':'inbound','segment_id':2,'translation_final':'第二句'})
        self.assertEqual(self.w.overlay.source_lines['inbound'].text(), 'Second sentence')
        self.assertEqual(self.w.overlay.lines['inbound'].text(), '第二句')

    def test_fast_subtitles_show_source_immediately_and_label_old_translation(self):
        self.w.fields['mode'].setCurrentIndex(self.w.fields['mode'].findData('subtitles'))
        self.assertEqual(self.w.fields['subtitle_profile'].currentData(), 'fast')
        emit = self.w.handle_event
        emit({'direction':'inbound','segment_id':1,'source_final':'First'})
        emit({'direction':'inbound','segment_id':1,'translation_final':'第一句'})
        emit({'direction':'inbound','segment_id':2,'source_final':'Second',
              'segment_seconds':4.0,'asr_queue_seconds':0.01,'asr_seconds':0.25})
        self.assertEqual(self.w.overlay.source_lines['inbound'].text(), 'Second')
        self.assertEqual(self.w.overlay.lines['inbound'].text(), '第一句')
        self.assertFalse(self.w.overlay.surfaces['inbound'].notice.isHidden())
        self.assertIn('ASR 0.25 s', self.w.pipeline_status.text())
        emit({'direction':'inbound','segment_id':2,'translation_final':'第二句',
              'translation_queue_seconds':0.2,'translation_seconds':4.5})
        self.assertTrue(self.w.overlay.surfaces['inbound'].notice.isHidden())
        self.assertIn('翻译排队 0.2 s', self.w.pipeline_status.text())

    def test_subtitle_profile_restored_and_locked_during_session(self):
        profile = self.w.fields['subtitle_profile']
        self.assertEqual(profile.count(), 2)
        profile.setCurrentIndex(profile.findData('complete'))
        self.assertEqual(self.settings.value('subtitle_profile'), 'complete')
        restored = native_app.Translator(self.settings, initialize=False)
        self.assertEqual(restored.fields['subtitle_profile'].currentData(), 'complete')
        restored.close()
        self.w.start()
        self.assertFalse(profile.isEnabled())
        self.w.stop()
        self.wait_for(lambda: self.w.session is None)
        self.assertTrue(profile.isEnabled())

    def test_overlay_styles_size_and_original_only(self):
        overlay = self.w.overlay
        for style in ('cinema', 'paper', 'clear'):
            overlay.style_combo.setCurrentIndex(overlay.style_combo.findData(style))
            self.assertEqual(self.settings.value('caption_style'), style)
            self.assertEqual(overlay.surfaces['inbound'].style, style)
        overlay.size_control.setValue(36)
        self.assertEqual(self.settings.value('caption_size', type=int), 36)
        overlay.original('inbound', 'Only source')
        self.assertEqual(overlay.lines['inbound'].text(), 'Only source')
        self.assertEqual(overlay.source_lines['inbound'].text(), '')

    def test_text_key_is_not_persisted_and_endpoint_change_clears_it(self):
        self.w.text_key.setText('test-text-secret')
        self.w.persist()
        self.assertNotIn('test-text-secret',(self.root/'preferences.ini').read_text())
        self.w.text_fields['text_endpoint'].setText('http://localhost:9999/v1')
        self.assertFalse(self.w.text_key.text())

    def test_codex_menu_refresh_keeps_selection_and_persists_model_id(self):
        combo = self.w.text_fields['codex_model']
        self.assertFalse(combo.isEditable())
        self.assertEqual(combo.count(), 2)
        self.assertEqual(combo.currentData(), 'gpt-5.6-luna')
        combo.setCurrentIndex(combo.findData('test-model'))
        self.assertEqual(self.settings.value('codex_model'), 'test-model')
        self.w.refresh_codex_models()
        self.assertFalse(self.w.codex_refresh_button.isEnabled())
        self.wait_for(lambda: not self.w.jobs)
        self.assertEqual(self.w.validate_text_config()['codex_model'], 'test-model')
        self.assertTrue(self.w.codex_refresh_button.isEnabled())
        restored = native_app.Translator(self.settings, initialize=False)
        self.assertEqual(restored.text_fields['codex_model'].currentData(), 'test-model')
        restored.close()

    def test_codex_menu_failure_and_missing_model_preserve_selection(self):
        combo = self.w.text_fields['codex_model']
        combo.setCurrentIndex(combo.findData('test-model'))
        self.w.populate_codex_models({'models':[{'model':'replacement','displayName':'Replacement'}]})
        self.assertEqual(combo.currentData(), 'test-model')
        self.assertIn('当前列表未提供', combo.currentText())
        (self.root / 'native_worker.py').write_text('print(\'{"error":"Catalog unavailable"}\')')
        self.w.refresh_codex_models()
        self.wait_for(lambda: not self.w.jobs)
        self.assertEqual(combo.currentData(), 'test-model')
        self.assertIn('Catalog unavailable', self.w.codex_model_status.text())
        self.assertTrue(self.w.codex_refresh_button.isEnabled())
        self.assertFalse(self.w.codex_models_loading)
        self.assertTrue(self.w.error_card.isHidden())

    def test_effort_options_follow_model_and_restore_per_model(self):
        model = self.w.text_fields['codex_model']
        effort = self.w.text_fields['codex_reasoning_effort']
        self.assertEqual(effort.currentData(), 'low')
        self.assertEqual(effort.findData('none'), -1)
        effort.setCurrentIndex(effort.findData('medium'))
        self.assertEqual(self.w.translation_config()['codex_reasoning_effort'], 'medium')
        model.setCurrentIndex(model.findData('test-model'))
        self.assertEqual(effort.findData('low'), -1)
        self.assertEqual(effort.currentData(), 'default')
        effort.setCurrentIndex(effort.findData('high'))
        model.setCurrentIndex(model.findData('gpt-5.6-luna'))
        self.assertEqual(effort.currentData(), 'medium')
        self.w.refresh_codex_models()
        self.wait_for(lambda: not self.w.jobs)
        self.assertEqual(effort.currentData(), 'medium')
        restored = native_app.Translator(self.settings)
        self.wait_for(lambda: not restored.jobs)
        self.assertEqual(restored.text_fields['codex_reasoning_effort'].currentData(), 'medium')
        restored.close()

    def test_usage_labels_distinguish_cached_input_and_local_reuse(self):
        text = self.w.usage_text({'usage': {'input_tokens': 100, 'cached_input_tokens': 50, 'output_tokens': 20}})
        self.assertIn('输入 100（缓存命中 50），输出 20', text)
        self.assertIn('未请求模型', self.w.usage_text({'cache_hit': True}))
        self.assertIn('未返回', self.w.usage_text({}))

    def test_antigravity_menu_persistence_and_missing_model(self):
        provider = self.w.text_fields['text_provider']
        provider.setCurrentIndex(provider.findData('antigravity'))
        self.assertFalse(self.w.antigravity_options.isHidden())
        self.assertTrue(self.w.codex_options.isHidden())
        combo = self.w.text_fields['antigravity_model']
        self.assertEqual(combo.currentData(), 'gemini-flash-low')
        combo.setCurrentIndex(combo.findData('sonnet'))
        self.w.refresh_antigravity_models()
        self.wait_for(lambda: not self.w.jobs)
        self.assertEqual(self.w.validate_text_config()['antigravity_model'], 'sonnet')
        self.w.populate_antigravity_models({'models': [{'model': 'new', 'displayName': 'New'}]})
        self.assertEqual(combo.currentData(), 'sonnet')
        self.assertIn('当前列表未提供', combo.currentText())
        self.assertTrue(self.w.antigravity_refresh_button.isEnabled())
        self.w.persist()
        restored = native_app.Translator(self.settings, initialize=False)
        self.assertEqual(restored.translation_config()['text_provider'], 'antigravity')
        self.assertEqual(restored.translation_config()['antigravity_model'], 'sonnet')
        restored.close()

    def test_web_page_owns_service_and_keeps_voice_credentials_out(self):
        import web_ui
        worker = '''import json, sys
config = json.loads(sys.stdin.readline())
assert set(config) == {'token', 'text_provider', 'codex_model', 'codex_reasoning_effort', 'antigravity_model', 'translation_concurrency'}
assert config['translation_concurrency'] == 8
assert len(config['token']) >= 24
print(json.dumps({'ready': True}), flush=True)
sys.stdin.readline()
'''
        (self.root / 'web_translation.py').write_text(worker)
        page = self.w.web_page
        parallel = self.w.text_fields['translation_concurrency']
        self.assertEqual(parallel.currentData(), 4)
        parallel.setCurrentIndex(parallel.findData(8))
        self.assertEqual(self.w.translation_config()['translation_concurrency'], 8)
        self.assertEqual(self.settings.value('translation_concurrency'), 8)
        self.w.navigate(5)
        self.assertEqual(self.w.pages.currentWidget(), page)
        with patch.object(web_ui, 'ROOT', self.root):
            page.start()
            self.wait_for(lambda: '已启动' in page.status.text())
            self.assertFalse(page.start_button.isEnabled())
            page.stop()
            self.wait_for(lambda: page.process is None)
            self.assertTrue(page.start_button.isEnabled())
        old_token = page.token
        page.rotate_token()
        self.assertNotEqual(page.token, old_token)
        self.assertEqual(page.token, self.settings.value('web/token'))


    def test_material_keyboard_navigation_and_copy_keep_transcript_plain(self):
        self.w.activateWindow()
        self.w.setFocus()
        QTest.qWait(10)
        QTest.keyClick(self.w, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(self.w.pages.currentIndex(), 1)
        QTest.keyClick(self.w, Qt.Key.Key_1, Qt.KeyboardModifier.ControlModifier)
        panel = self.w.transcripts['inbound']
        self.assertFalse(panel.copy_button.isEnabled())
        panel.append_entry('原文', '<video> & 你好', '12:00')
        panel.append_entry('译文', 'Hello', '12:00')
        panel.copy_button.click()
        copied = QApplication.clipboard().text()
        self.assertIn('<video> & 你好', copied)
        self.assertIn('Hello', copied)
        panel.clear_button.click()
        self.assertFalse(panel.entries)
        self.assertFalse(panel.copy_button.isEnabled())

    def test_reading_splitter_keyboard_and_orientation_restore(self):
        self.w.resize(1380, 1100)
        QTest.qWait(10)
        split = self.w.transcript_split
        split.setSizes([600, 400])
        handle = split.handle(1)
        before = split.sizes()
        QTest.keyClick(handle, Qt.Key.Key_Right)
        self.assertGreater(split.sizes()[0], before[0])
        saved = split.sizes()
        self.w.resize(800, 1100)
        QTest.qWait(10)
        self.assertEqual(split.orientation(), Qt.Orientation.Vertical)
        self.w.resize(1380, 1100)
        QTest.qWait(10)
        actual = split.sizes()
        self.assertAlmostEqual(actual[0] / sum(actual), saved[0] / sum(saved), delta=0.02)
        QTest.keyClick(split.handle(1), Qt.Key.Key_Home)
        self.assertLessEqual(abs(split.sizes()[0] - split.sizes()[1]), 1)

    def test_copy_feedback_does_not_outlive_its_panel(self):
        from desktop_ui import TranscriptPanel
        from PyQt6.QtCore import QCoreApplication, QEvent
        from PyQt6 import sip
        panel = TranscriptPanel('临时字幕', 'EN → 中', '', '')
        panel.colors = self.w.colors
        panel.append_entry('译文', '临时内容', '12:00')
        panel.copy_text()
        self.assertTrue(panel.copy_feedback_timer.isActive())
        panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(sip.isdeleted(panel))
        QTest.qWait(1700)

    def test_preferred_device_prefers_system_default_and_bluetooth_without_model_hardcoding(self):
        entries = [('HDMI Display Audio', 'alsa_output.hdmi'), ('Bluetooth Headset', 'bluez_output.headset')]
        self.assertEqual(native_app.preferred_device('headphones', entries, {'default_sink': 'alsa_output.hdmi'}),
                         'alsa_output.hdmi')
        self.assertEqual(native_app.preferred_device('headphones', entries, {'default_sink': 'meeting-remote'}),
                         'bluez_output.headset')
        mics = [('Muted Built-in Mic（静音）', 'alsa_input.internal'), ('USB Microphone', 'alsa_input.usb')]
        sources = [{'name': 'alsa_input.internal', 'mute': True}, {'name': 'alsa_input.usb', 'mute': False}]
        self.assertEqual(native_app.preferred_device('microphone', mics, {'sources': sources, 'default_source': 'alsa_input.internal'}),
                         'alsa_input.usb')


if __name__ == '__main__':
    unittest.main()
