"""Exercise PDF UI worker lifecycle with actual Qt signals and a fake worker."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QSettings
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QWidget
import pdf_ui

WORKER = '''import json,sys
from pathlib import Path
c=json.loads(sys.stdin.readline())
assert c['codex_model']=='gpt-6-luna'
print(json.dumps({'progress':40,'message':'翻译正文'}),flush=True)
if c['pages']=='2':
    sys.stdin.readline()
    print(json.dumps({'cancelled':True}),flush=True)
elif c['pages']=='3':
    print(json.dumps({'error':'test failure'}),flush=True)
else:
    root=Path(c['source']).parent
    for name in ('mono','dual'): (root/(name+'.pdf')).write_bytes(b'%PDF-test')
    print(json.dumps({'result':{'title':'paper','source':c['source'],
        'lang_out':'zh','mono':str(root/'mono.pdf'),'dual':str(root/'dual.pdf')},
        'message':'翻译完成','progress':100}),flush=True)
'''


class PdfUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'pdf_worker.py').write_text(WORKER)
        self.source = self.root / 'source.pdf'
        self.source.write_bytes(b'%PDF-test')
        self.window = QWidget()
        self.window.settings = QSettings(str(self.root / 'settings.ini'), QSettings.Format.IniFormat)
        self.window.translation_config = lambda: {'text_provider': 'codex', 'codex_model': 'gpt-6-luna',
                                                 'translation_concurrency': 2}
        self.window.navigate = lambda index: None
        self.patches = [patch.object(pdf_ui, 'ROOT', self.root), patch.object(pdf_ui, 'PDF_PYTHON', Path(sys.executable))]
        for p in self.patches:
            p.start()
        self.page = pdf_ui.PdfPage(self.window)
        self.page.source.setText(str(self.source))

    def wait(self, predicate):
        end = time.monotonic() + 5
        while not predicate() and time.monotonic() < end:
            QTest.qWait(10)
        self.assertTrue(predicate())

    def tearDown(self):
        if self.page.process:
            self.page.cancel()
            self.wait(lambda: self.page.process is None)
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_success_outputs_and_restore(self):
        self.page.start()
        self.assertFalse(self.page.start_button.isEnabled())
        self.wait(lambda: self.page.process is None)
        self.assertEqual(self.page.output.count(), 2)
        self.assertTrue(self.page.open_button.isEnabled())
        self.assertEqual(self.page.progress.value(), 100)
        restored = pdf_ui.PdfPage(self.window)
        self.assertEqual(restored.result['source'], str(self.source))

    def test_cancel_and_shutdown(self):
        self.page.pages.setText('2')
        self.page.start()
        self.wait(lambda: self.page.progress.value() == 40)
        self.assertFalse(self.page.shutdown())
        self.wait(lambda: self.page.process is None)
        self.assertIn('取消', self.page.status.text())
        self.assertFalse(self.page.save_button.isEnabled())
        self.assertTrue(self.page.start_button.isEnabled())

    def test_failure_does_not_enable_export(self):
        self.page.pages.setText('3')
        self.page.start()
        self.wait(lambda: self.page.process is None)
        self.assertEqual(self.page.status.text(), 'test failure')
        self.assertFalse(self.page.save_button.isEnabled())

    def test_original_cannot_be_overwritten(self):
        self.page.start()
        self.wait(lambda: self.page.process is None)
        original = self.source.read_bytes()
        with patch.object(pdf_ui, 'choose_file', return_value=str(self.source)):
            self.page.save_result()
        self.assertEqual(self.source.read_bytes(), original)
        self.assertIn('不能覆盖', self.page.status.text())
        alias = self.root / 'alias.pdf'
        alias.hardlink_to(self.source)
        with patch.object(pdf_ui, 'choose_file', return_value=str(alias)):
            self.page.save_result()
        self.assertEqual(self.source.read_bytes(), original)

    def test_pdf_results_prefer_papers(self):
        directory = self.root / '翻译 results'
        directory.mkdir()
        paths = [directory / name for name in ('dual.pdf', 'mono.PDF')]
        for path in paths:
            path.write_bytes(b'%PDF-test')
        self.page.show_result({'title': 'paper', 'dual': str(paths[0]), 'mono': str(paths[1])})
        with patch.object(pdf_ui.sys, 'platform', 'linux'), \
                patch.object(pdf_ui.shutil, 'which', return_value='/usr/bin/papers') as which, \
                patch.object(pdf_ui.QProcess, 'startDetached', return_value=(True, 123)) as launch, \
                patch.object(pdf_ui.QDesktopServices, 'openUrl') as default:
            for index, path in enumerate(paths):
                self.page.output.setCurrentIndex(index)
                self.page.open_result()
                launch.assert_called_with('/usr/bin/papers', [str(path)])
            self.assertEqual(which.call_count, 2)
            self.assertEqual(launch.call_count, 2)
            default.assert_not_called()

    def test_missing_papers_uses_system_reader(self):
        with patch.object(pdf_ui.sys, 'platform', 'linux'), \
                patch.object(pdf_ui.shutil, 'which', return_value=None), \
                patch.object(pdf_ui.QProcess, 'startDetached') as launch, \
                patch.object(pdf_ui.QDesktopServices, 'openUrl', return_value=True) as default:
            self.page.open_path(self.source)
            launch.assert_not_called()
            self.assertEqual(default.call_args.args[0].toLocalFile(), str(self.source))

    def test_papers_launch_failure_is_visible(self):
        with patch.object(pdf_ui.sys, 'platform', 'linux'), \
                patch.object(pdf_ui.shutil, 'which', return_value='/usr/bin/papers'), \
                patch.object(pdf_ui.QProcess, 'startDetached', return_value=(False, 0)), \
                patch.object(pdf_ui.QDesktopServices, 'openUrl') as default:
            self.page.open_path(self.source)
            self.assertIn('无法启动 Papers', self.page.status.text())
            default.assert_not_called()

    def test_directories_and_other_platforms_use_system_opener(self):
        for platform, path in [('linux', self.root), ('win32', self.source), ('darwin', self.source)]:
            with self.subTest(platform=platform, path=path), \
                    patch.object(pdf_ui.sys, 'platform', platform), \
                    patch.object(pdf_ui.shutil, 'which') as which, \
                    patch.object(pdf_ui.QProcess, 'startDetached') as launch, \
                    patch.object(pdf_ui.QDesktopServices, 'openUrl', return_value=True) as default:
                self.page.open_path(path)
                which.assert_not_called()
                launch.assert_not_called()
                self.assertEqual(default.call_args.args[0].toLocalFile(), str(path))


if __name__ == '__main__':
    unittest.main()
