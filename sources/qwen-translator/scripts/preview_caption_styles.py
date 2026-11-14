"""Render the actual Qt subtitle surfaces over the same illustrative video frame."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPolygonF
from PyQt6.QtWidgets import QApplication, QLabel, QWidget
from caption_style import CaptionSurface


class Comparison(QWidget):
    def __init__(self):
        super().__init__()
        self.selected = '--selected' in sys.argv
        self.resize(1440, 465 if self.selected else 1130)
        self.setStyleSheet('font-family: "Noto Sans CJK SC";')
        heading = QLabel('悬浮字幕 · 视频字幕' if self.selected else '悬浮字幕 · 三种外观', self)
        heading.setGeometry(48, 25, 1300, 55)
        heading.setStyleSheet('color:#182439;font-size:30px;font-weight:600;')
        sub = QLabel('原文 18 px  /  译文 22 px  /  鼠标移开后收起控制栏' if self.selected else
                     '同一视频背景  /  原文小字在上，译文突出  /  播放时收起控制栏', self)
        sub.setGeometry(48, 83, 1300, 30)
        sub.setStyleSheet('color:#68758A;font-size:16px;')
        for i, (style, title, note) in enumerate([
            ('cinema', '01  视频字幕', '半透明深色底 · 对比清晰'),
            ('paper', '02  简洁卡片', '柔和浅色底 · 清爽整齐'),
            ('clear', '03  极简透明', '无底色 · 文字阴影')]):
            if self.selected and i:
                break
            top = 135+i*320
            label = QLabel(title, self)
            label.setGeometry(48, top, 600, 40)
            label.setStyleSheet('font-size:21px;color:#24334B;font-weight:600;')
            desc = QLabel(note, self)
            desc.setAlignment(Qt.AlignmentFlag.AlignRight)
            desc.setGeometry(800, top, 592, 40)
            desc.setStyleSheet('font-size:16px;color:#68758A;')
            surface = CaptionSurface(style, 22 if self.selected else 34, self)
            surface.setGeometry(196, top+93, 1048, 165)
            surface.source.setText('Let’s focus on what matters, and take the next step together.')
            surface.translation.setText('把注意力放在重要的事上，\n一起迈出下一步。')

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor('#F0F3F8'))
        for i in range(1 if self.selected else 3):
            y = 180+i*320
            p.save()
            p.setClipRect(48, y, 1344, 255)
            gradient = QLinearGradient(0, y, 1440, y+255)
            gradient.setColorAt(0, QColor('#A6B4BC'))
            gradient.setColorAt(.52, QColor('#839EAD'))
            gradient.setColorAt(1, QColor('#D6B594'))
            p.fillRect(QRectF(48, y, 1344, 255), gradient)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor('#738F97'))
            p.drawPolygon(QPolygonF([QPointF(48,y+170),QPointF(240,y+60),QPointF(420,y+150),QPointF(720,y+36),QPointF(1040,y+175),QPointF(1392,y+90),QPointF(1392,y+255),QPointF(48,y+255)]))
            p.setBrush(QColor('#496A78'))
            p.drawPolygon(QPolygonF([QPointF(48,y+240),QPointF(340,y+155),QPointF(660,y+220),QPointF(1030,y+128),QPointF(1392,y+190),QPointF(1392,y+255),QPointF(48,y+255)]))
            p.setPen(QColor(255,255,255,125))
            p.setFont(QFont('Noto Sans CJK SC', 10))
            p.drawText(70,y+26,'视频背景示意')
            p.restore()


app = QApplication([])
app.setStyle('Fusion')
widget = Comparison()
widget.show()
app.processEvents()
widget.grab().save('/tmp/qwen-caption-styles.png')
