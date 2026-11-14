"""Render the desktop appearance editor without starting translation services."""
import os
from pathlib import Path
import sys

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication
from caption_settings import appearance_dialog
from caption_style import CaptionAppearance
from ui_theme import apply_theme


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    appearance = CaptionAppearance()
    dialog = appearance_dialog(None, appearance)
    output = ROOT / 'docs/previews'
    output.mkdir(parents=True, exist_ok=True)
    for name, theme, tab, width in (
        ('caption-editor-light', 'light', 0, 1040),
        ('caption-editor-presets', 'light', 1, 1040),
        ('caption-editor-dark', 'dark', 0, 1040),
        ('caption-editor-narrow', 'light', 0, 560),
    ):
        apply_theme(app, theme)
        dialog.editor.tabs.setCurrentIndex(tab)
        dialog.resize(width, 800)
        dialog.show()
        for _ in range(5):
            app.processEvents()
        path = output / (name + '.png')
        if not dialog.grab().save(str(path)):
            raise RuntimeError('Unable to save ' + str(path))
        print(path)
    dialog.close()
    dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    main()
