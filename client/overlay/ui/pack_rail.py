"""Bottom deck strip for the Pack tab's full view.

Lays out small glass cards side by side: archetype, curve, lanes. Visual
states are objectNames + dynamic properties resolved by the theme stylesheet.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from client.overlay.i18n import tr
from client.overlay.mana_icons import get_mana_icon_cache
from client.overlay.ui.theme import set_prop

_MANA_LETTERS = ("W", "U", "B", "R", "G")

_DECK_SIZE = 40

# Canonical MTG names for colour combinations (order-insensitive).
_ARCHETYPE_NAMES: dict[frozenset[str], str] = {
    frozenset("W"): "Mono-White",
    frozenset("U"): "Mono-Blue",
    frozenset("B"): "Mono-Black",
    frozenset("R"): "Mono-Red",
    frozenset("G"): "Mono-Green",
    frozenset("WU"): "Azorius",
    frozenset("UB"): "Dimir",
    frozenset("BR"): "Rakdos",
    frozenset("RG"): "Gruul",
    frozenset("GW"): "Selesnya",
    frozenset("WB"): "Orzhov",
    frozenset("UR"): "Izzet",
    frozenset("BG"): "Golgari",
    frozenset("RW"): "Boros",
    frozenset("GU"): "Simic",
    frozenset("GWU"): "Bant",
    frozenset("WUB"): "Esper",
    frozenset("UBR"): "Grixis",
    frozenset("BRG"): "Jund",
    frozenset("RGW"): "Naya",
    frozenset("WBG"): "Abzan",
    frozenset("URW"): "Jeskai",
    frozenset("BGU"): "Sultai",
    frozenset("RWB"): "Mardu",
    frozenset("GUR"): "Temur",
}


def archetype_display_name(colors: list[str]) -> str:
    """Human name for a colour combination ("UR" → "Izzet")."""
    combo = frozenset(c for c in colors if c in _MANA_LETTERS)
    if not combo:
        return "—"
    name = _ARCHETYPE_NAMES.get(combo)
    if name:
        return name
    return f"{len(combo)}-Color"


class FactionLine(QWidget):
    """Mana pips for a colour combination followed by its faction name.

    "UR" renders as the U and R mana icons then "Izzet". Shared by the Pack
    tab's deck strip and the Deck tab's header so both read identically.
    """

    def __init__(
        self,
        *,
        icon_size: int = 18,
        name_object_name: str = "archetypeName",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(5)

        cache = get_mana_icon_cache()
        box = icon_size + 2
        self._icons: dict[str, QLabel] = {}
        for c in _MANA_LETTERS:
            icon = QLabel()
            pm = cache.get_pixmap(c, icon_size)
            if pm:
                icon.setPixmap(pm)
            icon.setFixedSize(box, box)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon.setVisible(False)
            self._icons[c] = icon
            self._layout.addWidget(icon)

        self.name_label = QLabel("—")
        self.name_label.setObjectName(name_object_name)
        self.name_label.setWordWrap(False)
        self._layout.addWidget(self.name_label)

    def add_trailing_stretch(self) -> None:
        """Push the pips + name to the left when the line owns its full row."""
        self._layout.addStretch()

    def set_colors(self, colors: list[str]) -> None:
        active = {c for c in colors if c in _MANA_LETTERS}
        for c in _MANA_LETTERS:
            self._icons[c].setVisible(c in active)
        self.name_label.setText(archetype_display_name(colors))


class _ArchetypeCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("archetypeCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        cache = get_mana_icon_cache()

        # Row 1 — identity: the shared faction line + score chip right.
        top = QHBoxLayout()
        top.setSpacing(5)
        self._faction_line = FactionLine(icon_size=18, name_object_name="archetypeName")
        # Aliases preserve the previous public surface (tests + callers).
        self._color_icons = self._faction_line._icons
        self.name_label = self._faction_line.name_label
        top.addWidget(self._faction_line, 1)
        self.score_chip = QLabel("")
        self.score_chip.setObjectName("scoreChip")
        self.score_chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.score_chip.setVisible(False)
        top.addWidget(self.score_chip, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(top)

        # Row 2 — deck fill: thin progress bar towards 40 cards + count.
        fill = QHBoxLayout()
        fill.setSpacing(6)
        self.count_bar = QProgressBar()
        self.count_bar.setObjectName("deckCountBar")
        self.count_bar.setRange(0, _DECK_SIZE)
        self.count_bar.setTextVisible(False)
        self.count_bar.setFixedHeight(6)
        self.count_label = QLabel(f"0/{_DECK_SIZE}")
        self.count_label.setObjectName("railDetail")
        fill.addWidget(self.count_bar, 1)
        fill.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(fill)

        # Colour-commitment strip: one mana icon + pip count per WUBRG colour.
        self._commit_widget = QWidget()
        self._commit_layout = QHBoxLayout(self._commit_widget)
        self._commit_layout.setContentsMargins(0, 0, 0, 0)
        self._commit_layout.setSpacing(3)
        self._commit_layout.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self._pip_icons: dict[str, QLabel] = {}
        self._pip_counts: dict[str, QLabel] = {}
        for c in _MANA_LETTERS:
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(1)

            icon = QLabel()
            pm = cache.get_pixmap(c, 19)
            if pm:
                icon.setPixmap(pm)
            # Bounding box gets a small margin so the SVG's AA halo isn't clipped.
            icon.setFixedSize(24, 24)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            count = QLabel("0")
            count.setObjectName("pipCount")
            cell_layout.addWidget(icon)
            cell_layout.addWidget(count)
            self._pip_icons[c] = icon
            self._pip_counts[c] = count
            self._commit_layout.addWidget(cell)

        layout.addWidget(self._commit_widget)

    def set_values(self, name: str, score: float, colors: list[str], count: int) -> None:
        del name  # the colour combination drives the displayed name
        self._faction_line.set_colors(colors)

        if score >= 0:
            self.score_chip.setText(f"{score:.1f}")
            self.score_chip.setVisible(True)
        else:
            self.score_chip.setVisible(False)

        self.count_label.setText(f"{count}/{_DECK_SIZE}")
        self.count_bar.setValue(min(count, _DECK_SIZE))

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
            set_prop(self._pip_counts[c], "top", c in top)


class _CurveCard(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("railCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        header = QHBoxLayout()
        self._title_label = QLabel(tr("mana_curve_label"))
        self._title_label.setObjectName("railTitle")
        header.addWidget(self._title_label)
        header.addStretch()
        self.verdict_label = QLabel("")
        self.verdict_label.setObjectName("railDetail")
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
        self.setObjectName("railCard")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        self._title_label = QLabel(tr("open_lanes_label"))
        self._title_label.setObjectName("railTitle")
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
            state_label.setObjectName("laneState")
            state_label.setProperty("lane", state)
            row_layout.addWidget(state_label)
            row_layout.addStretch()
            self.rows_layout.addWidget(row)


class DeckRail(QWidget):
    """Horizontal strip of rail cards. Used by the Pack tab full view."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.archetype_card = _ArchetypeCard()
        self.curve_card = _CurveCard()
        self.lanes_card = _LanesCard()

        # Width split ~47/33/20: the colour-commitment card needs room for
        # five double-size pips; the lanes list reads fine narrow.
        layout.addWidget(self.archetype_card, 7)
        layout.addWidget(self.curve_card, 5)
        layout.addWidget(self.lanes_card, 3)

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
