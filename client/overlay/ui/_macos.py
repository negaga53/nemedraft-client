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

Two additional gotchas make this stateful rather than fire-and-forget:

* Qt delivers ``QShowEvent`` *before* the platform window is ordered
  in, so on the first show the NSView may not be attached to its
  NSWindow yet. ``apply_pinning`` reports that case (returns False)
  so the caller can retry on the next event-loop turn instead of
  silently giving up — the old behaviour, which left the overlay as a
  plain ``Qt.Tool`` panel that macOS hides the moment the app loses
  focus (i.e. as soon as the user clicks into Arena).
* Qt recomputes the native level / panel flags when it recreates or
  reconfigures the NSWindow, clobbering our tweaks. Callers re-assert
  on every show and on application-state changes; the calls are a few
  cheap objc messages, safe to repeat.

No-op on every other platform.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def apply_pinning(widget, enabled: bool = True) -> bool:
    """Pin (or unpin) the overlay above other windows on macOS.

    Args:
        widget: The top-level QWidget backing the overlay window
            (typed loose to avoid a Qt import on Linux).
        enabled: True pins the window to the status level across all
            Spaces; False restores a normal window level.

    Returns:
        False when the NSWindow does not exist yet and the caller
        should retry after the event loop turns; True otherwise
        (including every no-op and best-effort-failure path, which
        must not be retried in a loop).
    """
    if sys.platform != "darwin":
        return True
    try:
        import objc
        from AppKit import (
            NSNormalWindowLevel,
            NSStatusWindowLevel,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorDefault,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary,
        )
    except ImportError:
        logger.warning(
            "PyObjC not installed; cannot adjust NSWindow level. "
            "Install pyobjc-framework-Cocoa to keep the overlay above "
            "fullscreen Arena and other apps on macOS.",
        )
        return True
    try:
        ns_view = objc.objc_object(c_void_p=int(widget.winId()))
        ns_window = ns_view.window()
        if ns_window is None:
            # QShowEvent lands before the NSView is attached to its
            # NSWindow — tell the caller to retry next event-loop turn.
            return False
        if enabled:
            ns_window.setLevel_(NSStatusWindowLevel)
            ns_window.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorFullScreenAuxiliary
                | NSWindowCollectionBehaviorStationary,
            )
        else:
            ns_window.setLevel_(NSNormalWindowLevel)
            ns_window.setCollectionBehavior_(NSWindowCollectionBehaviorDefault)
        # Tool windows on macOS hide when the app loses focus by default;
        # opt out so the overlay stays visible while Arena is foregrounded
        # (even unpinned — hiding reads as "the app closed").
        try:
            ns_window.setHidesOnDeactivate_(False)
        except Exception:  # noqa: BLE001 — older AppKit signatures vary
            pass
        return True
    except Exception:
        logger.exception("Failed to adjust NSWindow level")
        return True
