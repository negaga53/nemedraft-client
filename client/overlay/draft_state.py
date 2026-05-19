"""Draft state machine — tracks current pack, pool, set, pick position, and seen cards."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Patterns for extracting set code from Arena event names like
# "PremierDraft_TMT_20250401", "QuickDraft_FIN_20250301", "BotDraft_ECL_..."
_EVENT_SET_RE = re.compile(
    r"(?:PremierDraft|QuickDraft|BotDraft|TradDraft|CompDraft)[_]([A-Z0-9]{3})",
    re.IGNORECASE,
)

# Reversed format used in SceneChange context, e.g. "TMT_Premier_Draft".
_CONTEXT_SET_RE = re.compile(
    r"([A-Z0-9]{3})[_](?:Premier[_]?Draft|Quick[_]?Draft|Bot[_]?Draft|Trad[_]?Draft|Comp[_]?Draft)",
    re.IGNORECASE,
)

# Mapping from Arena event prefix to 17Lands format string.
_FORMAT_MAP: dict[str, str] = {
    "PremierDraft": "PremierDraft",
    "QuickDraft": "QuickDraft",
    "BotDraft": "QuickDraft",
    "TradDraft": "TradDraft",
    "CompDraft": "PremierDraft",
}

_EVENT_FORMAT_RE = re.compile(
    r"(PremierDraft|QuickDraft|BotDraft|TradDraft|CompDraft)",
    re.IGNORECASE,
)

# Supported draft format keywords (case-insensitive substrings).
# Anything containing "draft" but NOT matching these is unsupported.
_SUPPORTED_FORMAT_KEYWORDS = {"premier", "quick", "bot", "trad", "comp"}


def extract_set_code(event_name: str) -> str | None:
    """Extract the set code from an Arena event name.

    Args:
        event_name: Arena event name, e.g. "PremierDraft_TMT_20250401".

    Returns:
        Uppercase set code if found, else None. The server's
        ``supported_sets`` health check is the authority on whether the
        model has been trained on the returned set.
    """
    m = _EVENT_SET_RE.search(event_name)
    if m:
        return m.group(1).upper()
    # Reversed format (SceneChange context), e.g. "TMT_Premier_Draft".
    m = _CONTEXT_SET_RE.search(event_name)
    if m:
        return m.group(1).upper()
    return None


def extract_draft_format(event_name: str) -> str | None:
    """Extract the 17Lands draft format from an Arena event name.

    Args:
        event_name: Arena event name, e.g. "QuickDraft_FDN_20260323".

    Returns:
        17Lands format string (e.g. ``"QuickDraft"``) or *None*.
    """
    m = _EVENT_FORMAT_RE.search(event_name)
    if m:
        prefix = m.group(1)
        # Normalise to title case for lookup.
        for key in _FORMAT_MAP:
            if key.lower() == prefix.lower():
                return _FORMAT_MAP[key]
    return None


def is_supported_draft_format(context: str) -> bool:
    """Return True if *context* refers to a supported draft format.

    Accepts both event names (``"PremierDraft_TMT_20250401"``) and
    SceneChange contexts (``"TMT_Premier_Draft"``).  Anything containing
    ``"draft"`` that does **not** match a known format keyword is
    considered unsupported (e.g. ``"TMT_PickTwo_Fast_Draft"``).

    Args:
        context: Arena event name or SceneChange context string.

    Returns:
        ``True`` for supported formats, ``False`` for unsupported ones.
        Returns ``True`` if the string doesn't look like a draft at all
        (caller should check separately).
    """
    lower = context.lower()
    if "draft" not in lower:
        return True  # not a draft context — not our concern
    return any(kw in lower for kw in _SUPPORTED_FORMAT_KEYWORDS)


@dataclass
class SeenCardEntry:
    """A card that the player saw in a pack but did not pick."""

    card_name: str
    pack_number: int
    pick_number: int


@dataclass
class PickHistoryEntry:
    """Snapshot of a single pick for the history navigator."""

    pack_number: int
    pick_number: int
    picked_card: str = ""
    picks: list[dict] = field(default_factory=list)
    """Each dict mirrors a serialised :class:`Pick`: card, rank, score, gihwr, ata,
    iwd, mana_cost, colors, type_line, is_elite, stats_loaded."""


@dataclass
class DraftState:
    """Tracks the evolving state of a single draft session."""

    set_code: str = ""
    event_name: str = ""
    draft_format: str = ""  # 17Lands format string, e.g. "QuickDraft"
    pack_number: int = 0
    pick_number: int = 0
    current_pack: list[str] = field(default_factory=list)
    pool: list[str] = field(default_factory=list)
    draft_active: bool = False

    # Cards the player saw but did not pick (for wheel tracking / signals).
    seen_cards: list[SeenCardEntry] = field(default_factory=list)

    # Pick-by-pick history for the navigator. Keyed by (pack_number, pick_number).
    pick_history: dict[tuple[int, int], PickHistoryEntry] = field(default_factory=dict)

    # Previous pack contents (used to compute "seen" when the next pack arrives).
    _prev_pack: list[str] = field(default_factory=list)
    _prev_pack_number: int = -1
    _prev_pick_number: int = -1

    # Card the player most recently picked. Consumed by the next /api/predict
    # call so the server can backfill draft-history rows with the actual pick.
    last_pick: str | None = None

    def on_draft_start(self, event_name: str) -> None:
        """Called when the player joins a draft event."""
        same_event = event_name == self.event_name and event_name
        self.event_name = event_name
        self.draft_active = True
        self.pool.clear()
        self.current_pack.clear()
        self.seen_cards.clear()
        # Preserve pick history on rejoin (same event) so navigation works.
        if not same_event:
            self.pick_history.clear()
        self._prev_pack.clear()
        self._prev_pack_number = -1
        self._prev_pick_number = -1
        self.pack_number = 0
        self.pick_number = 0

        code = extract_set_code(event_name)
        if code:
            self.set_code = code
        fmt = extract_draft_format(event_name)
        if fmt:
            self.draft_format = fmt
        logger.info(
            "Draft started: event=%s set=%s format=%s",
            event_name, self.set_code or "unknown", self.draft_format or "unknown",
        )

    def on_pack(
        self,
        card_names: list[str],
        pack_number: int,
        pick_number: int,
    ) -> None:
        """Called when a new pack is presented."""
        # Record cards from previous pack that were *not* picked as "seen".
        self._record_seen_from_prev_pack()

        self._prev_pack = list(self.current_pack)
        self._prev_pack_number = self.pack_number
        self._prev_pick_number = self.pick_number

        self.current_pack = list(card_names)
        self.pack_number = pack_number
        self.pick_number = pick_number
        self.draft_active = True
        logger.info(
            "Pack received: P%dP%d (%d cards)",
            pack_number + 1,
            pick_number + 1,
            len(card_names),
        )

    def on_pick(self, card_name: str) -> None:
        """Called when a pick is made — add card to pool."""
        self.pool.append(card_name)
        # Remove from current pack if present
        if card_name in self.current_pack:
            self.current_pack.remove(card_name)
        self.last_pick = card_name
        logger.info(
            "Picked: %s (pool size: %d)", card_name, len(self.pool)
        )

    def reset(self) -> None:
        """Reset for a new draft."""
        self.set_code = ""
        self.event_name = ""
        self.pack_number = 0
        self.pick_number = 0
        self.current_pack.clear()
        self.pool.clear()
        self.seen_cards.clear()
        self.last_pick = None
        self._prev_pack.clear()
        self._prev_pack_number = -1
        self._prev_pick_number = -1
        self.draft_active = False

    # -- internal helpers -------------------------------------------------------

    def _record_seen_from_prev_pack(self) -> None:
        """Record unpicked cards from the previous pack as seen."""
        if not self._prev_pack:
            return
        picked_set = set(self.pool)
        for name in self._prev_pack:
            if name not in picked_set:
                self.seen_cards.append(
                    SeenCardEntry(
                        card_name=name,
                        pack_number=self._prev_pack_number,
                        pick_number=self._prev_pick_number,
                    )
                )

    def infer_picked_cards(self) -> None:
        """Fill in missing ``picked_card`` in history by checking the pool.

        For each history entry without a ``picked_card``, looks for a card
        in the entry's pack that is also in the pool — that must be the pick.
        """
        pool_set = set(self.pool)
        for entry in self.pick_history.values():
            if entry.picked_card:
                continue
            # The picked card is the one from this pack that ended up in pool.
            candidates = [d["card"] for d in entry.picks if d["card"] in pool_set]
            if len(candidates) == 1:
                entry.picked_card = candidates[0]
                logger.debug(
                    "Inferred picked card for P%dP%d: %s",
                    entry.pack_number + 1,
                    entry.pick_number + 1,
                    candidates[0],
                )

    # -- state persistence ------------------------------------------------------

    _STATE_FILE = "draft_state.json"
    _HISTORY_FILE = "pick_history.json"

    def save_state(self, cache_dir: Path) -> None:
        """Persist pool and draft metadata to disk.

        Args:
            cache_dir: Directory to write the state file into.
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "event_name": self.event_name,
            "set_code": self.set_code,
            "draft_format": self.draft_format,
            "pool": list(self.pool),
            "pack_number": self.pack_number,
            "pick_number": self.pick_number,
            "seen_cards": [
                {
                    "card_name": s.card_name,
                    "pack_number": s.pack_number,
                    "pick_number": s.pick_number,
                }
                for s in self.seen_cards
            ],
        }
        path = cache_dir / self._STATE_FILE
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.debug("Saved draft state (%d pool cards) to %s", len(self.pool), path)

    def restore_pool_if_needed(self, cache_dir: Path) -> bool:
        """Restore pool from persisted state if current pool is incomplete.

        Only restores if the saved state matches the current event and has
        more cards in the pool than the current state.

        Args:
            cache_dir: Directory containing the state file.

        Returns:
            True if state was restored, False otherwise.
        """
        path = cache_dir / self._STATE_FILE
        if not path.exists():
            return False

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read persisted draft state")
            return False

        saved_event = data.get("event_name", "")
        saved_pool: list[str] = data.get("pool", [])

        # Only restore if same event and saved pool is larger.
        if saved_event != self.event_name or not saved_event:
            return False
        if len(saved_pool) <= len(self.pool):
            return False

        self.pool = saved_pool
        # Also restore seen cards if current is empty.
        if not self.seen_cards:
            for s in data.get("seen_cards", []):
                self.seen_cards.append(
                    SeenCardEntry(
                        card_name=s["card_name"],
                        pack_number=s["pack_number"],
                        pick_number=s["pick_number"],
                    )
                )
        logger.info(
            "Restored pool from disk: %d cards (event=%s)",
            len(self.pool),
            saved_event,
        )
        return True

    def save_pick_history(self, cache_dir: Path) -> None:
        """Persist the pick history to disk.

        Args:
            cache_dir: Directory to write the history file into.
        """
        cache_dir.mkdir(parents=True, exist_ok=True)
        entries: list[dict] = []
        for (pn, pkn), entry in sorted(self.pick_history.items()):
            entries.append({
                "pack_number": entry.pack_number,
                "pick_number": entry.pick_number,
                "picked_card": entry.picked_card,
                "picks": entry.picks,
            })
        data = {"event_name": self.event_name, "entries": entries}
        path = cache_dir / self._HISTORY_FILE
        path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        logger.debug("Saved pick history (%d entries) to %s", len(entries), path)

    def load_pick_history(self, cache_dir: Path) -> bool:
        """Load pick history from disk if it matches the current event.

        Args:
            cache_dir: Directory containing the history file.

        Returns:
            True if history was loaded, False otherwise.
        """
        path = cache_dir / self._HISTORY_FILE
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read pick history file")
            return False

        if data.get("event_name") != self.event_name or not self.event_name:
            return False

        for e in data.get("entries", []):
            key = (e["pack_number"], e["pick_number"])
            self.pick_history[key] = PickHistoryEntry(
                pack_number=e["pack_number"],
                pick_number=e["pick_number"],
                picked_card=e.get("picked_card", ""),
                picks=e.get("picks", []),
            )
        logger.info("Loaded pick history: %d entries", len(self.pick_history))
        return True
