"""Pick-history navigation — provisional entries + nav arrow enablement.

The nav arrows at the bottom of the pack tab depend on
``state.pick_history`` having entries for *past* picks. Before the
2026-06-12 fix, entries were only written when a prediction succeeded
and arrived before the user picked — fast picking or a server hiccup
left the history empty and the arrows permanently disabled.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

# Must be set before any PySide6 import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_app(mapper_names: list[str]):
    """Assemble a minimal OverlayApp via __new__ — just enough state for
    the PackEvent branch of _on_event. Collaborators are mocks; the
    prediction path exits early because auth_client.is_authenticated is
    falsy on a MagicMock configured below."""
    from client.overlay.draft_state import DraftState
    from client.overlay.main import OverlayApp

    app = OverlayApp.__new__(OverlayApp)
    app.state = DraftState()
    app.scryfall_cards = {}
    app._recent_event_signatures = {}
    app._pending_events = []
    app._draft_completed = False
    app._in_lobby_context = ""

    app.mapper = MagicMock()
    app.mapper.grpids_to_names.return_value = mapper_names

    app._set_data = MagicMock()
    app._set_data.is_ready = True
    app._set_data.loaded_set = "FIN"

    app.watcher = MagicMock()
    app.watcher.replaying = False
    app.watcher._cur_draft_event = "PremierDraft_FIN_20260601"

    app.auth_client = MagicMock()
    app.auth_client.is_authenticated = False  # _run_prediction no-ops
    app.auth_client.session = None
    app._auth_polling = MagicMock()
    app._auth_polling.is_vip.return_value = False  # _is_vip() → False

    app.window = MagicMock()
    return app


def _pack_event(grpids: list[int], pack: int, pick: int):
    from client.overlay.log_watcher import PackEvent

    return PackEvent(
        card_grpids=grpids,
        pack_number=pack,
        pick_number=pick,
        event_name="PremierDraft_FIN_20260601",
    )


def test_live_pack_event_records_provisional_history(qapp):
    """A live pack must create a history entry even when no prediction
    ever lands (server down / fast pick / not authenticated)."""
    app = _make_app(["Card A", "Card B"])

    app._on_event(_pack_event([101, 102], 0, 0), replaying=False)
    app._on_event(_pack_event([103, 104], 0, 1), replaying=False)

    assert (0, 0) in app.state.pick_history
    assert (0, 1) in app.state.pick_history
    entry = app.state.pick_history[(0, 0)]
    assert [p["card"] for p in entry.picks] == ["Card A", "Card B"]


def test_live_pack_event_syncs_history_to_window(qapp):
    """The window must receive the history as soon as the pack lands —
    not only after a successful prediction round-trip."""
    app = _make_app(["Card A"])

    app._on_event(_pack_event([101], 0, 0), replaying=False)

    app.window.sync_pick_history.assert_called_with(app.state.pick_history)


def test_provisional_entry_not_clobbered_on_duplicate_pack(qapp):
    """A re-delivered pack (LogWatcher+MemoryWatcher race outside the
    dedupe window) must not erase a scored entry."""
    app = _make_app(["Card A"])

    app._on_event(_pack_event([101], 0, 0), replaying=False)
    app.state.pick_history[(0, 0)].picks[0]["score"] = 9.9
    app._recent_event_signatures.clear()
    app._on_event(_pack_event([101], 0, 0), replaying=False)

    assert app.state.pick_history[(0, 0)].picks[0]["score"] == 9.9


def test_nav_arrows_enable_from_second_pick(qapp):
    """PackTab: ◀/≪ enable as soon as one past pick exists; ▶/≫ only
    while browsing history."""
    from client.overlay.ui.pack_tab import PackTab

    tab = PackTab()
    try:
        _exercise_nav(tab)
    finally:
        # Deterministic teardown — orphaned top-level widgets GC'd
        # mid-suite crash Qt teardown.
        tab.deleteLater()
        qapp.processEvents()


def _exercise_nav(tab) -> None:
    from client.overlay.api_client import Pick
    from client.overlay.draft_state import PickHistoryEntry

    def mk(name: str) -> Pick:
        return Pick(card=name, card_id=1, rank=1, score=1.0, gihwr=0.5,
                    ata=2.0, colors=["U"], mana_cost="{U}",
                    type_line="Creature", is_elite=False)

    hist: dict = {}

    tab.update_predictions([mk("A")], pack_number=0, pick_number=0)
    hist[(0, 0)] = PickHistoryEntry(pack_number=0, pick_number=0,
                                    picked_card="", picks=[])
    tab.set_pick_history(hist)
    assert not tab._nav_prev.isEnabled()  # P1P1: nothing to go back to

    tab.update_predictions([mk("B")], pack_number=0, pick_number=1)
    hist[(0, 1)] = PickHistoryEntry(pack_number=0, pick_number=1,
                                    picked_card="", picks=[])
    tab.set_pick_history(hist)
    assert tab._nav_prev.isEnabled()
    assert tab._nav_first.isEnabled()
    assert not tab._nav_next.isEnabled()  # live view: nothing ahead

    tab._nav_go_prev()
    assert tab._nav_next.isEnabled()
    assert tab._nav_last.isEnabled()
