"""View-mode state machine — FULL tabbed view vs COMPACT pin strip.

The controller owns the mode value, its config persistence, and the
per-mode geometry slots. The window connects ``mode_changed`` and does
the actual visual switching; keeping the state here makes transitions
testable without a window.
"""

from __future__ import annotations

import enum

from PySide6.QtCore import QObject, Signal

from client.overlay.config import OverlayConfig


class ViewMode(enum.Enum):
    FULL = "full"
    COMPACT = "compact"   # the slim pin strip


def _mode_from_string(value: str) -> ViewMode:
    try:
        return ViewMode(value)
    except ValueError:
        return ViewMode.FULL


class ViewModeController(QObject):
    """Tracks the active view mode and per-mode window geometry."""

    mode_changed = Signal(object, object)  # (old: ViewMode, new: ViewMode)

    def __init__(self, config: OverlayConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        # Always boot FULL — the home tab must be visible at startup; the
        # persisted mode is applied when a draft becomes active.
        self._mode = ViewMode.FULL

    @property
    def mode(self) -> ViewMode:
        return self._mode

    @property
    def is_compact(self) -> bool:
        return self._mode is ViewMode.COMPACT

    def persisted_mode(self) -> ViewMode:
        """Mode to restore once a draft is active."""
        return _mode_from_string(self._config.overlay.view_mode)

    def set_mode(self, mode: ViewMode, *, persist: bool = True) -> bool:
        """Switch modes. Returns False on no-op.

        ``persist=False`` is for system-driven transitions (draft ended,
        summary shown) that must not overwrite the user's preferred mode
        for the next draft.
        """
        if mode is self._mode:
            return False
        old = self._mode
        self._mode = mode
        if persist:
            self._config.overlay.view_mode = mode.value
        self.mode_changed.emit(old, mode)
        return True

    def toggle(self) -> None:
        self.set_mode(
            ViewMode.FULL if self._mode is ViewMode.COMPACT else ViewMode.COMPACT,
        )

    # -- per-mode geometry persistence ---------------------------------------

    def save_geometry(self, mode: ViewMode, geometry_b64: str) -> None:
        if mode is ViewMode.COMPACT:
            self._config.overlay.geometry_compact = geometry_b64
        else:
            self._config.overlay.geometry_full = geometry_b64

    def geometry_for(self, mode: ViewMode) -> str:
        if mode is ViewMode.COMPACT:
            return self._config.overlay.geometry_compact
        # Legacy single-slot geometry (pre-0.6 configs) seeds the FULL slot.
        return self._config.overlay.geometry_full or self._config.overlay.geometry
