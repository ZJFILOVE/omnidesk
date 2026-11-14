"""Small, reusable controls for the desktop caption appearance editor."""
from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QHBoxLayout,
                             QSizePolicy, QSlider, QWidget)

from caption_style import CaptionAppearance, CaptionSurface, PRESETS
from ui_controls import _PageWheelMixin, WheelSafeSpinBox


def _blend(first, second, amount):
    return QColor(*(round(a * (1 - amount) + b * amount)
                    for a, b in zip(first.getRgb()[:3], second.getRgb()[:3])))


_PRESET_PREVIEWS = {}


def _preset_preview(key):
    """Render once using the same text, effects and painting as live captions."""
    if key in _PRESET_PREVIEWS:
        return _PRESET_PREVIEWS[key]
    stage = QWidget()
    stage.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    stage.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    stage.setObjectName('caption_preset_stage')
    stage.setStyleSheet('QWidget#caption_preset_stage { background:#29312E; }')
    stage.resize(300, 100)
    profile = CaptionAppearance(parent=stage)
    profile.apply_preset(key)
    surface = CaptionSurface(parent=stage)
    surface.apply_appearance(profile.options)
    surface.source.setText('Original words')
    surface.translation.setText('让字幕更清晰')
    # The thumbnail keeps real preset typography/effects and only trims the
    # generous overlay padding to fit the 300 × 100 preview stage.
    surface.layout.setContentsMargins(16, 4, 16, 6)
    surface.setGeometry(8, 6, 284, 88)
    surface.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    stage.ensurePolished()
    surface.ensurePolished()
    surface.layout.activate()
    for label in (surface.source, surface.translation):
        if label.blur_animation is not None:
            label.blur_animation.stop()
    pixmap = QPixmap(300, 100)
    pixmap.fill(QColor('#29312E'))
    stage.render(pixmap)
    stage.deleteLater()
    _PRESET_PREVIEWS[key] = pixmap
    return pixmap


class PresetCard(QAbstractButton):
    """Checkable preset tile; the owner decides what selecting a tile changes."""
    def __init__(self, key, title, parent=None):
        super().__init__(parent)
        if key not in PRESETS:
            raise ValueError('Unknown caption preset: ' + str(key))
        self.key = key
        self.setText(title)
        self.setAccessibleName(title)
        self.setProperty('caption_control', 'preset_card')
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(120)
        self.setFixedHeight(104)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._hovered = False
        self._preview = _preset_preview(key)

    def sizeHint(self):
        return QSize(164, 104)

    def minimumSizeHint(self):
        return QSize(120, 104)

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def paintEvent(self, event):
        palette = self.palette()
        accent = palette.color(QPalette.ColorRole.Highlight)
        ink = palette.color(QPalette.ColorRole.WindowText)
        surface = _blend(palette.color(QPalette.ColorRole.Window),
                         palette.color(QPalette.ColorRole.Base), .65)
        border = _blend(surface, ink, .16)
        if self.isChecked():
            surface, border = _blend(surface, accent, .07), accent
        elif self._hovered or self.isDown():
            surface = _blend(surface, accent, .10 if self.isDown() else .04)
            border = _blend(border, accent, .55)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        if not self.isEnabled():
            painter.setOpacity(.45)
        painter.setBrush(surface)
        painter.setPen(QPen(border, 1.6 if self.isChecked() else 1))
        painter.drawRoundedRect(QRectF(.9, .9, self.width() - 1.8, self.height() - 1.8), 12, 12)

        preview_rect = QRectF(8, 8, self.width() - 16, 60)
        clip = QPainterPath()
        clip.addRoundedRect(preview_rect, 7, 7)
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(preview_rect, QColor('#29312E'))
        preview = self._preview.scaled(int(preview_rect.width()), 60,
                                       Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap(QPoint(round(preview_rect.center().x() - preview.width() / 2),
                                  round(preview_rect.center().y() - preview.height() / 2)), preview)
        painter.restore()

        font = self.font()
        font.setPixelSize(12)
        font.setWeight(font.Weight.Medium if self.isChecked() else font.Weight.Normal)
        painter.setFont(font)
        painter.setPen(ink)
        label = painter.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight,
                                                max(1, self.width() - 42))
        painter.drawText(QRectF(11, 75, self.width() - 42, 21),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        center = QPointF(self.width() - 20, 85.5)
        painter.setBrush(accent if self.isChecked() else Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(accent if self.isChecked() else border, 1.1))
        painter.drawEllipse(center, 6.5, 6.5)
        if self.isChecked():
            painter.setPen(QPen(palette.color(QPalette.ColorRole.HighlightedText), 1.6,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPolyline(QPolygonF([center + QPointF(-3, 0), center + QPointF(-.7, 2.1),
                                            center + QPointF(3.2, -2.1)]))
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(accent, 1, Qt.PenStyle.DashLine))
            painter.drawRoundedRect(QRectF(3.5, 3.5, self.width() - 7, self.height() - 7), 9, 9)


class WheelSafeSlider(_PageWheelMixin, QSlider):
    """Arrow keys and dragging edit values; the wheel scrolls the page."""


class ValueSlider(QWidget):
    """One integer value shared by a wheel-safe slider and compact number box."""
    valueChanged = pyqtSignal(int)

    def __init__(self, minimum, maximum, suffix='', parent=None):
        super().__init__(parent)
        if maximum < minimum:
            raise ValueError('Slider maximum must not be less than minimum')
        self.setProperty('caption_control', 'value_slider')
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.slider = WheelSafeSlider(Qt.Orientation.Horizontal, self)
        self.slider.setProperty('caption_control', 'slider')
        self.slider.setRange(minimum, maximum)
        self.slider.setPageStep(max(1, (maximum - minimum) // 10))
        self.slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.spin = WheelSafeSpinBox(self)
        self.spin.setProperty('caption_control', 'value_input')
        self.spin.setRange(minimum, maximum)
        self.spin.setSuffix(suffix)
        # Qt 6.12 miscalculates a styled NoButtons spin box's edit field as 1 px.
        # Keep its normal layout path; the editor hides the button subcontrols.
        self.spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        self.spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.spin.setFixedWidth(72)
        self.spin.setMinimumHeight(36)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)
        self.setFocusProxy(self.slider)
        self._value = minimum
        self.slider.valueChanged.connect(self.setValue)
        self.spin.valueChanged.connect(self.setValue)

    def value(self):
        return self._value

    def setValue(self, value):
        value = max(self.slider.minimum(), min(self.slider.maximum(), int(value)))
        changed = value != self._value
        self._value = value
        with QSignalBlocker(self.slider), QSignalBlocker(self.spin):
            self.slider.setValue(value)
            self.spin.setValue(value)
        if changed:
            self.valueChanged.emit(value)

    def setAccessibleName(self, name):
        super().setAccessibleName(name)
        for control in (self.slider, self.spin, self.spin.lineEdit()):
            control.setAccessibleName(name)
