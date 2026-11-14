"""Keep caption sliders editable without changing values during page scroll."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from caption_editor_widgets import PresetCard, ValueSlider


class CaptionEditorWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_value_sources_sync_once_and_respect_parent_signal_block(self):
        control = ValueSlider(14, 64, ' px')
        signal = QSignalSpy(control.valueChanged)
        for source, value in ((control, 24), (control.slider, 28), (control.spin, 32)):
            before = len(signal)
            source.setValue(value)
            self.assertEqual((control.value(), control.slider.value(), control.spin.value()), (value,) * 3)
            self.assertEqual(len(signal), before + 1)
        control.blockSignals(True)
        control.setValue(42)
        control.blockSignals(False)
        self.assertEqual(len(signal), 3)
        self.assertEqual(control.slider.value(), 42)
        self.assertEqual(control.spin.value(), 42)
        control.setValue(500)
        self.assertEqual(control.value(), 64)
        control.setValue(-1)
        self.assertEqual(control.value(), 14)
        control.setAccessibleName('字幕字号')
        self.assertEqual(control.slider.accessibleName(), '字幕字号')
        self.assertEqual(control.spin.accessibleName(), '字幕字号')
        control.deleteLater()

    def test_wheel_on_focused_slider_or_spin_scrolls_page_without_editing(self):
        page = QScrollArea()
        page.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        control = ValueSlider(0, 100, ' %')
        control.setValue(70)
        layout.addWidget(control)
        layout.addSpacing(1000)
        page.setWidget(content)
        page.resize(400, 300)
        page.show()
        self.app.processEvents()
        changed = QSignalSpy(control.valueChanged)
        for target in (control.slider, control.spin):
            page.verticalScrollBar().setValue(0)
            target.setFocus()
            point = QPointF(target.rect().center())
            event = QWheelEvent(point, target.mapToGlobal(point), QPoint(), QPoint(0, -120),
                                Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                Qt.ScrollPhase.NoScrollPhase, False)
            self.app.sendEvent(target, event)
            self.app.processEvents()
            self.assertGreater(page.verticalScrollBar().value(), 0)
            self.assertEqual(control.value(), 70)
            self.assertEqual(len(changed), 0)
        page.close()
        page.deleteLater()

    def test_slider_arrows_and_spin_typing_remain_available(self):
        control = ValueSlider(14, 64, ' px')
        control.setValue(22)
        control.show()
        control.slider.setFocus()
        QTest.keyClick(control.slider, Qt.Key.Key_Right)
        self.assertEqual(control.value(), 23)
        control.spin.setFocus()
        control.spin.selectAll()
        QTest.keyClicks(control.spin, '48')
        QTest.keyClick(control.spin, Qt.Key.Key_Return)
        self.assertEqual(control.value(), 48)
        control.close()
        control.deleteLater()

    def test_preset_card_supports_mouse_space_and_enter(self):
        card = PresetCard('blur', '模糊效果')
        card.resize(164, 104)
        card.show()
        card.setFocus()
        clicked = QSignalSpy(card.clicked)
        QTest.mouseClick(card, Qt.MouseButton.LeftButton)
        self.assertTrue(card.isChecked())
        QTest.keyClick(card, Qt.Key.Key_Space)
        self.assertFalse(card.isChecked())
        QTest.keyClick(card, Qt.Key.Key_Return)
        self.assertTrue(card.isChecked())
        self.assertEqual(len(clicked), 3)
        self.assertEqual(card.key, 'blur')
        self.assertEqual(card.height(), 104)
        self.assertFalse(card._preview.isNull())
        card.close()
        card.deleteLater()


if __name__ == '__main__':
    unittest.main()
