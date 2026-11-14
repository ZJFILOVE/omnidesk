"""Render the shipped caption presets with sample text and no services."""
import os
from pathlib import Path
import sys

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QGridLayout, QLabel, QVBoxLayout, QWidget
from caption_style import CaptionAppearance, CaptionSurface, STYLES


def main():
    app = QApplication([])
    app.setStyle('Fusion')
    canvas = QWidget()
    canvas.setObjectName('gallery')
    canvas.setStyleSheet('QWidget#gallery {background:#202824;}')
    layout = QGridLayout(canvas)
    layout.setContentsMargins(28, 28, 28, 28)
    layout.setSpacing(18)
    examples = [(key, values[0], {}) for key, values in STYLES.items()]
    examples.append(('cinema', '字幕效果 · 模糊 + 虚线 + 弱化原文',
                     dict(blur_translation=True, dashed_translation=True, dim_original=True)))
    for index, (key, name, effects) in enumerate(examples):
        tile = QWidget()
        tile.setFixedSize(430, 184)
        col = QVBoxLayout(tile)
        title = QLabel(name)
        title.setStyleSheet('color:#B9C7BF;font-size:14px;')
        col.addWidget(title)
        surface = CaptionSurface()
        model = CaptionAppearance()
        model.apply_preset(key)
        model.update(**effects)
        surface.apply_appearance(model.options)
        surface.source.setText('Let the words stay with you.')
        surface.translation.setText('让每一句话，都清晰可见。')
        surface.setMinimumHeight(128)
        col.addWidget(surface)
        layout.addWidget(tile, index // 3, index % 3)
    canvas.show()
    app.processEvents()
    output = ROOT / 'docs/previews/caption-style-gallery.png'
    if not canvas.grab().save(str(output)):
        raise RuntimeError('Unable to save caption style preview')
    print(output)
    canvas.close()


if __name__ == '__main__':
    main()
