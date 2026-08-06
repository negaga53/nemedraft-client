"""LoadingIndicator widget + its PackTab/OverlayWindow/OverlayApp wiring.

Covers Part B of the overlay-freeze-fix design (see
docs/superpowers/specs/2026-08-06-overlay-freeze-async-art-design.md): a
spinner + status text replaces the bare 3px indeterminate bar as the
primary "something is happening" affordance while a prediction is in
flight, on a retry, or while a draft has started but no pack has arrived
yet.

Visibility is asserted via ``isHidden()`` rather than ``isVisible()`` —
none of these widgets are ever actually ``.show()``-ed as a real
on-screen top-level window in this suite, and ``isVisible()`` on a child
whose ancestor chain was never shown is always False regardless of our
own ``setVisible`` calls. ``isHidden()`` only reflects the widget's own
explicit show/hide state, matching the convention the rest of this test
module uses (``isVisibleTo(ancestor)``) without needing an ancestor.
"""

from __future__ import annotations

import os

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# -- LoadingIndicator ---------------------------------------------------------

def test_hidden_by_default(qapp):
    from client.overlay.ui.widgets.loading_indicator import LoadingIndicator

    ind = LoadingIndicator()
    assert ind.isHidden()
    assert not ind.is_spinning()
    assert ind.message() == ""


def test_show_message_reveals_text_and_starts_spinner(qapp):
    from client.overlay.ui.widgets.loading_indicator import LoadingIndicator

    ind = LoadingIndicator()
    ind.show_message("Getting picks for P1P1...")

    assert not ind.isHidden()
    assert ind.message() == "Getting picks for P1P1..."
    assert ind.is_spinning()


def test_hide_indicator_stops_spinner_and_hides(qapp):
    from client.overlay.ui.widgets.loading_indicator import LoadingIndicator

    ind = LoadingIndicator()
    ind.show_message("waiting...")
    assert ind.is_spinning()

    ind.hide_indicator()

    assert ind.isHidden()
    assert not ind.is_spinning()


def test_set_message_updates_text_without_touching_visibility_or_timer(qapp):
    from client.overlay.ui.widgets.loading_indicator import LoadingIndicator

    ind = LoadingIndicator()
    ind.show_message("attempt 1")
    ind.set_message("attempt 2")

    assert ind.message() == "attempt 2"
    assert not ind.isHidden()
    assert ind.is_spinning()


# -- PackTab wiring ------------------------------------------------------------

def test_pack_tab_show_loading_shows_indicator_with_message(qapp):
    from client.overlay.ui.pack_tab import PackTab

    tab = PackTab(show_art=False)
    assert tab.loading_indicator.isHidden()

    tab.show_loading("Getting picks for P1P1...")

    assert not tab.loading_indicator.isHidden()
    assert tab.loading_indicator.message() == "Getting picks for P1P1..."
    assert tab.loading_indicator.is_spinning()


def test_pack_tab_hide_loading_hides_indicator_and_stops_timer(qapp):
    from client.overlay.ui.pack_tab import PackTab

    tab = PackTab(show_art=False)
    tab.show_loading("waiting...")

    tab.hide_loading()

    assert tab.loading_indicator.isHidden()
    assert not tab.loading_indicator.is_spinning()


def test_pack_tab_no_bare_progress_bar_left_over(qapp):
    """The LoadingIndicator replaces the old 3px indeterminate bar."""
    from client.overlay.ui.pack_tab import PackTab

    tab = PackTab(show_art=False)
    assert not hasattr(tab, "loading_bar")


def test_pack_tab_retranslate_does_not_crash_while_loading(qapp):
    from client.overlay.ui.pack_tab import PackTab

    tab = PackTab(show_art=False)
    tab.show_loading("Getting picks for P1P1...")

    tab.retranslate()  # must not raise

    assert not tab.loading_indicator.isHidden()


# -- OverlayWindow wiring -------------------------------------------------------

def test_show_prediction_loading_interpolates_pack_and_pick(qapp):
    from client.overlay.ui.window import OverlayWindow
    from client.overlay.config import OverlayConfig
    from client.overlay.i18n import tr

    w = OverlayWindow(OverlayConfig(), show_art=False)
    w.show_prediction_loading(pack_number=0, pick_number=7)

    expected = tr("predicting_pack_pick", pack=1, pick=8)
    assert w.pack_tab.loading_indicator.message() == expected
    assert not w.pack_tab.loading_indicator.isHidden()


def test_show_waiting_surfaces_waiting_first_pack_on_pack_tab(qapp):
    from client.overlay.ui.window import OverlayWindow
    from client.overlay.config import OverlayConfig
    from client.overlay.i18n import tr

    w = OverlayWindow(OverlayConfig(), show_art=False)
    w.show_waiting()

    assert w.pack_tab.loading_indicator.message() == tr("waiting_first_pack")
    assert not w.pack_tab.loading_indicator.isHidden()


def test_prediction_landing_replaces_waiting_message_and_then_hides(qapp):
    """draft start (waiting) -> prediction dispatched (predicting) ->
    results land (hidden) — the three-state contract from the design doc,
    using the real slot chain (``_on_prediction`` calls ``hide_loading``)."""
    from client.overlay.ui.window import OverlayWindow
    from client.overlay.config import OverlayConfig
    from client.overlay.api_client import Pick

    w = OverlayWindow(OverlayConfig(), show_art=False)
    w.show_waiting()
    assert not w.pack_tab.loading_indicator.isHidden()

    w.show_prediction_loading(pack_number=0, pick_number=0)
    assert not w.pack_tab.loading_indicator.isHidden()

    pick = Pick(
        card="Counterspell", score=0.8, rank=1, is_elite=False,
        colors=["U"], mana_cost="{U}{U}", type_line="Instant",
        gihwr=0.58, ata=3.4, iwd=0.0, stats_loaded=True,
    )
    w._on_prediction([pick], "FIN", 0, 0, 1, {})

    assert w.pack_tab.loading_indicator.isHidden()
    assert not w.pack_tab.loading_indicator.is_spinning()


# -- OverlayApp: retry text updates every attempt ------------------------------

def _make_retry_app():
    """Minimal OverlayApp stand-in — only ``window`` is touched by
    ``_on_prediction_retrying``, following the ``__new__`` pattern used in
    test_overlay_queue_wiring.py for exercising a single handler method."""
    from client.overlay.main import OverlayApp

    app = OverlayApp.__new__(OverlayApp)
    app.window = MagicMock()
    return app


def test_retrying_updates_loading_text_on_every_attempt(qapp):
    from client.overlay.i18n import tr

    app = _make_retry_app()

    app._on_prediction_retrying(1, 500)
    app.window.pack_tab.show_loading.assert_called_with(
        tr("prediction_retrying", attempt=1)
    )

    app._on_prediction_retrying(2, 1000)
    app.window.pack_tab.show_loading.assert_called_with(
        tr("prediction_retrying", attempt=2)
    )

    assert app.window.pack_tab.show_loading.call_count == 2
