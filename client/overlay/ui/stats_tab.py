"""Mana-curve plot used in the Stats tab and the Pack tab's deck strip."""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QFont
from PySide6.QtWidgets import QWidget

from client.overlay.i18n import tr
from client.overlay.ui.theme import tokens

# Ideal mana curve target for 23 spells (index = CMC 0, 1, 2, 3, 4, 5, 6, 7+).
_IDEAL_CURVE = [0, 2, 5, 5, 4, 3, 2, 2]  # sum = 23


class ManaCurvePlot(QWidget):
    """Bar chart showing the deck's mana curve vs ideal distribution."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(90)
        self._curve: list[int] = [0] * 8
        self._max_val = 1

    def update_curve(self, curve: list[int]) -> None:
        self._curve = list(curve) + [0] * (8 - len(curve))
        self._max_val = max(max(self._curve), max(_IDEAL_CURVE), 1)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        n = 8
        left_margin = 4
        right_margin = 4
        bar_w = max(12, (w - left_margin - right_margin) // n)
        top_pad = 14  # room for count text above bars
        bottom = h - 14  # room for CMC labels
        usable_h = bottom - top_pad

        for i in range(n):
            x = left_margin + i * bar_w
            val = self._curve[i]
            ideal = _IDEAL_CURVE[i]
            bar_h = int((val / self._max_val) * usable_h) if self._max_val else 0
            ideal_y = bottom - int((ideal / self._max_val) * usable_h) if self._max_val else bottom

            # Bar colour: green on target, yellow below, red excess.
            if val > ideal and ideal > 0:
                color = tokens.qcolor(tokens.SCORE_LOW)
            elif val < ideal and ideal > 0:
                color = tokens.qcolor(tokens.WARN)
            else:
                color = tokens.qcolor(tokens.OK)

            # Draw bar.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            bar_x = x + 2
            bw = bar_w - 4
            painter.drawRoundedRect(bar_x, bottom - bar_h, bw, max(bar_h, 0), 2, 2)

            # Ideal marker — horizontal line.
            if ideal > 0:
                painter.setPen(QPen(tokens.qcolor(tokens.ACCENT), 1.5, Qt.PenStyle.SolidLine))
                painter.drawLine(bar_x - 1, ideal_y, bar_x + bw + 1, ideal_y)

            # CMC label below.
            painter.setPen(QPen(tokens.qcolor(tokens.TEXT_MUTED)))
            painter.setFont(QFont("Inter", 7))
            label = f"{i}" if i < 7 else "7+"
            rect = QRectF(bar_x - 2, bottom + 1, bw + 4, 12)
            painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label)

            # Count label on top of bar.
            if val > 0:
                painter.setPen(QPen(tokens.qcolor(tokens.TEXT_PRIMARY)))
                painter.setFont(QFont("Inter", 7, QFont.Weight.Bold))
                rect = QRectF(bar_x - 2, bottom - bar_h - 12, bw + 4, 11)
                painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, str(val))

        # Sum label in top-right.
        total = sum(self._curve)
        painter.setPen(QPen(tokens.qcolor(tokens.TEXT_MUTED)))
        painter.setFont(QFont("Inter", 7))
        painter.drawText(QRectF(w - 60, 1, 56, 12), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, tr("spells_count", total=total))

        painter.end()
