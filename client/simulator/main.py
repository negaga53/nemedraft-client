"""Draft simulator — emulates MTG Arena's Player.log for the overlay.

The simulator writes Arena-format log entries to a file that the
already-running overlay's :class:`LogWatcher` can tail.  No model,
predictor, or overlay window is bundled — the simulator is purely
a UI + draft engine + log writer.

Usage::

    nemedraft-simulator --set TMT
    nemedraft-simulator --set FIN --seed 42
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication

logger = logging.getLogger("simulator")

# Paths for the simulator's fake log and lock file.
_CACHE_DIR = Path("data/cache")
_LOG_PATH = _CACHE_DIR / "simulator.log"
_LOCK_PATH = _CACHE_DIR / "simulator.lock"


# ---------------------------------------------------------------------------
# Lock / log file helpers
# ---------------------------------------------------------------------------


def _write_lock(set_code: str) -> None:
    """Create a lock file so the overlay can detect the simulator."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _LOCK_PATH.write_text(
        json.dumps({
            "set_code": set_code,
            "pid": os.getpid(),
            "log_path": str(_LOG_PATH.resolve()),
        }),
        encoding="utf-8",
    )


def _remove_lock() -> None:
    """Remove the simulator lock and grpId map files on exit."""
    try:
        _LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        _GRPID_MAP_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def read_simulator_lock() -> dict | None:
    """Read the simulator lock file and return its contents if valid.

    Returns:
        A dict with ``set_code``, ``pid``, and ``log_path`` keys, or
        ``None`` if the simulator is not running.
    """
    try:
        if not _LOCK_PATH.exists():
            return None
        data = json.loads(_LOCK_PATH.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if pid is None:
            return None
        # Verify the process is still alive.
        if sys.platform == "win32":
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x0400, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return data
            return None
        else:
            os.kill(pid, 0)
            return data
    except (ProcessLookupError, PermissionError):
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Arena log writer — emits the same JSON the overlay's LogWatcher expects
# ---------------------------------------------------------------------------


# Synthetic grpId range for cards without an Arena ID.
_SYNTHETIC_GRPID_BASE = 100_000

_GRPID_MAP_PATH = _CACHE_DIR / "simulator_grpid_map.json"


def _load_name_to_grpid(scryfall_path: Path) -> dict[str, int]:
    """Build a card-name → arena grpId mapping from a Scryfall JSON.

    Cards without an ``arena_id`` receive a deterministic synthetic ID
    derived from their name so the overlay can map grpIds back to names.
    The full mapping is written to :data:`_GRPID_MAP_PATH` for the
    overlay to load when it switches to simulator mode.
    """
    with open(scryfall_path, encoding="utf-8") as f:
        cards = json.load(f)
    mapping: dict[str, int] = {}
    next_synthetic = _SYNTHETIC_GRPID_BASE
    for c in cards:
        arena_id = c.get("arena_id")
        name = c.get("name", "")
        if not name:
            continue
        if name in mapping:
            continue
        if arena_id:
            mapping[name] = int(arena_id)
        else:
            mapping[name] = next_synthetic
            next_synthetic += 1

    # Persist grpId→name mapping so the overlay can import it.
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    grpid_to_name = {str(gid): name for name, gid in mapping.items()}
    _GRPID_MAP_PATH.write_text(
        json.dumps(grpid_to_name, ensure_ascii=False), encoding="utf-8",
    )
    return mapping


class ArenaLogWriter:
    """Writes Arena-format log entries that the overlay's LogWatcher can parse.

    Uses the bot-draft ``DraftStatus`` / ``BotDraft_DraftPick`` format
    because it carries all info in a single JSON blob (pack contents,
    pack/pick numbers, picked cards).

    Args:
        log_path: File to append log entries to.
        name_to_grpid: Card name → Arena grpId mapping.
        event_name: Draft event name (e.g. ``"PremierDraft_TMT_sim"``).
    """

    def __init__(
        self,
        log_path: Path,
        name_to_grpid: dict[str, int],
        event_name: str,
    ) -> None:
        self._path = log_path
        self._name_to_grpid = name_to_grpid
        self._event_name = event_name
        self._picked_grpids: list[int] = []

        # Truncate file on creation so the LogWatcher starts clean.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("", encoding="utf-8")

    def _write_entry(self, payload: str) -> None:
        """Append a ``[UnityCrossThreadLogger]`` line to the log."""
        with open(self._path, "a", encoding="utf-8") as f:
            ts = time.strftime("%m/%d/%Y %I:%M:%S %p")
            f.write(f"[UnityCrossThreadLogger]{ts}\n")
            f.write(payload)
            f.write("\n")

    def _names_to_grpids(self, names: list[str]) -> list[int]:
        """Convert card names to grpIds."""
        ids: list[int] = []
        for n in names:
            gid = self._name_to_grpid.get(n)
            if gid is not None:
                ids.append(gid)
            else:
                logger.warning("No grpId for %r — card will be missing from pack", n)
        return ids

    def write_event_join(self) -> None:
        """Emit an Event_Join entry to start the draft session."""
        blob = {"EventName": self._event_name, "Event_Join": True}
        self._write_entry(json.dumps(blob))

    def write_pack(
        self,
        card_names: list[str],
        pack_number: int,
        pick_number: int,
    ) -> None:
        """Emit a DraftStatus PickNext entry for the current pack."""
        card_ids = self._names_to_grpids(card_names)
        blob = {
            "DraftStatus": "PickNext",
            "EventName": self._event_name,
            "DraftPack": [str(x) for x in card_ids],
            "PackNumber": pack_number,
            "PickNumber": pick_number,
            "PickedCards": [str(x) for x in self._picked_grpids],
        }
        self._write_entry(json.dumps(blob))

    def write_pick(self, card_name: str) -> None:
        """Emit a BotDraft_DraftPick entry for the chosen card."""
        gid = self._name_to_grpid.get(card_name)
        if gid is None:
            gid = abs(hash(card_name)) % 900_000 + 100_000
        self._picked_grpids.append(gid)
        blob = {
            "BotDraft_DraftPick": True,
            "PickInfo": {
                "CardId": gid,
                "CardIds": [gid],
                "PackNumber": -1,
                "PickNumber": -1,
            },
        }
        self._write_entry(json.dumps(blob))

    def write_draft_complete(self) -> None:
        """Emit a DraftStatus Complete entry."""
        blob = {
            "DraftStatus": "Complete",
            "EventName": self._event_name,
            "DraftPack": [],
            "PackNumber": 0,
            "PickNumber": 0,
            "PickedCards": [str(x) for x in self._picked_grpids],
        }
        self._write_entry(json.dumps(blob))


# ---------------------------------------------------------------------------
# Bridge: SimulatorWindow signals → ArenaLogWriter
# ---------------------------------------------------------------------------


class DraftSimulatorBridge:
    """Connects the simulator UI to the log writer.

    When a pack is presented or a pick is made, this bridge writes the
    corresponding Arena-format log entry so the overlay picks it up.
    """

    def __init__(
        self,
        sim_window,          # SimulatorWindow
        log_writer: ArenaLogWriter,
    ) -> None:
        self._sim = sim_window
        self._log = log_writer

        self._sim.pack_presented.connect(self._on_pack)
        self._sim.pick_confirmed.connect(self._on_pick)
        self._sim.skip_draft_requested.connect(self._on_skip_draft)

        self._skipping = False

    def _on_pack(
        self,
        card_names: list[str],
        pack_number: int,
        pick_number: int,
    ) -> None:
        self._log.write_pack(card_names, pack_number, pick_number)

    def _on_pick(self, card_name: str) -> None:
        self._log.write_pick(card_name)

        if self._sim._engine.is_draft_complete:
            self._log.write_draft_complete()

    def _on_skip_draft(self) -> None:
        """Auto-pick using the bot's pick logic for each remaining pack."""
        self._skipping = True
        logger.info("Skipping draft — auto-picking remaining packs")

        while not self._sim._engine.is_draft_complete:
            pack = self._sim._engine.get_current_pack()
            if not pack:
                break

            # Write pack to log so the overlay sees it.
            pn = self._sim._engine.pack_number
            pk = self._sim._engine.pick_number
            names = [c.name for c in pack]
            self._log.write_pack(names, pn, pk)

            # Use the engine's bot AI to pick a reasonable card.
            pick = self._sim._engine._bot_pick(pack, self._sim._engine.player_pool)
            self._sim._engine.player_pick(pick.name)
            self._log.write_pick(pick.name)

            # Update the simulator's deck panel.
            self._sim._deck_panel.add_card(pick)

            QApplication.processEvents()

        self._log.write_draft_complete()
        self._sim.present_current_pack()  # shows the complete screen
        self._skipping = False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Draft simulator — emulates Arena for the NemeDraft overlay",
    )
    parser.add_argument(
        "--set",
        default="TMT",
        help="Set code to draft (default: TMT)",
    )
    parser.add_argument(
        "--scryfall-dir",
        default="data/scryfall",
        help="Scryfall JSON data directory",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for draft generation (for reproducibility)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    app = QApplication(sys.argv)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    set_code = args.set.upper()
    scryfall_path = Path(args.scryfall_dir) / f"{set_code.lower()}_cards.json"
    if not scryfall_path.exists():
        logger.error("Scryfall data not found for %s at %s", set_code, scryfall_path)
        sys.exit(1)

    # Build name → grpId mapping for Arena-format log entries.
    name_to_grpid = _load_name_to_grpid(scryfall_path)
    logger.info("Loaded %d name→grpId mappings for %s", len(name_to_grpid), set_code)

    # Write lock file so the overlay can detect us and find the log.
    _write_lock(set_code)
    atexit.register(_remove_lock)

    # Create Arena-format log writer.
    event_name = f"QuickDraft_{set_code}_Simulated"
    log_writer = ArenaLogWriter(_LOG_PATH, name_to_grpid, event_name)

    # --- Draft engine ---
    from client.simulator.engine import DraftEngine, load_set_cards

    logger.info("Loading cards for %s from %s", set_code, scryfall_path)
    cards = load_set_cards(scryfall_path, set_code)
    total = sum(len(v) for v in cards.values())
    logger.info("Loaded %d cards (%s)", total, {k: len(v) for k, v in cards.items()})

    engine = DraftEngine(cards, set_code, seed=args.seed)

    # --- Simulator window ---
    from client.simulator.window import SimulatorWindow

    sim_window = SimulatorWindow(engine)
    sim_window.show()
    app.processEvents()

    # Wire simulator signals → log writer.
    bridge = DraftSimulatorBridge(sim_window, log_writer)

    # Write draft start, then present the first pack.
    log_writer.write_event_join()
    sim_window.present_current_pack()

    exit_code = app.exec()

    _remove_lock()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
