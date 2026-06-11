"""Pack display tab — card rankings with mana icons, type, hover art preview."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from client.overlay.api_client import Pick
from client.overlay.draft_state import PickHistoryEntry
from common.inference.deck_builder import DeckSuggestion
from common.inference.pool_analyzer import PoolAnalysis, ScryfallCard
from client.overlay.i18n import tr
from common.inference.signals import SignalResult
from client.overlay.ui.pack_widgets import (
    CardRow,
    _CardPreview,
    _ColumnHeader,
)
from client.overlay.ui.theme import set_prop


class PackTab(QWidget):
    """Main pack display tab combining card table, hover preview, and wheel tracker."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        show_art: bool = False,
        show_stats: bool = True,
    ) -> None:
        super().__init__(parent)
        self._show_art = show_art
        self._show_stats = show_stats
        self._art_paths: dict[str, Path | None] = {}
        self._art_cache: object | None = None  # CardArtCache, set via set_art_cache()
        # Top rows to flag as recommended in the live view (PickTwo bumps to 2).
        self._recommend_count = 1
        self.setMouseTracking(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Stacked widget: page 0 = home, page 1 = pack predictions.
        self._stack = QStackedWidget()
        outer.addWidget(self._stack, stretch=1)

        # --- Page 0: home / waiting content (delegated to HomeTab) ---
        from client.overlay.ui.home_tab import HomeTab

        self.home_widget = HomeTab()
        self._stack.addWidget(self.home_widget)  # index 0

        # --- Page 1: pack prediction content ---
        pack_page = QWidget()
        layout = QVBoxLayout(pack_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        # Context pill: "P2·P5 · SET · pool N" — populated on every update.
        self.context_pill = QLabel("")
        self.context_pill.setObjectName("contextPill")
        self.context_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pill_row = QHBoxLayout()
        pill_row.addStretch()
        pill_row.addWidget(self.context_pill)
        pill_row.addStretch()
        layout.addLayout(pill_row)

        # Indeterminate loading bar shown while a prediction is in flight.
        # Sits between the context pill and the card list so users get
        # immediate visual feedback when a new pack opens; the existing
        # ranking stays on screen until the new prediction lands, but the
        # bar makes clear that a fresher one is on the way.
        self.loading_bar = QProgressBar()
        self.loading_bar.setObjectName("predictionLoading")
        self.loading_bar.setRange(0, 0)  # indeterminate — Qt animates it
        self.loading_bar.setTextVisible(False)
        self.loading_bar.setFixedHeight(3)
        self.loading_bar.setVisible(False)
        layout.addWidget(self.loading_bar)

        # Column header + scrollable card list.
        self._column_header = _ColumnHeader(show_stats=self._show_stats)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._card_container = QWidget()
        self._card_container.setMouseTracking(True)
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(1)
        self._card_layout.addStretch()
        scroll.setWidget(self._card_container)

        # Pick history navigation bar — spans the full window width.
        nav_bar = QHBoxLayout()
        nav_bar.setContentsMargins(8, 4, 8, 4)
        nav_bar.setSpacing(8)

        self._nav_first = QPushButton("≪")
        self._nav_prev = QPushButton("◀")
        self._nav_label = QLabel("")
        self._nav_label.setObjectName("navLabel")
        self._nav_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._nav_next = QPushButton("▶")
        self._nav_last = QPushButton("≫")

        for btn in (self._nav_first, self._nav_prev, self._nav_next, self._nav_last):
            btn.setObjectName("navBtn")
            btn.setFixedSize(44, 32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)

        self._nav_first.setToolTip("First pick of pack")
        self._nav_prev.setToolTip("Previous pick")
        self._nav_next.setToolTip("Next pick")
        self._nav_last.setToolTip("Current pick")

        self._nav_first.clicked.connect(self._nav_go_first)
        self._nav_prev.clicked.connect(self._nav_go_prev)
        self._nav_next.clicked.connect(self._nav_go_next)
        self._nav_last.clicked.connect(self._nav_go_last)

        nav_bar.addWidget(self._nav_first)
        nav_bar.addWidget(self._nav_prev)
        nav_bar.addWidget(self._nav_label, stretch=1)
        nav_bar.addWidget(self._nav_next)
        nav_bar.addWidget(self._nav_last)
        # History navigation state.
        self._history: dict[tuple[int, int], PickHistoryEntry] = {}
        self._history_keys: list[tuple[int, int]] = []  # sorted
        self._nav_index: int = -1  # -1 = live/current
        self._live_pack_number: int = 0
        self._live_pick_number: int = 0

        # Content split: pack list (left) + deck rail (right).
        content_split = QHBoxLayout()
        content_split.setContentsMargins(0, 0, 0, 0)
        content_split.setSpacing(4)

        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(1)
        left_col.addWidget(self._column_header)
        left_col.addWidget(scroll, stretch=1)
        left_container = QWidget()
        left_container.setLayout(left_col)
        # Bias more space to the pack list — deck rail is a sidebar, not a peer.
        content_split.addWidget(left_container, stretch=5)

        from client.overlay.ui.pack_rail import DeckRail
        self.deck_rail = DeckRail()
        self.deck_rail.setFixedWidth(210)
        content_split.addWidget(self.deck_rail)

        # Fill the remaining vertical space so the card list + rail reach
        # the bottom of the tab instead of leaving a big gap below them.
        layout.addLayout(content_split, 1)

        # Pick-history nav: full-width strip at the very bottom of the tab.
        self._nav_container = QWidget()
        self._nav_container.setObjectName("navBar")
        self._nav_container.setLayout(nav_bar)
        layout.addWidget(self._nav_container)

        self._stack.addWidget(pack_page)  # index 1

        # Start on home page.
        self._stack.setCurrentIndex(0)

        # Floating card art preview (created lazily).
        self._preview: _CardPreview | None = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(110)  # ms delay before showing
        self._hover_timer.timeout.connect(self._show_preview)
        self._pending_preview_path: Path | None = None
        self._pending_pos = QPoint()

    # -- home / pack page switching ------------------------------------------

    def show_home(self) -> None:
        """Switch to the home / waiting page."""
        self._stack.setCurrentIndex(0)

    def show_pack_view(self) -> None:
        """Switch to the pack predictions page."""
        self._stack.setCurrentIndex(1)

    @property
    def is_showing_pack(self) -> bool:
        """True when the pack predictions page is visible."""
        return self._stack.currentIndex() == 1

    def show_loading(self) -> None:
        """Show the prediction-in-flight loading bar."""
        self.loading_bar.setVisible(True)

    def hide_loading(self) -> None:
        """Hide the prediction loading bar (called when results arrive)."""
        self.loading_bar.setVisible(False)

    def update_predictions(
        self,
        picks: list[Pick],
        art_paths: dict[str, Path | None] | None = None,
        *,
        taken_names: list[str] | None = None,
        pack_number: int = 0,
        pick_number: int = 0,
    ) -> None:
        self._art_paths.update(art_paths or {})

        # Track live position and cache picks for nav.
        self._live_picks = list(picks)
        self._live_pack_number = pack_number
        self._live_pick_number = pick_number
        self._live_taken_names = taken_names

        # When a live prediction arrives, snap back to live view.
        self._nav_index = -1

        # Rebuild navigable keys (the live key is now excluded from history).
        self._rebuild_nav_keys()

        self._render_picks(picks, live=True)

        # Taken cards — greyed-out rows for cards picked by other players.
        if taken_names and picks:
            max_score = picks[0].score if picks else 1.0
            sep = QLabel(tr("taken_separator", count=len(taken_names)))
            sep.setObjectName("takenSeparator")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._card_layout.insertWidget(self._card_layout.count() - 1, sep)

            scryfall = getattr(self, "_scryfall", {})
            for name in taken_names:
                row = CardRow(
                    self._card_container,
                    show_stats=self._show_stats,
                    show_art=self._show_art,
                )
                sc = scryfall.get(name)
                mana_cost = sc.mana_cost if sc else ""
                colors = list(sc.colors) if sc else []
                type_line = sc.type_line if sc else ""
                taken_pick = Pick(
                    card=name,
                    card_id=0,
                    rank=0,
                    score=0.0,
                    gihwr=0.0,
                    ata=0.0,
                    colors=colors,
                    mana_cost=mana_cost,
                    type_line=type_line,
                    is_elite=False,
                )
                art = self._art_paths.get(name)
                if art is None and self._art_cache is not None:
                    art = self._art_cache.get(name)
                    if art is not None:
                        self._art_paths[name] = art
                row.set_data(taken_pick, max_score, art_path=art, dimmed=True)
                row.setMouseTracking(True)
                row.enterEvent = self._make_enter(name)
                row.leaveEvent = self._make_leave()
                self._card_layout.insertWidget(self._card_layout.count() - 1, row)

    # -- pick history navigation ---------------------------------------------

    def set_pick_history(
        self,
        history: dict[tuple[int, int], PickHistoryEntry],
    ) -> None:
        """Replace the pick history data and refresh nav button state."""
        self._history = history
        self._rebuild_nav_keys()

    def _rebuild_nav_keys(self) -> None:
        """Rebuild the sorted list of navigable history keys.

        All history entries except the current live pack/pick are included.
        Entries without a ``picked_card`` are still navigable — they just
        won't show a highlight.
        """
        live_key = (self._live_pack_number, self._live_pick_number)
        self._history_keys = sorted(
            k for k in self._history if k != live_key
        )
        self._update_nav_buttons()

    def _update_nav_buttons(self) -> None:
        """Enable/disable nav buttons and update the position label."""
        has_past = bool(self._history_keys)
        is_live = self._nav_index == -1

        # ≪ and ◀ are enabled when there are past picks to go back to.
        self._nav_first.setEnabled(has_past and (is_live or self._nav_index > 0))
        self._nav_prev.setEnabled(has_past and (is_live or self._nav_index > 0))
        # ▶ is enabled when browsing history (live view is always "ahead").
        self._nav_next.setEnabled(not is_live)
        # ≫ is enabled when not on live view.
        self._nav_last.setEnabled(not is_live)

        if is_live:
            self._nav_label.setText(
                f"P{self._live_pack_number + 1}P{self._live_pick_number + 1}"
            )
        else:
            key = self._history_keys[self._nav_index]
            pos = self._nav_index + 1
            total = len(self._history_keys)
            self._nav_label.setText(
                f"P{key[0] + 1}P{key[1] + 1}  ({pos}/{total})"
            )

    def _nav_go_first(self) -> None:
        """Jump to first pick of the current pack."""
        if not self._history_keys:
            return
        if self._nav_index == -1:
            pn = self._live_pack_number
        else:
            pn = self._history_keys[self._nav_index][0]
        # Find first entry in this pack.
        for i, (p, _) in enumerate(self._history_keys):
            if p == pn:
                self._nav_index = i
                self._show_history_at_index()
                return

    def _nav_go_prev(self) -> None:
        """Go to the previous pick."""
        if not self._history_keys:
            return
        if self._nav_index == -1:
            # Currently live — go to last history entry.
            self._nav_index = len(self._history_keys) - 1
        elif self._nav_index > 0:
            self._nav_index -= 1
        self._show_history_at_index()

    def _nav_go_next(self) -> None:
        """Go to the next pick."""
        if self._nav_index == -1 or not self._history_keys:
            return
        if self._nav_index >= len(self._history_keys) - 1:
            # Back to live.
            self._nav_go_last()
        else:
            self._nav_index += 1
            self._show_history_at_index()

    def _nav_go_last(self) -> None:
        """Return to the current (live) pick."""
        self._nav_index = -1
        self._update_nav_buttons()
        # Re-render the live predictions if we have cached results.
        if hasattr(self, "_live_picks") and self._live_picks:
            self._render_picks(self._live_picks, live=True)

    def _show_history_at_index(self) -> None:
        """Render the history entry at the current nav index."""
        if self._nav_index < 0 or self._nav_index >= len(self._history_keys):
            return
        key = self._history_keys[self._nav_index]
        entry = self._history.get(key)
        if not entry:
            return

        # Reconstruct Pick objects from the saved dicts.
        picks: list[Pick] = []
        for d in entry.picks:
            picks.append(Pick(
                card=d["card"],
                card_id=d.get("card_id", 0),
                rank=d.get("rank", 0),
                score=d.get("score", 0.0),
                gihwr=d.get("gihwr", 0.0),
                ata=d.get("ata", 0.0),
                iwd=d.get("iwd", 0.0),
                mana_cost=d.get("mana_cost", ""),
                colors=d.get("colors", []),
                type_line=d.get("type_line", ""),
                is_elite=d.get("is_elite", False),
                stats_loaded=d.get("stats_loaded", True),
                stats_format=d.get("stats_format", ""),
            ))

        highlight = tuple(entry.picked_cards) or (
            (entry.picked_card,) if entry.picked_card else ()
        )
        self._render_picks(picks, highlight_cards=highlight)
        self._update_nav_buttons()

    def set_recommend_count(self, count: int) -> None:
        """Set how many top rows to flag as recommended in the live view.

        PickTwo drafts pick 2 cards per pass, so the top 2 get the
        recommended highlight; single-pick formats use 1 (the default,
        which leaves the live view visually unchanged).
        """
        self._recommend_count = max(1, count)

    def _recommended_row_count(self, live: bool) -> int:
        """Number of top rows to flag as recommended.

        Only in the live prediction view (``live=True``) and only when the
        format picks more than one card — otherwise 0. History navigation
        renders with ``live=False`` (even when the picked card is unknown
        and ``highlight_card == ""``), so it never shows the recommend tint.
        """
        count = getattr(self, "_recommend_count", 1)
        return count if (live and count >= 2) else 0

    def _render_picks(
        self,
        picks: list[Pick],
        highlight_card: str = "",
        *,
        highlight_cards: tuple[str, ...] = (),
        live: bool = False,
    ) -> None:
        """Render a list of picks into the card layout.

        Args:
            picks: Picks to display.
            highlight_card: Single card name to highlight (back-compat).
            highlight_cards: All cards the player took at this coordinate
                (PickTwo takes two). Supersedes ``highlight_card`` when set;
                every named card is moved to the top and tinted.
        """
        highlight_set = set(highlight_cards) or ({highlight_card} if highlight_card else set())
        self.show_pack_view()
        if self._preview:
            self._preview.hide()

        # Clear old rows (keep the stretch at the end).
        while self._card_layout.count() > 1:
            item = self._card_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not picks:
            return

        # Move the picked card(s) to the top of the display order.
        if highlight_set:
            picked = [p for p in picks if p.card in highlight_set]
            rest = [p for p in picks if p.card not in highlight_set]
            ordered = picked + rest
        else:
            ordered = picks

        max_score = picks[0].score if picks else 1.0
        rec_count = self._recommended_row_count(live)

        gihwr_ranks: dict[str, int] = {}
        valid_gihwr = [(p.card, p.gihwr) for p in picks if p.gihwr > 0]
        valid_gihwr.sort(key=lambda x: x[1], reverse=True)
        for rank_idx, (cname, _) in enumerate(valid_gihwr[:3], start=1):
            gihwr_ranks[cname] = rank_idx

        for idx, p in enumerate(ordered):
            row = CardRow(self._card_container, show_stats=self._show_stats, show_art=self._show_art)
            art = self._art_paths.get(p.card)
            row.set_data(p, max_score, art_path=art, gihwr_rank=gihwr_ranks.get(p.card, 0))

            # Highlight the player's actual pick(s) (history view) and the
            # live-view recommended rows (PickTwo flags 2) via properties —
            # the theme stylesheet resolves the accent stroke + wash.
            if highlight_set and p.card in highlight_set:
                set_prop(row, "picked", True)
            if idx < rec_count:
                set_prop(row, "recommended", True)

            row.setMouseTracking(True)
            row.enterEvent = self._make_enter(p.card)
            row.leaveEvent = self._make_leave()
            self._card_layout.insertWidget(self._card_layout.count() - 1, row)

    # -- hover preview -------------------------------------------------------

    def _make_enter(self, card_name: str):
        def _enter(event):
            art = self._art_paths.get(card_name)
            # Lazily fetch art from cache if not already known.
            if art is None and self._art_cache is not None:
                art = self._art_cache.get(card_name)
                if art is not None:
                    self._art_paths[card_name] = art
            if art and self._show_art:
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

    def set_compact(self, compact: bool) -> None:
        """Compact mode = hide deck rail + nav bar; keep pill + card list."""
        self.deck_rail.setVisible(not compact)
        self._nav_container.setVisible(not compact)

    def set_show_art(self, enabled: bool) -> None:
        """Live-apply the show-art toggle; rows pick it up on next render."""
        self._show_art = enabled

    def retranslate(self) -> None:
        """Refresh all static labels with the current language."""
        self.home_widget.retranslate()
        self._column_header.retranslate()
        self.deck_rail.retranslate()

    def update_signals(self, signal_result: SignalResult | None) -> None:
        """Derive open/closing lanes from SignalResult.scores (dict[color -> float])."""
        if signal_result is None or not getattr(signal_result, "scores", None):
            self.deck_rail.set_lanes([])
            return
        scores = signal_result.scores
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        lanes: list[tuple[str, str]] = []
        for color, score in ranked:
            if score > 0.5:
                lanes.append((color, "open"))
            elif score > 0:
                lanes.append((color, "closing"))
            else:
                lanes.append((color, "closed"))
        self.deck_rail.set_lanes(lanes)

    def set_context(self, set_code: str, pack: int, pick: int, pool_size: int) -> None:
        """Update the top pill to show current pack/pick/pool."""
        text = f"P{pack + 1}·P{pick + 1}  ·  {set_code}  ·  pool {pool_size}"
        self.context_pill.setText(text)

    def update_color_pips(self, pip_totals) -> None:
        """Forward pool pip counts to the archetype card's colour-commitment strip."""
        if not pip_totals:
            return
        # PoolAnalysis.pip_totals is a dict[str, int] keyed by WUBRG letters.
        self.deck_rail.set_pips(pip_totals)

    # -- deck rail data update methods ----------------------------------------

    def set_art_cache(self, art_cache: object) -> None:
        """Store a reference to the :class:`CardArtCache` for on-demand art."""
        self._art_cache = art_cache

    def update_card_art(self, card_name: str, path: Path | None) -> None:
        """Apply newly fetched art to any visible row for *card_name*.

        Called from the per-card prefetch path so thumbnails appear as
        images arrive without rebuilding the whole row layout. The
        ``_art_paths`` cache is updated unconditionally so subsequent
        ``_render_picks`` calls (pack navigation, fresh pack) pick up
        the path even when no row currently shows the card.
        """
        self._art_paths[card_name] = path
        if path is None:
            return
        for i in range(self._card_layout.count() - 1):
            item = self._card_layout.itemAt(i)
            if item is None:
                continue
            w = item.widget()
            if (
                isinstance(w, CardRow)
                and getattr(w, "_card_name", None) == card_name
            ):
                w.set_art(path)

    def set_scryfall(self, scryfall_cards: dict[str, ScryfallCard]) -> None:
        """Store Scryfall lookup for deck stat computation."""
        self._scryfall: dict[str, ScryfallCard] = scryfall_cards

    def update_pool(self, pool_analysis: PoolAnalysis) -> None:
        """Update the embedded curve from pool analysis."""
        self.deck_rail.set_curve(pool_analysis)

    def update_deck_stats(self, suggestion: DeckSuggestion) -> None:
        """Drive the archetype card from the top DeckSuggestion.

        DeckSuggestion fields (see common/inference/deck_builder.py):
            archetype: str (e.g. "UR")
            score: float
            creature_count / spell_count / land_count: int
            avg_cmc: float
        """
        name = getattr(suggestion, "archetype", "—")
        score = float(getattr(suggestion, "score", -1.0))
        colors = [c for c in name if c in "WUBRG"]
        count = (
            int(getattr(suggestion, "creature_count", 0))
            + int(getattr(suggestion, "spell_count", 0))
            + int(getattr(suggestion, "land_count", 0))
        )
        self.deck_rail.set_archetype(name, score, colors, count)
