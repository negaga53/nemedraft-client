"""Tests for client.overlay.ui.toast.ToastHost."""

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


def _note(message="boom", severity=None, timeout_ms=0):
    from client.overlay.notifications import Notification, Severity

    return Notification(
        message=message,
        severity=severity if severity is not None else Severity.ERROR,
        timeout_ms=timeout_ms,
    )


def test_show_notification_creates_banner_with_severity_property(qapp):
    from client.overlay.notifications import Severity
    from client.overlay.ui.toast import ToastHost

    host = ToastHost()
    host.show_notification(_note("server down", Severity.ERROR))
    assert host.count() == 1
    banner = host.banners()[0]
    assert banner.objectName() == "toast"
    assert banner.property("severity") == "error"
    assert "server down" in banner.text()


def test_max_three_banners_evicts_oldest(qapp):
    from client.overlay.notifications import Severity
    from client.overlay.ui.toast import ToastHost

    host = ToastHost()
    for i in range(5):
        host.show_notification(_note(f"msg {i}", Severity.ERROR))
    assert host.count() == 3
    texts = [b.text() for b in host.banners()]
    assert any("msg 2" in t for t in texts)
    assert any("msg 4" in t for t in texts)
    assert not any("msg 0" in t for t in texts)


def test_info_auto_dismisses(qapp):
    from client.overlay.notifications import Severity
    from client.overlay.ui.toast import ToastHost

    host = ToastHost()
    host.show_notification(_note("quick info", Severity.INFO, timeout_ms=60))
    assert host.count() == 1

    deadline = time.monotonic() + 3
    while host.count() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert host.count() == 0


def test_error_is_sticky_with_close_button(qapp):
    from client.overlay.notifications import Severity
    from client.overlay.ui.toast import ToastHost

    host = ToastHost()
    host.show_notification(_note("fatal", Severity.ERROR))
    banner = host.banners()[0]
    assert banner.close_button is not None

    banner.close_button.click()
    qapp.processEvents()
    assert host.count() == 0


def test_warning_auto_dismiss_default_is_eight_seconds(qapp):
    from client.overlay.notifications import Severity
    from client.overlay.ui.toast import ToastHost

    host = ToastHost()
    assert host.DEFAULT_TIMEOUTS_MS[Severity.WARNING] == 8000
    assert host.DEFAULT_TIMEOUTS_MS[Severity.INFO] == 4000
    assert host.DEFAULT_TIMEOUTS_MS[Severity.ERROR] == 0  # sticky
