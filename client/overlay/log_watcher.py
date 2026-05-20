"""Tail MTG Arena's Player.log and emit draft-relevant events."""

from __future__ import annotations

import getpass
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Log path detection
# ---------------------------------------------------------------------------

import subprocess
import sys as _sys

CURRENT_LOG = "Player.log"
PREVIOUS_LOG = "Player-prev.log"


def _candidate_log_paths() -> list[str]:
    """Build a list of candidate Player.log paths for the current platform."""
    candidates: list[str] = []
    home = Path.home()

    if _sys.platform == "win32":
        appdata_low = os.path.join(
            "users", getpass.getuser(), "AppData", "LocalLow",
        )
        log_intermediate = os.path.join("Wizards Of The Coast", "MTGA")
        for drive in ("C:/", "D:/"):
            candidates.append(
                os.path.join(drive, appdata_low, log_intermediate, CURRENT_LOG)
            )
    elif _sys.platform == "darwin":
        # macOS: Wine / CrossOver / Game Porting Toolkit / native (if ever)
        # Unity's default Player.log location on macOS — most third-party
        # MTGA tools cite this path for native/GPTK installs.
        candidates.append(
            str(home / "Library" / "Logs"
                / "Wizards Of The Coast" / "MTGA" / CURRENT_LOG)
        )
        # Wizards' macOS support docs and Epic/native installs place the
        # log under the app-support bundle with a duplicated Logs/Logs
        # segment.
        candidates.append(
            str(home / "Library" / "Application Support"
                / "com.wizards.mtga" / "Logs" / "Logs" / CURRENT_LOG)
        )
        candidates.append(
            str(home / "Library" / "Application Support"
                / "com.wizards.mtga" / "Logs" / CURRENT_LOG)
        )
        candidates.append(
            str(home / "Library" / "Application Support"
                / "com.wizards.mtga" / CURRENT_LOG)
        )
        # Wine prefix default location
        candidates.append(
            str(home / ".wine" / "drive_c" / "users" / getpass.getuser()
                / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA"
                / CURRENT_LOG)
        )
    else:
        # Linux: Lutris / Wine / Proton / Flatpak
        candidates.append(
            str(home / ".wine" / "drive_c" / "users" / getpass.getuser()
                / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA"
                / CURRENT_LOG)
        )
        # Steam Proton common prefix
        steam = home / ".steam" / "steam" / "steamapps" / "compatdata"
        if steam.is_dir():
            for prefix_dir in steam.iterdir():
                pfx = (
                    prefix_dir / "pfx" / "drive_c" / "users" / "steamuser"
                    / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA"
                    / CURRENT_LOG
                )
                candidates.append(str(pfx))
        # Lutris default prefix
        candidates.append(
            str(home / "Games" / "magic-the-gathering-arena" / "drive_c"
                / "users" / getpass.getuser()
                / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA"
                / CURRENT_LOG)
        )

    return candidates


def find_log_path() -> Path | None:
    """Auto-detect the Arena Player.log path."""
    for p in _candidate_log_paths():
        if os.path.exists(p):
            return Path(p)
    return None


def is_arena_running() -> bool:
    """Check whether the MTG Arena process is currently running."""
    if _sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq MTGA.exe", "/NH"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return "MTGA.exe" in result.stdout
        except Exception:
            return False
    else:
        try:
            result = subprocess.run(
                ["pgrep", "-fi", "MTGA"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except FileNotFoundError:
            try:
                result = subprocess.run(
                    ["ps", "aux"],
                    capture_output=True, text=True, timeout=5,
                )
                return "MTGA" in result.stdout
            except Exception:
                return False
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Static log-header extraction helpers
# ---------------------------------------------------------------------------

_MONO_PATH_RE = re.compile(r"Mono path\[0\]\s*=\s*'([^']+)'")
_AUTH_RESPONSE_RE = re.compile(
    r'"authenticateResponse"\s*:\s*\{[^}]*"clientId"\s*:\s*"([^"]+)"'
)


def extract_mtga_install_dir(log_path: Path | None = None) -> Path | None:
    """Derive the MTGA installation directory from the Player.log header.

    The very first line of every Player.log looks like:
      ``Mono path[0] = 'C:/Program Files/.../MTGA/MTGA_Data/Managed'``

    This strips the ``MTGA_Data/Managed`` suffix to return the MTGA root.

    Args:
        log_path: Explicit path, or auto-detected via :func:`find_log_path`.

    Returns:
        The MTGA install directory (e.g. ``C:/Program Files/.../MTGA``),
        or *None* if the log cannot be read or parsed.
    """
    if log_path is None:
        log_path = find_log_path()
    if log_path is None:
        return None
    try:
        with open(log_path, errors="replace") as f:
            first_line = f.readline()
        m = _MONO_PATH_RE.search(first_line)
        if not m:
            return None
        managed = Path(m.group(1))
        # Strip MTGA_Data/Managed (or whatever two trailing segments)
        install_dir = managed.parent.parent
        if install_dir.exists():
            return install_dir
        return None
    except Exception:
        logger.debug("Failed to extract MTGA install dir from %s", log_path, exc_info=True)
        return None


def extract_arena_player_id(log_path: Path | None = None) -> str | None:
    """Extract the player's stable Arena client ID from Player.log.

    Scans for the ``authenticateResponse`` JSON whose ``clientId`` field
    is a short, stable identifier for the Arena account (e.g.
    ``"PVWXM52WLBD5PCX4SRIS7FPYKY"``).

    When Arena uses a saved refresh token ("fast login"), the
    ``authenticateResponse`` is not written to the current log.  In that
    case we fall back to ``Player-prev.log`` from the previous session.

    Args:
        log_path: Explicit path, or auto-detected via :func:`find_log_path`.

    Returns:
        The ``clientId`` string, or *None* if not found.
    """
    if log_path is None:
        log_path = find_log_path()
    if log_path is None:
        return None

    # Try the current log first, then the previous log as fallback.
    paths_to_check = [log_path]
    prev_log = log_path.parent / PREVIOUS_LOG
    if prev_log.exists():
        paths_to_check.append(prev_log)

    for path in paths_to_check:
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    m = _AUTH_RESPONSE_RE.search(line)
                    if m:
                        logger.debug("Found Arena player ID in %s", path.name)
                        return m.group(1)
        except Exception:
            logger.debug("Failed to read %s for player ID", path, exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Regex patterns (adapted from 17Lands mtga_follower.py)
# ---------------------------------------------------------------------------

LOG_START_REGEX_TIMED = re.compile(
    r"^\[(UnityCrossThreadLogger|Client GRE)\](\d[\d:/ .-]+(AM|PM)?)"
)
LOG_START_REGEX_UNTIMED = re.compile(
    r"^\[(UnityCrossThreadLogger|Client GRE)\]"
)
JSON_START_REGEX = re.compile(r"[\[\{]")


# ---------------------------------------------------------------------------
# Event dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DraftStartEvent:
    """Emitted when the player joins a draft pod."""
    event_name: str


@dataclass
class PackEvent:
    """Emitted when a new pack is presented to the player."""
    card_grpids: list[int]
    pack_number: int
    pick_number: int
    event_name: str = ""
    picked_grpids: list[int] = field(default_factory=list)


@dataclass
class PickEvent:
    """Emitted when the player (or autopick) makes a pick."""
    card_grpids: list[int]
    pack_number: int = -1
    pick_number: int = -1


@dataclass
class ReplayDoneEvent:
    """Emitted once after the initial log scan finishes replaying historical entries."""


@dataclass
class LogRotatedEvent:
    """Emitted when the Arena log is truncated/rotated (Arena restarted)."""


@dataclass
class DraftEndEvent:
    """Emitted when the player leaves the draft screen."""


@dataclass
class DraftCompleteEvent:
    """Emitted when the draft portion finishes (all picks made)."""


@dataclass
class DraftLobbyEvent:
    """Emitted when the player navigates to a draft event landing page."""
    context: str


@dataclass
class DeckPoolDetectedEvent:
    """Emitted when MTGA's deck-builder is showing a draft event pool.

    Fires once per transition into the deck-builder for a draft event —
    typically after the player has re-opened MTGA into a draft they
    completed earlier and the overlay's on-disk cache is empty or stale.
    Memory is authoritative here because the log from the prior session
    has rotated to Player-prev.log by the time this fires.
    """
    card_grpids: list[int]
    event_name: str = ""


DraftEvent = (
    DraftStartEvent | PackEvent | PickEvent | ReplayDoneEvent
    | LogRotatedEvent | DraftEndEvent | DraftCompleteEvent | DraftLobbyEvent
    | DeckPoolDetectedEvent
)

EventCallback = Callable[[DraftEvent], Any]


def _contains_log_key(key: str, full_log: str) -> bool:
    """Check if key exists in log string, with or without underscores."""
    return key in full_log or key.replace("_", "") in full_log


def _extract_payload(blob: Any, decoder: json.JSONDecoder) -> Any:
    """Recursively extract nested payloads from serialised JSON."""
    if not isinstance(blob, dict):
        return blob
    if "clientToMatchServiceMessageType" in blob:
        return blob
    for key in ("payload", "Payload", "request"):
        if key in blob:
            inner = blob[key]
            if isinstance(inner, str):
                try:
                    inner, _ = decoder.raw_decode(inner)
                except (json.JSONDecodeError, ValueError):
                    pass
            return _extract_payload(inner, decoder)
    return blob


def _first_string(blob: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string value for *keys*.

    Args:
        blob: Parsed JSON object.
        keys: Candidate key names in priority order.

    Returns:
        The first stripped string value, or an empty string.
    """
    for key in keys:
        value = blob.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def _is_draft_lobby_context(context: str) -> bool:
    """Return whether a scene context looks like a draft/sealed event.

    Args:
        context: Arena scene or event context string.

    Returns:
        ``True`` for draft or sealed event landing contexts.
    """
    lowered = context.lower()
    return any(keyword in lowered for keyword in ("draft", "sealed"))


# ---------------------------------------------------------------------------
# LogWatcher
# ---------------------------------------------------------------------------

class LogWatcher:
    """Tails Arena's Player.log and emits draft events via callbacks.

    Args:
        log_path: Path to Player.log (auto-detected if None).
        poll_interval: Seconds between file polls.
    """

    def __init__(
        self,
        log_path: Path | None = None,
        poll_interval: float = 0.5,
        *,
        always_replay: bool = False,
    ) -> None:
        if log_path is None:
            log_path = find_log_path()
        self.log_path = log_path
        self.poll_interval = poll_interval
        self._always_replay = always_replay

        self._callbacks: list[EventCallback] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._decoder = json.JSONDecoder()

        # Line-buffering state
        self._buffer: list[str] = []
        self._cur_draft_event: str = ""

        # True while replaying historical log entries on startup.
        self.replaying: bool = False

    # -- public API ----------------------------------------------------------

    def add_callback(self, cb: EventCallback) -> None:
        self._callbacks.append(cb)

    def start(self) -> None:
        """Start watching in a background daemon thread."""
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="log-watcher")
        self._thread.start()
        logger.info("LogWatcher started on %s", self.log_path)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        logger.info("LogWatcher stopped")

    # -- internals -----------------------------------------------------------

    _MAX_PACK = 2  # 3-pack draft, 0-indexed
    _MAX_PICK = 14  # 15-card pack, 0-indexed

    def _emit(self, event: DraftEvent) -> None:
        # Phantom-frame filter for PackEvent/PickEvent. Arena log paths
        # disagree on indexing at draft boundaries (bot draft `PackNumber`
        # is 0-indexed; human draft `SelfPack` sometimes emits values
        # beyond the 3-pack envelope at draft end). Drop frames outside
        # the valid range so downstream consumers (DraftLogger,
        # OverlayPredictor) don't record bogus "Pack 4" rows.
        pack = getattr(event, "pack_number", None)
        pick = getattr(event, "pick_number", None)
        if isinstance(pack, int) and isinstance(pick, int):
            if not (0 <= pack <= self._MAX_PACK and 0 <= pick <= self._MAX_PICK):
                logger.debug(
                    "LogWatcher: dropping out-of-range %s pack=%d pick=%d",
                    type(event).__name__, pack, pick,
                )
                return
        for cb in self._callbacks:
            try:
                cb(event)
            except Exception:
                logger.exception("Error in event callback")

    def _emit_replay_done(self) -> None:
        """Notify listeners that the initial log replay is complete."""
        self._emit(ReplayDoneEvent())

    def _run(self) -> None:
        """Main polling loop — tail the log file."""
        last_size = 0
        initial_scan = True
        while not self._stop.is_set():
            try:
                # If no log path is known yet, keep searching.
                if self.log_path is None:
                    self.log_path = find_log_path()
                    if self.log_path is None:
                        time.sleep(self.poll_interval * 2)
                        continue
                    logger.info("Arena log found at %s", self.log_path)

                if not self.log_path.exists():
                    time.sleep(self.poll_interval)
                    continue

                cur_size = self.log_path.stat().st_size
                if cur_size < last_size:
                    # File was rotated / truncated — start from beginning
                    logger.info("Log file rotated, resetting position")
                    last_size = 0
                    self._cur_draft_event = ""
                    self._emit(LogRotatedEvent())

                if cur_size == last_size:
                    if initial_scan:
                        # First scan complete — switch from replay to live mode.
                        initial_scan = False
                        if self.replaying:
                            self.replaying = False
                            logger.info("Initial log replay complete")
                            self._emit_replay_done()
                    time.sleep(self.poll_interval)
                    continue

                if initial_scan and last_size == 0 and cur_size > 0:
                    if not self._always_replay and not is_arena_running():
                        logger.info(
                            "Arena not running — skipping log replay, "
                            "waiting for Arena to start",
                        )
                        last_size = cur_size
                        initial_scan = False
                        self._emit_replay_done()
                        time.sleep(self.poll_interval)
                        continue
                    self.replaying = True

                with open(self.log_path, errors="replace") as f:
                    f.seek(last_size)
                    new_data = f.read()
                    last_size = f.tell()

                for line in new_data.splitlines(keepends=True):
                    self._append_line(line)
                # Flush any trailing entry
                self._handle_complete_entry()

            except Exception:
                logger.exception("Error in log watcher loop")
                time.sleep(self.poll_interval)

    def _append_line(self, line: str) -> None:
        """Buffer lines until a complete log entry is ready."""
        match = LOG_START_REGEX_UNTIMED.match(line)
        if match:
            # New entry starts — flush previous
            self._handle_complete_entry()
            timed = LOG_START_REGEX_TIMED.match(line)
            if timed:
                self._buffer.append(line[timed.end():])
            else:
                self._buffer.append(line[match.end():])
        else:
            self._buffer.append(line)

    def _handle_complete_entry(self) -> None:
        """Parse a complete log entry and dispatch if draft-relevant."""
        if not self._buffer:
            return

        full_log = "".join(self._buffer)
        self._buffer.clear()

        match = JSON_START_REGEX.search(full_log)
        if not match:
            return

        try:
            json_obj, _ = self._decoder.raw_decode(full_log, match.start())
        except (json.JSONDecodeError, ValueError):
            return

        json_obj = _extract_payload(json_obj, self._decoder)
        if not isinstance(json_obj, dict):
            return

        self._dispatch(json_obj, full_log)

    def _dispatch(self, blob: dict[str, Any], full_log: str) -> None:
        """Route a parsed JSON blob to the appropriate handler."""
        # Scene change — detect leaving the draft screen or entering/leaving lobby.
        if "SceneChange" in full_log:
            from_scene = _first_string(
                blob, "fromSceneName", "FromSceneName", "fromScene", "from",
            )
            to_scene = _first_string(
                blob, "toSceneName", "ToSceneName", "toScene", "to", "sceneName", "SceneName",
            )
            if from_scene == "Draft" and to_scene != "Draft":
                logger.info("Left draft screen \u2192 %s", to_scene)
                self._emit(DraftEndEvent())
            elif to_scene == "EventLanding":
                ctx = _first_string(
                    blob, "context", "Context", "eventName", "EventName", "eventId", "EventId",
                )
                if _is_draft_lobby_context(ctx):
                    logger.info("Draft lobby detected: context=%s", ctx)
                    self._emit(DraftLobbyEvent(context=ctx))
            elif from_scene == "EventLanding" and to_scene != "Draft":
                logger.info("Left draft lobby \u2192 %s", to_scene)
                self._emit(DraftLobbyEvent(context=""))
            return

        # Human draft completion (DraftCompleteDraft).
        if _contains_log_key("DraftCompleteDraft", full_log):
            logger.info("Human draft complete")
            self._emit(DraftCompleteEvent())
            return

        # Event join
        if _contains_log_key("Event_Join", full_log) and "EventName" in blob:
            self._handle_event_join(blob)

        # Bot draft pack
        elif "DraftStatus" in blob:
            self._handle_bot_draft_pack(blob)

        # Bot draft pick
        elif _contains_log_key("BotDraft_DraftPick", full_log) and "PickInfo" in blob:
            self._handle_bot_draft_pick(blob["PickInfo"])

        # Human draft combined (LogBusinessEvents with pack + pick)
        elif _contains_log_key("LogBusinessEvents", full_log) and "PickGrpId" in blob:
            self._handle_human_draft_combined(blob)

        # Human draft pack (Draft.Notify)
        elif "Draft.Notify " in full_log and "method" not in blob:
            self._handle_human_draft_pack(blob)

        # Human draft pick (EventPlayerDraftMakePick)
        elif (
            _contains_log_key("EventPlayerDraftMakePick", full_log)
            and "GrpIds" in blob
        ):
            self._handle_player_draft_pick(blob)

    # -- handlers ------------------------------------------------------------

    def _handle_event_join(self, blob: dict[str, Any]) -> None:
        event_name = blob["EventName"]
        self._cur_draft_event = event_name
        logger.info("Joined draft event: %s", event_name)
        self._emit(DraftStartEvent(event_name=event_name))

    def _handle_bot_draft_pack(self, blob: dict[str, Any]) -> None:
        status = blob.get("DraftStatus")
        if status == "Complete":
            logger.info("Bot draft complete")
            self._emit(DraftCompleteEvent())
            return
        if status != "PickNext":
            return
        self._cur_draft_event = blob.get("EventName", self._cur_draft_event)
        card_ids = [int(x) for x in blob["DraftPack"]]
        pack_num = int(blob["PackNumber"])
        pick_num = int(blob["PickNumber"])
        picked_ids = [int(x) for x in blob.get("PickedCards", [])]
        logger.info("Bot draft pack: P%dP%d (%d cards, %d picked)",
                     pack_num + 1, pick_num + 1, len(card_ids), len(picked_ids))
        self._emit(PackEvent(
            card_grpids=card_ids,
            pack_number=pack_num,
            pick_number=pick_num,
            event_name=self._cur_draft_event,
            picked_grpids=picked_ids,
        ))

    def _handle_bot_draft_pick(self, blob: dict[str, Any]) -> None:
        card_id = blob.get("CardId")
        card_ids_raw = blob.get("CardIds")
        if card_ids_raw:
            card_ids = [int(x) for x in card_ids_raw]
        elif card_id is not None:
            card_ids = [int(card_id)]
        else:
            return
        pack_num = int(blob.get("PackNumber", -1))
        pick_num = int(blob.get("PickNumber", -1))
        logger.info("Bot draft pick: %s", card_ids)
        self._emit(PickEvent(card_grpids=card_ids, pack_number=pack_num, pick_number=pick_num))

    def _handle_human_draft_pack(self, blob: dict[str, Any]) -> None:
        card_ids = [int(x) for x in blob["PackCards"].split(",")]
        pack_num = int(blob["SelfPack"])
        pick_num = int(blob["SelfPick"])
        logger.info("Human draft pack: P%dP%d (%d cards)", pack_num + 1, pick_num + 1, len(card_ids))
        self._emit(PackEvent(
            card_grpids=card_ids,
            pack_number=pack_num,
            pick_number=pick_num,
            event_name=self._cur_draft_event,
        ))

    def _handle_human_draft_combined(self, blob: dict[str, Any]) -> None:
        self._cur_draft_event = blob.get("EventId", self._cur_draft_event)
        card_ids = [int(x) for x in blob["CardsInPack"]]
        pack_num = int(blob["PackNumber"])
        pick_num = int(blob["PickNumber"])
        logger.info("Human draft combined pack: P%dP%d (%d cards)", pack_num + 1, pick_num + 1, len(card_ids))
        self._emit(PackEvent(
            card_grpids=card_ids,
            pack_number=pack_num,
            pick_number=pick_num,
            event_name=self._cur_draft_event,
        ))

        pick_grpid = blob.get("PickGrpId")
        if pick_grpid and int(pick_grpid) != 0:
            self._emit(PickEvent(
                card_grpids=[int(pick_grpid)],
                pack_number=pack_num,
                pick_number=pick_num,
            ))

    def _handle_player_draft_pick(self, blob: dict[str, Any]) -> None:
        card_ids = [int(x) for x in blob["GrpIds"]]
        pack_num = int(blob.get("Pack", -1))
        pick_num = int(blob.get("Pick", -1))
        logger.info("Human draft pick: %s", card_ids)
        self._emit(PickEvent(card_grpids=card_ids, pack_number=pack_num, pick_number=pick_num))
