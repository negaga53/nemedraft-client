"""Draft-end summary tab — pick recap, follow rate, and deck export."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from client.overlay.deck_export import build_arena_deck_string, build_pool_string
from client.overlay.draft_summary import DraftSummary
from client.overlay.i18n import card_name, tr


class SummaryTab(QWidget):
    """Shown when a draft completes; hidden again when the next one starts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._summary: DraftSummary | None = None
        self._suggestion: object | None = None
        self._suggestion_pool: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self._header = QLabel("")
        self._header.setObjectName("summaryHeader")
        self._header.setWordWrap(True)
        layout.addWidget(self._header)

        self._stats = QLabel("")
        self._stats.setObjectName("summaryStats")
        self._stats.setWordWrap(True)
        layout.addWidget(self._stats)

        self._build_line = QLabel("")
        self._build_line.setObjectName("summaryBuild")
        self._build_line.setWordWrap(True)
        layout.addWidget(self._build_line)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self._copy_deck_btn = QPushButton(tr("summary_copy_deck_btn"))
        self._copy_deck_btn.setObjectName("summaryCopyDeck")
        self._copy_deck_btn.setEnabled(False)
        self._copy_deck_btn.clicked.connect(self._on_copy_deck)
        buttons.addWidget(self._copy_deck_btn)
        self._copy_pool_btn = QPushButton(tr("summary_copy_pool_btn"))
        self._copy_pool_btn.setObjectName("summaryCopyPool")
        self._copy_pool_btn.clicked.connect(self._on_copy_pool)
        buttons.addWidget(self._copy_pool_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        self._recap_title = QLabel(tr("summary_recap_title"))
        self._recap_title.setObjectName("sectionTitle")
        layout.addWidget(self._recap_title)

        self._recap_scroll = QScrollArea()
        self._recap_scroll.setWidgetResizable(True)
        self._recap_scroll.setFrameShape(self._recap_scroll.Shape.NoFrame)
        self._recap_container = QWidget()
        self._recap_layout = QVBoxLayout(self._recap_container)
        self._recap_layout.setContentsMargins(0, 0, 0, 0)
        self._recap_layout.setSpacing(2)
        self._recap_layout.addStretch()
        self._recap_scroll.setWidget(self._recap_container)
        layout.addWidget(self._recap_scroll, stretch=1)

    # -- population ----------------------------------------------------------

    def set_summary(self, summary: DraftSummary) -> None:
        self._summary = summary
        self._header.setText(tr(
            "summary_header",
            set_code=summary.set_code or "?",
            arena_format=summary.arena_format or "",
        ).strip())

        rated = sum(
            1 for r in summary.rows if r.followed_recommendation is not None
        )
        stats_parts = [tr("summary_picks_made", count=summary.picks_made)]
        if rated:
            stats_parts.append(tr(
                "summary_follow_rate",
                followed=summary.recommendations_followed,
                rated=rated,
            ))
        self._stats.setText("  ·  ".join(stats_parts))

        self._render_recap(summary)
        self._render_build_line()

    def set_best_suggestion(self, suggestion: object, pool_names: list[str]) -> None:
        """Deck suggestions arrive async after completion — fill in late."""
        self._suggestion = suggestion
        self._suggestion_pool = list(pool_names)
        self._copy_deck_btn.setEnabled(suggestion is not None)
        self._render_build_line()

    def clear(self) -> None:
        self._summary = None
        self._suggestion = None
        self._suggestion_pool = []
        self._copy_deck_btn.setEnabled(False)
        self._header.setText("")
        self._stats.setText("")
        self._build_line.setText("")
        self._clear_recap()

    # -- internals -------------------------------------------------------------

    def _render_build_line(self) -> None:
        sug = self._suggestion
        if sug is None:
            self._build_line.setText(
                tr("summary_no_suggestion") if self._summary else "",
            )
            return
        spells = len(getattr(sug, "main_deck", []))
        lands = len(getattr(sug, "lands", [])) + len(
            getattr(sug, "nonbasic_lands", []),
        )
        self._build_line.setText(tr(
            "summary_suggested_build",
            archetype=getattr(sug, "archetype", "?"),
            spells=spells,
            lands=lands,
        ))

    def _clear_recap(self) -> None:
        while self._recap_layout.count() > 1:
            item = self._recap_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _render_recap(self, summary: DraftSummary) -> None:
        self._clear_recap()
        for row in summary.rows:
            label = QLabel(self._format_row(row))
            label.setObjectName("summaryRecapRow")
            label.setTextFormat(Qt.TextFormat.RichText)
            self._recap_layout.insertWidget(self._recap_layout.count() - 1, label)

    @staticmethod
    def _format_row(row) -> str:
        coord = f"P{row.pack_number + 1}P{row.pick_number + 1}"
        picked = ", ".join(card_name(c) for c in row.picked_cards) or "—"
        if row.followed_recommendation is True:
            mark, color = "✓", "#4caf50"
        elif row.followed_recommendation is False:
            mark, color = "✗", "#f44336"
        else:
            mark, color = "·", "#888888"
        text = (
            f'<span style="color:#888888">{coord}</span> '
            f'<span style="color:{color}">{mark}</span> {picked}'
        )
        if row.followed_recommendation is False and row.top_recommendation:
            ai = tr("summary_ai_pick", card=card_name(row.top_recommendation))
            text += f' <span style="color:#888888">({ai})</span>'
        return text

    # -- actions ---------------------------------------------------------------

    def _on_copy_deck(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._suggestion is None:
            return
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(build_arena_deck_string(
                self._suggestion, self._suggestion_pool,
            ))
        self._flash(self._copy_deck_btn, tr("summary_copy_deck_btn"))

    def _on_copy_pool(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._summary is None or not self._summary.pool:
            return
        clipboard = QApplication.clipboard()
        if clipboard:
            clipboard.setText(build_pool_string(self._summary.pool))
        self._flash(self._copy_pool_btn, tr("summary_copy_pool_btn"))

    @staticmethod
    def _flash(button: QPushButton, original: str) -> None:
        button.setText(tr("copied_confirmation"))
        QTimer.singleShot(1500, lambda: button.setText(original))

    def retranslate(self) -> None:
        self._copy_deck_btn.setText(tr("summary_copy_deck_btn"))
        self._copy_pool_btn.setText(tr("summary_copy_pool_btn"))
        self._recap_title.setText(tr("summary_recap_title"))
        if self._summary is not None:
            self.set_summary(self._summary)
