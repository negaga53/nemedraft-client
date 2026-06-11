"""Snapshot artifact generator — renders each surface to build/ui_snaps/.

These are reviewable artifacts, not pixel asserts: offscreen rendering
is deterministic per machine + Qt version, but font hinting differs
across platforms, so CI only checks that rendering succeeds.
"""

from __future__ import annotations

import os
from pathlib import Path

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

SNAP_DIR = Path(__file__).resolve().parent.parent / "build" / "ui_snaps"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    from client.overlay.ui.theme import fonts
    fonts.load_fonts()
    yield app


def _snap(widget, name: str) -> None:
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    widget.resize(widget.sizeHint().expandedTo(widget.size()))
    pixmap = widget.grab()
    assert not pixmap.isNull()
    assert pixmap.save(str(SNAP_DIR / f"{name}.png"))


class _Pick:
    def __init__(self, card, score, rank, colors, mana_cost="{1}{U}"):
        self.card = card
        self.score = score
        self.rank = rank
        self.gihwr = 0.55 + 0.02 * rank
        self.ata = 3.5
        self.iwd = 0.0
        self.mana_cost = mana_cost
        self.colors = colors
        self.type_line = "Creature — Bird"
        self.is_elite = rank == 1
        self.stats_loaded = True
        self.stats_format = ""


def _fake_picks() -> list[_Pick]:
    return [
        _Pick("Aether Skywatcher", 0.92, 1, ["U"]),
        _Pick("Bolt of Ruin", 0.71, 2, ["R"], "{1}{R}"),
        _Pick("Verdant Sproutling", 0.55, 3, ["G"], "{G}"),
        _Pick("Gilded Trinket", 0.31, 4, [], "{2}"),
    ]


def test_snapshot_overlay_window(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.window import OverlayWindow

    window = OverlayWindow(OverlayConfig())
    window.resize(620, 820)
    _snap(window, "window_boot")

    window.show_model_ready()
    _snap(window, "window_home")


def test_snapshot_pack_tab_with_results(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.window import OverlayWindow

    window = OverlayWindow(OverlayConfig())
    window.show_model_ready()
    window.show_draft_started()
    window._on_prediction(_fake_picks(), "TMT", 0, 2, 2, {})
    window.resize(620, 820)
    _snap(window, "window_pack")
    window.show_draft_ended()


def test_snapshot_compact_strip(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.view_mode import ViewMode
    from client.overlay.ui.window import OverlayWindow

    window = OverlayWindow(OverlayConfig())
    window.show_model_ready()
    window.show_draft_started()
    window._on_prediction(_fake_picks(), "TMT", 0, 2, 2, {})
    window._view_mode.set_mode(ViewMode.COMPACT, persist=False)
    _snap(window, "window_compact")
    window._view_mode.set_mode(ViewMode.FULL, persist=False)
    window.show_draft_ended()


def test_snapshot_settings_and_summary(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.settings_tab import SettingsTab
    from client.overlay.ui.summary_tab import SummaryTab
    from client.overlay.draft_summary import DraftSummary, PickRecapRow

    settings = SettingsTab(OverlayConfig())
    settings.resize(604, 700)
    _snap(settings, "settings_tab")

    summary = SummaryTab()
    summary.set_summary(DraftSummary(
        set_code="TMT", arena_format="PremierDraft",
        pool=["Aether Skywatcher", "Bolt of Ruin"],
        rows=[
            PickRecapRow(0, 0, ["Aether Skywatcher"], "Aether Skywatcher", True),
            PickRecapRow(0, 1, ["Bolt of Ruin"], "Other Card", False),
        ],
        picks_made=2, recommendations_followed=1,
    ))
    summary.resize(604, 700)
    _snap(summary, "summary_tab")


def test_font_loader_is_offscreen_safe(qapp):
    from client.overlay.ui.theme import fonts

    # Repeat calls are safe; result is either "Inter" (assets present)
    # or "" (clean fallback) — never an exception.
    family = fonts.load_fonts()
    assert family in ("Inter", "")
