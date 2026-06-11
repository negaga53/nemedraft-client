"""Tests for the overlay's window-scoped keyboard shortcuts."""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _window(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.window import OverlayWindow

    return OverlayWindow(OverlayConfig())


def _shortcut(window, sequence: str):
    for sc in window._shortcuts:
        if sc.key() == QKeySequence(sequence):
            return sc
    raise AssertionError(f"shortcut {sequence} not registered")


def test_expected_shortcuts_registered(qapp):
    window = _window(qapp)
    for seq in ("Ctrl+M", "Ctrl+H", "Ctrl+1", "Ctrl+2", "Ctrl+3", "Ctrl+4", "Esc"):
        assert _shortcut(window, seq) is not None


def test_tab_shortcuts_switch_visible_tabs(qapp):
    window = _window(qapp)
    window.show_model_ready()  # reveals deck + settings tabs

    _shortcut(window, "Ctrl+2").activated.emit()
    assert window.tabs.currentIndex() == window._tab_deck_idx
    _shortcut(window, "Ctrl+1").activated.emit()
    assert window.tabs.currentIndex() == window._tab_pack_idx


def test_tab_shortcut_skips_hidden_summary(qapp):
    window = _window(qapp)
    window.show_model_ready()
    window.tabs.setCurrentIndex(window._tab_pack_idx)

    # Summary tab is hidden until a draft completes — Ctrl+3 must no-op.
    assert window.tabs.isTabVisible(window._tab_summary_idx) is False
    _shortcut(window, "Ctrl+3").activated.emit()
    assert window.tabs.currentIndex() == window._tab_pack_idx


def test_compact_shortcut_noops_without_active_draft(qapp):
    window = _window(qapp)
    window.show_model_ready()
    assert window._view_mode.is_compact is False
    _shortcut(window, "Ctrl+M").activated.emit()
    # Toggle button is disabled outside a draft — mode must not change.
    assert window._view_mode.is_compact is False


def test_compact_shortcut_toggles_during_draft(qapp):
    window = _window(qapp)
    window.show_model_ready()
    window.show_draft_started()

    _shortcut(window, "Ctrl+M").activated.emit()
    assert window._view_mode.is_compact is True
    _shortcut(window, "Esc").activated.emit()
    assert window._view_mode.is_compact is False
    window.show_draft_ended()


def test_esc_noops_when_not_compact(qapp):
    window = _window(qapp)
    window.show_model_ready()
    _shortcut(window, "Esc").activated.emit()
    assert window._view_mode.is_compact is False
