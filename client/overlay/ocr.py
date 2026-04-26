"""P1P1 OCR detection — screenshot-based first-pick card identification."""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

OCR_ENDPOINT = "https://us-central1-mtgalimited.cloudfunctions.net/pack_parser"
OCR_TIMEOUT = 8.0

# Basic lands always included in the candidate list for the OCR service.
_BASIC_LANDS = ["Plains", "Island", "Swamp", "Mountain", "Forest"]


def capture_screenshot_qt() -> bytes | None:
    """Capture the primary screen as JPEG bytes using PySide6.

    Returns:
        JPEG image bytes, or *None* on failure.
    """
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QBuffer, QIODevice

        app = QApplication.instance()
        if app is None:
            return None
        screen = app.primaryScreen()
        if screen is None:
            return None
        pixmap = screen.grabWindow(0)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buf, "JPEG", 80)
        return bytes(buf.data())
    except Exception:
        logger.exception("Screenshot capture failed")
        return None


def detect_p1p1(
    all_card_names: list[str],
    screenshot_png: bytes | None = None,
    save_path: Path | None = None,
) -> list[str]:
    """Send a screenshot to the OCR service and return detected card names.

    Args:
        all_card_names: Full card name list for the set (used as OCR candidates).
        screenshot_png: PNG image bytes. If *None*, captures the screen automatically.
        save_path: If provided, saves the screenshot here for debugging.

    Returns:
        List of detected card names (may be empty on failure).
    """
    if screenshot_png is None:
        screenshot_png = capture_screenshot_qt()
    if not screenshot_png:
        logger.warning("No screenshot available for OCR")
        return []

    if save_path:
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(screenshot_png)
            logger.debug("Screenshot saved to %s", save_path)
        except OSError:
            logger.warning("Failed to save screenshot to %s", save_path)

    b64 = base64.b64encode(screenshot_png).decode("ascii")
    candidates = list(all_card_names) + _BASIC_LANDS

    payload = {
        "card_names": candidates,
        "image": b64,
    }

    try:
        resp = httpx.post(OCR_ENDPOINT, json=payload, timeout=OCR_TIMEOUT)
        resp.raise_for_status()
        detected = resp.json()
        if isinstance(detected, list):
            logger.info("OCR detected %d cards", len(detected))
            return [str(name) for name in detected if isinstance(name, str)]
        return []
    except httpx.HTTPError:
        logger.warning("OCR request failed", exc_info=True)
        return []
    except Exception:
        logger.exception("Unexpected OCR error")
        return []
