"""Overlay application entry point — wires log watcher, state, and server API client."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from typing import Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from client.overlay.api_client import NemeDraftClient
from client.overlay.arena_memory import (
    ArenaCurrentEvent,
    ArenaPlayerIdentity,
    get_arena_current_event,
    get_arena_player_identity,
)
from client.overlay.auth_client import AuthClient
from client.overlay.card_art import CardArtCache
from client.overlay.card_mapper import ArenaCardMapper
from client.overlay.config import OverlayConfig, load_config, save_config, LOG_FILE
from client.overlay.draft_state import DraftState, PickHistoryEntry, extract_draft_format
from client.overlay.env import ClientEnv, load_client_env, save_arena_player_id
from client.overlay.i18n import Translator, tr
from client.overlay.log_watcher import (
    DeckPoolDetectedEvent,
    DraftCompleteEvent,
    DraftEndEvent,
    DraftEvent,
    DraftLobbyEvent,
    DraftStartEvent,
    LogRotatedEvent,
    LogWatcher,
    PackEvent,
    PickEvent,
    ReplayDoneEvent,
    extract_arena_player_id,
    is_arena_running,
)
from client.overlay.memory.platform import is_memory_supported
from client.overlay.memory_watcher import MemoryWatcher
from client.overlay.ui.window import OverlayWindow

logger = logging.getLogger("overlay")


# ---------------------------------------------------------------------------
# Auto-update worker — runs before boot to check / apply updates
# ---------------------------------------------------------------------------

class _UpdateWorker(QThread):
    """Checks for a newer release on GitHub and downloads it if found.

    Emits ``progress`` with human-readable status strings, then either
    ``update_ready`` (with the path to the new binary) or ``no_update``
    when no action is needed.
    """

    progress = Signal(str)
    update_ready = Signal(object)   # Path to downloaded binary
    no_update = Signal()
    update_failed = Signal(str)

    def run(self) -> None:  # noqa: D401
        from client.overlay.updater import check_for_update, download_update
        from client.overlay.i18n import tr

        self.progress.emit(tr("update_checking"))
        try:
            result = check_for_update()
        except Exception as exc:
            logger.warning("Update check failed: %s", exc)
            self.no_update.emit()
            return

        if result is None:
            self.no_update.emit()
            return

        latest_version, download_url = result
        self.progress.emit(tr("update_downloading", version=latest_version))

        try:
            new_binary = download_update(
                download_url,
                progress_callback=self._on_download_progress,
            )
        except Exception as exc:
            logger.error("Update download failed: %s", exc, exc_info=True)
            self.update_failed.emit(str(exc))
            return

        self.update_ready.emit(new_binary)

    def _on_download_progress(self, downloaded: int, total: int) -> None:
        from client.overlay.i18n import tr
        pct = int(downloaded / total * 100)
        self.progress.emit(tr("update_download_progress", pct=pct))


# ---------------------------------------------------------------------------
# Dataclass to bundle results from the background boot worker
# ---------------------------------------------------------------------------

@dataclass
class _BootResult:
    """Components built during background startup."""

    mapper: ArenaCardMapper
    auth_client: AuthClient
    api_client: NemeDraftClient
    art_cache: CardArtCache
    watcher: LogWatcher
    memory_watcher: "MemoryWatcher | None"
    server_supported_sets: list[str]
    has_arena_player_id: bool


@dataclass(frozen=True)
class _ArenaIdentityResolution:
    """Arena player identity resolved from memory or Player.log."""

    player_id: str
    source: str
    display_name: str = ""


def _resolve_arena_identity() -> _ArenaIdentityResolution | None:
    """Resolve the Arena player ID from the best available local source."""
    arena_identity = get_arena_player_identity()
    if arena_identity is not None:
        player_id = arena_identity.player_id.strip()
        if player_id:
            return _ArenaIdentityResolution(
                player_id=player_id,
                source="memory",
                display_name=arena_identity.display_name.strip(),
            )

    log_player_id = (extract_arena_player_id() or "").strip()
    if log_player_id:
        return _ArenaIdentityResolution(player_id=log_player_id, source="log")

    return None


class _BootWorker(QThread):
    """Loads minimal resources off the UI thread.

    Card mappings and Scryfall data are **not** loaded here — they are
    deferred to :class:`_SetDataWorker` once a draft set is detected.
    """

    progress = Signal(str)          # status message for the home tab
    finished_ok = Signal(object)    # _BootResult
    finished_err = Signal(str)      # error message

    def __init__(
        self,
        args: argparse.Namespace,
        config: OverlayConfig,
        env: ClientEnv,
    ) -> None:
        super().__init__()
        self._args = args
        self._config = config
        self._env = env

    def run(self) -> None:  # noqa: D401 — Qt override
        try:
            args = self._args

            # 1. Scryfall refresh (optional) --------------------------------
            if args.update_scryfall:
                self.progress.emit("Updating Scryfall data...")
                from client.overlay.card_mapper import update_scryfall
                update_scryfall(Path(args.scryfall_dir))

            # 2. Card ID map only (lightweight) -----------------------------
            self.progress.emit("Loading card ID map...")
            mapper = ArenaCardMapper(
                scryfall_dir=Path(args.scryfall_dir),
                card_id_map_path=Path(args.card_id_map),
                lazy=True,
            )

            # Card translations for the active language.
            translator = Translator.instance()
            translator.load_card_translations(Path(args.scryfall_dir))

            # 3. Auth + API client ------------------------------------------
            self.progress.emit("Connecting to server...")
            arena_player_id = ""
            arena_identity = _resolve_arena_identity()
            if arena_identity:
                arena_player_id = arena_identity.player_id
                if arena_identity.source == "memory":
                    logger.info(
                        "Arena player ID from memory: %s (display=%s)",
                        arena_player_id,
                        arena_identity.display_name or "unknown",
                    )
                else:
                    logger.info("Arena player ID from Player.log: %s", arena_player_id)
                save_arena_player_id(arena_player_id)
            else:
                arena_player_id = os.getenv("ARENA_PLAYER_ID", "") or ""
                if arena_player_id:
                    logger.warning(
                        "Using cached Arena player ID because memory reader is unavailable: %s",
                        arena_player_id,
                    )
                else:
                    logger.warning(
                        "Arena player ID not found via MTGA memory or Player.log "
                        "- sign-in will be unavailable"
                    )
            auth_client = AuthClient(self._env, arena_player_id=arena_player_id)
            api_client = NemeDraftClient(self._env, auth_client)

            # Only attempt auto-login when we have a player ID to send
            if arena_player_id and auth_client.try_auto_login():
                logger.info("Auto-login succeeded for %s", auth_client.user_email)
            elif arena_player_id and auth_client.session:
                logger.info(
                    "Session restored but expired — will retry refresh "
                    "(email=%s)", auth_client.user_email,
                )
            elif not arena_player_id:
                logger.info("Skipping auto-login — no arena player ID")
            else:
                logger.info("No stored session — user will need to sign in")

            # Query server for supported sets.
            server_supported_sets: list[str] = []
            health = api_client.health()
            if health:
                server_supported_sets = health.get("supported_sets", [])
                logger.info("Server supported sets: %s", server_supported_sets)

            # 4. Remaining light components ----------------------------------
            art_cache = CardArtCache(enabled=self._config.overlay.show_art)
            log_path = Path(args.log_file) if args.log_file else None
            watcher = LogWatcher(log_path=log_path)
            memory_watcher = MemoryWatcher() if is_memory_supported() else None

            self.finished_ok.emit(_BootResult(
                mapper=mapper,
                auth_client=auth_client,
                api_client=api_client,
                art_cache=art_cache,
                watcher=watcher,
                memory_watcher=memory_watcher,
                server_supported_sets=server_supported_sets,
                has_arena_player_id=bool(arena_player_id),
            ))

        except Exception as exc:
            logger.exception("Boot worker failed")
            self.finished_err.emit(str(exc))


@dataclass
class _SetDataResult:
    """Data loaded for a specific set."""

    set_code: str
    scryfall_cards: dict
    mappings_added: int


class _SetDataWorker(QThread):
    """Loads set-specific card data in the background.

    Emits progress messages so the UI can show loading steps.
    """

    progress = Signal(str)
    finished_ok = Signal(object)   # _SetDataResult
    finished_err = Signal(str)

    def __init__(
        self,
        set_code: str,
        mapper: ArenaCardMapper,
        scryfall_dir: Path,
    ) -> None:
        super().__init__()
        self._set_code = set_code
        self._mapper = mapper
        self._scryfall_dir = scryfall_dir

    def run(self) -> None:  # noqa: D401
        try:
            sc = self._set_code
            self.progress.emit(f"Loading {sc} card mappings...")
            added = self._mapper.load_set(sc)
            logger.info("Loaded %d arena mappings for %s", added, sc)

            self.progress.emit(f"Loading {sc} Scryfall data...")
            from common.inference.pool_analyzer import load_scryfall_cards_for_set
            scryfall_cards = load_scryfall_cards_for_set(self._scryfall_dir, sc)

            self.progress.emit(f"Loading MTGA fallback DB...")
            self._mapper.ensure_mtga_fallback()

            self.finished_ok.emit(_SetDataResult(
                set_code=sc,
                scryfall_cards=scryfall_cards,
                mappings_added=added,
            ))
        except Exception as exc:
            logger.exception("Set data worker failed for %s", self._set_code)
            self.finished_err.emit(str(exc))


class _ArenaIdentityWorker(QThread):
    """Reads Arena identity from local sources without blocking the UI thread."""

    finished_identity = Signal(object)  # ArenaPlayerIdentity | None

    def run(self) -> None:  # noqa: D401
        try:
            self.finished_identity.emit(_resolve_arena_identity())
        except Exception:
            logger.debug("Arena memory identity retry failed", exc_info=True)
            self.finished_identity.emit(None)


class _ArenaCurrentEventWorker(QThread):
    """Reads Arena current event state without blocking the UI thread."""

    finished_event = Signal(object)  # ArenaCurrentEvent | None

    def run(self) -> None:  # noqa: D401
        try:
            self.finished_event.emit(get_arena_current_event())
        except Exception:
            logger.debug("Arena current event memory poll failed", exc_info=True)
            self.finished_event.emit(None)


_DEDUPE_WINDOW_S = 2.0


def _event_signature(event: DraftEvent) -> tuple | None:
    """Build a stable identity for cross-source duplicate detection.

    Returns ``None`` for events that are inherently unique to one source
    (``ReplayDoneEvent`` and ``LogRotatedEvent`` only LogWatcher emits) —
    those bypass dedupe.
    """
    if isinstance(event, PackEvent):
        return ("pack", event.pack_number, event.pick_number,
                tuple(event.card_grpids))
    if isinstance(event, PickEvent):
        return ("pick", event.pack_number, event.pick_number,
                tuple(event.card_grpids))
    if isinstance(event, DraftStartEvent):
        return ("start", event.event_name)
    if isinstance(event, DraftLobbyEvent):
        return ("lobby", event.context)
    if isinstance(event, DraftEndEvent):
        return ("end",)
    if isinstance(event, DraftCompleteEvent):
        return ("complete",)
    return None


def _should_drop_duplicate(
    event: DraftEvent, recent: dict[tuple, float]
) -> bool:
    sig = _event_signature(event)
    if sig is None:
        return False
    now = time.monotonic()
    cutoff = now - _DEDUPE_WINDOW_S
    # Opportunistic GC of stale entries.
    if len(recent) > 16:
        for k in [k for k, t in recent.items() if t < cutoff]:
            recent.pop(k, None)
    last = recent.get(sig)
    if last is not None and last >= cutoff:
        return True
    recent[sig] = now
    return False


class _UiMarshaler(QObject):
    """Marshal watcher-thread callbacks onto the Qt main thread.

    LogWatcher and MemoryWatcher run on plain ``threading.Thread``s and
    invoke registered callbacks inline. Mutating Qt widgets off the GUI
    thread is undefined behaviour — symptom is "Could not parse stylesheet
    of QLabel" warnings escalating to a silent C++ segfault (observed on
    leave-then-rejoin where both watchers race to deliver lobby events).

    The marshaler lives on whichever thread created it (the main thread in
    ``OverlayApp.__init__``). Cross-thread ``emit`` is queued by Qt, so the
    ``_dispatch_*`` slots always run on the GUI thread.

    The bool argument on ``event_received`` is the watcher's ``replaying``
    flag captured AT EMIT TIME. Reading it at dispatch time gives stale
    values: the watcher flips to live mode just before emitting
    ReplayDoneEvent, so queued replay events would be treated as live and
    ``show_draft_started()`` would fire while the user is on home.
    """

    event_received = Signal(object, bool)   # DraftEvent, replaying
    set_load_requested = Signal(str)         # set_code — from ensure-set-data

    def __init__(self) -> None:
        super().__init__()
        self._on_event_handler: Callable[[DraftEvent, bool], None] | None = None
        self._on_set_load_handler: Callable[[str], None] | None = None
        self.event_received.connect(self._dispatch_event)
        self.set_load_requested.connect(self._dispatch_set_load)

    def bind(
        self,
        on_event: Callable[[DraftEvent, bool], None],
        on_set_load: Callable[[str], None],
    ) -> None:
        self._on_event_handler = on_event
        self._on_set_load_handler = on_set_load

    @Slot(object, bool)
    def _dispatch_event(self, event: object, replaying: bool) -> None:
        if self._on_event_handler is not None and isinstance(event, DraftEvent):
            self._on_event_handler(event, replaying)

    @Slot(str)
    def _dispatch_set_load(self, set_code: str) -> None:
        if self._on_set_load_handler is not None:
            self._on_set_load_handler(set_code)


class OverlayApp:
    """Orchestrates all overlay components (thin client)."""

    # Retry configuration for server API calls during a draft.
    _RETRY_TIMEOUT_S = 60       # total window to keep retrying
    _RETRY_INTERVALS_MS = [     # delay before each successive retry
        2_000, 3_000, 5_000, 5_000, 10_000, 10_000, 15_000, 15_000,
    ]

    def __init__(
        self,
        mapper: ArenaCardMapper,
        api_client: NemeDraftClient,
        auth_client: AuthClient,
        watcher: LogWatcher,
        window: OverlayWindow,
        art_cache: CardArtCache,
        config: OverlayConfig,
        server_supported_sets: list[str] | None = None,
        scryfall_dir: Path | None = None,
        cache_dir: Path | None = None,
        has_arena_player_id: bool = False,
        memory_watcher: MemoryWatcher | None = None,
    ) -> None:
        self.mapper = mapper
        self.api_client = api_client
        self.auth_client = auth_client
        self.watcher = watcher
        self.memory_watcher = memory_watcher
        self.window = window
        self.art_cache = art_cache
        self.config = config
        self.scryfall_cards: dict = {}
        self.state = DraftState()
        # Dedupe map: events fired by both LogWatcher and MemoryWatcher
        # within a 2s window — first one wins, rest are dropped.
        self._recent_event_signatures: dict[tuple, float] = {}
        from client.overlay.env import bundle_root, _project_root
        self._scryfall_dir = scryfall_dir or (bundle_root() / "data" / "scryfall")
        self._cache_dir = cache_dir or (_project_root() / "data" / "cache")
        self._draft_completed = False

        # Server-supported sets (queried at boot).
        self._server_supported_sets: list[str] = server_supported_sets or []
        # Set code whose data is currently loaded (or loading).
        self._loaded_set: str = ""
        self._set_data_ready = False
        self._set_data_worker: _SetDataWorker | None = None
        # Workers still running. self._set_data_worker only points at the
        # latest one; without this set, replacing the pointer drops the
        # Python ref while QThread.run is still active and Python GCs it →
        # "QThread: Destroyed while thread is still running".
        self._inflight_set_workers: set[_SetDataWorker] = set()
        # Whether the current set is untrained (no model support).
        self._set_untrained = False
        # Current lobby context (e.g. "TMT_Quick_Draft"), empty when not in a lobby.
        self._in_lobby_context: str = ""
        # Memory-side draft lobby presence — independent of `_in_lobby_context`
        # which can be cleared by LogWatcher faster than memory polls.
        self._memory_in_draft_lobby: bool = False
        # Whether we have a valid arena player ID for auth.
        self._has_arena_player_id = has_arena_player_id
        # Consecutive failed identity reads on an attached memory session.
        self._identity_failure_count = 0
        # Events queued while set data is loading.
        # (event, replaying_at_emit_time). Snapshot lets _flush_pending_events
        # re-dispatch with the original replaying flag — otherwise the queued
        # events get treated as live after the watcher flips out of replay.
        self._pending_events: list[tuple[DraftEvent, bool]] = []
        # Deferred DeckPoolDetectedEvent — survives DraftEnd's
        # _pending_events.clear() because it represents the *current*
        # MTGA deck-builder state, not a queued log-replay event we want
        # to drop. Applied on the next _on_set_data_ready.
        self._deferred_deck_pool: DeckPoolDetectedEvent | None = None

        # Server retry state.
        self._retry_timer = QTimer()
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_prediction)
        self._retry_attempt = 0
        self._retry_start: float = 0.0

        # Marshal watcher-thread callbacks onto the Qt main thread.
        # See _UiMarshaler — registering _on_event directly on the watchers
        # mutates Qt widgets off the GUI thread and racey-crashes.
        self._marshaler = _UiMarshaler()
        self._marshaler.bind(
            on_event=self._on_event,
            on_set_load=self._ensure_set_data_on_main,
        )

        self.watcher.add_callback(self._emit_log_event)
        if self.memory_watcher is not None:
            self.memory_watcher.add_callback(self._emit_memory_event)
        self.window.settings_tab.settings_changed.connect(self._on_settings_changed)
        self.window.settings_tab.opacity_preview.connect(self.window.setWindowOpacity)
        self.window.settings_tab.language_changed.connect(self._on_language_changed)
        self.window.pack_tab.set_scryfall(self.scryfall_cards)
        self.window.pack_tab.set_art_cache(self.art_cache)

        # Wire home-tab auth buttons
        home = self.window.pack_tab.home_widget
        home.login_google_requested.connect(self._on_login_google)
        home.login_microsoft_requested.connect(self._on_login_microsoft)
        home.login_discord_requested.connect(self._on_login_discord)
        home.logout_requested.connect(self._on_logout)
        home.simulator_detected.connect(self._on_simulator_detected)

        # Start periodic server health / auth polling
        self._health_timer = QTimer()
        self._health_timer.setInterval(30_000)
        self._health_timer.timeout.connect(self._poll_server_status)
        self._health_timer.start()
        self._login_worker: QThread | None = None

        # Ref-keeper for arena memory-poll workers. Same rationale as
        # _inflight_set_workers above: the per-poll single-slot pointer can
        # be cleared by the _on_..._done slot while the worker's run() is
        # still finishing on macOS, dropping the last Python ref and
        # triggering ~QThread on a still-running thread (qFatal → abort).
        # Discard only when the QThread's own `finished` signal fires —
        # that fires after run() has fully returned.
        self._inflight_arena_workers: set[QThread] = set()

        # If the overlay starts before Arena, keep trying once Arena appears.
        self._arena_identity_worker: _ArenaIdentityWorker | None = None
        self._arena_identity_timer = QTimer()
        self._arena_identity_timer.setInterval(5_000)
        self._arena_identity_timer.timeout.connect(self._retry_arena_identity)
        if not self._has_arena_player_id:
            self._arena_identity_timer.start()

        self._arena_current_event_worker: _ArenaCurrentEventWorker | None = None
        self._arena_current_event_timer = QTimer()
        self._arena_current_event_timer.setInterval(5_000)
        self._arena_current_event_timer.timeout.connect(self._poll_arena_current_event)
        self._arena_current_event_timer.start()

        # Set initial server status
        self._poll_server_status()

    def start(self) -> None:
        self.watcher.start()
        if self.memory_watcher is not None:
            try:
                self.memory_watcher.start()
            except Exception:
                logger.exception("MemoryWatcher failed to start; continuing log-only")
        self.window.show()

    def stop(self) -> None:
        self._retry_timer.stop()
        self._arena_identity_timer.stop()
        self._arena_current_event_timer.stop()
        self.watcher.stop()
        if self.memory_watcher is not None:
            self.memory_watcher.stop()
        self.api_client.close()
        save_config(self.config)

    # -- auth helpers --------------------------------------------------------

    def _is_vip(self) -> bool:
        """Return True if the user is authenticated with VIP status."""
        if not self.auth_client.is_authenticated:
            return False
        session = self.auth_client.session
        return bool(session and session.is_vip)

    # Number of consecutive failed identity reads on an attached session
    # before forcing a re-attach. The cached image may be missing
    # assemblies that loaded after our initial attach (Core loads before
    # SharedClientCore where AccountInformation lives).
    _IDENTITY_REATTACH_AFTER = 3

    def _retry_arena_identity(self) -> None:
        """Retry the Arena player ID lookup after Arena starts."""
        if self._has_arena_player_id:
            self._arena_identity_timer.stop()
            return
        if self._arena_identity_worker is not None and self._arena_identity_worker.isRunning():
            return
        if not is_arena_running():
            return

        worker = _ArenaIdentityWorker()
        worker.finished_identity.connect(self._on_arena_identity_retry_done)
        worker.finished.connect(lambda w=worker: self._inflight_arena_workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        self._inflight_arena_workers.add(worker)
        self._arena_identity_worker = worker
        worker.start()

    def _on_arena_identity_retry_done(self, identity: object) -> None:
        """Apply an Arena identity discovered after startup.

        Args:
            identity: Worker result, expected to be ``ArenaPlayerIdentity``.

        Returns:
            None.
        """
        self._arena_identity_worker = None
        if not isinstance(identity, _ArenaIdentityResolution):
            self._on_identity_read_failed()
            return

        logger.info(
            "Arena player ID discovered after startup from %s: %s%s",
            identity.source,
            identity.player_id,
            (
                f" (display={identity.display_name or 'unknown'})"
                if identity.source == "memory"
                else ""
            ),
        )
        self._has_arena_player_id = True
        self._identity_failure_count = 0
        self._arena_identity_timer.stop()
        save_arena_player_id(identity.player_id)
        self.auth_client.set_arena_player_id(identity.player_id)
        if self.auth_client.try_auto_login():
            logger.info("Auto-login succeeded after Arena identity retry")
        self._poll_server_status()

    def _on_identity_read_failed(self) -> None:
        """Handle a failed identity read — force a session re-attach after N tries.

        The session may have been attached while MTGA was still loading
        ``SharedClientCore`` (where AccountInformation lives). Detaching
        forces the next ensure_attached to re-walk assemblies and pick up
        anything that loaded since.
        """
        from client.overlay.memory.session import MemorySession

        self._identity_failure_count += 1
        if self._identity_failure_count >= self._IDENTITY_REATTACH_AFTER:
            logger.info(
                "Arena identity not found after %d attempts — "
                "forcing memory session re-attach to refresh assemblies",
                self._identity_failure_count,
            )
            MemorySession.instance().detach()
            self._identity_failure_count = 0

    def _poll_arena_current_event(self) -> None:
        """Poll Arena memory for event lobby enter/exit state."""
        if self._arena_current_event_worker is not None and self._arena_current_event_worker.isRunning():
            return
        if not is_arena_running():
            return

        worker = _ArenaCurrentEventWorker()
        worker.finished_event.connect(self._on_arena_current_event_done)
        worker.finished.connect(lambda w=worker: self._inflight_arena_workers.discard(w))
        worker.finished.connect(worker.deleteLater)
        self._inflight_arena_workers.add(worker)
        self._arena_current_event_worker = worker
        worker.start()

    def _on_arena_current_event_done(self, current_event: object) -> None:
        """Apply current event memory state.

        Args:
            current_event: Worker result, expected to be ``ArenaCurrentEvent``.

        Returns:
            None.
        """
        self._arena_current_event_worker = None
        if not isinstance(current_event, ArenaCurrentEvent):
            return

        # Reading current event implies the memory session is attached. If
        # we still don't have an Arena player ID, retry that walk now —
        # AccountInformation may have only just populated post sign-in.
        if not self._has_arena_player_id:
            self._retry_arena_identity()

        if current_event.is_draft_lobby:
            self._memory_in_draft_lobby = True
            context = current_event.internal_event_name
            if context != self._in_lobby_context and not self.state.draft_active:
                logger.info("Draft lobby detected from memory: %s", context)
                self._on_event(DraftLobbyEvent(context=context), False)
            return

        # Memory says we are NOT in a draft event lobby. Detect leave when
        # we previously saw one — independent of `_in_lobby_context` so we
        # also catch queue exits after Event_Join (which clears that field).
        if self._memory_in_draft_lobby:
            self._memory_in_draft_lobby = False
            logger.info(
                "Left draft lobby from memory: content=%s",
                current_event.content_type or "unknown",
            )
            self._on_event(DraftLobbyEvent(context=""), False)

    # -- simulator detection -------------------------------------------------

    def _on_simulator_detected(self, log_path: str) -> None:
        """Switch the LogWatcher to tail the simulator's log file.

        Called when the home tab detects that the draft simulator has
        started and written a lock file containing *log_path*.
        """
        from pathlib import Path

        sim_log = Path(log_path)
        if not sim_log.exists():
            logger.warning("Simulator log %s does not exist yet", sim_log)
            return

        logger.info("Simulator detected \u2014 switching LogWatcher to %s", sim_log)

        # Load the simulator's grpId\u2192name mapping so the overlay can
        # convert synthetic grpIds back to card names (needed when Scryfall
        # arena_id values are absent for a set).
        grpid_map_path = sim_log.parent / "simulator_grpid_map.json"
        if grpid_map_path.exists():
            try:
                raw = json.loads(grpid_map_path.read_text(encoding="utf-8"))
                added = 0
                for gid_str, name in raw.items():
                    gid = int(gid_str)
                    if gid not in self.mapper._grpid_to_name:
                        self.mapper._grpid_to_name[gid] = name
                        self.mapper._name_to_grpid.setdefault(name, gid)
                        added += 1
                logger.info(
                    "Loaded %d simulator grpId mappings from %s",
                    added, grpid_map_path,
                )
            except Exception:
                logger.exception("Failed to load simulator grpId map")

        # Stop the current watcher (Arena log).
        self.watcher.stop()

        # Create a new watcher pointed at the simulator log.
        # always_replay=True skips the is_arena_running() gate so the
        # Event_Join + first pack already in the file are replayed.
        self.watcher = LogWatcher(log_path=sim_log, always_replay=True)
        self.watcher.add_callback(self._emit_log_event)
        self.watcher.start()

    # -- server / auth polling -----------------------------------------------

    def _poll_server_status(self) -> None:
        """Check server health and update home tab status."""
        health = self.api_client.health()
        reachable = health is not None
        maintenance = bool(health.get("maintenance")) if health else False
        # Auto-refresh expired tokens so the sign-in button doesn't
        # reappear when the user returns from a long draft.
        if (
            self._has_arena_player_id
            and self.auth_client.session
            and self.auth_client.session.is_expired
        ):
            self.auth_client.refresh()
        authed = self.auth_client.is_authenticated

        # Ask the server for the current VIP status so upgrades made
        # since the JWT was issued (and stale claims from a restored
        # session) are picked up.  On failure we keep the cached value.
        if authed and reachable and self.auth_client.session is not None:
            info = self.api_client.fetch_user_info()
            if info is not None and info.is_vip != self.auth_client.session.is_vip:
                old_is_vip = self.auth_client.session.is_vip
                # /api/me reflects the live DB value, but the JWT in
                # session.token has is_vip baked in at login time —
                # /api/predict trusts the JWT claim, so a mid-session
                # VIP promotion (or revocation) leaves the gate stale
                # until the JWT is re-issued. Refresh now so the next
                # predict call sees the new claim.
                refreshed = self.auth_client.refresh()
                if refreshed is None:
                    # No refresh token, or Supabase/server unreachable:
                    # mirror the value locally so the home tab UI is
                    # accurate, but the JWT stays stale and predict will
                    # keep being rejected until the user signs in again.
                    self.auth_client.session.is_vip = info.is_vip
                logger.info(
                    "VIP status changed: %s -> %s (jwt refresh: %s)",
                    old_is_vip, self.auth_client.session.is_vip,
                    "ok" if refreshed else "failed",
                )

        email = self.auth_client.user_email
        is_vip = (
            self.auth_client.session.is_vip
            if authed and self.auth_client.session
            else False
        )

        self.window.pack_tab.home_widget.set_server_status(
            reachable, authed, email, is_vip=is_vip,
            has_arena_player_id=self._has_arena_player_id,
            maintenance=maintenance,
        )

        # Refresh supported sets list from server health endpoint.
        if health:
            supported = health.get("supported_sets", [])
            if supported:
                self._server_supported_sets = supported

    # -- lazy set data loading -----------------------------------------------

    def _ensure_set_data(self, set_code: str) -> None:
        """Request loading of set-specific data, safe to call from any thread.

        Off-thread calls are marshalled to the Qt main thread via
        ``_UiMarshaler.set_load_requested`` (queued signal → slot on the
        QObject living on the main thread).
        """
        if not set_code:
            return

        app = QApplication.instance()
        if app is not None and QThread.currentThread() is not app.thread():
            self._marshaler.set_load_requested.emit(set_code)
            return

        self._ensure_set_data_on_main(set_code)

    def _ensure_set_data_on_main(self, set_code: str) -> None:
        """Start loading set-specific data (must be called on the main thread).

        If data for *set_code* is already loaded (or loading), this is a
        no-op.  Otherwise it spawns a :class:`_SetDataWorker` and sets the
        draft row to yellow with loading steps.
        """
        if not set_code:
            return
        if self._loaded_set == set_code and self._set_data_ready:
            return
        if (
            self._set_data_worker is not None
            and self._set_data_worker.isRunning()
            and self._loaded_set == set_code
        ):
            return  # already loading this set

        # Only drop pending events when switching sets — otherwise we race
        # with the watcher thread, which may have queued events for this
        # same set between emitting set_load_requested and this handler
        # running.
        if self._loaded_set and self._loaded_set != set_code:
            self._pending_events.clear()

        self._loaded_set = set_code
        self._set_data_ready = False
        self._set_untrained = False

        home = self.window.pack_tab.home_widget
        home.set_draft_loading(f"Detected {set_code} — loading data...")

        worker = _SetDataWorker(set_code, self.mapper, self._scryfall_dir)
        worker.progress.connect(self._on_set_data_progress)
        worker.finished_ok.connect(self._on_set_data_ready)
        worker.finished_err.connect(self._on_set_data_error)
        worker.finished.connect(lambda w=worker: self._inflight_set_workers.discard(w))
        self._inflight_set_workers.add(worker)
        self._set_data_worker = worker
        worker.start()

    def _on_set_data_progress(self, msg: str) -> None:
        """Update the draft row with the current loading step."""
        self.window.pack_tab.home_widget.set_draft_loading(msg)

    def _on_set_data_ready(self, result: _SetDataResult) -> None:
        """Set-specific data loaded — update mapper and scryfall cards."""
        self.scryfall_cards = result.scryfall_cards
        self.window.pack_tab.set_scryfall(self.scryfall_cards)
        self._set_data_ready = True

        home = self.window.pack_tab.home_widget
        home.set_draft_loading("")  # clear loading message

        # Check if the server supports this set.
        set_code = result.set_code
        if (
            self._server_supported_sets
            and set_code not in self._server_supported_sets
        ):
            self._set_untrained = True
            home.set_draft_untrained(True)
            logger.warning(
                "Set %s is not supported by the server model — "
                "predictions will use the closest available model",
                set_code,
            )
        else:
            self._set_untrained = False
            home.set_draft_untrained(False)

        # If no draft is active yet (lobby pre-load), show green "Ready"
        # only if the player is still in a lobby.
        if not self.state.draft_active and self._in_lobby_context:
            home.set_lobby_ready(True)

        logger.info(
            "Set data loaded for %s: %d scryfall cards, %d new mappings",
            set_code, len(result.scryfall_cards), result.mappings_added,
        )

        self._flush_pending_events()

    def _on_set_data_error(self, msg: str) -> None:
        """Handle set data loading failure."""
        logger.error("Failed to load set data: %s", msg)
        home = self.window.pack_tab.home_widget
        home.set_draft_loading(f"Data load error: {msg}")
        # Allow draft to proceed anyway — predictions may partially work.
        self._set_data_ready = True
        self._flush_pending_events()

    def _flush_pending_events(self) -> None:
        """Re-dispatch events deferred while set data was loading.

        Drops each event's signature from the dedupe dict before re-dispatch,
        so the cross-watcher duplicate filter does not suppress the replay
        when set data loads inside the dedupe window.
        """
        pending = list(self._pending_events)
        self._pending_events.clear()
        for event, replaying in pending:
            sig = _event_signature(event)
            if sig is not None:
                self._recent_event_signatures.pop(sig, None)
            self._on_event(event, replaying)

        # A DeckPoolDetectedEvent that arrived before set data was ready
        # is stashed separately because DraftEnd's _pending_events.clear()
        # would otherwise drop it (the log replay's DraftEnd lands before
        # _on_set_data_ready and discards the queue). Apply it here now
        # that the mapper has the grpId→name mappings to resolve the pool.
        # ``getattr`` shields the path tested via OverlayApp.__new__ —
        # those tests skip __init__ and don't set this attribute.
        deferred = getattr(self, "_deferred_deck_pool", None)
        if deferred is not None:
            self._deferred_deck_pool = None
            if not self.state.pool:
                self._apply_deck_pool(deferred)

    def _handle_deck_pool_detected(self, event: DeckPoolDetectedEvent) -> None:
        """Dispatch a memory-detected deck pool, deferring until ready.

        When set data hasn't loaded yet the mapper has no grpId→name
        entries for the active set and an immediate ``grpids_to_names``
        call would spam "Unknown Arena grpId" warnings and bail with
        empty names — losing the only signal we have for restoring the
        pool. Stash the event in ``_deferred_deck_pool`` and apply it
        once ``_on_set_data_ready`` fires.
        """
        if self.state.draft_active or self.state.pool:
            logger.debug(
                "Ignoring DeckPoolDetectedEvent — pool already known "
                "(active=%s size=%d)",
                self.state.draft_active, len(self.state.pool),
            )
            return

        # Kick off (or pre-kick) set data load using the event name when
        # nothing else has triggered it yet — typical at overlay startup
        # straight into the deck-builder.
        if event.event_name and not self._loaded_set:
            from client.overlay.draft_state import extract_set_code
            code = extract_set_code(event.event_name)
            if code:
                self._ensure_set_data(code)

        if not self._set_data_ready:
            self._deferred_deck_pool = event
            logger.info(
                "Deferred DeckPoolDetectedEvent (event=%s, ids=%d) — "
                "waiting for set data",
                event.event_name, len(event.card_grpids),
            )
            return

        self._apply_deck_pool(event)

    def _apply_deck_pool(self, event: DeckPoolDetectedEvent) -> None:
        """Restore ``state.pool`` from a memory-detected deck pool.

        Pre-condition: ``_set_data_ready`` is True so the mapper can
        resolve grpIds → names without spamming warnings.
        """
        names = self.mapper.grpids_to_names(event.card_grpids)
        if not names:
            logger.warning(
                "DeckPoolDetectedEvent: no grpIds mapped to names "
                "(event=%s, ids=%d)", event.event_name, len(event.card_grpids),
            )
            return
        logger.info(
            "DeckPoolDetectedEvent: restoring pool of %d cards from "
            "memory (event=%s)", len(names), event.event_name,
        )
        if event.event_name and not self.state.set_code:
            self.state.on_draft_start(event.event_name)
        self.state.pool = names
        self.state.draft_active = False
        self._draft_completed = True
        self.state.save_state(self._cache_dir)

        # Push set context + art paths + pool analysis to the deck tab so
        # it renders with full card previews. The normal post-draft flow
        # accumulates these via PackEvent / prediction-response handlers;
        # the memory-restore path has none of those signals, so we
        # synthesise them here from the cached set data we just loaded.
        if self.state.set_code:
            self.window._draft_set_code = self.state.set_code
            self.window.load_card_translations_async(self.state.set_code)

        if self.art_cache.enabled:
            art_paths = {name: self.art_cache.get(name) for name in self.state.pool}
            self.window.deck_tab.set_art_paths(art_paths)

        try:
            from common.inference.pool_analyzer import analyze_pool
            pool_analysis = analyze_pool(self.state.pool, self.scryfall_cards)
            self.window.update_pool_analysis(pool_analysis)
        except Exception:
            logger.exception("Pool analysis failed during deck-pool restore")

        if self.config.features.deck_builder_enabled:
            self._update_deck_suggestions()
        self.window.show_draft_complete()

    def _on_login_google(self) -> None:
        """Handle Google login button click (runs OAuth in background thread)."""
        self._do_login("google")

    def _on_login_microsoft(self) -> None:
        """Handle Microsoft login button click."""
        self._do_login("microsoft")

    def _on_login_discord(self) -> None:
        """Handle Discord login button click."""
        self._do_login("discord")

    def _do_login(self, provider: str) -> None:
        """Run OAuth in a background thread to avoid blocking the UI."""
        # Cancel any in-flight login first
        if self._login_worker is not None and self._login_worker.isRunning():
            self.auth_client.cancel_login()
            self._login_worker.wait(2000)

        home = self.window.pack_tab.home_widget
        home.show_login_error("")

        class _LoginWorker(QThread):
            finished = Signal(object)  # ServerSession | None
            error = Signal(str)

            def __init__(self, auth: AuthClient, provider: str) -> None:
                super().__init__()
                self._auth = auth
                self._provider = provider

            def run(self) -> None:
                try:
                    if self._provider == "google":
                        session = self._auth.login_google()
                    elif self._provider == "discord":
                        session = self._auth.login_discord()
                    else:
                        session = self._auth.login_microsoft()
                    self.finished.emit(session)
                except Exception as exc:
                    self.error.emit(str(exc))

        worker = _LoginWorker(self.auth_client, provider)

        def _on_done(session: object) -> None:
            if session is None:
                home.show_login_error("Login failed — please try again")
            else:
                self._poll_server_status()
                # If a draft is already active and the user is now VIP,
                # switch to the pick view and run a prediction.
                if self.state.draft_active and self._is_vip():
                    self.window.show_draft_started()
                    if self.state.current_pack:
                        self._run_prediction()

        def _on_err(msg: str) -> None:
            home.show_login_error(f"Login error: {msg}")

        worker.finished.connect(_on_done)
        worker.error.connect(_on_err)
        # Keep reference to prevent GC
        self._login_worker = worker
        worker.start()

    def _on_logout(self) -> None:
        """Handle logout button click."""
        self.auth_client.logout()
        self._poll_server_status()

    # -- event handling (from LogWatcher) ------------------------------------

    def _emit_log_event(self, event: DraftEvent) -> None:
        """Marshal a log-watcher event with a snapshot of the replaying flag.

        Called from the LogWatcher's background thread. The marshaler queues
        the event on the Qt main thread for handling — by the time the slot
        runs, ``self.watcher.replaying`` may have flipped to False, so we
        snapshot it here at emit time.
        """
        self._marshaler.event_received.emit(event, self.watcher.replaying)

    def _emit_memory_event(self, event: DraftEvent) -> None:
        """Marshal a memory-watcher event (memory polling has no replay)."""
        self._marshaler.event_received.emit(event, False)

    def _on_event(self, event: DraftEvent, replaying: bool | None = None) -> None:
        """Handle a draft event from a watcher (log or memory).

        With both LogWatcher and MemoryWatcher active, identical events can
        arrive twice within ~250 ms. Drop the duplicate using a stable
        signature with a 2 s window — first source wins.

        ``replaying`` is the snapshot captured at emit time when the event
        was queued by ``_UiMarshaler``. For direct (synchronous) calls from
        within this method or from main-thread slots, pass it explicitly or
        leave None to read the watcher's current flag.
        """
        if replaying is None:
            replaying = self.watcher.replaying

        # Cross-watcher dedup is for the live LogWatcher+MemoryWatcher race.
        # During replay only LogWatcher runs, and the synthetic DraftEnd
        # emitted by the lobby-leave-cleanup path (see DraftLobbyEvent
        # handler) records ("end",) in the dict, which then silently drops
        # every real DraftEnd that follows within 2 s — leaving the overlay
        # in the pick view after replay.
        if not replaying and _should_drop_duplicate(
            event, self._recent_event_signatures,
        ):
            logger.debug("Dropping duplicate event %s", type(event).__name__)
            return

        if isinstance(event, LogRotatedEvent):
            logger.info("Log rotated — resetting draft state, showing home")
            self.state = DraftState()
            self._draft_completed = False
            self._loaded_set = ""
            self._set_data_ready = False
            self._set_untrained = False
            self._in_lobby_context = ""
            self._pending_events.clear()
            self.window.show_draft_ended()
            return

        if isinstance(event, DraftEndEvent):
            if self._draft_completed:
                logger.info("Draft ended (post-completion) — keeping deck view")
            else:
                logger.info("Draft ended — switching to home view")
                if not replaying:
                    self.window.show_draft_ended()
            self.state.draft_active = False
            self.state.current_pack.clear()
            # Drop any queued events from the ended draft. Otherwise a
            # PackEvent queued while set data was loading can resurrect
            # the draft via the auto-start branch in the PackEvent handler
            # once _flush_pending_events fires.
            self._pending_events.clear()
            # SceneChange Draft→Home only emits DraftEndEvent (the log
            # watcher's if/elif structure means no DraftLobbyEvent("")
            # follows). Without clearing here, _in_lobby_context survives
            # past the end of the draft, and ReplayDoneEvent /
            # _on_set_data_ready then treat the user as still in the lobby
            # and flip the home Draft row to "Ready" while they're on home.
            self._in_lobby_context = ""
            return

        if isinstance(event, DraftCompleteEvent):
            logger.info("Draft complete — keeping deck view")
            self._draft_completed = True
            self.state.draft_active = False
            self.state.current_pack.clear()
            if self.config.features.deck_builder_enabled:
                self._update_deck_suggestions()
            self.window.show_draft_complete()
            return

        if isinstance(event, DeckPoolDetectedEvent):
            self._handle_deck_pool_detected(event)
            return

        if isinstance(event, ReplayDoneEvent):
            # Drop the dedup signatures accumulated during replay so that
            # live LogWatcher+MemoryWatcher events start with a clean slate.
            self._recent_event_signatures.clear()
            if self.state.draft_active:
                # Start loading set data if not yet loaded.
                if self.state.set_code:
                    self._ensure_set_data(self.state.set_code)
                restored = self.state.restore_pool_if_needed(self._cache_dir)
                if restored:
                    logger.info("Pool restored from disk after replay")
                self.state.load_pick_history(self._cache_dir)
                self.state.infer_picked_cards()
                if self.state.pick_history:
                    self.window.sync_pick_history(self.state.pick_history)
                    self.state.save_pick_history(self._cache_dir)
            logger.info(
                "Replay done: draft_active=%s, pool=%d, pack=%d cards",
                self.state.draft_active,
                len(self.state.pool),
                len(self.state.current_pack),
            )

            # A stale Event_Join at the end of the log can leave
            # draft_active=True with zero cards.  Treat that as no draft.
            # However, if events are still queued (waiting for set data),
            # the pack hasn't been processed yet — don't treat as stale.
            if (
                self.state.draft_active
                and not self.state.current_pack
                and not self.state.pool
                and not self._pending_events
            ):
                logger.info("Draft flagged active but empty after replay — ignoring stale event")
                self.state.draft_active = False

            if self.state.draft_active:
                if self._is_vip():
                    self.window.show_draft_started()
                else:
                    logger.info("Draft active after replay but user is not VIP — staying on home")
                    self.window.pack_tab.home_widget.set_draft_active(True)
                if self.state.set_code:
                    self.window.load_card_translations_async(self.state.set_code)
                if self.state.current_pack and self._set_data_ready:
                    self._run_prediction()
                elif self.state.current_pack:
                    # Data still loading — prediction will run once data is ready.
                    logger.info("Pack waiting for set data to finish loading")

            # Handle final lobby state from replay.
            if not self.state.draft_active and self._in_lobby_context:
                from client.overlay.draft_state import extract_set_code, is_supported_draft_format
                if is_supported_draft_format(self._in_lobby_context):
                    code = extract_set_code(self._in_lobby_context)
                    if code:
                        logger.info(
                            "Replay ended in lobby for %s — loading set data",
                            code,
                        )
                        self._ensure_set_data(code)
            return

        if isinstance(event, DraftLobbyEvent):
            home = self.window.pack_tab.home_widget
            # Empty context means player left the lobby.
            if not event.context:
                self._in_lobby_context = ""
                if not replaying:
                    logger.info("Left draft lobby — clearing ready state")
                    home.set_lobby_ready(False)
                    home.set_unsupported_format(False)
                    # Reset lingering preload status (yellow loading row,
                    # untrained warning) so the home row returns to
                    # "Waiting" when the draft never actually started.
                    if not self.state.draft_active:
                        home.set_draft_loading("")
                        home.set_draft_untrained(False)
                    # Re-entry into Arena's DeckBuilder for an existing
                    # draft pool: switch to the overlay's deck tab and
                    # nudge MemoryWatcher to re-fire DeckPoolDetectedEvent
                    # so the suggestions refresh.
                    if event.destination == "DeckBuilder":
                        if self.memory_watcher is not None:
                            self.memory_watcher.reset_deck_pool_fingerprint()
                        if self.state.pool:
                            self._draft_completed = True
                            if self.config.features.deck_builder_enabled:
                                self._update_deck_suggestions()
                            self.window.show_draft_complete()
                # If draft was flagged active (e.g. Event_Join) but no pack
                # has arrived yet, the player backed out from the queue —
                # reset the session so the UI returns to the home view.
                if (
                    self.state.draft_active
                    and not self.state.current_pack
                    and not self.state.pool
                ):
                    logger.info("Left lobby with no draft progress — ending draft session")
                    self._on_event(DraftEndEvent(), replaying)
                return
            # Check for unsupported draft formats (e.g. PickTwo).
            from client.overlay.draft_state import extract_set_code, is_supported_draft_format
            if not is_supported_draft_format(event.context):
                self._in_lobby_context = ""
                if not replaying:
                    logger.warning("Unsupported draft format: %s", event.context)
                    home.set_unsupported_format(True)
                return
            # Track lobby state; during replay only the final state matters.
            self._in_lobby_context = event.context
            if replaying:
                return
            home.set_unsupported_format(False)
            # Player navigated to a draft event landing page — pre-load data.
            code = extract_set_code(event.context)
            if code:
                # If data is already loaded for this set, just show ready.
                if self._loaded_set == code and self._set_data_ready:
                    logger.info("Set data already loaded for %s — lobby ready", code)
                    # Re-apply the untrained warning from cached state —
                    # leaving the lobby cleared the home-tab flag, so we
                    # need to set it again on re-entry.
                    home.set_draft_untrained(self._set_untrained)
                    home.set_lobby_ready(True)
                else:
                    logger.info("Pre-loading set data for lobby: %s (context=%s)", code, event.context)
                    self._ensure_set_data(code)
            return

        if isinstance(event, DraftStartEvent):
            # Block unsupported draft formats from proceeding.
            from client.overlay.draft_state import is_supported_draft_format
            if not is_supported_draft_format(event.event_name):
                logger.warning("Ignoring draft start for unsupported format: %s", event.event_name)
                self.window.pack_tab.home_widget.set_unsupported_format(True)
                return

            self.state.on_draft_start(event.event_name)
            self._draft_completed = False

            # Clear stale cache from a previous draft session so
            # restore_pool_if_needed won't load an outdated pool.
            for stale in ("draft_state.json", "pick_history.json"):
                stale_path = self._cache_dir / stale
                if stale_path.exists():
                    stale_path.unlink()
                    logger.debug("Removed stale cache file %s", stale)

            # If the event name didn't yield a set code (e.g. "Play"),
            # fall back to the lobby context captured earlier.
            if not self.state.set_code and self._in_lobby_context:
                from client.overlay.draft_state import extract_set_code
                lobby_code = extract_set_code(self._in_lobby_context)
                if lobby_code:
                    self.state.set_code = lobby_code
                    logger.info(
                        "Set code from lobby context: %s (event=%s)",
                        lobby_code, event.event_name,
                    )
            self._in_lobby_context = ""

            # Trigger lazy loading of set-specific data.
            if self.state.set_code:
                self._ensure_set_data(self.state.set_code)

            if not replaying:
                if self._is_vip():
                    self.window.show_draft_started()
                    self.window.show_waiting()
                else:
                    logger.info("Draft detected but user is not VIP — staying on home")
                    self.window.pack_tab.home_widget.set_draft_active(True)

            self.window._draft_set_code = self.state.set_code

            if not replaying and self.state.set_code:
                self.window.load_card_translations_async(self.state.set_code)

        elif isinstance(event, PackEvent):
            # If set data is not ready yet, queue the event for replay.
            if not self._set_data_ready:
                # Try to extract set code from the event to start loading.
                if not self._loaded_set and event.event_name:
                    from client.overlay.draft_state import extract_set_code
                    code = extract_set_code(event.event_name)
                    if code:
                        self._ensure_set_data(code)

                self._pending_events.append((event, replaying))
                logger.info(
                    "Queued PackEvent (P%dP%d) — waiting for set data",
                    event.pack_number + 1, event.pick_number + 1,
                )
                return

            card_names = self.mapper.grpids_to_names(event.card_grpids)
            if not card_names:
                logger.warning("Could not map any grpIds to names for pack")
                return

            if not self.state.draft_active:
                event_name = event.event_name or self.watcher._cur_draft_event
                logger.info("Auto-starting draft from PackEvent (event=%s)", event_name)
                self.state.on_draft_start(event_name)
                self.window._draft_set_code = self.state.set_code
                if not replaying:
                    if self._is_vip():
                        self.window.show_draft_started()
                    else:
                        logger.info("Draft detected but user is not VIP — staying on home")
                        self.window.pack_tab.home_widget.set_draft_active(True)
                    if self.state.set_code:
                        self.window.load_card_translations_async(self.state.set_code)

            if event.event_name and not self.state.set_code:
                from client.overlay.draft_state import extract_set_code

                code = extract_set_code(event.event_name)
                if code:
                    self.state.set_code = code
                    self.window._draft_set_code = code

            self.state.on_pack(card_names, event.pack_number, event.pick_number)

            if event.picked_grpids and not self.state.pool:
                picked_names = self.mapper.grpids_to_names(event.picked_grpids)
                if picked_names:
                    self.state.pool = picked_names
                    logger.info(
                        "Restored pool from PickedCards: %d cards",
                        len(picked_names),
                    )

            if replaying:
                self._record_replay_history(card_names, event.pack_number, event.pick_number)
            else:
                self._run_prediction()

        elif isinstance(event, PickEvent):
            # Queue pick events while set data is loading.
            if not self._set_data_ready:
                self._pending_events.append((event, replaying))
                return
            if event.card_grpids:
                names = self.mapper.grpids_to_names(event.card_grpids)
                if names:
                    picked = names[0]
                    key = (self.state.pack_number, self.state.pick_number)
                    if key in self.state.pick_history:
                        self.state.pick_history[key].picked_card = picked

                    self.state.on_pick(picked)
                    if not replaying:
                        self.state.save_state(self._cache_dir)
                        self.state.save_pick_history(self._cache_dir)
                        self._update_deck_suggestions()

    def _record_replay_history(
        self,
        card_names: list[str],
        pack_number: int,
        pick_number: int,
    ) -> None:
        """Create a minimal history entry from replayed log data (no ML scores)."""
        key = (pack_number, pick_number)
        if key in self.state.pick_history:
            return

        picks_data: list[dict] = []
        for name in card_names:
            sc = self.scryfall_cards.get(name)
            picks_data.append({
                "card": name,
                "card_id": 0,
                "rank": 0,
                "score": 0.0,
                "gihwr": 0.0,
                "ata": 0.0,
                "iwd": 0.0,
                "mana_cost": sc.mana_cost if sc else "",
                "colors": list(sc.colors) if sc else [],
                "type_line": sc.type_line if sc else "",
                "is_elite": False,
                "stats_loaded": True,
            })

        self.state.pick_history[key] = PickHistoryEntry(
            pack_number=pack_number,
            pick_number=pick_number,
            picked_card="",
            picks=picks_data,
        )

    def _run_prediction(self) -> None:
        """Call the server API for predictions and push results to the UI."""
        if not self.auth_client.is_authenticated:
            logger.warning("Not authenticated — skipping prediction")
            return
        session = self.auth_client.session
        if not session or not session.is_vip:
            logger.info("VIP required for predictions — skipping")
            self.window.show_vip_required()
            return

        # Reset retry state for a fresh prediction attempt.
        self._retry_timer.stop()
        self._retry_attempt = 0
        self._retry_start = time.monotonic()
        self._attempt_prediction()

    def _draft_format(self) -> str:
        """Return the 17Lands format the player is currently drafting.

        Derived from ``state.event_name`` (e.g. ``"QuickDraft_EOE_..."``
        → ``"QuickDraft"``, ``"BotDraft_..."`` → ``"QuickDraft"``). Empty
        string when no draft event is active or the prefix doesn't map to
        a known 17Lands format; the server then falls back to its
        manager-default format.
        """
        return extract_draft_format(self.state.event_name) or ""

    def _attempt_prediction(self) -> None:
        """Single prediction attempt — schedules retry on failure."""
        try:
            results = self.api_client.predict(
                pack_cards=self.state.current_pack,
                pool_cards=self.state.pool,
                set_code=self.state.set_code,
                pack_number=self.state.pack_number,
                pick_number=self.state.pick_number,
                draft_format=self._draft_format(),
                last_pick=self.state.last_pick,
            )
            if not results:
                logger.warning(
                    "No prediction results from server (attempt %d)",
                    self._retry_attempt + 1,
                )
                self._schedule_retry()
                return

            # Success — clear retry state and push results.
            self._retry_attempt = 0
            self._retry_start = 0.0

            art_paths: dict[str, Path | None] = {}
            if self.art_cache.enabled:
                art_paths = {
                    r.card: self.art_cache.get(r.card) for r in results
                }
            self.window.update_predictions(
                results=results,
                set_code=self.state.set_code,
                pack_number=self.state.pack_number,
                pick_number=self.state.pick_number,
                pool_size=len(self.state.pool),
                art_paths=art_paths if art_paths else None,
            )

            # Record pick history for the navigator.
            key = (self.state.pack_number, self.state.pick_number)
            self.state.pick_history[key] = PickHistoryEntry(
                pack_number=self.state.pack_number,
                pick_number=self.state.pick_number,
                picked_card="",
                picks=[
                    {
                        "card": p.card,
                        "card_id": p.card_id,
                        "rank": p.rank,
                        "score": p.score,
                        "gihwr": p.gihwr,
                        "ata": p.ata,
                        "iwd": p.iwd,
                        "mana_cost": p.mana_cost,
                        "colors": list(p.colors),
                        "type_line": p.type_line,
                        "is_elite": p.is_elite,
                        "stats_loaded": p.stats_loaded,
                        "stats_format": p.stats_format,
                    }
                    for p in results
                ],
            )
            self.window.sync_pick_history(self.state.pick_history)

            # Pool analysis stays client-side (lightweight).
            from common.inference.pool_analyzer import analyze_pool

            pool_analysis = analyze_pool(self.state.pool, self.scryfall_cards)
            self.window.update_pool_analysis(pool_analysis)

            # Signals computed server-side.
            if self.config.features.signals_enabled:
                self._update_signals()

            # Deck suggestions client-side.
            if self.config.features.deck_builder_enabled:
                self._update_deck_suggestions()

        except Exception:
            logger.exception(
                "Prediction failed (attempt %d)", self._retry_attempt + 1,
            )
            self._schedule_retry()

    def _schedule_retry(self) -> None:
        """Schedule the next retry, or give up after ``_RETRY_TIMEOUT_S``."""
        elapsed = time.monotonic() - self._retry_start
        if elapsed >= self._RETRY_TIMEOUT_S:
            logger.error(
                "Server unreachable after %ds — giving up", int(elapsed),
            )
            self._on_server_failure()
            return

        idx = min(self._retry_attempt, len(self._RETRY_INTERVALS_MS) - 1)
        delay = self._RETRY_INTERVALS_MS[idx]
        self._retry_attempt += 1
        logger.info(
            "Retrying prediction in %dms (attempt %d, %.0fs elapsed)",
            delay, self._retry_attempt, elapsed,
        )
        self._retry_timer.start(delay)

    def _retry_prediction(self) -> None:
        """Timer callback — re-attempt prediction if still in draft."""
        if not self.state.draft_active or not self.state.current_pack:
            logger.info("Draft no longer active — cancelling retry")
            return
        self._attempt_prediction()

    def _on_server_failure(self) -> None:
        """Handle persistent server failure: switch to home, show status."""
        self._retry_timer.stop()
        self._retry_attempt = 0
        self._retry_start = 0.0

        # Mark server as unreachable on the home tab.
        self.window.pack_tab.home_widget.set_server_status(
            reachable=False, authenticated=False,
        )
        self.window.show_draft_ended()

    def _update_signals(self) -> None:
        """Fetch signal scores from server and push to UI."""
        try:
            if not self.state.set_code or not self.state.seen_cards:
                return

            seen_items = [
                {
                    "card_name": entry.card_name,
                    "colors": [],
                    "gihwr": 0.0,
                    "ata": 0.0,
                    "pack_number": entry.pack_number,
                    "pick_number": entry.pick_number,
                }
                for entry in self.state.seen_cards
            ]

            scores = self.api_client.compute_signals(
                seen_items, self.state.set_code,
                draft_format=self._draft_format(),
            )
            if scores:
                from common.inference.signals import SignalResult
                result = SignalResult(scores=scores)
                self.window.update_signals(result)
        except Exception:
            logger.exception("Signal fetch failed")

    def _update_deck_suggestions(self) -> None:
        """Fetch deck suggestions from the server and push to the UI."""
        try:
            if len(self.state.pool) < 15:
                return

            raw = self.api_client.deck_suggestions(
                pool_cards=self.state.pool,
                set_code=self.state.set_code,
                draft_format=self._draft_format(),
            )
            if not raw:
                return

            from common.inference.deck_builder import DeckSuggestion

            suggestions: dict[str, DeckSuggestion] = {}
            for key, s in raw.items():
                suggestions[key] = DeckSuggestion(
                    archetype=s["archetype"],
                    main_deck=s["main_deck"],
                    main_deck_cmc=s["main_deck_cmc"],
                    lands=s["lands"],
                    nonbasic_lands=s["nonbasic_lands"],
                    score=s["score"],
                    creature_count=s["creature_count"],
                    spell_count=s["spell_count"],
                    land_count=s["land_count"],
                    avg_cmc=s["avg_cmc"],
                )

            self.window.update_deck_suggestions(
                suggestions, self.state.pool, self.scryfall_cards,
            )
        except Exception:
            logger.exception("Deck suggestion failed")

    def _on_settings_changed(self) -> None:
        """Persist settings and apply changes."""
        self.window.setWindowOpacity(self.config.overlay.opacity)
        save_config(self.config)

        # Re-predict only when non-display settings (e.g. user_group) change.
        # Opacity changes go through opacity_preview and don't need a refresh.
        if self.state.current_pack and self._settings_require_refresh():
            self._run_prediction()

    def _settings_require_refresh(self) -> bool:
        """Return True when last settings commit affected prediction inputs.

        Currently we always refresh on commit — opacity is handled separately
        via opacity_preview, so committing it won't hit this path mid-drag.
        Kept as a hook so future per-field diffing can short-circuit easily.
        """
        return True

    def _on_language_changed(self, language: str) -> None:
        """Save config on language change."""
        save_config(self.config)


def parse_args() -> argparse.Namespace:
    from client.overlay.env import bundle_root
    root = bundle_root()
    parser = argparse.ArgumentParser(description="NemeDraft Arena Overlay")
    parser.add_argument(
        "--card-id-map",
        default=str(root / "data" / "processed" / "card_id_map.json"),
        help="Path to card_id_map.json",
    )
    parser.add_argument(
        "--scryfall-dir",
        default=str(root / "data" / "scryfall"),
        help="Scryfall JSON data directory",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Override Arena Player.log path (auto-detected if omitted)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--update-scryfall",
        action="store_true",
        help="Re-download Scryfall bulk data before starting",
    )
    parser.add_argument(
        "--transparent",
        action="store_true",
        help="Use frameless transparent overlay window",
    )
    parser.add_argument(
        "--show-art",
        action="store_true",
        help="Show card art thumbnails in the overlay",
    )
    parser.add_argument(
        "--opacity",
        type=float,
        default=0.85,
        help="Window opacity for transparent mode (0.0-1.0)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    from client.overlay.env import _project_root
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Also log to a file so users can export logs from the settings tab.
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    ))
    logging.getLogger().addHandler(_file_handler)

    app = QApplication(sys.argv)

    # Allow Ctrl+C to close the app.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Load persisted configuration and client env.
    config = load_config()
    env = load_client_env()

    # CLI flags override config.
    if args.transparent:
        config.overlay.transparent = True
    if args.show_art:
        config.overlay.show_art = True
    if args.opacity != 0.85:
        config.overlay.opacity = args.opacity

    # --- Initialise translator (must happen before building UI) ---
    translator = Translator.instance()
    translator.set_language(config.display.language)

    # --- Show window immediately (Home tab is visible) ---
    window = OverlayWindow(
        config,
        transparent=config.overlay.transparent,
        show_art=config.overlay.show_art,
        opacity=config.overlay.opacity,
        scryfall_dir=Path(args.scryfall_dir),
    )
    # Restore persisted window geometry before show_loading so it can be
    # saved and re-applied after boot completes.
    if config.overlay.geometry:
        from PySide6.QtCore import QByteArray
        window.restoreGeometry(QByteArray.fromBase64(config.overlay.geometry.encode()))

    window.show_loading()
    window.show()

    # --- Check for updates before anything else ---
    overlay_holder: list[OverlayApp | None] = [None]

    def _start_boot() -> None:
        """Kick off the normal boot sequence (called after update check)."""
        boot_worker = _BootWorker(args, config, env)
        boot_worker.progress.connect(_on_boot_progress)
        boot_worker.finished_ok.connect(_on_boot_done)
        boot_worker.finished_err.connect(_on_boot_error)
        # Store reference so it isn't garbage-collected.
        window._boot_worker = boot_worker  # type: ignore[attr-defined]
        boot_worker.start()

    def _on_boot_progress(msg: str) -> None:
        window.status.setText(msg)

    def _on_boot_done(result: _BootResult) -> None:
        overlay = OverlayApp(
            mapper=result.mapper,
            api_client=result.api_client,
            auth_client=result.auth_client,
            watcher=result.watcher,
            window=window,
            art_cache=result.art_cache,
            config=config,
            server_supported_sets=result.server_supported_sets,
            scryfall_dir=Path(args.scryfall_dir),
            cache_dir=_project_root() / "data" / "cache",
            has_arena_player_id=result.has_arena_player_id,
            memory_watcher=result.memory_watcher,
        )
        overlay_holder[0] = overlay
        window.show_model_ready()
        overlay.start()

    def _on_boot_error(msg: str) -> None:
        window.status.setText(tr("startup_error", error=msg))

    def _on_update_progress(msg: str) -> None:
        window.status.setText(msg)

    def _on_no_update() -> None:
        _start_boot()

    def _on_update_ready(new_binary: object) -> None:
        from client.overlay.updater import apply_update_and_restart
        window.status.setText(tr("update_applying"))
        try:
            apply_update_and_restart(Path(str(new_binary)))
        except Exception as exc:
            logger.error("Failed to apply update: %s", exc, exc_info=True)
            window.status.setText(tr("update_failed", error=exc))
            _start_boot()

    def _on_update_failed(msg: str) -> None:
        logger.warning("Update failed, continuing normally: %s", msg)
        _start_boot()

    update_worker = _UpdateWorker()
    update_worker.progress.connect(_on_update_progress)
    update_worker.no_update.connect(_on_no_update)
    update_worker.update_ready.connect(_on_update_ready)
    update_worker.update_failed.connect(_on_update_failed)
    # Store reference so it isn't garbage-collected.
    window._update_worker = update_worker  # type: ignore[attr-defined]
    update_worker.start()

    exit_code = app.exec()

    # Persist window geometry before exit.
    config.overlay.geometry = bytes(window.saveGeometry().toBase64()).decode()
    save_config(config)

    if overlay_holder[0] is not None:
        overlay_holder[0].stop()
    sys.exit(exit_code)
