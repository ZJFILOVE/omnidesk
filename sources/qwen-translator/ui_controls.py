"""Form controls that leave wheel scrolling to the containing page."""

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import QAbstractScrollArea, QComboBox, QDoubleSpinBox, QFontComboBox, QSpinBox


class _PageWheelMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        # Ignore even while focused: a previous click must not turn subsequent
        # page scrolling into a settings change. An open combo's popup view
        # handles its own wheel events, so its list remains scrollable.
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractScrollArea):
                viewport = parent.viewport()
                forwarded = QWheelEvent(
                    viewport.mapFromGlobal(event.globalPosition()),
                    event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                    event.buttons(), event.modifiers(), event.phase(),
                    event.inverted(), Qt.MouseEventSource.MouseEventSynthesizedByApplication,
                    event.pointingDevice(),
                )
                QCoreApplication.sendEvent(viewport, forwarded)
                event.setAccepted(forwarded.isAccepted())
                return
            parent = parent.parentWidget()
        event.ignore()


class WheelSafeComboBox(_PageWheelMixin, QComboBox):
    """Select with a click or keyboard; scroll only inside the open popup."""


class WheelSafeFontComboBox(_PageWheelMixin, QFontComboBox):
    """Font selector with the same page-scroll behavior."""


class WheelSafeSpinBox(_PageWheelMixin, QSpinBox):
    """Edit with typing, arrow keys or buttons, without wheel changes."""


class WheelSafeDoubleSpinBox(_PageWheelMixin, QDoubleSpinBox):
    """Decimal input with the same wheel behavior as integer inputs."""
