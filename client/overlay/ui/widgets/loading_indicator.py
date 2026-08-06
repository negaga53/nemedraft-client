"""LoadingIndicator — a rotating arc spinner plus a status label.

The overlay's only feedback while a prediction is in flight (or before the
first pack of a draft arrives) used to be a bare 3px indeterminate
``QProgressBar``, which reads as a hang on anything longer than a glance.
This widget is the primary "something is happening" affordance instead:
a small QPainter-drawn spinner (no bundled image assets, so the
PyInstaller ``datas`` list needs no changes) next to a translated status
line.

Kept intentionally dumb: callers own the message text and visibility —
``show_message()`` / ``hide_indicator()`` are the only entry points, and
the spin timer is started/stopped from those same two calls so there is
no idle repaint cost while hidden.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from client.overlay.ui.theme import tokens

_SIZE = 18
_TICK_MS = 80
_STEP_DEG = 24  # degrees advanced per tick — a full turn in ~1.2s
_ARC_SPAN_DEG = 300
_PEN_WIDTH = 2.5


class _SpinnerArc(QWidget):
    """Small rotating arc painted in the theme's accent colour.

    ``start()``/``stop()`` are the only timer controls — no showEvent /
    hideEvent magic, so behaviour doesn't depend on whether this widget's
    ancestor chain is actually on screen (relevant for headless tests).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(_SIZE, _SIZE)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._advance)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def is_spinning(self) -> bool:
        return self._timer.isActive()

    def _advance(self) -> None:
        self._angle = (self._angle + _STEP_DEG) % 360
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 — Qt convention
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        pen = QPen(tokens.qcolor(tokens.ACCENT))
        pen.setWidthF(_PEN_WIDTH)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)

        margin = _PEN_WIDTH
        rect = QRectF(margin, margin, _SIZE - 2 * margin, _SIZE - 2 * margin)
        # QPainter angles are in 1/16ths of a degree, counter-clockwise
        # from the 3 o'clock position — negate so the arc turns clockwise.
        start_angle = int(-self._angle * 16)
        p.drawArc(rect, start_angle, _ARC_SPAN_DEG * 16)
        p.end()


class LoadingIndicator(QWidget):
    """Spinner + status label, laid out horizontally, hidden by default."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("loadingIndicator")

        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(8)

        self._spinner = _SpinnerArc(self)
        row.addWidget(self._spinner)

        self._label = QLabel("", self)
        self._label.setObjectName("loadingLabel")
        row.addWidget(self._label, stretch=1)

        self.setVisible(False)

    def show_message(self, message: str) -> None:
        """Show the indicator with *message* and start the spin timer."""
        self._label.setText(message)
        self.setVisible(True)
        self._spinner.start()

    def set_message(self, message: str) -> None:
        """Update the status text without changing visibility or the timer."""
        self._label.setText(message)

    def hide_indicator(self) -> None:
        """Hide the indicator and stop the spin timer (no idle repaint)."""
        self._spinner.stop()
        self.setVisible(False)

    def is_spinning(self) -> bool:
        return self._spinner.is_spinning()

    def message(self) -> str:
        return self._label.text()
