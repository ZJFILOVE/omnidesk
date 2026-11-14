"""Exercise the real video worker and mpv IPC on synthetic local media."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QSettings
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox
from PyQt6.QtNetwork import QLocalSocket

from native_app import Translator
from video_subtitles import Cue, compose
from video_player import VideoPlayer, player_command
from video_ui import subtitle_save_path


class LocalSubtitleUITests(unittest.TestCase):
    """Use a placeholder video: direct SRT loading must never probe or run ASR."""
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-subtitle-ui-')
        self.root = Path(self.tmp.name)
        self.media = self.root / 'episode.mkv'
        self.media.write_bytes(b'not a media file')
        self.subtitle = self.root / 'episode.en.srt'
        self.body = compose([Cue(100, 900, 'An English sentence')])
        self.subtitle.write_text(self.body)
        self.w = Translator(QSettings(str(self.root / 'prefs.ini'), QSettings.Format.IniFormat), initialize=False)
        self.page = self.w.video_page
        self.page.source.setText(str(self.media))

    def tearDown(self):
        self.page.cancel()
        self.wait_for(lambda: self.page.process is None)
        self.w.close()
        self.app.processEvents()
        import hashlib
        stat = self.media.stat()
        key = hashlib.sha256(json.dumps([str(self.media.resolve()), stat.st_size, stat.st_mtime_ns],
                                       ensure_ascii=False).encode()).hexdigest()[:24]
        cache = Path(__file__).resolve().parents[1] / 'video-cache' / key
        if cache.is_dir():
            shutil.rmtree(cache)
        self.tmp.cleanup()

    def wait_for(self, predicate, timeout=5000):
        end = time.monotonic() + timeout / 1000
        while not predicate() and time.monotonic() < end:
            QTest.qWait(20)
        self.assertTrue(predicate(), 'subtitle operation timed out')

    def load(self):
        self.page.add_files([{'path': str(self.subtitle), 'title': self.subtitle.name}])
        self.page.load_selected_subtitle()
        self.wait_for(lambda: self.page.process is None)
        self.assertIsNotNone(self.page.result, self.page.status.text())

    def test_cached_title_survives_restart_and_restores_without_probing_media(self):
        self.load()
        expected = dict(self.page.result)
        self.w.close()
        self.w = Translator(QSettings(str(self.root / 'prefs.ini'), QSettings.Format.IniFormat), initialize=False)
        self.page = self.w.video_page
        self.page.source.setText(str(self.root / 'new-video.mkv'))
        self.page.cache_search.setText('EPISODE')
        index = next(i for i in range(self.page.cached.count())
                     if (self.page.cached.itemData(i) or {}).get('source') == str(self.media))
        self.page.cached.setCurrentIndex(index)
        self.assertIn(self.media.name, self.page.cached.currentText())
        self.assertTrue(self.page.open_cached_button.isEnabled())
        self.page.open_cached_button.click()
        self.wait_for(lambda: self.page.process is None)
        self.assertEqual(self.page.result, expected, self.page.status.text())
        self.assertEqual(self.page.source.text(), str(self.media))
        self.assertEqual(self.page.info.text(), self.media.name)
        self.assertTrue(self.page.play_button.isEnabled())
        with patch.object(self.page.player, 'play') as play:
            self.page.play()
        self.assertEqual(play.call_args.args[1:], (str(self.media), expected['subtitle']))
        with patch('video_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):
            self.page.clear_cache('subtitles')
        self.assertNotIn(str(self.media), [(self.page.cached.itemData(i) or {}).get('source')
                                         for i in range(self.page.cached.count())])
        self.assertFalse(self.page.play_button.isEnabled())

    def test_title_filter_empty_state_and_duplicate_titles(self):
        entries = [{'title': '同名视频', 'source': 'https://youtu.be/first'},
                   {'title': '同名视频', 'source': 'https://youtu.be/second'}]
        with patch('video_ui.cached_videos', return_value=entries):
            self.page.refresh_cached_videos()
        self.assertNotEqual(self.page.cached.itemText(0), self.page.cached.itemText(1))
        self.page.cache_search.setText('不存在的标题')
        self.assertFalse(self.page.open_cached_button.isEnabled())
        self.assertIsNone(self.page.cached.currentData())
        self.page.cache_search.clear()
        self.assertEqual(self.page.cached.count(), 2)
        self.assertTrue(self.page.open_cached_button.isEnabled())
        self.page.cached.setCurrentIndex(1)
        with patch.object(self.page, 'start_job') as start:
            self.page.open_cached_video()
        start.assert_called_once_with('open_cached')
        self.assertEqual(self.page.source.text(), entries[1]['source'])

    def test_download_progress_stream_updates_bar_without_flooding_log(self):
        process = Mock()
        self.page.process = process
        def send(events):
            payload = ''.join(json.dumps(e) + '\n' for e in events).encode()
            # Worker output can arrive in arbitrary chunks.
            for chunk in (payload[:12], payload[12:]):
                process.readAllStandardOutput.return_value = chunk
                self.page.events()
        try:
            before = self.page.log.toPlainText()
            send([{'message': '下载视频 · 5 MiB / 10 MiB', 'progress': .5,
                   'progress_label': '下载视频 %p%', 'transient': True}])
            self.assertEqual(self.page.progress.value(), 50)
            self.assertEqual(self.page.progress.maximum(), 100)
            self.assertEqual(self.page.progress.text(), '下载视频 50%')
            self.assertEqual(self.page.status.text(), '下载视频 · 5 MiB / 10 MiB')
            self.assertEqual(self.page.log.toPlainText(), before)
            send([{'message': '正在合并音视频…', 'progress': None, 'transient': True}])
            self.assertEqual(self.page.progress.maximum(), 0)
            send([{'message': '已准备字幕', 'progress': 1}])
            self.assertEqual(self.page.progress.value(), 100)
            self.assertEqual(self.page.progress.format(), '%p%')
            self.page.cancel_pending = True
            self.page.status.setText('正在取消…')
            send([{'message': '下载音轨', 'progress': .2, 'transient': True}])
            self.assertEqual(self.page.status.text(), '正在取消…')
        finally:
            self.page.process = None
            self.page.cancel_pending = False

    def test_cache_menu_cancel_and_busy_do_not_delete(self):
        with patch('video_ui.delete_cache') as delete, \
                patch('video_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as question:
            self.page.clear_cache('video')
            question.assert_called_once()
            delete.assert_not_called()
            self.page.process = object()
            try:
                self.page.clear_cache('video')
                question.assert_called_once()
                delete.assert_not_called()
            finally:
                self.page.process = None

    def test_translated_subtitles_default_to_bilingual_for_play_and_export(self):
        result = {'source': str(self.media), 'media': str(self.media), 'title': 'episode',
                  'subtitle': '/cache/zh.srt', 'original': '/cache/original.srt',
                  'timed_source': '/cache/source.srt', 'bilingual': '/cache/bilingual.srt'}
        self.page.show_result(result)
        self.assertEqual(self.page.output.currentData(), result['bilingual'])
        self.assertEqual(subtitle_save_path(result, self.page.output.currentData()).name,
                         'episode.bilingual.srt')
        with patch.object(self.page.player, 'play') as play:
            self.page.play()
        self.assertEqual(play.call_args.args[2], result['bilingual'])
        self.page.output.setCurrentIndex(0)
        self.assertEqual(self.page.output.currentData(), result['subtitle'])

    def test_video_playback_uses_selected_output_instead_of_system_default(self):
        self.page.show_result({'media': str(self.media), 'subtitle': str(self.subtitle), 'loaded': True})
        headphones = self.w.fields['headphones']
        headphones.addItem('Streaming virtual sink', 'sink-sunshine-stereo')
        headphones.addItem('USB headphones', 'alsa_output.usb-headphones')
        headphones.setCurrentIndex(headphones.findData('alsa_output.usb-headphones'))
        with patch.object(self.page.player, 'play') as play:
            self.page.play()
            play.assert_called_once_with('mpv', str(self.media), str(self.subtitle),
                                         audio_sink='alsa_output.usb-headphones')
            # A changed device must be read again on the next play action.
            headphones.setCurrentIndex(headphones.findData('sink-sunshine-stereo'))
            self.page.play()
            self.assertEqual(play.call_args.kwargs['audio_sink'], 'sink-sunshine-stereo')
            headphones.clear()
            self.page.play()
            self.assertIsNone(play.call_args.kwargs['audio_sink'])

    def test_cache_deletion_clears_stale_outputs_and_preserves_originals(self):
        from video_cache import source_directory
        cache = source_directory(self.root / 'video-cache', str(self.media))
        cache.mkdir(parents=True)
        generated = cache / 'generated.srt'
        generated.write_text(self.body)
        self.page.add_files([{'path': str(generated), 'title': 'cached'},
                             {'path': str(self.subtitle), 'title': 'original'}])
        self.page.show_result({'subtitle': str(generated), 'media': str(self.media), 'loaded': True})
        with patch('video_ui.ROOT', self.root), \
                patch('video_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
                patch.object(self.page.player, 'close') as close:
            self.page.clear_cache('subtitles')
        close.assert_called_once()
        self.assertFalse(generated.exists())
        self.assertTrue(self.subtitle.exists())
        self.assertTrue(self.media.exists())
        self.assertIsNone(self.page.result)
        self.assertFalse(self.page.play_button.isEnabled())
        self.assertFalse(self.page.export_button.isEnabled())
        paths = [(self.page.subtitles.itemData(i) or {}).get('subtitle_path')
                 for i in range(self.page.subtitles.count())]
        self.assertNotIn(str(generated), paths)
        self.assertIn(str(self.subtitle), paths)

    def test_import_loads_without_translation_configuration_or_timing_changes(self):
        self.page.offset.setValue(20)
        with patch.object(self.w, 'translation_config', side_effect=AssertionError('must not require translation')) as config, \
                patch('video_ui.choose_file', return_value=str(self.subtitle)):
            self.page.import_subtitle()
            self.wait_for(lambda: self.page.process is None)
        config.assert_not_called()
        self.assertIsNotNone(self.page.result, self.page.status.text())
        self.assertTrue(self.page.result['loaded'])
        self.assertEqual(Path(self.page.result['subtitle']).read_text(), self.body)
        self.assertEqual(self.subtitle.read_text(), self.body)
        self.assertEqual(self.page.output.count(), 1)
        self.assertTrue(self.page.play_button.isEnabled())
        with patch.object(self.page.player, 'play') as play:
            self.page.play()
        self.assertEqual(play.call_args.args[1:], (str(self.media), self.page.result['subtitle']))

    def test_save_dialog_suggests_sidecar_and_cancel_keeps_existing_file(self):
        self.load()
        suggestion = self.root / 'episode.original.srt'
        suggestion.write_text('existing subtitle')
        with patch('video_ui.choose_file', return_value='') as dialog:
            self.page.export()
        self.assertEqual(dialog.call_args.args[2], str(suggestion))
        self.assertEqual(suggestion.read_text(), 'existing subtitle')
        target = self.root / 'chosen.srt'
        with patch('video_ui.choose_file', return_value=str(target)):
            self.page.export()
        self.assertEqual(target.read_text(), self.body)

    def test_read_only_destination_reports_another_directory(self):
        self.load()
        with patch('video_ui.choose_file', return_value=str(self.root / 'target.srt')), \
                patch('video_ui.shutil.copyfile', side_effect=PermissionError('read-only directory')):
            self.page.export()
        self.assertIn('选择可写目录', self.page.status.text())

    def test_each_saved_version_has_its_own_suffix(self):
        result = {'source': str(self.media), 'title': self.media.name,
                  'subtitle': '/cache/zh.srt', 'original': '/cache/original.srt',
                  'timed_source': '/cache/source.srt', 'bilingual': '/cache/bilingual.srt'}
        for field, suffix in (('subtitle', 'zh'), ('original', 'original'),
                              ('timed_source', 'source'), ('bilingual', 'bilingual')):
            with self.subTest(version=field):
                self.assertEqual(subtitle_save_path(result, result[field]), self.root / f'episode.{suffix}.srt')
        result['subtitle'] = result['timed_source']
        self.assertEqual(subtitle_save_path(result, result['subtitle']), self.root / 'episode.source.srt')

    def test_youtube_original_can_be_requested_without_a_subtitle_selection(self):
        self.page.source.setText('https://www.youtube.com/watch?v=example')
        self.page.offline.setChecked(False)
        with patch.object(self.page, 'start_job') as start:
            self.page.obtain_original()
        self.assertEqual(start.call_args.args, ('obtain_original',))
        self.assertFalse(start.call_args.kwargs['download_video'])
        self.assertNotIn('subtitle_path', start.call_args.kwargs)

    def test_youtube_and_asr_originals_have_explicit_labels_and_save_names(self):
        for origin, label in (('youtube_manual', 'YouTube 上传字幕'), ('youtube_auto', 'YouTube 自动字幕'),
                              ('asr', '本地 ASR 识别原文')):
            with self.subTest(origin=origin):
                result = {'source': 'https://www.youtube.com/watch?v=example', 'title': 'Example lesson',
                          'subtitle': '/cache/source.srt', 'original': '/cache/original.srt',
                          'timed_source': '/cache/source.srt', 'origin': origin, 'origin_label': label,
                          'original_only': True}
                self.page.show_result(result)
                self.assertEqual(self.page.output.count(), 1)
                self.assertIn(label, self.page.output.currentText())
                suggestion = subtitle_save_path(result, self.page.output.currentData())
                self.assertEqual(suggestion.name, f'Example lesson.{origin}.original.srt')

    def test_ready_translation_replaces_subtitle_in_same_running_mpv(self):
        result = {'source': str(self.media), 'media': str(self.media), 'title': self.media.name,
                  'subtitle': '/cache/zh.srt', 'original': '/cache/original.srt',
                  'timed_source': '/cache/source.srt'}
        self.page.player.current_media = str(self.media)
        self.page.player.current_player = 'mpv'
        with patch.object(self.page.player, 'process', object()), \
                patch.object(self.page.player.socket, 'state', return_value=QLocalSocket.LocalSocketState.ConnectedState) as socket_state, \
                patch.object(self.page.player, 'send') as send, patch.object(self.page.player, 'play') as play:
            self.page.show_result(result)
            send.assert_called_once_with(['sub-add', result['subtitle'], 'select'])
            play.assert_not_called()
            send.reset_mock()
            self.page.player.current_media = '/different/video.mkv'
            self.page.show_result(result)
            self.page.reload_subtitle()
            send.assert_not_called()
            self.page.player.current_media = str(self.media)
            socket_state.return_value = QLocalSocket.LocalSocketState.UnconnectedState
            self.page.show_result(result)
            send.assert_not_called()


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required for synthetic media')
class VideoWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='qwen-video-test-')
        self.root = Path(self.tmp.name)
        self.media = self.root / 'show S01E01 ; literal.mkv'
        self.subtitle = self.root / 'provided.srt'
        self.subtitle.write_text(compose([Cue(0,1500,'第一条字幕'),Cue(1800,2800,'第二条字幕')]))
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=32x32:rate=5',
                        '-t','3','-c:v','ffv1',str(self.media)],check=True,capture_output=True)
        self.w=Translator(QSettings(str(self.root/'prefs.ini'),QSettings.Format.IniFormat),initialize=False)
        self.w.navigate(4)
        self.w.show()

    def tearDown(self):
        self.w.video_page.cancel()
        self.wait_for(lambda: self.w.video_page.process is None)
        self.w.close()
        self.app.processEvents()
        # Test video caches live in the project cache, never remove user results.
        import hashlib
        stat=self.media.stat()
        key=hashlib.sha256(json.dumps([str(self.media.resolve()),stat.st_size,stat.st_mtime_ns],ensure_ascii=False).encode()).hexdigest()[:24]
        cache=Path(__file__).resolve().parents[1]/'video-cache'/key
        if cache.is_dir():
            shutil.rmtree(cache)
        self.tmp.cleanup()

    def wait_for(self,predicate,timeout=10000):
        end=time.monotonic()+timeout/1000
        while not predicate() and time.monotonic()<end:
            QTest.qWait(20)
        self.assertTrue(predicate(),'video operation timed out')

    def test_prepare_chinese_srt_without_devices_and_restore(self):
        page=self.w.video_page
        page.source.setText(str(self.media))
        page.inspect()
        self.wait_for(lambda:page.process is None)
        self.assertIn('0.1',page.info.text())
        page.add_files([{'path':str(self.subtitle),'title':'中文 SRT'}])
        page.sync.setCurrentIndex(page.sync.findData('none'))
        page.prepare()
        self.wait_for(lambda:page.process is None)
        self.assertIsNotNone(page.result,page.status.text())
        self.assertTrue(page.play_button.isEnabled())
        self.assertEqual(Path(page.result['subtitle']).read_text(),self.subtitle.read_text())
        page.invalidate_result()
        page.inspect()
        self.wait_for(lambda:page.process is None)
        self.assertIsNotNone(page.result)
        page.source.setText(str(self.root/'different.mkv'))
        self.assertIsNone(page.result)
        self.assertFalse(page.play_button.isEnabled())

    def test_asr_resume_and_reset_choices_survive_inspection_and_source_changes(self):
        page = self.w.video_page
        page.source.setText(str(self.media))
        info = {'source': str(self.media), 'title': 'fixture', 'audio': [],
                'subtitles': [{'path': str(self.subtitle), 'title': '中文字幕'}]}
        page.show_inspection(info)
        self.assertEqual(page.subtitles.count(), 4)
        for index, reset in ((1, False), (2, True)):
            page.subtitles.setCurrentIndex(index)
            with patch.object(page, 'start_job') as start:
                page.prepare()
            self.assertTrue(start.call_args.kwargs['force_asr'])
            self.assertEqual(start.call_args.kwargs.get('reset_asr_cache', False), reset)
        page.source.setText(str(self.root / 'next.mkv'))
        self.assertEqual(page.subtitles.count(), 3)
        self.assertEqual(page.subtitles.currentIndex(), 0)
        page.show_inspection(info)
        self.assertEqual(page.subtitles.count(), 4)
        self.assertTrue(page.subtitles.itemData(2)['reset_asr_cache'])

    @unittest.skipUnless(shutil.which('mpv'),'mpv required')
    def test_mpv_load_seek_and_subtitle_switch(self):
        actual=shutil.which('mpv')
        wrapper=self.root/'headless-mpv'
        wrapper.write_text('#!'+sys.executable+'\nimport os,sys\nos.execv('+repr(actual)+', ['
                           +repr(actual)+',"--no-config","--vo=null","--ao=null","--pause=yes",*sys.argv[1:]])\n')
        wrapper.chmod(0o700)
        player=self.w.video_page.player
        positions=[]
        player.position.connect(positions.append)
        with patch('video_player.shutil.which',return_value=str(wrapper)):
            player.play('mpv',str(self.media),str(self.subtitle))
        self.wait_for(lambda:bool(positions))
        player.send(['seek',2,'absolute+exact'])
        self.wait_for(lambda:any(p>=1.8 for p in positions))
        player.send(['sub-add',str(self.subtitle),'select'])
        player.close()
        self.assertIsNone(player.process)
        self.assertIsNone(player.current_media)
        self.assertIsNone(player.current_player)


class PlayerArgumentTests(unittest.TestCase):
    def test_selected_output_is_explicit_for_local_and_online_mpv(self):
        sink = 'alsa_output.usb-headphones'
        for media in ('/tmp/video.mkv', 'https://www.youtube.com/watch?v=test'):
            with self.subTest(media=media):
                args = player_command('mpv', media, '/tmp/sub.srt', '/tmp/ipc', audio_sink=sink)
                self.assertIn('--audio-device=pulse/' + sink, args)
                self.assertLess(args.index('--audio-device=pulse/' + sink), args.index('--'))
        default = player_command('mpv', '/tmp/video.mkv', '/tmp/sub.srt', '/tmp/ipc')
        self.assertFalse(any(arg.startswith('--audio-device=') for arg in default))
        for player in ('vlc', 'smplayer'):
            self.assertEqual(player_command(player, '/tmp/video.mkv', '/tmp/sub.srt', audio_sink=sink),
                             player_command(player, '/tmp/video.mkv', '/tmp/sub.srt'))

    def test_online_playback_uses_explicit_downloader(self):
        args=player_command('mpv','https://www.youtube.com/watch?v=test','/tmp/sub.srt','/tmp/ipc')
        self.assertIn('--ytdl=yes',args)
        self.assertIn('--ytdl-raw-options=js-runtimes=node',args)
        with self.assertRaises(ValueError):
            player_command('vlc','https://www.youtube.com/watch?v=test','/tmp/sub.srt')

    def test_paths_are_single_arguments(self):
        path='/tmp/video ; $(touch file).mkv'
        args=player_command('mpv',path,'/tmp/sub title.srt','/tmp/ipc')
        self.assertEqual(args[-2:],['--',path])
        self.assertIn('--sub-file=/tmp/sub title.srt',args)
        self.assertIn('--sub-font-size=28', args)
        self.assertIn('--sub-margin-x=12', args)


if __name__=='__main__':
    unittest.main()
