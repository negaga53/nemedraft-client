"""ScoreBar — a 78×16 progress bar with the percentage rendered inside the fill."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QWidget

from client.overlay.ui.theme import tokens

_W = 92
_H = 22
_RADIUS = 4


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

        # track well + inner hairline
        p.setPen(QPen(tokens.qcolor(tokens.L1_STROKE)))
        p.setBrush(tokens.qcolor(tokens.L0_WELL))
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), _RADIUS, _RADIUS)
        p.setPen(Qt.PenStyle.NoPen)

        # fill
        fill_w = _W * self._fraction
        if fill_w > 0:
            fill_rect = QRectF(0, 0, fill_w, _H)
            grad = QLinearGradient(0, 0, _W, 0)
            deep, bright = tokens.score_gradient(self._fraction)
            grad.setColorAt(0, deep)
            grad.setColorAt(1, bright)
            p.setBrush(QBrush(grad))
            p.drawRoundedRect(fill_rect, _RADIUS, _RADIUS)

        # label (score number, centered, white with shadow)
        font = QFont()
        font.setPointSize(11)
        font.setBold(True)
        p.setFont(font)

        # shadow
        p.setPen(QPen(QColor(0, 0, 0, 140)))
        p.drawText(rect.translated(0, 1), Qt.AlignmentFlag.AlignCenter, self._label)
        # text
        p.setPen(QPen(QColor("#ffffff")))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._label)

        p.end()
