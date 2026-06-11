"""Pack row widget primitives — moved from pack_tab.py.

Visual states are expressed as dynamic properties (``tint``, ``top``,
``dimmed``, ``medal``, ``skeleton``, …) resolved by the generated theme
stylesheet — no per-widget setStyleSheet calls.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QWidget,
)

from client.overlay.i18n import tr
from client.overlay.mana_icons import get_mana_icon_cache, parse_mana_pips
from client.overlay.ui.theme import set_prop
from client.overlay.ui.theme import tokens

# Column widths (v4 — larger, more readable rows + thumbnails).
_W_RANK    = 20
_W_ART     = 48
_W_MANA    = 48
_W_NAME_MIN = 96  # stretches
_W_BAR     = 92   # ScoreBar width — must match ScoreBar._W
_W_GIHWR   = 48
_W_ATA     = 40
_ROW_H     = 36
_SPACING   = 5
_MARGIN    = 8
_ART_H     = 34


class _ManaBar(QWidget):
    """Horizontal strip of mana-pip icons."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_cost(self, mana_cost: str) -> None:
        # Clear.
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        cache = get_mana_icon_cache()
        for pip in parse_mana_pips(mana_cost):
            pm = cache.get_pixmap(pip, 14)
            if pm:
                lbl = QLabel()
                lbl.setPixmap(pm)
                lbl.setFixedSize(14, 14)
                self._layout.addWidget(lbl)
            else:
                # Fallback text.
                lbl = QLabel(pip)
                lbl.setObjectName("manaPipFallback")
                lbl.setFixedSize(14, 14)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._layout.addWidget(lbl)


class CardRow(QFrame):
    """Row: rank | art crop | mana | name | score bar | GIH | ATA."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        show_stats: bool = True,
        show_art: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setFixedHeight(_ROW_H)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._art_path: Path | None = None
        self._show_art = show_art

        layout = QHBoxLayout(self)
        layout.setContentsMargins(_MARGIN, 0, _MARGIN, 0)
        layout.setSpacing(_SPACING)

        self.rank_label = QLabel()
        self.rank_label.setObjectName("rowRank")
        self.rank_label.setFixedWidth(_W_RANK)
        self.rank_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.art_label = QLabel()
        self.art_label.setObjectName("rowArt")
        self.art_label.setFixedSize(_W_ART, _ART_H)
        self.art_label.setScaledContents(True)
        if not show_art:
            self.art_label.hide()

        self.mana_bar = _ManaBar()
        self.mana_bar.setFixedWidth(_W_MANA)

        self.name_label = QLabel()
        self.name_label.setObjectName("rowName")
        self.name_label.setMinimumWidth(_W_NAME_MIN)

        from client.overlay.ui.widgets.score_bar import ScoreBar
        self.score_bar = ScoreBar()

        self.gihwr_label: QLabel | None = None
        self.ata_label: QLabel | None = None
        if show_stats:
            self.gihwr_label = QLabel()
            self.gihwr_label.setObjectName("rowStat")
            self.gihwr_label.setFixedWidth(_W_GIHWR)
            self.gihwr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.ata_label = QLabel()
            self.ata_label.setObjectName("rowStat")
            self.ata_label.setFixedWidth(_W_ATA)
            self.ata_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(self.rank_label)
        layout.addWidget(self.art_label)
        layout.addWidget(self.mana_bar)
        layout.addWidget(self.name_label, stretch=1)
        layout.addWidget(self.score_bar)
        if self.gihwr_label:
            layout.addWidget(self.gihwr_label)
        if self.ata_label:
            layout.addWidget(self.ata_label)

    @staticmethod
    def _set_skeleton(label: QLabel) -> None:
        """Show a pulsing skeleton bar in a stat label to indicate loading."""
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        from PySide6.QtWidgets import QGraphicsOpacityEffect
        label.setText("")
        set_prop(label, "skeleton", True)
        effect = QGraphicsOpacityEffect(label)
        label.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, b"opacity", label)
        anim.setDuration(1500)
        anim.setKeyValueAt(0, 0.3)
        anim.setKeyValueAt(0.5, 1.0)
        anim.setKeyValueAt(1.0, 0.3)
        anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        anim.setLoopCount(-1)
        anim.start()

    @staticmethod
    def _set_stat(label: QLabel, *, text: str, medal: int = 0,
                  empty: bool = False) -> None:
        label.setGraphicsEffect(None)
        label.setText(text)
        set_prop(label, "skeleton", False)
        set_prop(label, "medal", medal)
        set_prop(label, "empty", empty)

    def set_data(
        self,
        pick,                         # Pick
        max_score: float,
        art_path: Path | None = None,
        gihwr_rank: int = 0,
        *,
        dimmed: bool = False,
    ) -> None:
        from client.overlay.i18n import card_name

        # Stored so the pack tab can find this row when art lands later.
        self._card_name = pick.card
        self._art_path = art_path
        # Desaturate the thumbnail visually on wheeled rows, but still render it.
        self._apply_art(art_path, dimmed=dimmed)

        is_top = pick.rank == 1 and not dimmed
        set_prop(self, "dimmed", dimmed)
        set_prop(self, "top", is_top)
        set_prop(self, "tint", "" if dimmed else tokens.card_tint(pick.colors))

        self.mana_bar.set_cost(pick.mana_cost)
        self.name_label.setText(card_name(pick.card))
        set_prop(self.name_label, "dimmed", dimmed)
        set_prop(self.name_label, "top", is_top)

        if dimmed:
            self.rank_label.setText("")
            set_prop(self.rank_label, "top", False)
            self.score_bar.set_score(0.0)
            if self.gihwr_label:
                self._set_stat(self.gihwr_label, text="")
            if self.ata_label:
                self._set_stat(self.ata_label, text="")
            self.setToolTip("")
            return

        self.rank_label.setText("★" if pick.is_elite else str(pick.rank))
        set_prop(self.rank_label, "top", is_top)

        pct = pick.score / max_score if max_score > 0 else 0.0
        self.score_bar.set_score(pct)

        if self.gihwr_label:
            if not pick.stats_loaded:
                self._set_skeleton(self.gihwr_label)
            elif pick.gihwr > 0:
                # GIH shown without % suffix per spec
                self._set_stat(
                    self.gihwr_label,
                    text=f"{pick.gihwr * 100:.1f}",
                    medal=gihwr_rank if tokens.medal_color(gihwr_rank) else 0,
                )
            else:
                self._set_stat(self.gihwr_label, text="—", empty=True)
        if self.ata_label:
            if not pick.stats_loaded:
                self._set_skeleton(self.ata_label)
            elif pick.ata > 0:
                self._set_stat(self.ata_label, text=f"{pick.ata:.1f}")
            else:
                self._set_stat(self.ata_label, text="—", empty=True)

        if pick.gihwr > 0:
            tip_parts = [f"GIH WR: {pick.gihwr:.1%}"]
            if pick.ata > 0:
                tip_parts.append(f"ATA: {pick.ata:.1f}")
            if pick.iwd != 0:
                tip_parts.append(f"IWD: {pick.iwd:+.1f}pp")
            # Surface the source format when stats came from the fallback
            # bundle, so the player knows the numbers aren't from the
            # format they're actually drafting.
            if pick.stats_format:
                tip_parts.append(f"src: {pick.stats_format}")
            self.setToolTip(" · ".join(tip_parts))
        else:
            self.setToolTip("")

    def set_art(self, art_path: Path | None) -> None:
        """Swap the row's thumbnail without rebuilding the whole row.

        Used by the per-card art prefetch path so cards can pop in as
        their images arrive without tearing down stats labels, tooltips,
        or hover state.
        """
        self._art_path = art_path
        self._apply_art(art_path)

    def _apply_art(self, art_path: Path | None, *, dimmed: bool = False) -> None:
        """Load and crop card art to the row art size, or show a neutral placeholder.

        Wheeled / taken rows get a slightly darkened thumbnail so the reader's
        eye still skips past them, but they keep visual parity with live rows.
        """
        if not self._show_art:
            return
        from PySide6.QtGui import QPainter, QPixmap
        if art_path is None or not Path(art_path).exists():
            self.art_label.clear()
            return
        pm = QPixmap(str(art_path))
        if pm.isNull():
            self.art_label.clear()
            return
        # Scryfall "small" is 146×204 with a ~7% frame on each side and the
        # art window starting around 12% down. Crop *inside* those margins so
        # the card border never shows through.
        src_w, src_h = pm.width(), pm.height()
        x = int(src_w * 0.09)
        y = int(src_h * 0.13)
        w = max(1, src_w - 2 * x)
        h = max(1, int(src_h * 0.33))
        cropped = pm.copy(x, y, w, h)
        if dimmed:
            # Overlay a translucent black so wheeled thumbnails read as "taken".
            dim = QPixmap(cropped.size())
            dim.fill(Qt.GlobalColor.black)
            p = QPainter(dim)
            p.setOpacity(0.45)
            p.drawPixmap(0, 0, cropped)
            p.end()
            self.art_label.setPixmap(dim)
        else:
            self.art_label.setPixmap(cropped)


class _ColumnHeader(QFrame):
    """Header row whose widths match :class:`CardRow`."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        show_stats: bool = True,
    ) -> None:
        super().__init__(parent)
        self._show_stats = show_stats
        self.setObjectName("columnHeader")
        self.setFixedHeight(18)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(_MARGIN, 0, _MARGIN, 0)
        layout.setSpacing(_SPACING)

        def _lbl(text: str, width: int) -> QLabel:
            l = QLabel(text)
            l.setFixedWidth(width)
            l.setAlignment(Qt.AlignmentFlag.AlignCenter)
            return l

        self._rank_lbl = _lbl(tr("col_rank"), _W_RANK)
        layout.addWidget(self._rank_lbl)
        layout.addWidget(_lbl("", _W_ART))   # art column spacer
        layout.addWidget(_lbl("", _W_MANA))  # mana column spacer
        self._name_lbl = QLabel(tr("col_card"))
        layout.addWidget(self._name_lbl, stretch=1)
        self._score_lbl = _lbl(tr("col_score"), _W_BAR)
        layout.addWidget(self._score_lbl)
        self._gihwr_lbl: QLabel | None = None
        self._ata_lbl: QLabel | None = None
        if show_stats:
            self._gihwr_lbl = _lbl(tr("col_gihwr"), _W_GIHWR)
            layout.addWidget(self._gihwr_lbl)
            self._ata_lbl = _lbl(tr("col_ata"), _W_ATA)
            layout.addWidget(self._ata_lbl)

    def set_right_gutter(self, px: int) -> None:
        """Pad the right edge so columns stay aligned with the card rows
        when the list below shows a vertical scrollbar."""
        m = self.layout().contentsMargins()
        self.layout().setContentsMargins(
            m.left(), m.top(), _MARGIN + max(0, px), m.bottom()
        )

    def retranslate(self) -> None:
        """Refresh header labels with the current language."""
        self._rank_lbl.setText(tr("col_rank"))
        self._name_lbl.setText(tr("col_card"))
        self._score_lbl.setText(tr("col_score"))
        if self._gihwr_lbl:
            self._gihwr_lbl.setText(tr("col_gihwr"))
        if self._ata_lbl:
            self._ata_lbl.setText(tr("col_ata"))


class _CardPreview(QLabel):
    """Floating card art preview that follows the mouse.

    A parentless ToolTip window — the application stylesheet does not
    cascade into it, so its look is built from tokens here (one of the
    two documented inline-style exceptions).
    """

    _CARD_W = 200
    _CARD_H = 280

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.ToolTip)
        self.setFixedSize(self._CARD_W, self._CARD_H)
        self.setScaledContents(True)
        self.setStyleSheet(
            f"background: {tokens.L0_WINDOW_OPAQUE};"
            f" border: 1px solid {tokens.L3_STROKE};"
            f" border-radius: {tokens.RADIUS_CARD}px;"
        )
        self.hide()

    def show_art(self, art_path: Path | None, global_pos: QPoint) -> None:
        if art_path is None:
            self.hide()
            return
        pm = QPixmap(str(art_path))
        if pm.isNull():
            self.hide()
            return
        self.setPixmap(pm)
        # Position to the right of the cursor, clamped to screen.
        self.move(global_pos.x() + 16, global_pos.y() - self._CARD_H // 2)
        if not self.isVisible():
            self.show()
