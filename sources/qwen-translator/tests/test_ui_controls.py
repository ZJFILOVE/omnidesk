"""Regression coverage for accidental setting changes during page scrolling."""
import os
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication, QLineEdit

from desktop_ui import Select, scroll_page
from ui_controls import WheelSafeComboBox, WheelSafeDoubleSpinBox, WheelSafeSpinBox


class ScrollControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')

    def setUp(self):
        self.page, layout = scroll_page()
        self.other = QLineEdit()
        layout.addWidget(self.other)
        self.controls = [Select(), WheelSafeComboBox(), WheelSafeSpinBox(),
                         WheelSafeDoubleSpinBox()]
        for control in self.controls:
            if isinstance(control, WheelSafeComboBox):
                control.addItems([f'Option {i}' for i in range(60)])
                control.setCurrentIndex(1)
            else:
                control.setValue(50)
            layout.addWidget(control)
        layout.addSpacing(1200)
        self.page.resize(500, 400)
        self.page.show()
        self.app.processEvents()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()

    def wheel(self, target, delta):
        position = QPointF(target.rect().center())
        event = QWheelEvent(
            position, target.mapToGlobal(position), QPoint(), QPoint(0, delta),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        self.app.sendEvent(target, event)
        self.app.processEvents()

    def test_wheel_scrolls_page_without_changing_focused_or_unfocused_controls(self):
        bar = self.page.verticalScrollBar()
        for control in self.controls:
            signal = (control.currentIndexChanged if isinstance(control, WheelSafeComboBox)
                      else control.valueChanged)
            changed = QSignalSpy(signal)
            for focused in (False, True):
                with self.subTest(control=type(control).__name__, focused=focused):
                    bar.setValue(0)
                    (control if focused else self.other).setFocus()
                    self.app.processEvents()
                    self.assertEqual(control.hasFocus(), focused)
                    self.wheel(control, -120)
                    down = bar.value()
                    self.assertGreater(down, 0)
                    self.wheel(control, 120)
                    self.assertLess(bar.value(), down)
                    self.assertEqual(len(changed), 0)
                    self.assertEqual(control.hasFocus(), focused)

    def test_popup_scrolls_without_committing_until_mouse_selection(self):
        combo = self.controls[0]
        combo.setMaxVisibleItems(6)
        changed = QSignalSpy(combo.currentIndexChanged)
        QTest.mouseClick(combo, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        view = combo.view()
        self.assertTrue(view.isVisible())
        self.wheel(view.viewport(), -120)
        self.assertGreater(view.verticalScrollBar().value(), 0)
        self.assertEqual(len(changed), 0)
        self.assertEqual(self.page.verticalScrollBar().value(), 0)
        index = view.indexAt(view.viewport().rect().center())
        self.assertTrue(index.isValid())
        # Qt briefly suppresses releases after opening a popup, to prevent the
        # opening click from also committing the item underneath the pointer.
        QTest.qWait(QApplication.doubleClickInterval() + 10)
        QTest.mouseMove(view.viewport(), view.visualRect(index).center())
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.visualRect(index).center())
        self.assertFalse(view.isVisible())
        self.assertEqual(combo.currentIndex(), index.row())

    def test_keyboard_and_typing_still_edit_values(self):
        for control in self.controls:
            control.setFocus()
            if isinstance(control, WheelSafeComboBox):
                QTest.keyClick(control, Qt.Key.Key_Down)
                self.assertEqual(control.currentIndex(), 2)
            else:
                QTest.keyClick(control, Qt.Key.Key_Up)
                self.assertEqual(control.value(), 51)
                control.selectAll()
                QTest.keyClicks(control, '42')
                QTest.keyClick(control, Qt.Key.Key_Return)
                self.assertEqual(control.value(), 42)
