"""HUD theme: tokens, generated QSS, fonts, and the property helper."""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from client.overlay.ui.theme.qss import build_stylesheet


def apply_theme(window: QWidget) -> None:
    """Apply the generated application stylesheet to *window*."""
    window.setStyleSheet(build_stylesheet())


def set_prop(widget: QWidget, name: str, value: object) -> None:
    """Set a dynamic property and re-polish so QSS attribute selectors apply.

    Qt evaluates ``[prop="value"]`` selectors at polish time; changing a
    property on a live widget needs an unpolish/polish cycle to restyle.
    """
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
