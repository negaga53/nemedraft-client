"""Headless smoke tests for overlay UI widgets.

Run with: QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe -m pytest tests/test_overlay_ui.py -v
"""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    """Shared QApplication instance for all widget tests."""
    app = QApplication.instance() or QApplication([])
    yield app


def test_medal_color_gold_silver_bronze():
    from client.overlay.ui.styles import medal_color
    assert medal_color(1) == "#cfb53b"
    assert medal_color(2) == "#f5f5f5"
    assert medal_color(3) == "#cd7f32"
    assert medal_color(0) is None
    assert medal_color(4) is None


def test_score_fill_gradient_thresholds():
    from client.overlay.ui.styles import score_fill_gradient
    assert "4caf50" in score_fill_gradient(0.80)
    assert "ffc107" in score_fill_gradient(0.50)
    assert "f44336" in score_fill_gradient(0.20)


def test_palette_constants_exist():
    from client.overlay.ui import styles
    assert styles.BG_PRIMARY == "#0f0f1c"
    assert styles.BG_ELEVATED == "rgba(20,20,36,.6)"
    assert styles.BORDER_SUBTLE == "#1f1f30"
    assert styles.ACCENT_GOLD == "#cfb53b"


def test_score_bar_renders_with_score(qapp):
    from client.overlay.ui.widgets.score_bar import ScoreBar
    bar = ScoreBar()
    bar.set_score(0.95)
    assert bar.fraction == 0.95
    assert bar.label_text == "95"
    # Size is fixed per design.
    assert bar.sizeHint().width() == 78
    assert bar.sizeHint().height() == 16


def test_score_bar_clamps_and_rounds(qapp):
    from client.overlay.ui.widgets.score_bar import ScoreBar
    bar = ScoreBar()
    bar.set_score(1.5)
    assert bar.fraction == 1.0
    assert bar.label_text == "100"
    bar.set_score(-0.1)
    assert bar.fraction == 0.0
    assert bar.label_text == "0"
    bar.set_score(0.555)
    assert bar.label_text == "56"  # rounded to nearest %


def test_card_row_has_art_and_scorebar(qapp):
    from client.overlay.ui.pack_widgets import CardRow
    from client.overlay.ui.widgets.score_bar import ScoreBar
    from client.overlay.api_client import Pick

    pick = Pick(
        card="Counterspell", score=0.8, rank=1, is_elite=False,
        colors=["U"], mana_cost="{U}{U}", type_line="Instant",
        gihwr=0.58, ata=3.4, iwd=0.0, stats_loaded=True,
    )
    row = CardRow(show_stats=True)
    row.set_data(pick, max_score=1.0)
    assert isinstance(row.score_bar, ScoreBar)
    assert row.score_bar.label_text == "80"
    # art label exists (may be empty when show_art=False or no path)
    assert hasattr(row, "art_label")
    # row height per spec
    assert row.height() == 30


def test_card_row_medal_color_for_top_gihwr(qapp):
    from client.overlay.ui.pack_widgets import CardRow
    from client.overlay.api_client import Pick

    pick = Pick(
        card="Scourge", score=0.95, rank=1, is_elite=False,
        colors=["R"], mana_cost="{R}{R}", type_line="Creature",
        gihwr=0.62, ata=2.1, iwd=0.0, stats_loaded=True,
    )
    row = CardRow(show_stats=True)
    row.set_data(pick, max_score=1.0, gihwr_rank=1)
    assert "cfb53b" in row.gihwr_label.styleSheet()  # gold medal


def test_pack_tab_has_pill_and_no_column_header(qapp):
    from client.overlay.ui.pack_tab import PackTab
    tab = PackTab(show_art=False)
    # pill holds the "P2·P5 · TMT · pool N" context label
    assert hasattr(tab, "context_pill")
    # _col_header removed — attribute should not exist
    assert not hasattr(tab, "_col_header")


def test_overlay_window_has_no_bottom_pool_label(qapp):
    from client.overlay.ui.window import OverlayWindow
    from client.overlay.config import OverlayConfig
    w = OverlayWindow(OverlayConfig(), transparent=False, show_art=False)
    assert not hasattr(w, "pool_label")


def test_deck_rail_constructs(qapp):
    from client.overlay.ui.pack_rail import DeckRail
    rail = DeckRail()
    assert hasattr(rail, "archetype_card")
    assert hasattr(rail, "curve_card")
    assert hasattr(rail, "lanes_card")


def test_deck_rail_archetype_text(qapp):
    from client.overlay.ui.pack_rail import DeckRail
    rail = DeckRail()
    rail.set_archetype("UR Tempo", score=47.3, colors=["U", "U", "R", "R", "R"], count=14)
    assert "UR Tempo" in rail.archetype_card.name_label.text()
    assert "14/40" in rail.archetype_card.count_label.text()


def test_window_has_two_row_header(qapp):
    from client.overlay.ui.window import OverlayWindow
    from client.overlay.config import OverlayConfig
    w = OverlayWindow(OverlayConfig(), transparent=False, show_art=False)
    assert hasattr(w, "_drag_row_widget")
    assert hasattr(w, "_brand_label")
    # brand shows app name, not pack/pick
    assert "NEMEDRAFT" in w._brand_label.text().upper()


def test_compact_height_matches_spec(qapp):
    from client.overlay.ui.window import OverlayWindow
    from client.overlay.config import OverlayConfig
    w = OverlayWindow(OverlayConfig(), transparent=False, show_art=False)
    # Per spec: drag row + pill + 3 rows + padding ≈ 110-160
    h = w._compact_height()
    assert 110 <= h <= 160


def test_home_tab_has_brand_and_status_card(qapp):
    from client.overlay.ui.home_tab import HomeTab
    t = HomeTab()
    assert hasattr(t, "_brand_label")
    # Brand uses serif styling per spec
    assert "georgia" in t._brand_label.styleSheet().lower()
    # Status card groups the 4 rows
    assert hasattr(t, "_status_card")


def test_deck_tab_has_archetype_strip(qapp):
    from client.overlay.ui.deck_tab import DeckTab
    t = DeckTab()
    assert hasattr(t, "_archetype_strip")
    assert hasattr(t, "_sideboard_toggle")


def test_sideboard_starts_collapsed(qapp):
    from client.overlay.ui.deck_tab import DeckTab
    t = DeckTab()
    # The scroll widget that contains sideboard rows is hidden by default
    assert not t._sb_scroll.isVisible()


def test_settings_has_show_art_and_transparent_toggles(qapp):
    from client.overlay.ui.settings_tab import SettingsTab
    from client.overlay.config import OverlayConfig
    t = SettingsTab(OverlayConfig())
    assert hasattr(t, "_show_art_checkbox")
    assert hasattr(t, "_transparent_checkbox")


def test_settings_toggle_syncs_config(qapp):
    from client.overlay.ui.settings_tab import SettingsTab
    from client.overlay.config import OverlayConfig
    cfg = OverlayConfig()
    cfg.overlay.show_art = True
    cfg.overlay.transparent = False
    t = SettingsTab(cfg)
    t._show_art_checkbox.setChecked(False)
    assert cfg.overlay.show_art is False
    t._transparent_checkbox.setChecked(True)
    assert cfg.overlay.transparent is True


def test_overlay_stylesheet_uses_new_bg(qapp):
    from client.overlay.ui.styles import OVERLAY_STYLESHEET, TRANSPARENT_STYLESHEET
    assert "#0f0f1c" in OVERLAY_STYLESHEET
    # Transparent stylesheet allows flexible spacing in the rgba string.
    assert (
        "rgba(15, 15, 28" in TRANSPARENT_STYLESHEET
        or "rgba(15,15,28" in TRANSPARENT_STYLESHEET
    )
    # Old selectors we removed shouldn't be present
    assert "QLabel#poolLabel" not in OVERLAY_STYLESHEET
    assert "QLabel#cardName" not in OVERLAY_STYLESHEET
    assert "QFrame#cardRow" not in OVERLAY_STYLESHEET


def test_compact_mode_shows_context_pill(qapp):
    from client.overlay.ui.window import OverlayWindow
    from client.overlay.config import OverlayConfig
    w = OverlayWindow(OverlayConfig(), transparent=False, show_art=False)
    assert hasattr(w, "_mini_pill")


def test_show_art_false_hides_art_label(qapp):
    from client.overlay.ui.pack_widgets import CardRow
    from client.overlay.api_client import Pick
    pick = Pick(
        card="Shock", score=0.5, rank=1, is_elite=False,
        colors=["R"], mana_cost="{R}", type_line="Instant",
        gihwr=0.5, ata=5.0, iwd=0.0, stats_loaded=True,
    )
    row = CardRow(show_stats=True, show_art=False)
    row.set_data(pick, max_score=1.0)
    assert row.art_label.isHidden()


def test_overlay_window_has_minimize_button(qapp):
    from PySide6.QtCore import Qt
    from client.overlay.ui.window import OverlayWindow
    from client.overlay.config import OverlayConfig

    w = OverlayWindow(OverlayConfig(), transparent=False, show_art=False)
    assert hasattr(w, "_min_btn")
    # Tooltip mentions the hide-to-tray behaviour added so Windows
    # users (where Qt.Tool windows have no taskbar entry) can find
    # the overlay again after clicking minimise.
    assert "tray" in w._min_btn.toolTip().lower()
    # Click hides the window. On a headless test runner there's no
    # system tray, so _minimize_to_tray falls back to showMinimized —
    # either way the window leaves the normal visible state.
    w.show()
    w._min_btn.click()
    qapp.processEvents()
    assert (
        not w.isVisible()
        or bool(w.windowState() & Qt.WindowState.WindowMinimized)
    )


def test_elevate_to_floating_is_noop_off_darwin():
    """On non-Mac platforms (including the WSL test machine) the helper must not raise."""
    from client.overlay.ui._macos import elevate_to_floating

    # Pass a dummy object; the function should bail out before touching it.
    class _Dummy:
        def winId(self) -> int:
            raise AssertionError("should not be called off-darwin")

    elevate_to_floating(_Dummy())
