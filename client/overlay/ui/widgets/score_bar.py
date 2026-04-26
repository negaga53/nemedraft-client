"""ScoreBar — a 78×16 progress bar with the percentage rendered inside the fill."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

_W = 78
_H = 16
_RADIUS = 2


class ScoreBar(QWidget):
    """Fixed-size horizontal bar with a score percentage drawn inside.

    The fill width is proportional to ``fraction`` (0..1); the bar radius
    and font are fixed so rows align visually.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fraction: float = 0.0
        self._label: str = "0"
        self.setFixedSize(_W, _H)

    @property
    def fraction(self) -> float:
        return self._fraction

    @property
    def label_text(self) -> str:
        return self._label

    def set_score(self, fraction: float) -> None:
        """Set the fill fraction (0..1). Clamped and rounded for display."""
        clamped = max(0.0, min(1.0, fraction))
        self._fraction = clamped
        self._label = str(round(clamped * 100))
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 — Qt convention
        return QSize(_W, _H)

    def paintEvent(self, _event) -> None:  # noqa: N802 — Qt convention
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(0, 0, _W, _H)

        # background
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#0b0b18"))
        p.drawRoundedRect(rect, _RADIUS, _RADIUS)

        # fill
        fill_w = _W * self._fraction
        if fill_w > 0:
            fill_rect = QRectF(0, 0, fill_w, _H)
            grad = QLinearGradient(0, 0, _W, 0)
            if self._fraction > 0.7:
                grad.setColorAt(0, QColor("#2e7d32"))
                grad.setColorAt(1, QColor("#4caf50"))
            elif self._fraction > 0.35:
                grad.setColorAt(0, QColor("#b4811c"))
                grad.setColorAt(1, QColor("#ffc107"))
            else:
                grad.setColorAt(0, QColor("#852a25"))
                grad.setColorAt(1, QColor("#f44336"))
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(fill_rect, _RADIUS, _RADIUS)

        # label (score number, centered, white with shadow)
        font = QFont()
        font.setPointSize(8)
        font.setBold(True)
        p.setFont(font)

        # shadow
        p.setPen(QPen(QColor(0, 0, 0, 140)))
        p.drawText(rect.translated(0, 1), Qt.AlignmentFlag.AlignCenter, self._label)
        # text
        p.setPen(QPen(QColor("#ffffff")))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._label)

        p.end()
