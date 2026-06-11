"""Off-screen window recovery — keep the frameless overlay grabbable.

The overlay restores its persisted geometry on boot; if the monitor it
lived on is gone (or gets unplugged mid-session), the window can land
where the user cannot reach the drag header. These helpers detect that
and recenter the window on the primary screen.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QRect
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

logger = logging.getLogger("overlay")

# The user must be able to grab the drag row: require this much of the
# top header strip visible on some screen.
HEADER_STRIP_H = 40
MIN_GRAB_W = 80
MIN_GRAB_H = 40


def visible_enough(
    frame: QRect,
    screens: list[QRect],
    *,
    min_w: int = MIN_GRAB_W,
    min_h: int = MIN_GRAB_H,
    header_h: int = HEADER_STRIP_H,
) -> bool:
    """Return True when the window's header strip is usably visible.

    Checking only the window position is insufficient — a window whose
    bottom peeks onto a screen is still unusable. The *header* (top
    ``header_h`` px) must intersect some screen by at least
    ``min_w`` × ``min_h``.
    """
    header = QRect(frame.x(), frame.y(), frame.width(), header_h)
    for screen in screens:
        overlap = screen.intersected(header)
        if overlap.width() >= min_w and overlap.height() >= min_h:
            return True
    return False


def ensure_on_screen(window: QWidget) -> bool:
    """Recenter *window* on the primary screen when it is unreachable.

    Returns:
        True when the window was rescued, False when it was already fine.
    """
    screens = [s.availableGeometry() for s in QGuiApplication.screens()]
    if not screens:
        return False
    if visible_enough(window.frameGeometry(), screens):
        return False

    primary_screen = QGuiApplication.primaryScreen()
    primary = primary_screen.availableGeometry() if primary_screen else screens[0]

    width = min(window.width(), primary.width())
    height = min(window.height(), primary.height())
    if (width, height) != (window.width(), window.height()):
        window.resize(width, height)

    window.move(primary.center() - QPoint(width // 2, height // 2))
    logger.info(
        "Overlay window was off-screen — recentered on the primary display",
    )
    return True
