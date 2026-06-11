"""Tests for live-applied settings (typed setting_changed signal + window hooks)."""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_settings_tab(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.settings_tab import SettingsTab

    config = OverlayConfig()
    return SettingsTab(config), config


def test_setting_changed_emits_per_changed_key(qapp):
    tab, config = _make_settings_tab(qapp)
    changes: list[tuple[str, object]] = []
    tab.setting_changed.connect(lambda k, v: changes.append((k, v)))

    new_value = not config.overlay.show_art
    tab._show_art_checkbox.setChecked(new_value)
    assert ("overlay.show_art", new_value) in changes


def test_setting_changed_not_emitted_for_unchanged_keys(qapp):
    tab, config = _make_settings_tab(qapp)
    changes: list[tuple[str, object]] = []
    tab.setting_changed.connect(lambda k, v: changes.append((k, v)))

    # Toggling show_art must not also report transparent/user_group.
    tab._show_art_checkbox.setChecked(True)
    keys = [k for k, _ in changes]
    assert "overlay.transparent" not in keys
    assert "data.user_group" not in keys


def test_transparent_toggle_emits_key(qapp):
    tab, config = _make_settings_tab(qapp)
    changes: list[tuple[str, object]] = []
    tab.setting_changed.connect(lambda k, v: changes.append((k, v)))

    tab._transparent_checkbox.setChecked(not config.overlay.transparent)
    assert ("overlay.transparent", True) in changes


def test_window_set_show_art_rerenders_cached_results(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.window import OverlayWindow

    window = OverlayWindow(OverlayConfig(), show_art=False)
    assert window._show_art is False

    window.set_show_art(True)
    assert window._show_art is True
    assert window.pack_tab._show_art is True

    window.set_show_art(False)
    assert window.pack_tab._show_art is False


def test_window_set_transparent_preserves_geometry(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.window import OverlayWindow

    window = OverlayWindow(OverlayConfig(), transparent=False)
    window.resize(620, 700)
    window.move(123, 77)
    geo_before = window.geometry()

    window.set_transparent(True)
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is True
    assert window.geometry() == geo_before

    window.set_transparent(False)
    assert window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is False
    assert window.geometry() == geo_before
