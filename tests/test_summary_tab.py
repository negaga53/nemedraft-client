"""Headless smoke tests for the draft-end SummaryTab."""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _summary():
    from client.overlay.draft_summary import DraftSummary, PickRecapRow

    return DraftSummary(
        set_code="TMT",
        arena_format="PremierDraft",
        pool=["Aether Bolt", "Skywatcher"],
        rows=[
            PickRecapRow(0, 0, ["Aether Bolt"], "Aether Bolt", True),
            PickRecapRow(0, 1, ["Skywatcher"], "Other Card", False),
            PickRecapRow(0, 2, ["Replay Pick"], "", None),
        ],
        picks_made=3,
        recommendations_followed=1,
    )


def test_set_summary_renders_header_and_rows(qapp):
    from client.overlay.ui.summary_tab import SummaryTab

    tab = SummaryTab()
    tab.set_summary(_summary())
    assert "TMT" in tab._header.text()
    # 3 recap rows + trailing stretch.
    assert tab._recap_layout.count() == 4
    assert tab._copy_deck_btn.isEnabled() is False  # no suggestion yet


def test_best_suggestion_enables_deck_copy(qapp):
    from client.overlay.ui.summary_tab import SummaryTab
    from common.inference.deck_builder import DeckSuggestion

    tab = SummaryTab()
    tab.set_summary(_summary())
    sug = DeckSuggestion(
        archetype="WU Fliers", main_deck=["Skywatcher"], lands=["Plains"],
    )
    tab.set_best_suggestion(sug, ["Skywatcher", "Aether Bolt"])
    assert tab._copy_deck_btn.isEnabled() is True
    assert "WU Fliers" in tab._build_line.text()


def test_copy_pool_puts_pool_on_clipboard(qapp):
    from client.overlay.ui.summary_tab import SummaryTab

    tab = SummaryTab()
    tab.set_summary(_summary())
    tab._on_copy_pool()
    assert qapp.clipboard().text() == "1 Aether Bolt\n1 Skywatcher"


def test_clear_resets_everything(qapp):
    from client.overlay.ui.summary_tab import SummaryTab

    tab = SummaryTab()
    tab.set_summary(_summary())
    tab.clear()
    assert tab._header.text() == ""
    assert tab._recap_layout.count() == 1  # just the stretch
    assert tab._copy_deck_btn.isEnabled() is False


def test_window_reveals_and_hides_summary_tab(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.window import OverlayWindow

    window = OverlayWindow(OverlayConfig())
    assert window.tabs.isTabVisible(window._tab_summary_idx) is False

    window._on_draft_summary(_summary())
    assert window.tabs.isTabVisible(window._tab_summary_idx) is True
    assert window.tabs.currentIndex() == window._tab_summary_idx

    window.hide_draft_summary()
    assert window.tabs.isTabVisible(window._tab_summary_idx) is False
