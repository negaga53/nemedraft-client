"""Tests for the view-mode controller and PickTwo-aware compact view."""

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


def _controller():
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.view_mode import ViewModeController

    config = OverlayConfig()
    return ViewModeController(config), config


def test_starts_full_regardless_of_persisted_mode(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.view_mode import ViewMode, ViewModeController

    config = OverlayConfig()
    config.overlay.view_mode = "compact"
    controller = ViewModeController(config)
    assert controller.mode is ViewMode.FULL
    assert controller.persisted_mode() is ViewMode.COMPACT


def test_toggle_emits_old_and_new_and_persists(qapp):
    from client.overlay.ui.view_mode import ViewMode

    controller, config = _controller()
    transitions: list[tuple] = []
    controller.mode_changed.connect(lambda o, n: transitions.append((o, n)))

    controller.toggle()
    assert controller.mode is ViewMode.COMPACT
    assert config.overlay.view_mode == "compact"
    controller.toggle()
    assert controller.mode is ViewMode.FULL
    assert config.overlay.view_mode == "full"
    assert transitions == [
        (ViewMode.FULL, ViewMode.COMPACT),
        (ViewMode.COMPACT, ViewMode.FULL),
    ]


def test_set_same_mode_is_noop(qapp):
    from client.overlay.ui.view_mode import ViewMode

    controller, _ = _controller()
    fired: list[tuple] = []
    controller.mode_changed.connect(lambda o, n: fired.append((o, n)))
    assert controller.set_mode(ViewMode.FULL) is False
    assert fired == []


def test_per_mode_geometry_slots(qapp):
    from client.overlay.ui.view_mode import ViewMode

    controller, config = _controller()
    controller.save_geometry(ViewMode.FULL, "RlVMTA==")
    controller.save_geometry(ViewMode.COMPACT, "Q09NUEFDVA==")
    assert controller.geometry_for(ViewMode.FULL) == "RlVMTA=="
    assert controller.geometry_for(ViewMode.COMPACT) == "Q09NUEFDVA=="
    assert config.overlay.geometry_full == "RlVMTA=="
    assert config.overlay.geometry_compact == "Q09NUEFDVA=="


def test_legacy_geometry_seeds_full_slot(qapp):
    from client.overlay.ui.view_mode import ViewMode

    controller, config = _controller()
    config.overlay.geometry = "TEVHQUNZ"
    assert controller.geometry_for(ViewMode.FULL) == "TEVHQUNZ"
    config.overlay.geometry_full = "TkVX"
    assert controller.geometry_for(ViewMode.FULL) == "TkVX"


def test_unknown_persisted_mode_falls_back_to_full(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.view_mode import ViewMode, ViewModeController

    config = OverlayConfig()
    config.overlay.view_mode = "bogus"
    assert ViewModeController(config).persisted_mode() is ViewMode.FULL


def test_mini_view_row_count_respects_recommend_count(qapp):
    """PickTwo recommends 2 cards — compact view must show >= that many rows."""
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.window import OverlayWindow

    class _Pick:
        def __init__(self, card, score, rank):
            self.card = card
            self.score = score
            self.rank = rank
            self.gihwr = 0.6
            self.ata = 5.0
            self.iwd = 0.0
            self.mana_cost = "{1}{U}"
            self.colors = ["U"]
            self.type_line = "Creature"
            self.is_elite = False
            self.stats_loaded = True
            self.stats_format = ""

    window = OverlayWindow(OverlayConfig())
    window._last_results = [
        _Pick(f"Card {i}", 0.9 - i * 0.1, i + 1) for i in range(6)
    ]
    window.set_recommend_count(2)
    window._refresh_mini()
    assert window._mini_layout.count() == 3  # max(3, 2)

    window.set_recommend_count(2)
    window._refresh_mini()
    # First recommend_count rows carry the "recommended" property for the
    # theme; the rest do not.
    flagged = [
        bool(window._mini_layout.itemAt(i).widget().property("recommended"))
        for i in range(window._mini_layout.count())
    ]
    assert flagged == [True, True, False]
