"""Native NAS controls, persistent settings and real worker lifecycle."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QSettings
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from native_app import Translator
from nas_queue import Queue
from nas_ui import NasPage


class NasUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = QSettings(str(self.root / 'settings.ini'), QSettings.Format.IniFormat)
        self.w = Translator(self.settings, initialize=False)
        self.page = self.w.nas_page
        self.w.navigate(6)
        self.w.show()

    def tearDown(self):
        self.page.shutdown()
        self.wait(lambda: self.page.process is None and not self.page.helpers)
        self.w.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def wait(self, condition, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not condition():
            QTest.qWait(15)
        self.assertTrue(condition(), 'NAS UI worker timed out')

    def test_page_preserves_existing_navigation_and_no_automatic_initial_processing(self):
        self.assertEqual(self.w.pages.count(), 9)
        self.assertEqual(self.w.pages.widget(4), self.w.video_page)
        self.assertEqual(self.w.pages.widget(5), self.w.web_page)
        self.assertFalse(self.page.enabled.isChecked())
        self.assertFalse(self.page.auto.isChecked())
        self.assertIsNone(self.page.process)
        self.assertEqual(self.page.provider.currentData(), 'subhd')

    def test_settings_exclude_all_keys_and_reuse_current_model_concurrency(self):
        self.page.connections['radarr']['key'].setText('ARR-SHOULD-NOT-PERSIST')
        self.page.connections['sonarr']['key'].setText('SONARR-SHOULD-NOT-PERSIST')
        self.w.text_key.setText('MODEL-SHOULD-NOT-PERSIST')
        self.page.apply()
        config, key = self.page.configuration()
        self.assertEqual(key, 'MODEL-SHOULD-NOT-PERSIST')
        self.assertEqual(config['processing']['codex_model'], self.w.translation_config()['codex_model'])
        self.assertEqual(config['processing']['translation_concurrency'], self.w.translation_config()['translation_concurrency'])
        self.settings.sync()
        text = (self.root / 'settings.ini').read_text()
        self.assertNotIn('SHOULD-NOT-PERSIST', text)

    def test_real_mapping_worker_keeps_ui_responsive_and_saves_only_matching_proof(self):
        controls = self.page.connections['radarr']
        controls['local'].setText(str(self.root))
        controls['mounted'].setChecked(False)
        self.page.test_mapping('radarr')
        self.w.set_theme('dark')
        self.wait(lambda: not self.page.helpers)
        self.assertTrue(self.page.proofs['radarr']['read_ok'])
        self.assertTrue(self.page.proofs['radarr']['write_ok'])
        self.assertEqual(self.page.proofs['radarr']['proof']['local'], str(self.root))
        controls['local'].setText(str(self.root / 'unverified'))
        self.assertNotEqual(self.page.mapping('radarr')['local'], self.page.mapping('radarr')['proof']['local'])

    def test_real_scheduler_restores_queue_and_stops_when_window_closes(self):
        path = self.root / 'queue.sqlite3'
        queue = Queue(path)
        service = {'kind': 'radarr', 'url': 'http://127.0.0.1:1', 'enabled': False}
        config = {'outputs': ['zh'], 'search_provider': 'none'}
        queue.sync(service, [{'file_id': '7', 'path': '/media/test.mkv', 'title': 'A show', 'episodes': [], 'version': 'one'}], config)
        jid = queue.enqueue([queue.snapshot()[0]['id']], config)[0]
        queue.update(jid, state='running')
        queue.close()
        self.page.start({'database': str(path), 'services': [service], 'processing': config})
        self.page.send({'command': 'pause_all', 'value': True})  # Before QProcess.started.
        self.wait(lambda: self.page.table.rowCount() == 1 and self.page.paused)
        self.assertEqual(self.page.table.item(0, 2).text(), '等待处理')
        self.page.table.selectRow(0)
        self.page.selected_command('pause')
        self.wait(lambda: self.page.table.item(0, 2).text() == '已暂停')
        self.page.selected_command('retry')
        self.wait(lambda: self.page.table.item(0, 2).text() == '等待处理')
        self.page.selected_command('cancel')
        self.wait(lambda: self.page.table.item(0, 2).text() == '已取消')
        self.assertFalse(self.page.shutdown())
        self.wait(lambda: self.page.process is None)
        queue = Queue(path)
        try:
            self.assertEqual(queue.job(jid)['state'], 'cancelled')
        finally:
            queue.close()

    def test_candidate_handoff_and_generated_status_do_not_claim_verified(self):
        job = {'id': 'job1', 'state': 'generated', 'origin': '英文字幕', 'stage': '字幕已生成',
               'progress': 1, 'error': '', 'result': None, 'override': {'choices': {'candidates':
               [{'provider': 'subhd', 'id': 'abc', 'title': 'Release', 'url': 'https://subhd.tv/a/abc'}]}}}
        self.page.entries = [{'id': 'media1', 'service': 'radarr:http://nas.test', 'present': True,
                              'data': {'title': 'Movie', 'episodes': [], 'path': '/remote/Movie.mkv'}, 'jobs': [job]}]
        self.page.render()
        self.page.table.selectRow(0)
        self.assertEqual(self.page.table.item(0, 2).text(), '字幕已生成')
        with patch.object(self.page, 'send') as send:
            self.page.choose_candidate()
        self.assertEqual(send.call_args.args[0]['override']['candidate']['id'], 'abc')
        controls = self.page.connections['radarr']
        controls['remote'].setText('/remote')
        controls['local'].setText('/nonexistent/test/mapping')
        # Handoff is pure path arithmetic; the video worker will perform actual I/O.
        self.page.open_video()
        self.assertEqual(self.w.video_page.source.text(), '/nonexistent/test/mapping/Movie.mkv')
        self.assertEqual(self.w.pages.currentIndex(), 4)
        self.assertEqual(self.w.video_page.nas_context, 'job1')
        self.assertTrue(self.w.video_page.nas_auto.isChecked())

    def prepared(self):
        return {'source': '/mapped/Film.mp4', 'title': 'Film', 'media': '/mapped/Film.mp4',
                'subtitle': '/cache/zh.srt', 'bilingual': '/cache/bilingual.srt',
                'timed_source': '/cache/source.srt', 'original': '/cache/original.srt'}

    def test_cached_result_only_returns_on_explicit_click_and_shows_disabled_queue(self):
        video = self.w.video_page
        prepared = self.prepared()
        with patch.object(self.page, 'helper') as helper:
            video.show_result(prepared)
            helper.assert_not_called()
            video.nas_button.click()
            config, done = helper.call_args.args
            self.assertEqual(config['action'], 'nas_adopt')
            self.assertEqual(config['prepared'], prepared)
            self.assertFalse(video.nas_button.isEnabled())
            done({'nas_result': {'job_id': 'returned', 'message': '已接回'}})
        self.assertEqual(video.nas_job_id, 'returned')
        self.assertIn('启用后台同步', video.nas_status.text())
        self.assertTrue(video.nas_button.isEnabled())

    def test_successful_manual_prepare_auto_returns_but_failure_does_not(self):
        from PyQt6.QtCore import QProcess
        video = self.w.video_page
        video.nas_auto.setChecked(True)
        for failed in (False, True):
            process = QProcess(video)
            process.setProperty('nas_auto', True)
            video.process = process
            video.last_error = failed
            video.show_result(self.prepared())
            with patch.object(video, 'events'), patch.object(self.page, 'accept_video_result') as accept:
                video.finished(process, 0)
            self.assertEqual(accept.call_count, 0 if failed else 1)

    def test_late_import_result_does_not_attach_to_another_video(self):
        video = self.w.video_page
        video.show_result(self.prepared())
        with patch.object(self.page, 'helper') as helper:
            video.nas_button.click()
            done = helper.call_args.args[1]
            video.source.setText('/other.mp4')
            done({'nas_result': {'job_id': 'wrong-video', 'message': '已接回'}})
        self.assertIsNone(video.nas_job_id)
        self.assertNotIn('已接回', video.nas_status.text())


if __name__ == '__main__':
    unittest.main()
