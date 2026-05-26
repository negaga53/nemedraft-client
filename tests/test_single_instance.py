"""Tests for the overlay's single-instance lock.

Run with: QT_QPA_PLATFORM=offscreen pytest tests/test_single_instance.py -v
"""

from __future__ import annotations

import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from client.overlay.single_instance import SingleInstance


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def unique_name() -> str:
    name = f"nemedraft-test-{uuid.uuid4().hex[:12]}"
    yield name
    # Clean up any stale socket file left on disk (cover both the raw
    # name and any per-user-hashed variant the module might construct).
    QLocalServer.removeServer(name)


def _spin_event_loop(ms: int) -> None:
    """Pump the Qt event loop briefly so async signals can deliver."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def test_first_acquire_returns_true(qapp, unique_name):
    first = SingleInstance()
    assert first.acquire(unique_name) is True


def test_second_acquire_returns_false(qapp, unique_name):
    first = SingleInstance()
    assert first.acquire(unique_name) is True

    second = SingleInstance()
    assert second.acquire(unique_name) is False


def test_acquire_succeeds_after_first_destroyed(qapp, unique_name):
    first = SingleInstance()
    assert first.acquire(unique_name) is True
    first.deleteLater()
    del first
    _spin_event_loop(50)
    QLocalServer.removeServer(unique_name)

    third = SingleInstance()
    assert third.acquire(unique_name) is True


def test_raise_signal_fires_when_client_sends_raise(qapp, unique_name):
    """Server emits ``raise_requested`` when a peer sends the RAISE command.

    In production the peer is a separate process; here we drive a
    ``QLocalSocket`` manually with event-loop spinning between steps
    because client and server share the same Qt event loop in-process
    (the synchronous ``waitFor*`` calls in ``_notify_existing`` would
    starve the server's slots).
    """
    first = SingleInstance()
    assert first.acquire(unique_name) is True

    fired: list[bool] = []
    first.raise_requested.connect(lambda: fired.append(True))

    sock = QLocalSocket()
    sock.connectToServer(first.socket_name)
    assert sock.waitForConnected(500), sock.errorString()

    sock.write(b"RAISE\n")
    sock.flush()

    deadline = 500
    elapsed = 0
    while not fired and elapsed < deadline:
        _spin_event_loop(20)
        elapsed += 20

    sock.disconnectFromServer()
    assert fired == [True]
