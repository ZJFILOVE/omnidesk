"""Render the actual desktop widgets with sample text; never starts a worker."""
import os
from pathlib import Path
import sys
import tempfile

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from native_app import Translator


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    output = ROOT / 'docs/previews'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        settings = QSettings(str(Path(folder) / 'preview.ini'), QSettings.Format.IniFormat)
        settings.setValue('onboarded', True)
        window = Translator(settings, initialize=False)
        window.resize(1380, 960)
        for key, title, device in [('headphones', '示例耳机', 'preview-output'), ('microphone', '示例麦克风', 'preview-input')]:
            window.fields[key].addItem(title, device)
        window.devices_ready = True
        window.route()
        window.set_badge('界面预览')
        window.status.setText('示例字幕 · 未启动翻译服务')
        window.show()
        window.resize(1380, 960)
        for direction, entries in {
            'inbound': [('原文', 'Let’s start with what matters most: making every conversation easier to understand.'),
                        ('译文', '让我们先从最重要的事开始：让每一次对话都更容易理解。')],
            'outbound': [('原文', '好的。原文和译文放在一起，我就可以更专注地参与讨论。'),
                         ('译文', 'With the original and translation together, I can focus on the discussion.')],
        }.items():
            for kind, text in entries:
                window.transcripts[direction].append_entry(kind, text, '09:41:08')
        for theme in ('light', 'dark'):
            window.set_theme(theme)
            QTest.qWait(50)
            path = output / f'material-desktop-{theme}.png'
            if not window.grab().save(str(path)):
                raise RuntimeError(f'Could not save {path}')
            print(path)
        window.close()
        app.processEvents()


if __name__ == '__main__':
    main()
