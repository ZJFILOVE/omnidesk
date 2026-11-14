"""Exercise the real Qt picker, including old network-directory preferences."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QFileDialog

from file_dialogs import choose_file, initial_directory


class FileDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_offline_mounts_are_not_probed_when_restoring_directory(self):
        mounts = '1 0 0:1 / /home/zhu/NAS\\040files rw - fuse.davfs server rw\n'
        with patch('file_dialogs.Path.read_text', return_value=mounts), \
                patch('file_dialogs.Path.stat', side_effect=AssertionError('must not stat a mount')):
            for path in ('/mnt/feiniu/serial', '/media/disk', '/home/zhu/NAS files/series'):
                self.assertEqual(initial_directory(path), str(Path.home()))
            self.assertEqual(initial_directory('/tmp/local-video'), '/tmp/local-video')
            self.assertEqual(initial_directory('/home/zhu/NAS files-other'), '/home/zhu/NAS files-other')

    def run_picker(self, initial, *, save=False, accept=False):
        observed = {}

        def finish():
            dialog = self.app.activeModalWidget()
            # Record in the Qt callback, assert outside it (uncaught slot errors abort PyQt).
            if isinstance(dialog, QFileDialog):
                observed.update(directory=dialog.directory().absolutePath(),
                                options=dialog.options(), selected=dialog.selectedFiles(),
                                history=dialog.history())
                if accept:
                    dialog.accept()
                else:
                    dialog.reject()

        QTimer.singleShot(100, finish)
        result = choose_file(None, 'Test picker', initial, 'SRT (*.srt)', save=save)
        self.assertTrue(observed, 'picker must run the Qt event loop')
        return result, observed

    def test_old_nas_directory_opens_at_home_and_cancel_returns_empty(self):
        result, observed = self.run_picker('/mnt/feiniu/serial')
        self.assertEqual(result, '')
        self.assertEqual(observed['directory'], str(Path.home()))
        self.assertFalse(any('/mnt/feiniu' in item for item in observed['history']))
        self.assertTrue(observed['options'] & QFileDialog.Option.DontUseNativeDialog)
        self.assertTrue(observed['options'] & QFileDialog.Option.DontUseCustomDirectoryIcons)

    def test_save_retains_local_directory_filename_and_overwrite_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'episode.zh.srt')
            result, observed = self.run_picker(path, save=True, accept=True)
            self.assertEqual(result, path)
            self.assertEqual(observed['directory'], directory)
            self.assertFalse(observed['options'] & QFileDialog.Option.DontConfirmOverwrite)
            self.assertFalse(Path(path).exists(), 'picker must not create the output')

    def test_nas_save_retains_filename_without_opening_mount(self):
        _, observed = self.run_picker('/mnt/feiniu/episode.zh.srt', save=True)
        self.assertEqual(observed['directory'], str(Path.home()))
        self.assertEqual(observed['selected'], [str(Path.home() / 'episode.zh.srt')])


if __name__ == '__main__':
    unittest.main()
