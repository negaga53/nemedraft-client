"""Event-dispatch plumbing shared by the overlay watchers.

Cross-source duplicate detection (LogWatcher + MemoryWatcher can deliver
the same event twice within ~250 ms) and the Qt-thread marshaler that
keeps watcher callbacks off the GUI thread.
"""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

from client.overlay.log_watcher import (
    DraftCompleteEvent,
    DraftEndEvent,
    DraftEvent,
    DraftLobbyEvent,
    DraftStartEvent,
    PackEvent,
    PickEvent,
)

DEDUPE_WINDOW_S = 2.0


def event_signature(event: DraftEvent) -> tuple | None:
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


def should_drop_duplicate(
    event: DraftEvent, recent: dict[tuple, float]
) -> bool:
    sig = event_signature(event)
    if sig is None:
        return False
    now = time.monotonic()
    cutoff = now - DEDUPE_WINDOW_S
    # Opportunistic GC of stale entries.
    if len(recent) > 16:
        for k in [k for k, t in recent.items() if t < cutoff]:
            recent.pop(k, None)
    last = recent.get(sig)
    if last is not None and last >= cutoff:
        return True
    recent[sig] = now
    return False


class UiMarshaler(QObject):
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
