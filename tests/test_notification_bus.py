"""Tests for client.overlay.notifications.NotificationBus."""

from __future__ import annotations

import os
import threading
import time

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_bus(time_fn=None):
    from client.overlay.notifications import NotificationBus

    return NotificationBus(time_fn=time_fn or time.monotonic)


def test_post_emits_notification(qapp):
    from client.overlay.notifications import Severity

    bus = _make_bus()
    received: list[object] = []
    bus.posted.connect(received.append)

    assert bus.post("Server error", severity=Severity.ERROR) is True
    qapp.processEvents()
    assert len(received) == 1
    note = received[0]
    assert note.message == "Server error"
    assert note.severity == Severity.ERROR


def test_same_key_deduped_within_window(qapp):
    from client.overlay.notifications import NotificationBus

    clock = {"now": 0.0}
    bus = _make_bus(time_fn=lambda: clock["now"])
    received: list[object] = []
    bus.posted.connect(received.append)

    assert bus.post("retrying", key="predict-retry") is True
    assert bus.post("retrying again", key="predict-retry") is False
    clock["now"] = NotificationBus.DEDUPE_WINDOW_S + 1
    assert bus.post("retrying later", key="predict-retry") is True
    qapp.processEvents()
    assert len(received) == 2


def test_message_is_default_dedupe_key(qapp):
    bus = _make_bus(time_fn=lambda: 0.0)
    assert bus.post("same text") is True
    assert bus.post("same text") is False
    assert bus.post("different text") is True


def test_rate_cap_suppresses_burst_with_single_overflow_warning(qapp):
    from client.overlay.notifications import NotificationBus, Severity

    bus = _make_bus(time_fn=lambda: 0.0)
    received: list[object] = []
    bus.posted.connect(received.append)

    delivered = sum(
        1 for i in range(NotificationBus.RATE_MAX + 4)
        if bus.post(f"error {i}")
    )
    qapp.processEvents()

    assert delivered == NotificationBus.RATE_MAX
    overflow = [n for n in received if "suppressed" in n.message.lower()]
    assert len(overflow) == 1
    assert overflow[0].severity == Severity.WARNING


def test_post_from_background_thread_delivers_on_main_thread(qapp):
    bus = _make_bus()
    received_threads: list[int] = []
    bus.posted.connect(lambda n: received_threads.append(threading.get_ident()))

    worker = threading.Thread(target=lambda: bus.post("from thread"))
    worker.start()
    worker.join()

    deadline = time.monotonic() + 3
    while not received_threads and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)

    assert received_threads == [threading.get_ident()]


def test_singleton_instance(qapp):
    from client.overlay.notifications import NotificationBus

    NotificationBus.reset_instance()
    a = NotificationBus.instance()
    b = NotificationBus.instance()
    assert a is b
    NotificationBus.reset_instance()
