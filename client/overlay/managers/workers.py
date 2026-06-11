"""QThread workers that run blocking calls off the Qt main thread."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from client.overlay.api_client import NemeDraftClient
from client.overlay.arena_memory import get_arena_current_event
from client.overlay.boot import resolve_arena_identity
from client.overlay.card_mapper import ArenaCardMapper

logger = logging.getLogger("overlay")


@dataclass
class SetDataResult:
    """Data loaded for a specific set."""

    set_code: str
    scryfall_cards: dict
    mappings_added: int


class SetDataWorker(QThread):
    """Loads set-specific card data in the background.

    Emits progress messages so the UI can show loading steps.
    """

    progress = Signal(str)
    finished_ok = Signal(object)   # SetDataResult
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

    @property
    def set_code(self) -> str:
        return self._set_code

    def run(self) -> None:  # noqa: D401
        try:
            sc = self._set_code
            self.progress.emit(f"Loading {sc} card mappings...")
            added = self._mapper.load_set(sc)
            logger.info("Loaded %d arena mappings for %s", added, sc)

            self.progress.emit(f"Loading {sc} Scryfall data...")
            from common.inference.pool_analyzer import load_scryfall_cards_for_set
            scryfall_cards = load_scryfall_cards_for_set(self._scryfall_dir, sc)

            self.progress.emit("Loading MTGA fallback DB...")
            self._mapper.ensure_mtga_fallback()

            self.finished_ok.emit(SetDataResult(
                set_code=sc,
                scryfall_cards=scryfall_cards,
                mappings_added=added,
            ))
        except Exception as exc:
            logger.exception("Set data worker failed for %s", self._set_code)
            self.finished_err.emit(str(exc))


class ArenaIdentityWorker(QThread):
    """Reads Arena identity from local sources without blocking the UI thread."""

    finished_identity = Signal(object)  # ArenaIdentityResolution | None

    def run(self) -> None:  # noqa: D401
        try:
            self.finished_identity.emit(resolve_arena_identity())
        except Exception:
            logger.debug("Arena memory identity retry failed", exc_info=True)
            self.finished_identity.emit(None)


class ArenaCurrentEventWorker(QThread):
    """Reads Arena current event state without blocking the UI thread."""

    finished_event = Signal(object)  # ArenaCurrentEvent | None

    def run(self) -> None:  # noqa: D401
        try:
            self.finished_event.emit(get_arena_current_event())
        except Exception:
            logger.debug("Arena current event memory poll failed", exc_info=True)
            self.finished_event.emit(None)


class PredictionWorker(QThread):
    """Background worker that calls ``api_client.predict`` off the UI thread.

    The HTTP round-trip can take a few seconds; running it synchronously
    on the Qt main thread freezes the overlay (the dreaded new-pack
    stutter). Emits ``finished_ok`` with the picks or ``failed`` with an
    error string. The dispatching call also remembers the pack/pick the
    worker was started for so stale results from a previous pack can be
    discarded.
    """

    finished_ok = Signal(object, int, int)  # list[Pick], pack_number, pick_number
    failed = Signal(str, int, int)          # error, pack_number, pick_number

    def __init__(
        self,
        api_client: NemeDraftClient,
        *,
        pack_cards: list[str],
        pool_cards: list[str],
        set_code: str,
        pack_number: int,
        pick_number: int,
        draft_format: str,
        arena_format: str,
        last_pick: str | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._api_client = api_client
        self._pack_cards = list(pack_cards)
        self._pool_cards = list(pool_cards)
        self._set_code = set_code
        self._pack_number = pack_number
        self._pick_number = pick_number
        self._draft_format = draft_format
        self._arena_format = arena_format
        self._last_pick = last_pick

    def run(self) -> None:  # noqa: D401
        try:
            results = self._api_client.predict(
                pack_cards=self._pack_cards,
                pool_cards=self._pool_cards,
                set_code=self._set_code,
                pack_number=self._pack_number,
                pick_number=self._pick_number,
                draft_format=self._draft_format,
                arena_format=self._arena_format,
                last_pick=self._last_pick,
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc), self._pack_number, self._pick_number)
            return
        self.finished_ok.emit(
            list(results or []), self._pack_number, self._pick_number,
        )
