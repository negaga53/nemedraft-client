"""macOS-only NSWindow tweaks for the overlay.

Qt's ``WindowStaysOnTopHint`` maps to ``NSFloatingWindowLevel`` on
macOS, which sits below the menu bar and — crucially — below
fullscreen apps. Drafting Arena via Wine/CrossOver in fullscreen mode
puts the game on its own Space, and a plain floating window stays
behind on the desktop Space.

To actually stay on top we need two things:

* A higher level. ``NSStatusWindowLevel`` (= 25) sits above the menu
  bar and the standard floating tier, while still being below popups
  and the cursor — appropriate for a HUD overlay.
* A collection behaviour that lets the window appear in *every*
  Space, including the auxiliary stack rendered above fullscreen
  apps. Without ``NSWindowCollectionBehaviorCanJoinAllSpaces`` the
  overlay is pinned to one Space; without
  ``NSWindowCollectionBehaviorFullScreenAuxiliary`` it gets pushed
  underneath any fullscreen window. ``Stationary`` keeps it from
  sliding along when the user swipes between Spaces.

No-op on every other platform.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def elevate_to_floating(widget) -> None:  # widget: QWidget — typed loose to avoid import on Linux
    """Pin the overlay above other windows on macOS. Best-effort."""
    if sys.platform != "darwin":
        return
    try:
        import objc
        from AppKit import (
            NSStatusWindowLevel,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary,
        )
    except ImportError:
        logger.warning(
            "PyObjC not installed; cannot elevate NSWindow level. "
            "Install pyobjc-framework-Cocoa to keep the overlay above "
            "fullscreen Arena and other apps on macOS.",
        )
        return
    try:
        ns_view = objc.objc_object(c_void_p=int(widget.winId()))
        ns_window = ns_view.window()
        ns_window.setLevel_(NSStatusWindowLevel)
        ns_window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary,
        )
        # Tool windows on macOS hide when the app loses focus by default;
        # opt out so the overlay stays visible while Arena is foregrounded.
        try:
            ns_window.setHidesOnDeactivate_(False)
        except Exception:  # noqa: BLE001 — older AppKit signatures vary
            pass
    except Exception:
        logger.exception("Failed to elevate NSWindow level")
