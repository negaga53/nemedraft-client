"""Tests for client.overlay.ui.screen_utils — off-screen window recovery."""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QWidget


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


SCREEN = QRect(0, 0, 1920, 1080)
SECOND_SCREEN = QRect(1920, 0, 1920, 1080)


def _visible(frame: QRect, screens: list[QRect]) -> bool:
    from client.overlay.ui.screen_utils import visible_enough

    return visible_enough(frame, screens)


def test_fully_on_screen_is_visible():
    assert _visible(QRect(100, 100, 620, 820), [SCREEN]) is True


def test_fully_off_screen_is_not_visible():
    assert _visible(QRect(5000, 5000, 620, 820), [SCREEN]) is False


def test_header_above_screen_top_is_not_grabbable():
    # The window body peeks onto the screen but the 40px header strip is
    # above the top edge — the user can't grab the drag row.
    assert _visible(QRect(100, -300, 620, 820), [SCREEN]) is False


def test_mostly_offscreen_left_with_tiny_sliver_is_not_visible():
    # Only 30px of width visible — below the 80px minimum grab width.
    assert _visible(QRect(-590, 100, 620, 820), [SCREEN]) is False


def test_window_on_second_monitor_is_visible():
    assert _visible(QRect(2200, 200, 620, 820), [SCREEN, SECOND_SCREEN]) is True


def test_window_on_disconnected_monitor_is_not_visible():
    # Same frame, but the second monitor is gone.
    assert _visible(QRect(2200, 200, 620, 820), [SCREEN]) is False


def test_negative_y_with_enough_header_visible():
    # Header strip from y=-10 to y=30: 30px on-screen < 40px required.
    assert _visible(QRect(100, -10, 620, 820), [SCREEN]) is False
    # Header fully on-screen at y=0.
    assert _visible(QRect(100, 0, 620, 820), [SCREEN]) is True


def test_ensure_on_screen_rescues_offscreen_window(qapp):
    from client.overlay.ui.screen_utils import ensure_on_screen

    w = QWidget()
    w.resize(400, 300)
    w.move(20000, 20000)
    rescued = ensure_on_screen(w)
    assert rescued is True
    # After rescue the window must pass its own visibility check.
    from client.overlay.ui.screen_utils import visible_enough
    from PySide6.QtGui import QGuiApplication

    screens = [s.availableGeometry() for s in QGuiApplication.screens()]
    assert visible_enough(w.frameGeometry(), screens) is True


def test_ensure_on_screen_leaves_visible_window_alone(qapp):
    from client.overlay.ui.screen_utils import ensure_on_screen

    w = QWidget()
    w.resize(400, 300)
    w.move(50, 50)
    pos_before = w.pos()
    assert ensure_on_screen(w) is False
    assert w.pos() == pos_before
