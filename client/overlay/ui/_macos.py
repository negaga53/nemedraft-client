"""macOS-only NSWindow tweaks for the overlay.

Qt's WindowStaysOnTopHint maps to a level on macOS that other floating
windows (and Arena through Wine/CrossOver) can still stack on top of.
Bumping the NSWindow level to NSFloatingWindowLevel after the window
becomes visible keeps the overlay above peers reliably. No-op on every
other platform.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def elevate_to_floating(widget) -> None:  # widget: QWidget — typed loose to avoid import on Linux
    """Raise the NSWindow level to NSFloatingWindowLevel. Best-effort."""
    if sys.platform != "darwin":
        return
    try:
        import objc
        from AppKit import NSFloatingWindowLevel
    except ImportError:
        logger.warning(
            "PyObjC not installed; cannot elevate NSWindow level. "
            "Install pyobjc-framework-Cocoa to keep the overlay above peers on macOS.",
        )
        return
    try:
        ns_view = objc.objc_object(c_void_p=int(widget.winId()))
        ns_window = ns_view.window()
        ns_window.setLevel_(NSFloatingWindowLevel)
    except Exception:
        logger.exception("Failed to elevate NSWindow level")
