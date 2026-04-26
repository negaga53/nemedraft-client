"""Download and cache official MTG mana symbol SVGs from Scryfall's CDN.

Provides :func:`mana_icon_path` to get a local PNG for any mana symbol,
and :func:`mana_cost_pixmaps` to get ordered pixmaps for a full cost string.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path

import httpx
from PySide6.QtGui import QImage, QPainter, QPixmap

from client.overlay.env import bundle_root

logger = logging.getLogger(__name__)

# Scryfall hosts mana SVGs at this base.  We download them once and
# rasterise via QPixmap (Qt can render SVGs natively).
_SVG_URL_BASE = "https://svgs.scryfall.io/card-symbols"

_PIP_RE = re.compile(r"\{(.*?)\}")

DEFAULT_ICON_DIR = bundle_root() / "data" / "mana_icons"

_ICON_SIZE = 16  # px — display size for mana pips in the overlay


def _normalise_symbol(pip: str) -> str:
    """Convert a pip string to the filename Scryfall uses.

    ``W`` → ``W.svg``, ``2/W`` → ``2W.svg``, ``X`` → ``X.svg``, etc.
    """
    return pip.replace("/", "").upper()


class ManaIconCache:
    """Downloads SVG mana symbols from Scryfall and caches them locally.

    Args:
        icon_dir: Directory to store cached SVG files.
    """

    def __init__(self, icon_dir: Path = DEFAULT_ICON_DIR) -> None:
        self._dir = icon_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._pix: dict[str, QPixmap | None] = {}

    def get_pixmap(self, pip: str, size: int = _ICON_SIZE) -> QPixmap | None:
        """Return a ``QPixmap`` for a single mana pip like ``W``, ``2``, ``X``.

        Downloads the SVG on first access and caches it permanently.
        Uses QSvgRenderer for crisp vector rendering at any size.
        """
        key = f"{_normalise_symbol(pip)}_{size}"
        if key in self._pix:
            return self._pix[key]

        sym = _normalise_symbol(pip)
        svg_path = self._dir / f"{sym}.svg"
        if not svg_path.exists():
            self._fetch(sym, svg_path)

        if svg_path.exists():
            pm = self._render_svg(svg_path, size)
            if pm and not pm.isNull():
                self._pix[key] = pm
                return pm

        self._pix[key] = None
        return None

    @staticmethod
    def _render_svg(svg_path: Path, size: int) -> QPixmap | None:
        """Render an SVG file to a QPixmap at *size* × *size* logical pixels.

        Renders at 2× then downscales with smooth filtering for antialiased edges.
        """
        from PySide6.QtCore import QRectF, QSize, Qt
        from PySide6.QtSvg import QSvgRenderer

        renderer = QSvgRenderer(str(svg_path))
        if not renderer.isValid():
            return None
        render_size = size * 2
        image = QImage(QSize(render_size, render_size), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        renderer.render(painter, QRectF(0, 0, render_size, render_size))
        painter.end()
        pm = QPixmap.fromImage(image)
        return pm.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _fetch(self, key: str, dest: Path) -> None:
        url = f"{_SVG_URL_BASE}/{key}.svg"
        try:
            with httpx.Client(timeout=10, follow_redirects=True) as c:
                resp = c.get(url)
                if resp.status_code == 200:
                    dest.write_bytes(resp.content)
                    logger.debug("Cached mana icon %s", key)
                else:
                    logger.debug("Mana icon HTTP %d for %s", resp.status_code, key)
        except Exception:
            logger.debug("Failed to fetch mana icon %s", key, exc_info=True)

    # ------------------------------------------------------------------

    def clear(self) -> int:
        """Delete all cached icons.  Returns the number of files removed."""
        count = 0
        for f in self._dir.glob("*.svg"):
            f.unlink(missing_ok=True)
            count += 1
        self._pix.clear()
        return count


# Need Qt import for scaling.
from PySide6.QtCore import Qt  # noqa: E402 — deferred to avoid circular


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: ManaIconCache | None = None


def get_mana_icon_cache() -> ManaIconCache:
    """Return the module-level :class:`ManaIconCache` singleton."""
    global _instance
    if _instance is None:
        _instance = ManaIconCache()
    return _instance


def parse_mana_pips(mana_cost: str) -> list[str]:
    """Parse ``{1}{W}{U}`` into ``["1", "W", "U"]``."""
    return [m.group(1).upper() for m in _PIP_RE.finditer(mana_cost)]
