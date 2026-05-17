"""Memory-driven draft event watcher.

Mirrors :class:`client.overlay.log_watcher.LogWatcher`'s public interface
(``add_callback``, ``start``, ``stop``, ``replaying``) so
:meth:`OverlayApp._on_event` consumes events from either source
transparently. Emits the SAME event dataclasses defined in
:mod:`client.overlay.log_watcher` — never redefines them.

The watcher polls :func:`client.overlay.memory.walker.read_draft_state` and
diffs the snapshot against the previous tick. While ``read_draft_state``
remains a stub returning ``None`` (pending the live-pod field-path
investigation; see ``docs/draft-state-investigation.md``), the watcher
runs harmlessly: it attaches when MTGA is up, polls quietly, and emits
nothing.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from client.overlay.log_watcher import (
    DeckPoolDetectedEvent,
    DraftCompleteEvent,
    DraftEndEvent,
    DraftEvent,
    DraftStartEvent,
    EventCallback,
    PackEvent,
    PickEvent,
)
from client.overlay.memory.platform import is_memory_supported
from client.overlay.memory.session import MemorySession
from client.overlay.memory.walker import read_deck_pool, read_draft_state

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.25


@dataclass(frozen=True)
class _DraftSnapshot:
    """Frozen snapshot of one ``read_draft_state`` poll cycle."""

    is_active: bool
    event_name: str
    pack_number: int
    pick_number: int
    current_pack: tuple[int, ...]
    picked_cards: tuple[int, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "_DraftSnapshot":
        pack_raw = payload.get("pack_number")
        pick_raw = payload.get("pick_number")
        return cls(
            is_active=bool(payload.get("is_active", False)),
            event_name=str(payload.get("event_name", "") or ""),
            # ``a or -1`` collapses a legitimate 0 (pack 1 pick 1) to -1,
            # which then mismatches LogWatcher's 0/0 and defeats dedup.
            pack_number=int(pack_raw) if pack_raw is not None else -1,
            pick_number=int(pick_raw) if pick_raw is not None else -1,
            current_pack=tuple(payload.get("current_pack") or ()),
            picked_cards=tuple(payload.get("picked_cards") or ()),
        )


class MemoryWatcher:
    """Polls Mono memory for draft state and emits draft events."""

    def __init__(self, *, poll_interval: float = POLL_INTERVAL_S) -> None:
        self._poll_interval = poll_interval
        self._callbacks: list[EventCallback] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous: _DraftSnapshot | None = None
        # ``(event_name, tuple(grpids))`` for the last deck pool we
        # emitted. The fingerprint suppresses re-fires while the player
        # idles in the deck-builder; a *different* draft (e.g. they
        # finished another one) will fingerprint differently and emit.
        self._previous_deck_pool: tuple[str, tuple[int, ...]] | None = None
        # API parity with LogWatcher; memory has no notion of replay.
        self.replaying: bool = False

    # -------- public API (mirrors LogWatcher) ------------------------------

    def add_callback(self, cb: EventCallback) -> None:
        """Register a callback invoked on each emitted draft event."""
        self._callbacks.append(cb)

    def start(self) -> None:
        """Start the polling thread if memory access is supported."""
        if not is_memory_supported():
            logger.info("Memory access unsupported on this platform — MemoryWatcher disabled")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="memory-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the polling thread to exit and wait briefly for it."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._thread = None

    # -------- internals ----------------------------------------------------

    def _run(self) -> None:
        session = MemorySession.instance()
        logger.info("MemoryWatcher started (poll=%.0f ms)", self._poll_interval * 1000)
        while not self._stop.is_set():
            try:
                self._tick(session)
            except Exception:
                logger.warning("MemoryWatcher tick failed", exc_info=True)
            self._stop.wait(self._poll_interval)
        logger.info("MemoryWatcher stopped")

    def _tick(self, session: MemorySession) -> None:
        if not session.ensure_attached():
            # MTGA not running yet (or not loaded). When it later attaches,
            # the next tick will pick up state cleanly.
            self._previous = None
            self._previous_deck_pool = None
            return
        payload = read_draft_state(session)
        if payload is None:
            # No live draft view. Two sub-cases — the user is in the
            # deck-builder for a finished draft (emit DeckPoolDetectedEvent
            # once) or somewhere else entirely (emit DraftEnd if we were
            # previously active, then idle).
            if self._previous is not None and self._previous.is_active:
                self._emit(DraftEndEvent())
            self._previous = None
            self._maybe_emit_deck_pool(session)
            return
        # Active draft view — clear the deck-pool fingerprint so re-entry
        # into the deck-builder after another draft re-fires the event.
        self._previous_deck_pool = None
        current = _DraftSnapshot.from_payload(payload)
        prev = self._previous
        for event in _diff_snapshots(prev, current):
            self._emit(event)
        self._previous = current

    def _maybe_emit_deck_pool(self, session: MemorySession) -> None:
        """Emit a DeckPoolDetectedEvent on first sight of a draft pool."""
        deck = read_deck_pool(session)
        if deck is None:
            self._previous_deck_pool = None
            return
        event_name = str(deck.get("event_name", "") or "")
        pool = tuple(int(g) for g in deck.get("card_pool") or ())
        if not pool:
            self._previous_deck_pool = None
            return
        fingerprint = (event_name, pool)
        if fingerprint == self._previous_deck_pool:
            return
        self._previous_deck_pool = fingerprint
        self._emit(DeckPoolDetectedEvent(
            card_grpids=list(pool),
            event_name=event_name,
        ))

    def _emit(self, event: DraftEvent) -> None:
        logger.debug("MemoryWatcher emit: %s", type(event).__name__)
        for cb in list(self._callbacks):
            try:
                cb(event)
            except Exception:
                logger.exception("MemoryWatcher callback raised on %s", type(event).__name__)


def _diff_snapshots(
    prev: _DraftSnapshot | None, curr: _DraftSnapshot
) -> list[DraftEvent]:
    """Return the draft events implied by the prev → curr transition."""
    events: list[DraftEvent] = []

    # Draft-active transitions ---------------------------------------------
    if curr.is_active and (prev is None or not prev.is_active):
        events.append(DraftStartEvent(event_name=curr.event_name))
    elif (prev is not None and prev.is_active) and not curr.is_active:
        events.append(DraftEndEvent())
        return events  # nothing else meaningful when leaving a draft

    if not curr.is_active:
        return events

    pack_changed = (
        prev is None
        or prev.pack_number != curr.pack_number
        or prev.pick_number != curr.pick_number
        or prev.current_pack != curr.current_pack
    )
    if pack_changed and curr.current_pack:
        events.append(
            PackEvent(
                card_grpids=list(curr.current_pack),
                pack_number=curr.pack_number,
                pick_number=curr.pick_number,
                event_name=curr.event_name,
                picked_grpids=list(curr.picked_cards),
            )
        )

    # Pick detection: a card was added to the picked list since the last tick.
    if prev is not None and len(curr.picked_cards) > len(prev.picked_cards):
        new_picks = curr.picked_cards[len(prev.picked_cards):]
        events.append(
            PickEvent(
                card_grpids=list(new_picks),
                pack_number=prev.pack_number,
                pick_number=prev.pick_number,
            )
        )

    # Draft completion is signalled when current_pack empties while still
    # nominally "active" — the pod has handed out the last pick. This is a
    # heuristic; the live walker may set is_active=False directly instead.
    if (
        prev is not None
        and prev.is_active
        and curr.is_active
        and prev.current_pack
        and not curr.current_pack
        and curr.pack_number >= 2  # last pack
    ):
        events.append(DraftCompleteEvent())

    return events
