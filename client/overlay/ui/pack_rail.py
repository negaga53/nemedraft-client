"""Right-hand deck rail for the Pack tab's full view.

Stacks four small cards: archetype, curve, lanes, wheel.
Replaces the old charts/meters rows and bottom wheel tracker.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from client.overlay.i18n import tr
from client.overlay.mana_icons import get_mana_icon_cache
from client.overlay.ui.styles import BG_CARD

_MANA_LETTERS = ("W", "U", "B", "R", "G")

_SECTION_LABEL_STYLE = (
    "color: #888888; font-size: 9px; text-transform: uppercase; "
    "letter-spacing: .08em; font-weight: 700;"
)
_CARD_STYLE = (
    f"QFrame[railCard='true'] {{ background: {BG_CARD}; border-radius: 4px; }}"
)


def _pip_html(color: str) -> str:
    colors = {"W": "#f5e6a3", "U": "#3b8fd4", "B": "#9b8e82", "R": "#e74c3c", "G": "#2ecc71"}
    return f'<span style="color:{colors.get(color, "#aaa")}">●</span>'


class _ArchetypeCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("railCard", True)
        self.setStyleSheet(
            "QFrame { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, "
            "stop:0 rgba(207,181,59,.16), stop:1 rgba(20,20,36,.5)); "
            "border-radius: 4px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(4)
        self.name_label = QLabel("—")
        self.name_label.setStyleSheet(
            'font-family: Georgia, serif; color: #cfb53b; font-weight: 700; font-size: 13px;'
        )
        self.name_label.setWordWrap(False)
        self.count_label = QLabel("0/40")
        self.count_label.setStyleSheet("color: #888; font-size: 10px;")
        top.addWidget(self.name_label, 1)
        top.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(top)

        # Colour-commitment strip: one mana icon + pip count per WUBRG colour.
        self._commit_widget = QWidget()
        self._commit_layout = QHBoxLayout(self._commit_widget)
        self._commit_layout.setContentsMargins(0, 0, 0, 0)
        self._commit_layout.setSpacing(6)
        self._commit_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._pip_icons: dict[str, QLabel] = {}
        self._pip_counts: dict[str, QLabel] = {}
        cache = get_mana_icon_cache()
        for c in _MANA_LETTERS:
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(2)

            icon = QLabel()
            pm = cache.get_pixmap(c, 11)
            if pm:
                icon.setPixmap(pm)
            # Bounding box gets a small margin so the SVG's AA halo isn't clipped.
            icon.setFixedSize(14, 14)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            count = QLabel("0")
            count.setStyleSheet("color: #777; font-size: 11px;")
            cell_layout.addWidget(icon)
            cell_layout.addWidget(count)
            self._pip_icons[c] = icon
            self._pip_counts[c] = count
            self._commit_layout.addWidget(cell)

        layout.addWidget(self._commit_widget)

    def set_values(self, name: str, score: float, colors: list[str], count: int) -> None:
        score_text = f"  score {score:.1f}" if score >= 0 else ""
        self.name_label.setText(f"{name}{score_text}")
        self.count_label.setText(f"{count}/40")

    def set_pips(self, pip_totals: dict[str, int]) -> None:
        """Update the WUBRG pip counts, bolding the 2 most-represented colours."""
        for c in _MANA_LETTERS:
            self._pip_counts[c].setText(str(pip_totals.get(c, 0)))

        # Identify the top-2 colours by pip count; ties broken by WUBRG order
        # (deterministic). Colours with 0 pips are never highlighted.
        ranked = sorted(
            _MANA_LETTERS,
            key=lambda c: (-pip_totals.get(c, 0), _MANA_LETTERS.index(c)),
        )
        top = {c for c in ranked[:2] if pip_totals.get(c, 0) > 0}
        for c in _MANA_LETTERS:
            is_top = c in top
            self._pip_counts[c].setStyleSheet(
                f"color: {'#ffffff' if is_top else '#666'};"
                f" font-size: 11px;"
                f" font-weight: {'700' if is_top else '400'};"
            )
            # Dim icons for non-top colours without hiding them entirely.
            self._pip_icons[c].setStyleSheet(
                "" if is_top else "opacity: 0.35;"
            )


class _CurveCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("railCard", True)
        self.setStyleSheet(_CARD_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        header = QHBoxLayout()
        self._title_label = QLabel(tr("mana_curve_label"))
        self._title_label.setStyleSheet(_SECTION_LABEL_STYLE)
        header.addWidget(self._title_label)
        header.addStretch()
        self.verdict_label = QLabel("")
        self.verdict_label.setStyleSheet("color: #888; font-size: 9px;")
        header.addWidget(self.verdict_label)
        layout.addLayout(header)

        from client.overlay.ui.stats_tab import ManaCurvePlot
        self.plot = ManaCurvePlot()
        layout.addWidget(self.plot)

    def retranslate(self) -> None:
        self._title_label.setText(tr("mana_curve_label"))


class _LanesCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("railCard", True)
        self.setStyleSheet(_CARD_STYLE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        self._title_label = QLabel(tr("open_lanes_label"))
        self._title_label.setStyleSheet(_SECTION_LABEL_STYLE)
        layout.addWidget(self._title_label)

        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(0)
        layout.addLayout(self.rows_layout)

    def retranslate(self) -> None:
        self._title_label.setText(tr("open_lanes_label"))

    def set_lanes(self, lanes: list[tuple[str, str]]) -> None:
        """lanes: [(color_letter, "open"|"closing"|"closed"), ...] in priority order."""
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        color_map = {"open": "#4caf50", "closing": "#cd7f32", "closed": "#888"}
        cache = get_mana_icon_cache()
        for color, state in lanes[:5]:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 1, 0, 1)
            row_layout.setSpacing(6)

            icon = QLabel()
            pm = cache.get_pixmap(color, 14)
            if pm:
                icon.setPixmap(pm)
            icon.setFixedSize(14, 14)
            row_layout.addWidget(icon)

            state_label = QLabel(state)
            state_label.setStyleSheet(
                f'color: {color_map.get(state, "#888")}; font-size: 10px;'
            )
            row_layout.addWidget(state_label)
            row_layout.addStretch()
            self.rows_layout.addWidget(row)


class DeckRail(QWidget):
    """Vertical stack of rail cards. Used by the Pack tab full view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.archetype_card = _ArchetypeCard()
        self.curve_card = _CurveCard()
        self.lanes_card = _LanesCard()

        layout.addWidget(self.archetype_card)
        layout.addWidget(self.curve_card)
        layout.addWidget(self.lanes_card)
        layout.addStretch()

    def set_archetype(self, name: str, score: float, colors: list[str], count: int) -> None:
        self.archetype_card.set_values(name, score, colors, count)

    def set_pips(self, pip_totals: dict[str, int]) -> None:
        """Forward the colour-commitment pip counts to the archetype card."""
        self.archetype_card.set_pips(pip_totals)

    def set_curve(self, pool_analysis) -> None:
        """Update the embedded mana-curve plot from PoolAnalysis.curve (list[int])."""
        curve = getattr(pool_analysis, "curve", None) or [0] * 8
        self.curve_card.plot.update_curve(curve)

    def set_lanes(self, lanes: list[tuple[str, str]]) -> None:
        self.lanes_card.set_lanes(lanes)

    def retranslate(self) -> None:
        self.curve_card.retranslate()
        self.lanes_card.retranslate()
