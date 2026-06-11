"""Bundled font loading — Inter (OFL), with a silent system fallback."""

from __future__ import annotations

import logging

logger = logging.getLogger("overlay")

_loaded = False


def load_fonts() -> str:
    """Register the bundled Inter TTFs with Qt's font database.

    Safe to call multiple times and safe in environments without the
    fonts on disk (offscreen CI) — the QSS family stack falls back to
    "Segoe UI" / sans-serif.

    Returns:
        The resolved family name ("Inter" when loaded, "" on fallback).
    """
    global _loaded
    from PySide6.QtGui import QFontDatabase

    from client.overlay.env import bundle_root

    fonts_dir = bundle_root() / "assets" / "fonts"
    if not fonts_dir.is_dir():
        # Dev checkouts resolve relative to the submodule root.
        alt = bundle_root() / "external" / "nemedraft-client" / "assets" / "fonts"
        if alt.is_dir():
            fonts_dir = alt
        else:
            logger.info("Bundled fonts not found (%s) — using system fonts", fonts_dir)
            return ""

    loaded_any = False
    for ttf in sorted(fonts_dir.glob("*.ttf")):
        font_id = QFontDatabase.addApplicationFont(str(ttf))
        if font_id < 0:
            logger.warning("Failed to load bundled font %s", ttf.name)
        else:
            loaded_any = True

    if loaded_any:
        _loaded = True
        return "Inter"
    return ""


def is_loaded() -> bool:
    return _loaded
