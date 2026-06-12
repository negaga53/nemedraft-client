"""End-to-end simulator pipeline tests.

Drives the REAL pipeline the draft simulator exercises:

    simulator (ArenaLogWriter / SimulatorWindow / DraftEngine)
      →  real LogWatcher(always_replay=True)
      →  UiMarshaler  →  OverlayApp._on_event  →  real OverlayWindow

Reproduces the reported simulator bug: every ``BotDraft_DraftPick`` log
entry carried ``PackNumber/PickNumber = -1``, which the overlay's envelope
clamp (``LogWatcher._emit``) dropped — so ``on_pick`` never ran, the drafted
pool stayed stuck at the single card restored from the first pack's
``PickedCards``, and the deck-builder + draft summary were built from a
1-card pool.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

# The draft simulator lives in the private parent repo. When this client is
# checked out standalone (public CI) it isn't importable — skip the module.
pytest.importorskip("simulator.main", reason="parent-repo simulator not available")

_CLIENT_ROOT = Path(__file__).resolve().parent.parent
_TMT_SCRYFALL = _CLIENT_ROOT / "data" / "scryfall" / "tmt_cards.json"


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _build_app(qapp, sim_log: Path, grpid_to_name: dict[int, str]):
    """Assemble an OverlayApp via __new__ with a REAL window + REAL watcher.

    The mapper resolves grpIds the way ``_on_simulator_detected`` does after
    loading ``simulator_grpid_map.json``. Predictions are gated off so the
    pipeline mirrors a simulator run without a reachable model server — the
    pick-history nav and pool tracking must work on log events alone.
    """
    from client.overlay.config import OverlayConfig
    from client.overlay.draft_state import DraftState
    from client.overlay.events import UiMarshaler
    from client.overlay.log_watcher import LogWatcher
    from client.overlay.main import OverlayApp
    from client.overlay.ui.window import OverlayWindow

    app = OverlayApp.__new__(OverlayApp)
    app.state = DraftState()
    app.scryfall_cards = {}
    app._recent_event_signatures = {}
    app._pending_events = []
    app._deferred_deck_pool = None
    app._draft_completed = False
    app._in_lobby_context = ""
    app._set_untrained = False
    app._server_supported_sets = ["TMT"]
    app._has_arena_player_id = True
    app._cache_dir = Path(tempfile.mkdtemp())

    app.mapper = MagicMock()
    app.mapper.grpids_to_names.side_effect = lambda gids: [
        grpid_to_name[g] for g in gids if g in grpid_to_name
    ]

    app._set_data = MagicMock()
    app._set_data.is_ready = True
    app._set_data.loaded_set = "TMT"
    app._set_data.ensure.return_value = False

    app.art_cache = MagicMock()
    app.art_cache.enabled = False

    app.auth_client = MagicMock()
    app.auth_client.is_authenticated = False  # _run_prediction no-ops
    app.auth_client.session = None

    app._auth_polling = MagicMock()
    app._auth_polling.is_vip.return_value = True

    app._prediction = MagicMock()
    app._arena_poller = MagicMock()
    app.memory_watcher = None
    app.config = OverlayConfig()

    app.window = OverlayWindow(OverlayConfig(), show_art=False)
    app.window.show_model_ready()

    app._marshaler = UiMarshaler()
    app._marshaler.bind(on_event=app._on_event, on_set_load=lambda s: None)

    app.watcher = LogWatcher(log_path=sim_log, poll_interval=0.02, always_replay=True)
    app.watcher.add_callback(app._emit_log_event)
    return app


def _pump(qapp, seconds: float = 0.25):
    """Let the watcher thread read + the marshaler deliver queued events."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()


# ---------------------------------------------------------------------------
# Focused regression test — synthetic packs, exercises the pick→pool path.
# ---------------------------------------------------------------------------

def test_simulator_pick_events_build_pool(qapp):
    """Every BotDraft pick must reach the controller so the pool grows by
    one card per pick — the root cause of the reported simulator breakage."""
    from simulator.main import ArenaLogWriter

    tmp = Path(tempfile.mkdtemp())
    sim_log = tmp / "simulator.log"

    grpids = list(range(100, 200))
    name_to_grpid = {f"Card {g}": g for g in grpids}
    grpid_to_name = {g: n for n, g in name_to_grpid.items()}

    writer = ArenaLogWriter(sim_log, name_to_grpid, "QuickDraft_TMT_Simulated")
    pack_size, n_packs = 3, 3

    def pack_cards(pack, pick):
        base = 100 + pack * 30 + pick * pack_size
        return [f"Card {g}" for g in range(base, base + pack_size)]

    writer.write_event_join()
    writer.write_pack(pack_cards(0, 0), 0, 0)

    app = _build_app(qapp, sim_log, grpid_to_name)
    app.watcher.start()
    try:
        _pump(qapp, 0.3)  # replay Event_Join + P1P1, flip to live

        nav_enabled = []
        n_picks = 0
        for pack in range(n_packs):
            for pick in range(pack_size):
                writer.write_pick([pack_cards(pack, pick)[0]], pack, pick)
                n_picks += 1
                if pack == n_packs - 1 and pick == pack_size - 1:
                    writer.write_draft_complete()
                else:
                    npack, npick = (pack, pick + 1) if pick + 1 < pack_size else (pack + 1, 0)
                    writer.write_pack(pack_cards(npack, npick), npack, npick)
                _pump(qapp, 0.2)
                nav_enabled.append(app.window.pack_tab._nav_prev.isEnabled())

        _pump(qapp, 0.3)
    finally:
        app.watcher.stop()

    assert len(app.state.pool) == n_picks, (
        f"pool should hold all {n_picks} picks, got {len(app.state.pool)}"
    )
    assert any(nav_enabled[1:]), "pick-history nav arrows never enabled"
    assert app.window.tabs.isTabVisible(app.window._tab_summary_idx), (
        "summary tab never became visible after draft completion"
    )


# ---------------------------------------------------------------------------
# True cross-app E2E — real DraftEngine + SimulatorWindow + bridge + writer.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _TMT_SCRYFALL.exists(), reason="TMT scryfall data missing")
def test_simulator_real_engine_live_draft(qapp, tmp_path, monkeypatch):
    """Drive the actual simulator (engine + window + bridge + log writer)
    through a full live draft and verify the overlay tracks the whole pool
    and reveals the summary at the end."""
    from simulator.engine import DraftEngine, load_set_cards
    from simulator.main import ArenaLogWriter, DraftSimulatorBridge, _load_name_to_grpid
    from simulator.window import SimulatorWindow

    # _load_name_to_grpid writes simulator_grpid_map.json into ./data/cache —
    # keep that side effect inside the temp dir, not the repo.
    monkeypatch.chdir(tmp_path)
    tmp = tmp_path
    sim_log = tmp / "simulator.log"

    name_to_grpid = _load_name_to_grpid(_TMT_SCRYFALL)
    grpid_to_name = {g: n for n, g in name_to_grpid.items()}

    writer = ArenaLogWriter(sim_log, name_to_grpid, "QuickDraft_TMT_Simulated")
    cards = load_set_cards(_TMT_SCRYFALL, "TMT")
    engine = DraftEngine(cards, "TMT", seed=7)
    expected_picks = engine.total_picks

    sim_window = SimulatorWindow(engine)
    sim_window._image_loader.enqueue = lambda *a, **k: None  # no Scryfall network
    bridge = DraftSimulatorBridge(sim_window, writer)  # noqa: F841 — wires signals

    # Simulator startup: Event_Join + first pack, before the overlay attaches.
    writer.write_event_join()
    sim_window.present_current_pack()

    app = _build_app(qapp, sim_log, grpid_to_name)
    app.watcher.start()
    try:
        _pump(qapp, 0.3)  # replay Event_Join + P1P1, flip to live

        # Draft the whole pack by picking the first card each pass.
        guard = 0
        while not engine.is_draft_complete and guard < 200:
            guard += 1
            pack = engine.get_current_pack()
            assert pack, "pack empty before completion"
            sim_window.select_card(pack[0].name)
            sim_window._on_confirm()      # writes pick + next pack to the log
            _pump(qapp, 0.12)

        _pump(qapp, 0.4)
    finally:
        app.watcher.stop()
        sim_window.close()

    assert engine.is_draft_complete
    assert len(app.state.pool) == expected_picks, (
        f"overlay pool {len(app.state.pool)} != drafted {expected_picks}"
    )
    assert app.window.pack_tab._nav_prev.isEnabled(), "nav arrows stayed disabled"
    assert app.window.tabs.isTabVisible(app.window._tab_summary_idx), (
        "summary tab never became visible"
    )
    assert app.window.tabs.currentIndex() == app.window._tab_summary_idx, (
        "overlay did not focus the summary tab after the last pick"
    )
