"""Deck builder tab — suggests a 40-card deck from the drafted pool."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from common.inference.deck_builder import DeckSuggestion
from common.inference.pool_analyzer import ScryfallCard
from client.overlay.i18n import card_name, tr
from client.overlay.mana_icons import get_mana_icon_cache, parse_mana_pips
from client.overlay.ui.pack_widgets import _CardPreview
from client.overlay.ui.styles import card_row_bg, short_type

# Row constants.
_ROW_H = 24
_W_COUNT = 24
_W_ART = 34
_W_MANA = 54
_W_TYPE = 30
_MARGIN = 4
_SPACING = 5
_ICON_SZ = 12
_ART_H = 20

# Basic land → deck color mapping.
_BASIC_COLOR: dict[str, str] = {
    "Plains": "W", "Island": "U", "Swamp": "B",
    "Mountain": "R", "Forest": "G",
}


class _DeckManaBar(QWidget):
    """Horizontal strip of mana-pip icons (compact for deck rows)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_cost(self, mana_cost: str) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        cache = get_mana_icon_cache()
        for pip in parse_mana_pips(mana_cost):
            pm = cache.get_pixmap(pip, _ICON_SZ)
            if pm:
                lbl = QLabel()
                lbl.setPixmap(pm)
                lbl.setFixedSize(_ICON_SZ, _ICON_SZ)
                self._layout.addWidget(lbl)
            else:
                lbl = QLabel(pip)
                lbl.setFixedSize(_ICON_SZ, _ICON_SZ)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet("color: #aaa; font-size: 10px;")
                self._layout.addWidget(lbl)


class _DeckCardRow(QFrame):
    """A single row: art | count | mana icons | name | type, with colour-tinted background."""

    def __init__(
        self,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFixedHeight(_ROW_H)
        self.setMouseTracking(True)
        self._card_name: str = ""

        layout = QHBoxLayout(self)
        layout.setContentsMargins(_MARGIN, 0, _MARGIN, 0)
        layout.setSpacing(_SPACING)

        self.art_label = QLabel()
        self.art_label.setFixedSize(_W_ART, _ART_H)
        self.art_label.setStyleSheet(
            "border-radius: 2px; background: #1a1a28;"
        )
        self.art_label.setScaledContents(True)

        self.count_label = QLabel()
        self.count_label.setFixedWidth(_W_COUNT)
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_label.setStyleSheet("font-size: 12px;")

        self.mana_bar = _DeckManaBar()
        self.mana_bar.setFixedWidth(_W_MANA)

        self.name_label = QLabel()

        self.type_label = QLabel()
        self.type_label.setFixedWidth(_W_TYPE)
        self.type_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.type_label.setStyleSheet("color: #888; font-size: 11px;")

        layout.addWidget(self.art_label)
        layout.addWidget(self.count_label)
        layout.addWidget(self.mana_bar)
        layout.addWidget(self.name_label, stretch=1)
        layout.addWidget(self.type_label)

    def set_art(self, art_path: Path | None) -> None:
        """Render a cropped thumbnail on the left of the row, or clear it."""
        if art_path is None or not Path(art_path).exists():
            self.art_label.clear()
            self.art_label.setStyleSheet(
                "border-radius: 2px; background: #1a1a28;"
            )
            return
        pm = QPixmap(str(art_path))
        if pm.isNull():
            self.art_label.clear()
            return
        # Crop inside the card's frame so the border is never visible.
        src_w, src_h = pm.width(), pm.height()
        x = int(src_w * 0.09)
        y = int(src_h * 0.13)
        w = max(1, src_w - 2 * x)
        h = max(1, int(src_h * 0.33))
        self.art_label.setPixmap(pm.copy(x, y, w, h))

    def set_data(
        self,
        name: str,
        count: int,
        mana_cost: str,
        colors: list[str],
        type_line: str,
        *,
        dimmed: bool = False,
    ) -> None:
        self._card_name = name
        if dimmed:
            self.setStyleSheet(
                "_DeckCardRow { background: rgba(40,40,60,0.6); border-radius: 2px; }"
            )
            self.name_label.setStyleSheet(
                'font-size: 12px; color: #777; font-style: italic;'
            )
            self.count_label.setStyleSheet("color: #555; font-size: 12px;")
            self.type_label.setStyleSheet("color: #555; font-size: 11px;")
        else:
            bg = card_row_bg(colors)
            self.setStyleSheet(
                f"_DeckCardRow {{ background: {bg}; border-radius: 2px; }}"
                f"_DeckCardRow:hover {{ background: rgba(255,255,255,20); }}"
            )
            self.name_label.setStyleSheet(
                'font-size: 12px; color: #e0e0e0;'
            )
        self.count_label.setText(f"{count}x" if count > 1 else "")
        self.mana_bar.set_cost(mana_cost)
        self.name_label.setText(card_name(name))
        self.type_label.setText(short_type(type_line) if type_line else "")


class _CategorySection(QFrame):
    """Section header + card list for a deck category."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)

        self._header = QLabel(title)
        self._header.setObjectName("sectionTitle")
        self._header.setStyleSheet(
            "color: #cfb53b; font-size: 11px; font-weight: 700;"
            " text-transform: uppercase; letter-spacing: 1px; padding: 6px 6px 4px 6px;"
            " border-bottom: 1px solid #1f1f30;"
        )
        self._layout.addWidget(self._header)

    def add_card(self, row: _DeckCardRow) -> None:
        self._layout.addWidget(row)


class DeckTab(QWidget):
    """Deck builder tab with archetype selector, categorised main deck, and sideboard."""

    # Emitted when the user changes the archetype dropdown.
    archetype_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMouseTracking(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Archetype strip — gold-tinted gradient pill with stats inline.
        self._archetype_strip = QFrame()
        self._archetype_strip.setStyleSheet(
            "QFrame { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, "
            "stop:0 rgba(207,181,59,.14), stop:1 rgba(20,20,36,.5)); "
            "border-radius: 6px; }"
        )
        strip_layout = QHBoxLayout(self._archetype_strip)
        strip_layout.setContentsMargins(10, 6, 8, 6)
        strip_layout.setSpacing(8)

        arch_col = QVBoxLayout()
        arch_col.setSpacing(2)
        self._arch_name_label = QLabel("—")
        self._arch_name_label.setStyleSheet(
            "color: #cfb53b; font-size: 15px; font-weight: 700;"
        )
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("color: #aaa; font-size: 11px;")
        self._stats_label.setWordWrap(True)
        arch_col.addWidget(self._arch_name_label)
        arch_col.addWidget(self._stats_label)
        strip_layout.addLayout(arch_col, stretch=1)

        self._copy_btn = QPushButton(tr("copy_deck_btn"))
        self._copy_btn.setMinimumWidth(80)
        strip_layout.addWidget(self._copy_btn)

        self._arch_combo = QComboBox()
        self._arch_combo.setMinimumWidth(90)
        self._arch_combo.setToolTip(tr("archetype_label"))
        strip_layout.addWidget(self._arch_combo)

        layout.addWidget(self._archetype_strip)

        # Main deck (scrollable).
        scroll_main = QScrollArea()
        scroll_main.setWidgetResizable(True)
        scroll_main.setFrameShape(QFrame.Shape.NoFrame)
        self._main_container = QWidget()
        self._main_container.setMouseTracking(True)
        self._main_layout = QVBoxLayout(self._main_container)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(2)
        self._main_layout.addStretch()
        scroll_main.setWidget(self._main_container)
        layout.addWidget(scroll_main, stretch=1)

        # Sideboard — collapsed by default.
        self._sideboard_toggle = QPushButton(tr("sideboard_header"))
        self._sideboard_toggle.setCheckable(True)
        self._sideboard_toggle.setChecked(False)
        self._sideboard_toggle.setStyleSheet(
            "QPushButton { background: transparent; border: none;"
            " color: #aaaaaa; font-size: 11px; font-weight: 700;"
            " text-transform: uppercase; letter-spacing: 1px; text-align: left;"
            " padding: 8px 6px; border-top: 1px solid #1f1f30; }"
            "QPushButton:hover { color: #e0e0e0; }"
        )
        layout.addWidget(self._sideboard_toggle)

        self._sb_scroll = QScrollArea()
        self._sb_scroll.setWidgetResizable(True)
        self._sb_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._sb_scroll.setMaximumHeight(160)
        self._sb_container = QWidget()
        self._sb_container.setMouseTracking(True)
        self._sb_layout = QVBoxLayout(self._sb_container)
        self._sb_layout.setContentsMargins(0, 0, 0, 0)
        self._sb_layout.setSpacing(1)
        self._sb_layout.addStretch()
        self._sb_scroll.setWidget(self._sb_container)
        self._sb_scroll.setVisible(False)
        layout.addWidget(self._sb_scroll)

        self._sideboard_toggle.toggled.connect(self._on_sideboard_toggle)

        # Internal state.
        self._suggestions: dict[str, DeckSuggestion] = {}
        self._scryfall: dict[str, ScryfallCard] = {}
        self._current_key: str = ""
        self._pool_names: list[str] = []
        self._art_paths: dict[str, Path | None] = {}

        # Hover card preview.
        self._preview: _CardPreview | None = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(200)
        self._hover_timer.timeout.connect(self._show_preview)
        self._pending_preview_path: Path | None = None
        self._pending_pos = QPoint()

        self._arch_combo.currentTextChanged.connect(self._on_archetype_changed)
        self._copy_btn.clicked.connect(self._copy_to_clipboard)

    def set_art_paths(self, art_paths: dict[str, Path | None]) -> None:
        """Accumulate art paths for hover previews (called each pack)."""
        for k, v in art_paths.items():
            if v is not None:
                self._art_paths[k] = v

    def update_suggestions(
        self,
        suggestions: dict[str, DeckSuggestion],
        pool_names: list[str],
        scryfall_cards: dict[str, ScryfallCard] | None = None,
    ) -> None:
        """Replace displayed suggestions."""
        self._suggestions = suggestions
        self._pool_names = pool_names
        if scryfall_cards is not None:
            self._scryfall = scryfall_cards

        # Sort by score descending so "best" is the first entry.
        ordered = sorted(
            suggestions.items(),
            key=lambda kv: kv[1].score,
            reverse=True,
        )

        self._arch_combo.blockSignals(True)
        self._arch_combo.clear()
        for key, sug in ordered:
            score_text = f"{sug.score:.1f}" if sug.score >= 0 else "N/A"
            self._arch_combo.addItem(f"{key}  ({score_text})", key)
        self._arch_combo.blockSignals(False)

        if ordered:
            best_key = ordered[0][0]
            self._arch_combo.setCurrentIndex(0)
            self._show_suggestion(best_key)

    def _on_archetype_changed(self, _text: str) -> None:
        key = self._arch_combo.currentData()
        if key and key in self._suggestions:
            self._show_suggestion(key)
            self.archetype_changed.emit(key)

    def _show_suggestion(self, key: str) -> None:
        self._current_key = key
        sug = self._suggestions.get(key)
        if not sug:
            return

        # Hide hover preview.
        if self._preview:
            self._preview.hide()

        self._arch_name_label.setText(key)
        self._stats_label.setText(
            tr(
                "deck_stats",
                score=f"{sug.score:.1f}" if sug.score >= 0 else "N/A",
                creatures=sug.creature_count,
                spells=sug.spell_count,
                lands=sug.land_count,
                cmc=f"{sug.avg_cmc:.1f}",
            )
        )

        # --- Build categorised main deck ---
        self._clear_layout(self._main_layout)

        creatures: list[tuple[str, int, str, list[str], str]] = []
        spells: list[tuple[str, int, str, list[str], str]] = []

        name_counts = Counter(sug.main_deck)
        seen: set[str] = set()
        for name in sug.main_deck:
            if name in seen:
                continue
            seen.add(name)
            sc = self._scryfall.get(name)
            tl = sc.type_line if sc else ""
            mc = sc.mana_cost if sc else ""
            colors = list(sc.colors) if sc else []
            entry = (name, name_counts[name], mc, colors, tl)
            if sc and "creature" in sc.type_line.lower():
                creatures.append(entry)
            else:
                spells.append(entry)

        def _sort_key(e: tuple) -> float:
            sc = self._scryfall.get(e[0])
            return sc.cmc if sc else 0.0

        creatures.sort(key=_sort_key)
        spells.sort(key=_sort_key)

        # Creatures section.
        if creatures:
            sec = _CategorySection(tr("section_creatures", count=sum(e[1] for e in creatures)))
            for name, count, mc, colors, tl in creatures:
                row = self._make_row(name, count, mc, colors, tl)
                sec.add_card(row)
            self._main_layout.insertWidget(self._main_layout.count() - 1, sec)

        # Non-creature spells section.
        if spells:
            sec = _CategorySection(tr("section_spells", count=sum(e[1] for e in spells)))
            for name, count, mc, colors, tl in spells:
                row = self._make_row(name, count, mc, colors, tl)
                sec.add_card(row)
            self._main_layout.insertWidget(self._main_layout.count() - 1, sec)

        # Lands section: nonbasic drafted lands + basic lands.
        all_lands = list(sug.nonbasic_lands) + list(sug.lands)
        if all_lands:
            land_counts = Counter(all_lands)
            sec = _CategorySection(tr("section_lands", count=sum(land_counts.values())))
            for land_name in sorted(land_counts.keys()):
                sc = self._scryfall.get(land_name)
                mc = sc.mana_cost if sc else ""
                colors = list(sc.colors) if sc else self._basic_color(land_name)
                tl = sc.type_line if sc else tr("basic_land_type")
                row = self._make_row(land_name, land_counts[land_name], mc, colors, tl)
                sec.add_card(row)
            self._main_layout.insertWidget(self._main_layout.count() - 1, sec)

        # --- Sideboard (non-main-deck cards) ---
        self._clear_layout(self._sb_layout)
        main_set = Counter(sug.main_deck)
        nb_set = Counter(sug.nonbasic_lands)
        sb_counts: Counter[str] = Counter()
        for name in self._pool_names:
            in_main = main_set.get(name, 0)
            in_nb = nb_set.get(name, 0)
            if in_main > 0:
                main_set[name] -= 1
            elif in_nb > 0:
                nb_set[name] -= 1
            else:
                sb_counts[name] += 1

        for name in sorted(sb_counts.keys()):
            sc = self._scryfall.get(name)
            mc = sc.mana_cost if sc else ""
            colors = list(sc.colors) if sc else []
            tl = sc.type_line if sc else ""
            row = self._make_row(name, sb_counts[name], mc, colors, tl)
            self._sb_layout.insertWidget(self._sb_layout.count() - 1, row)



    # -- row factory + hover helpers -----------------------------------------

    def _make_row(
        self,
        name: str,
        count: int,
        mana_cost: str,
        colors: list[str],
        type_line: str,
        *,
        dimmed: bool = False,
    ) -> _DeckCardRow:
        row = _DeckCardRow(self._main_container)
        row.set_data(name, count, mana_cost, colors, type_line, dimmed=dimmed)
        row.set_art(self._art_paths.get(name))
        row.setMouseTracking(True)
        row.enterEvent = self._make_enter(name)
        row.leaveEvent = self._make_leave()
        return row

    @staticmethod
    def _basic_color(land_name: str) -> list[str]:
        c = _BASIC_COLOR.get(land_name)
        return [c] if c else []

    def _make_enter(self, card_name: str):
        def _enter(event):
            art = self._art_paths.get(card_name)
            if art:
                self._pending_preview_path = art
                self._pending_pos = QCursor.pos()
                self._hover_timer.start()
        return _enter

    def _make_leave(self):
        def _leave(event):
            self._hover_timer.stop()
            if self._preview:
                self._preview.hide()
        return _leave

    def _show_preview(self) -> None:
        if self._preview is None:
            self._preview = _CardPreview()
        self._preview.show_art(self._pending_preview_path, self._pending_pos)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_timer.stop()
        if self._preview:
            self._preview.hide()

    def _on_sideboard_toggle(self, checked: bool) -> None:
        self._sb_scroll.setVisible(checked)
        suffix = tr("sideboard_expand_hint_open") if checked else tr("sideboard_expand_hint_closed")
        self._sideboard_toggle.setText(tr("sideboard_header") + "  " + suffix)

    # -- helpers -------------------------------------------------------------

    def retranslate(self) -> None:
        """Refresh all static labels with the current language."""
        self._copy_btn.setText(tr("copy_deck_btn"))
        suffix = (
            tr("sideboard_expand_hint_open")
            if self._sideboard_toggle.isChecked()
            else tr("sideboard_expand_hint_closed")
        )
        self._sideboard_toggle.setText(tr("sideboard_header") + "  " + suffix)
        # re-render suggestion if one is loaded
        if self._current_key and self._current_key in self._suggestions:
            self._show_suggestion(self._current_key)

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        """Remove all widgets except the trailing stretch."""
        while layout.count() > 1:
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _copy_to_clipboard(self) -> None:
        from PySide6.QtWidgets import QApplication

        sug = self._suggestions.get(self._current_key)
        if not sug:
            return

        # MTG Arena format.
        lines: list[str] = ["Deck"]

        # Main deck spells.
        name_counts = Counter(sug.main_deck)
        for name in sorted(name_counts.keys()):
            lines.append(f"{name_counts[name]} {name}")

        # Lands (nonbasic + basic).
        land_counts = Counter(sug.nonbasic_lands + sug.lands)
        for name in sorted(land_counts.keys()):
            lines.append(f"{land_counts[name]} {name}")

        # Sideboard.
        main_set = Counter(sug.main_deck)
        nb_set = Counter(sug.nonbasic_lands)
        sb_counts: Counter[str] = Counter()
        for name in self._pool_names:
            in_main = main_set.get(name, 0)
            in_nb = nb_set.get(name, 0)
            if in_main > 0:
                main_set[name] -= 1
            elif in_nb > 0:
                nb_set[name] -= 1
            else:
                sb_counts[name] += 1

        if sb_counts:
            lines.append("")
            lines.append("Sideboard")
            for name in sorted(sb_counts.keys()):
                lines.append(f"{sb_counts[name]} {name}")

        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText("\n".join(lines))
        self._copy_btn.setText(tr("copied_confirmation"))
        QTimer.singleShot(1500, lambda: self._copy_btn.setText(tr("copy_deck_btn")))
