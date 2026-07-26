"""Tests for live-applied settings (typed setting_changed signal + window hooks)."""

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

    # Toggling show_art must not also report user_group.
    tab._show_art_checkbox.setChecked(True)
    keys = [k for k, _ in changes]
    assert "data.user_group" not in keys


def test_always_on_top_toggle_emits_setting_changed(qapp):
    tab, config = _make_settings_tab(qapp)
    changes: list[tuple[str, object]] = []
    tab.setting_changed.connect(lambda k, v: changes.append((k, v)))

    assert tab._on_top_checkbox.isChecked() is config.overlay.always_on_top

    new_value = not config.overlay.always_on_top
    tab._on_top_checkbox.setChecked(new_value)
    assert ("overlay.always_on_top", new_value) in changes
    assert config.overlay.always_on_top is new_value


def test_always_on_top_persists_in_config_roundtrip(tmp_path):
    from client.overlay.config import OverlayConfig, load_config, save_config

    cfg = OverlayConfig()
    cfg.overlay.always_on_top = False
    path = tmp_path / "config.json"
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.overlay.always_on_top is False


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
