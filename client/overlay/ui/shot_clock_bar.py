"""Thin per-pick shot-clock bar.

The server computes the deadline (see server/seat_manager.shot_clock_duration);
this widget only renders it. Between server updates — a predict response or a
15 s queue heartbeat — it interpolates locally at 1 Hz so the bar moves
smoothly instead of jumping.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QWidget

_WARN_FRACTION = 0.33
_CRIT_SECONDS = 15.0


def _fmt_mmss(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60}:{total % 60:02d}"


class ShotClockBar(QWidget):
    """Always-visible progress bar that escalates as time runs out."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self._bar = QProgressBar()
        self._bar.setObjectName("shotClockBar")
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(3)
        self._bar.setRange(0, 1000)
        row.addWidget(self._bar, stretch=1)

        self._label = QLabel("")
        self._label.setObjectName("shotClockLabel")
        self._label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._label.hide()
        row.addWidget(self._label)

        self._remaining = 0.0
        self._total = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self._render()

    def set_remaining(self, remaining_s: float, total_s: float) -> None:
        """Reconcile against a fresh server value.

        Replaces whatever the local interpolation had decayed to — a fresh
        predict response or heartbeat is always authoritative, never
        blended with the locally-ticked estimate.
        """
        self._remaining = max(0.0, float(remaining_s))
        self._total = max(1.0, float(total_s))
        self._render()
        if not self._timer.isActive():
            self._timer.start()

    def clear(self) -> None:
        """Reset to a neutral, inactive state (seat lost / draft ended)."""
        self._timer.stop()
        self._remaining = 0.0
        self._total = 0.0
        self._render()

    def remaining_s(self) -> float:
        return self._remaining

    def fraction(self) -> float:
        if self._total <= 0:
            return 0.0
        return max(0.0, min(1.0, self._remaining / self._total))

    def severity(self) -> str:
        # A cleared/inactive bar (_total == 0) has no live countdown running
        # — it's a neutral non-event, not an emergency, so it must not read
        # as "crit" even though _remaining <= _CRIT_SECONDS is technically
        # true at 0.
        if self._total <= 0:
            return "ok"
        if self._remaining <= _CRIT_SECONDS:
            return "crit"
        if self.fraction() < _WARN_FRACTION:
            return "warn"
        return "ok"

    def label_text(self) -> str:
        return self._label.text()

    def _tick(self) -> None:
        self._remaining = max(0.0, self._remaining - 1.0)
        self._render()
        if self._remaining <= 0.0:
            self._timer.stop()

    def _render(self) -> None:
        from client.overlay.ui.theme import set_prop

        self._bar.setValue(int(self.fraction() * 1000))
        sev = self.severity()
        set_prop(self._bar, "severity", sev)
        if sev == "crit" and self._total > 0:
            self._label.setText(_fmt_mmss(self._remaining))
            self._label.show()
        else:
            self._label.setText("")
            self._label.hide()
