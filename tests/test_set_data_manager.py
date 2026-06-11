"""Tests for client.overlay.managers.set_data.SetDataManager."""

from __future__ import annotations

import os
import time

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeMapper:
    def __init__(self, *, delay: float = 0.0, error: Exception | None = None):
        self.delay = delay
        self.error = error
        self.load_calls: list[str] = []

    def load_set(self, set_code: str) -> int:
        self.load_calls.append(set_code)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return 42

    def ensure_mtga_fallback(self) -> None:
        pass


@pytest.fixture()
def fake_scryfall(monkeypatch):
    import common.inference.pool_analyzer as pa

    monkeypatch.setattr(
        pa, "load_scryfall_cards_for_set", lambda d, sc: {"Some Card": object()},
    )


def _wait_until(predicate, qapp, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        qapp.processEvents()
        time.sleep(0.01)
    return predicate()


def _make_manager(mapper, tmp_path, **kwargs):
    from client.overlay.managers.set_data import SetDataManager

    return SetDataManager(mapper, tmp_path, **kwargs)


def test_ensure_loads_and_emits_ready(qapp, tmp_path, fake_scryfall):
    mapper = _FakeMapper()
    manager = _make_manager(mapper, tmp_path)
    ready: list[object] = []
    manager.ready.connect(ready.append)

    assert manager.is_ready is False
    started = manager.ensure("TMT")
    assert started is True
    assert manager.loaded_set == "TMT"

    assert _wait_until(lambda: ready, qapp)
    result = ready[0]
    assert result.set_code == "TMT"
    assert result.mappings_added == 42
    assert manager.is_ready is True


def test_ensure_is_idempotent_while_loading_and_after(qapp, tmp_path, fake_scryfall):
    mapper = _FakeMapper(delay=0.2)
    manager = _make_manager(mapper, tmp_path)
    ready: list[object] = []
    manager.ready.connect(ready.append)

    assert manager.ensure("TMT") is True
    assert manager.ensure("TMT") is False  # already loading
    assert _wait_until(lambda: ready, qapp)
    assert manager.ensure("TMT") is False  # already loaded
    assert mapper.load_calls == ["TMT"]


def test_ensure_switches_sets(qapp, tmp_path, fake_scryfall):
    mapper = _FakeMapper()
    manager = _make_manager(mapper, tmp_path)
    ready: list[object] = []
    manager.ready.connect(ready.append)

    manager.ensure("TMT")
    assert _wait_until(lambda: len(ready) == 1, qapp)
    assert manager.ensure("SOS") is True
    assert manager.loaded_set == "SOS"
    assert manager.is_ready is False
    assert _wait_until(lambda: len(ready) == 2, qapp)
    assert ready[1].set_code == "SOS"


def test_load_error_emits_failed_and_degrades_to_ready(qapp, tmp_path, fake_scryfall):
    mapper = _FakeMapper(error=RuntimeError("disk on fire"))
    manager = _make_manager(mapper, tmp_path)
    failures: list[tuple[str, str]] = []
    manager.failed.connect(lambda sc, msg: failures.append((sc, msg)))

    manager.ensure("TMT")
    assert _wait_until(lambda: failures, qapp)
    set_code, message = failures[0]
    assert set_code == "TMT"
    assert "disk on fire" in message
    # Degraded-continue: the draft proceeds; predictions may partially work.
    assert manager.is_ready is True


def test_watchdog_timeout_emits_failed_and_degrades(qapp, tmp_path, fake_scryfall):
    mapper = _FakeMapper(delay=1.0)
    manager = _make_manager(mapper, tmp_path, load_timeout_ms=80)
    failures: list[tuple[str, str]] = []
    manager.failed.connect(lambda sc, msg: failures.append((sc, msg)))

    manager.ensure("TMT")
    assert _wait_until(lambda: failures, qapp)
    set_code, message = failures[0]
    assert set_code == "TMT"
    assert "timed out" in message
    assert manager.is_ready is True


def test_reset_clears_loaded_state(qapp, tmp_path, fake_scryfall):
    mapper = _FakeMapper()
    manager = _make_manager(mapper, tmp_path)
    ready: list[object] = []
    manager.ready.connect(ready.append)
    manager.ensure("TMT")
    assert _wait_until(lambda: ready, qapp)

    manager.reset()
    assert manager.loaded_set == ""
    assert manager.is_ready is False
