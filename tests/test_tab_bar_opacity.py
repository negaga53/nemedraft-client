"""Guard the tab-bar chrome: opaque bar background + full-width document mode.

A styled QTabWidget doesn't paint behind its tab-bar row, so a transparent
QTabBar leaves the row unpainted (it showed the desktop through in the old
glass mode). The bar must paint the window background, and the tab widget
must be in documentMode so the bar spans the full width rather than only
the tabs' span.
"""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_tabbar_paints_window_background_not_transparent(qapp):
    from client.overlay.ui.theme import tokens
    from client.overlay.ui.theme.qss import build_stylesheet

    qss = build_stylesheet()
    # Find the QTabBar block and assert its background is the window colour.
    start = qss.index("QTabBar {")
    block = qss[start:qss.index("}", start)]
    assert "transparent" not in block
    assert tokens.L0_WINDOW_OPAQUE in block


def test_tab_widget_uses_document_mode(qapp):
    from client.overlay.config import OverlayConfig
    from client.overlay.ui.window import OverlayWindow

    window = OverlayWindow(OverlayConfig())
    assert window.tabs.documentMode() is True
