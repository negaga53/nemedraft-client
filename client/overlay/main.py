"""Overlay application entry point — wires log watcher, state, and server API client."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication

from client.overlay.api_client import NemeDraftClient
from client.overlay.arena_memory import ArenaCurrentEvent
from client.overlay.auth_client import AuthClient
from client.overlay.boot import (
    ArenaIdentityResolution,
    BootResult,
    BootWorker,
    UpdateWorker,
)
from client.overlay.card_art import CardArtCache
from client.overlay.card_mapper import ArenaCardMapper
from client.overlay.config import OverlayConfig, load_config, save_config, LOG_FILE
from client.overlay.draft_state import DraftState, PickHistoryEntry, extract_draft_format
from client.overlay.env import ClientEnv, load_client_env, save_arena_player_id
from client.overlay.events import UiMarshaler, event_signature, should_drop_duplicate
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
    is_arena_running,
)
from client.overlay.managers.prediction import PredictionManager, PredictionRequest
from client.overlay.managers.set_data import SetDataManager
from client.overlay.managers.workers import (
    ArenaCurrentEventWorker,
    ArenaIdentityWorker,
    SetDataResult,
)
from client.overlay.memory_watcher import MemoryWatcher
from client.overlay.single_instance import SingleInstance
from client.overlay.ui.window import OverlayWindow

logger = logging.getLogger("overlay")


class OverlayApp:
    """Orchestrates all overlay components (thin client)."""

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
        # Lazy per-set data loading (worker + 90s watchdog), signal-driven.
        self._set_data = SetDataManager(mapper, self._scryfall_dir)
        self._set_data.progress.connect(self._on_set_data_progress)
        self._set_data.ready.connect(self._on_set_data_ready)
        self._set_data.failed.connect(self._on_set_data_error)
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

        # Prediction lifecycle: dispatch, retry backoff, signals fetch,
        # deck suggestions — all on background workers, results delivered
        # via main-thread signals.
        self._prediction = PredictionManager(
            api_client,
            request_provider=self._prediction_request,
            is_active=lambda: (
                self.state.draft_active and bool(self.state.current_pack)
            ),
        )
        self._prediction.loading.connect(self._on_prediction_loading)
        self._prediction.results_ready.connect(self._on_prediction_results)
        self._prediction.gave_up.connect(self._on_server_failure)
        self._prediction.signals_ready.connect(self._on_signals_ready)
        self._prediction.deck_suggestions_ready.connect(
            self._on_deck_suggestions_ready,
        )

        # Marshal watcher-thread callbacks onto the Qt main thread.
        # See UiMarshaler — registering _on_event directly on the watchers
        # mutates Qt widgets off the GUI thread and racey-crashes.
        self._marshaler = UiMarshaler()
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
        self._arena_identity_worker: ArenaIdentityWorker | None = None
        self._arena_identity_timer = QTimer()
        self._arena_identity_timer.setInterval(5_000)
        self._arena_identity_timer.timeout.connect(self._retry_arena_identity)
        if not self._has_arena_player_id:
            self._arena_identity_timer.start()

        self._arena_current_event_worker: ArenaCurrentEventWorker | None = None
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
        self._prediction.cancel()
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

        worker = ArenaIdentityWorker()
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
        if not isinstance(identity, ArenaIdentityResolution):
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

        worker = ArenaCurrentEventWorker()
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
        ``UiMarshaler.set_load_requested`` (queued signal → slot on the
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
        no-op.  Otherwise the SetDataManager spawns a worker and the
        draft row goes yellow with loading steps.
        """
        if not set_code:
            return
        # Only drop pending events when switching sets — otherwise we race
        # with the watcher thread, which may have queued events for this
        # same set between emitting set_load_requested and this handler
        # running.
        if self._set_data.loaded_set and self._set_data.loaded_set != set_code:
            self._pending_events.clear()

        if self._set_data.ensure(set_code):
            self._set_untrained = False
            self.window.pack_tab.home_widget.set_draft_loading(
                f"Detected {set_code} — loading data...",
            )

    def _on_set_data_progress(self, msg: str) -> None:
        """Update the draft row with the current loading step."""
        self.window.pack_tab.home_widget.set_draft_loading(msg)

    def _on_set_data_ready(self, result: SetDataResult) -> None:
        """Set-specific data loaded — update mapper and scryfall cards."""
        self.scryfall_cards = result.scryfall_cards
        self.window.pack_tab.set_scryfall(self.scryfall_cards)

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

    def _on_set_data_error(self, set_code: str, msg: str) -> None:
        """Handle set data loading failure (manager already degraded to ready)."""
        home = self.window.pack_tab.home_widget
        home.set_draft_loading(f"Data load error: {msg}")
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
            sig = event_signature(event)
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
        if event.event_name and not self._set_data.loaded_set:
            from client.overlay.draft_state import extract_set_code
            code = extract_set_code(event.event_name)
            if code:
                self._ensure_set_data(code)

        if not self._set_data.is_ready:
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
        was queued by ``UiMarshaler``. For direct (synchronous) calls from
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
        if not replaying and should_drop_duplicate(
            event, self._recent_event_signatures,
        ):
            logger.debug("Dropping duplicate event %s", type(event).__name__)
            return

        if isinstance(event, LogRotatedEvent):
            logger.info("Log rotated — resetting draft state, showing home")
            self.state = DraftState()
            self._draft_completed = False
            self._set_data.reset()
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
                if self.state.current_pack and self._set_data.is_ready:
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
            # Reject draft formats the overlay doesn't support yet.
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
                if self._set_data.loaded_set == code and self._set_data.is_ready:
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
            if not self._set_data.is_ready:
                # Try to extract set code from the event to start loading.
                if not self._set_data.loaded_set and event.event_name:
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
            if not self._set_data.is_ready:
                self._pending_events.append((event, replaying))
                return
            if event.card_grpids:
                names = self.mapper.grpids_to_names(event.card_grpids)
                if names:
                    # PickTwo passes carry two picked cards; single-pick
                    # formats keep cards_per_pick == 1. Slicing covers both
                    # the one-event-two-grpids and two-single-events shapes.
                    picked_names = names[: max(1, self.state.cards_per_pick)]
                    key = (self.state.pack_number, self.state.pick_number)
                    if key in self.state.pick_history:
                        self.state.pick_history[key].picked_card = picked_names[0]
                        self.state.pick_history[key].picked_cards = list(picked_names)

                    for picked in picked_names:
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

        self._prediction.request_prediction()

    def _prediction_request(self) -> PredictionRequest:
        """Snapshot the current draft state for a prediction dispatch.

        Called by ``PredictionManager`` at every attempt — including
        retries, which must pick up the *current* pack if the draft has
        advanced since the failed attempt.
        """
        return PredictionRequest(
            pack_cards=tuple(self.state.current_pack),
            pool_cards=tuple(self.state.pool),
            set_code=self.state.set_code,
            pack_number=self.state.pack_number,
            pick_number=self.state.pick_number,
            draft_format=self._draft_format(),
            arena_format=self.state.arena_format,
            last_pick=self.state.last_pick,
        )

    def _draft_format(self) -> str:
        """Return the 17Lands format the player is currently drafting.

        Derived from ``state.event_name`` (e.g. ``"QuickDraft_EOE_..."``
        → ``"QuickDraft"``, ``"BotDraft_..."`` → ``"QuickDraft"``). Empty
        string when no draft event is active or the prefix doesn't map to
        a known 17Lands format; the server then falls back to its
        manager-default format.
        """
        return extract_draft_format(self.state.event_name) or ""

    def _on_prediction_loading(self, pack_number: int, pick_number: int) -> None:
        """Surface a spinner while a prediction call is in flight."""
        self.window.show_prediction_loading(pack_number, pick_number)
        self.window.pack_tab.set_recommend_count(self.state.recommend_count)

    def _on_prediction_results(
        self,
        results: list,
        pack_number: int,
        pick_number: int,
    ) -> None:
        """Apply prediction results delivered by ``PredictionManager``."""
        if (
            pack_number != self.state.pack_number
            or pick_number != self.state.pick_number
        ):
            # The user already moved past this pack — drop the result.
            logger.debug(
                "Prediction arrived for P%dP%d but state is P%dP%d — discarding",
                pack_number + 1, pick_number + 1,
                self.state.pack_number + 1, self.state.pick_number + 1,
            )
            return

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

    def _on_server_failure(self) -> None:
        """Handle persistent server failure: switch to home, show status."""
        self._prediction.cancel()
        self.window.pack_tab.hide_loading()

        # Mark server as unreachable on the home tab.
        self.window.pack_tab.home_widget.set_server_status(
            reachable=False, authenticated=False,
        )
        self.window.show_draft_ended()

    def _update_signals(self) -> None:
        """Fetch signal scores from server (background) and push to UI."""
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
        self._prediction.update_signals(
            seen_items=seen_items,
            set_code=self.state.set_code,
            draft_format=self._draft_format(),
        )

    def _on_signals_ready(self, result: object) -> None:
        self.window.update_signals(result)

    def _update_deck_suggestions(self) -> None:
        """Fetch deck suggestions from the server (background) and push to UI."""
        if len(self.state.pool) < 15:
            return
        self._prediction.update_deck_suggestions(
            pool_cards=self.state.pool,
            set_code=self.state.set_code,
            draft_format=self._draft_format(),
        )

    def _on_deck_suggestions_ready(self, suggestions: object) -> None:
        self.window.update_deck_suggestions(
            suggestions, self.state.pool, self.scryfall_cards,
        )

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

    # Single-instance lock — frozen builds only. Devs running from source
    # can launch multiple overlays side-by-side. Lock dies with this
    # process, so the updater's spawn-after-PID-exits pattern is unaffected.
    single_instance = SingleInstance()
    if getattr(sys, "frozen", False) and not single_instance.acquire(
        "nemedraft-overlay",
    ):
        logger.info(
            "Another NemeDraft overlay is already running — raised its "
            "window and exiting.",
        )
        return

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

    def _raise_to_foreground() -> None:
        if window.isMinimized():
            window.showNormal()
        window.show()
        window.raise_()
        window.activateWindow()

    single_instance.raise_requested.connect(_raise_to_foreground)

    # --- Check for updates before anything else ---
    overlay_holder: list[OverlayApp | None] = [None]

    def _start_boot() -> None:
        """Kick off the normal boot sequence (called after update check)."""
        boot_worker = BootWorker(args, config, env)
        boot_worker.progress.connect(_on_boot_progress)
        boot_worker.finished_ok.connect(_on_boot_done)
        boot_worker.finished_err.connect(_on_boot_error)
        # Store reference so it isn't garbage-collected.
        window._boot_worker = boot_worker  # type: ignore[attr-defined]
        boot_worker.start()

    def _on_boot_progress(msg: str) -> None:
        window.status.setText(msg)

    def _on_boot_done(result: BootResult) -> None:
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

    update_worker = UpdateWorker()
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
