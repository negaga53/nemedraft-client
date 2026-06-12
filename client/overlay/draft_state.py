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
    r"(?:^|_)(?:PremierDraft|QuickDraft|BotDraft|TradDraft|CompDraft|ContenderDraft|PickTwoDraft)"
    r"[_]([A-Z0-9]{3})(?=_|$)",
    re.IGNORECASE,
)

# Reversed format used in SceneChange context, e.g. "TMT_Premier_Draft".
_CONTEXT_SET_RE = re.compile(
    r"(?:^|_)([A-Z0-9]{3})[_]"
    r"(?:Premier[_]?Draft|Quick[_]?Draft|Bot[_]?Draft|Trad[_]?Draft|Comp[_]?Draft|Contender[_]?Draft|PickTwo[_]?Draft)"
    r"(?=_|$)",
    re.IGNORECASE,
)

# Midweek Magic's SOS Cascade bot draft can show up as
# "MWM_SOS_Cascade_BotDraft_..." rather than the normal format-first shape.
_CASCADE_BOT_DRAFT_SET_RE = re.compile(
    r"(?:^|_)(SOS|CAS)[_](?:Cascade|CAS)[_](?:Bot[_]?Draft)(?=_|$)",
    re.IGNORECASE,
)

_SET_CODE_ALIASES: dict[str, str] = {
    # Arena uses CAS as a pseudo-set code for the SOS Cascade bot-draft event.
    "CAS": "SOS",
}

# Mapping from Arena event prefix to 17Lands format string.
_FORMAT_MAP: dict[str, str] = {
    "PremierDraft": "PremierDraft",
    "QuickDraft": "QuickDraft",
    "BotDraft": "QuickDraft",
    "TradDraft": "TradDraft",
    "CompDraft": "PremierDraft",
    "ContenderDraft": "PremierDraft",
    # PickTwo has no 17Lands sample; reuse PremierDraft stats.
    "PickTwoDraft": "PremierDraft",
}

_EVENT_FORMAT_RE = re.compile(
    r"(PremierDraft|QuickDraft|BotDraft|TradDraft|CompDraft|ContenderDraft|PickTwoDraft)",
    re.IGNORECASE,
)

# Supported draft format keywords (case-insensitive substrings).
# Anything containing "draft" but NOT matching these is unsupported.
_SUPPORTED_FORMAT_KEYWORDS = {"premier", "quick", "bot", "trad", "comp", "contender", "picktwo"}

# Raw Arena format token, normalized to a canonical display string. Matches
# both the forward join name ("PickTwoDraft_SOS_…") and the reversed
# SceneChange context ("SOS_PickTwo_Draft"). This is sent to the server as
# `arena_format` (mechanics + troubleshooting) — distinct from the 17Lands
# stats format above.
_ARENA_FORMAT_RE = re.compile(
    r"(Premier|Quick|Bot|Trad|Comp|Contender|PickTwo)[_]?Draft",
    re.IGNORECASE,
)
_ARENA_FORMAT_CANON: dict[str, str] = {
    "premier": "PremierDraft",
    "quick": "QuickDraft",
    "bot": "BotDraft",
    "trad": "TradDraft",
    "comp": "CompDraft",
    "contender": "ContenderDraft",
    "picktwo": "PickTwo",
}


def extract_set_code(event_name: str) -> str | None:
    """Extract the set code from an Arena event name.

    Args:
        event_name: Arena event name, e.g. "PremierDraft_TMT_20250401".

    Returns:
        Uppercase set code if found, else None. The server's
        ``supported_sets`` health check is the authority on whether the
        model has been trained on the returned set.
    """
    m = _CASCADE_BOT_DRAFT_SET_RE.search(event_name)
    if m:
        return _normalize_set_code(m.group(1))
    m = _EVENT_SET_RE.search(event_name)
    if m:
        return _normalize_set_code(m.group(1))
    # Reversed format (SceneChange context), e.g. "TMT_Premier_Draft".
    m = _CONTEXT_SET_RE.search(event_name)
    if m:
        return _normalize_set_code(m.group(1))
    return None


def _normalize_set_code(code: str) -> str:
    """Return the production set code for an Arena event-set token."""
    upper = code.upper()
    return _SET_CODE_ALIASES.get(upper, upper)


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


def extract_arena_format(event_name: str) -> str:
    """Extract the canonical raw Arena format token from an event name.

    Args:
        event_name: Arena event name or SceneChange context, e.g.
            ``"PickTwoDraft_SOS_20260601"`` or ``"SOS_PickTwo_Draft"``.

    Returns:
        A canonical token (``"PremierDraft"``, ``"QuickDraft"``,
        ``"PickTwo"``, …) or ``""`` if no draft format is recognized. This
        is the server's source of truth for draft mechanics and is logged
        in draft history for troubleshooting.
    """
    m = _ARENA_FORMAT_RE.search(event_name)
    if m:
        return _ARENA_FORMAT_CANON.get(m.group(1).lower(), "")
    return ""


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


@dataclass(frozen=True)
class DraftFormatProfile:
    """Format-specific draft mechanics, keyed by the Arena format token.

    Centralizes the numbers that were previously hardcoded as a 3×14×1
    booster draft. PickTwo numbers were verified against a real Player.log on
    2026-06-01 (see the design spec §7).
    """

    arena_format: str
    cards_per_pick: int
    recommend_count: int
    picks_per_pack: int
    num_packs: int = 3


_DEFAULT_PROFILE = DraftFormatProfile(
    arena_format="", cards_per_pick=1, recommend_count=1, picks_per_pack=14,
)
_PROFILES: dict[str, DraftFormatProfile] = {
    "picktwo": DraftFormatProfile(
        arena_format="PickTwo", cards_per_pick=2, recommend_count=2, picks_per_pack=7,
    ),
}


def profile_for(arena_format: str) -> DraftFormatProfile:
    """Return the draft mechanics for *arena_format* (case-insensitive).

    Unknown / empty formats fall back to the single-pick default, which
    leaves all existing behavior byte-identical.
    """
    return _PROFILES.get(arena_format.strip().lower(), _DEFAULT_PROFILE)


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
    # Every card taken at this coordinate. PickTwo takes two; single-pick
    # formats one. picked_card stays = picked_cards[0] for callers that show
    # a single pick.
    picked_cards: list[str] = field(default_factory=list)
    picks: list[dict] = field(default_factory=list)
    """Each dict mirrors a serialised :class:`Pick`: card, rank, score, gihwr, ata,
    iwd, mana_cost, colors, type_line, is_elite, stats_loaded."""


@dataclass
class DraftState:
    """Tracks the evolving state of a single draft session."""

    set_code: str = ""
    event_name: str = ""
    draft_format: str = ""  # 17Lands format string, e.g. "QuickDraft"
    arena_format: str = ""  # canonical Arena format token, e.g. "PickTwo"
    cards_per_pick: int = 1
    recommend_count: int = 1
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
        self.arena_format = extract_arena_format(event_name)
        profile = profile_for(self.arena_format)
        self.cards_per_pick = profile.cards_per_pick
        self.recommend_count = profile.recommend_count
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
        self.draft_format = ""
        self.arena_format = ""
        self.cards_per_pick = 1
        self.recommend_count = 1
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
                if not entry.picked_cards:
                    entry.picked_cards = [candidates[0]]
                logger.debug(
                    "Inferred picked card for P%dP%d: %s",
                    entry.pack_number + 1,
                    entry.pick_number + 1,
                    candidates[0],
                )

    def reconstruct_pick_history_from_pool(self) -> None:
        """Synthesise provisional history entries for picks made before the
        overlay observed the draft.

        On a mid-draft attach only the current pack is seen (the MemoryWatcher's
        first poll, or a ``Player.log`` whose early packs rotated out), so the
        navigator would have a single entry and stay dead. The cumulative pool
        still records *which* card was taken at each past pick, so map the pool
        back onto ``(pack, pick)`` coordinates. The full pack ranking is left
        empty — it was never observed. Existing entries (including live-scored
        ones) are never overwritten.
        """
        cpp = max(1, self.cards_per_pick)
        picks_made = len(self.pool) // cpp
        if picks_made == 0:
            return
        if self.pack_number > 0:
            # Packs are uniform; derive their size from the known position.
            pack_size = (picks_made - self.pick_number) // self.pack_number
            if pack_size <= 0:
                pack_size = picks_made
        else:
            # Everything taken so far belongs to the opening pack.
            pack_size = max(picks_made, self.pick_number + 1)
        for g in range(picks_made):
            pack, pick = divmod(g, pack_size)
            key = (pack, pick)
            if key in self.pick_history:
                continue
            taken = self.pool[g * cpp:(g + 1) * cpp]
            self.pick_history[key] = PickHistoryEntry(
                pack_number=pack,
                pick_number=pick,
                picked_card=taken[0] if taken else "",
                picked_cards=list(taken),
                picks=[],
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

        # NOTE: the format fields (arena_format / cards_per_pick /
        # recommend_count) are NOT persisted or restored here — they are set
        # by on_draft_start, which always runs before this method on reconnect.

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
                "picked_cards": entry.picked_cards,
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
            picked_card = e.get("picked_card", "")
            self.pick_history[key] = PickHistoryEntry(
                pack_number=e["pack_number"],
                pick_number=e["pick_number"],
                picked_card=picked_card,
                picked_cards=e.get("picked_cards") or ([picked_card] if picked_card else []),
                picks=e.get("picks", []),
            )
        logger.info("Loaded pick history: %d entries", len(self.pick_history))
        return True
