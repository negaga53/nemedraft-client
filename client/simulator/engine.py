"""Draft engine — pack generation, bot AI, and pack-passing logic.

Pack generation models the MTG Arena Play Booster used in Premier Draft
since *Murders at Karlov Manor* (Feb 2024). Each 14-card pack has this
slot structure:

1. Slots 1-6: non-land commons
2. Slot 7:    non-land common (tabletop's The List slot; Arena has no
              List so it's a seventh common)
3. Slots 8-10: uncommons
4. Slot 11:   rare or mythic (1-in-7.4 mythic ≈ 13.5%)
5. Slot 12:   land slot — common land-cycle card, basic land, or an
              upgraded uncommon/rare/mythic land
6. Slots 13-14: two wildcards — any rarity, drawn from the set. Arena
              replaces tabletop's foil slot with a second wildcard.

Wildcard rarity distribution (per slot) follows the published Play
Booster analysis: ~70% common, ~17.5% uncommon, ~10.7% rare, ~1.8%
mythic. This lets a single pack hold 1-4 rares, matching the observed
~58% / ~37% / ~4% / <1% distribution in real packs.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

# Rarity weights for the simple bot picker.
_RARITY_WEIGHT: dict[str, float] = {
    "mythic": 5.0,
    "rare": 4.0,
    "uncommon": 2.5,
    "common": 1.0,
}

# Arena Play Booster — 14 cards, foil slot replaced by a second wildcard.
_PACK_SIZE = 14

# Probability the rare slot upgrades to a mythic (1 in 7.4 ≈ 13.5%).
_MYTHIC_RATE = 1.0 / 7.4

# Wildcard slot rarity distribution. Derived from the Play Booster
# average of 1.4 commons / 0.35 uncommons / 0.25 rares across the two
# wildcard slots (≈0.7/0.175/0.125 per slot), with the rare fraction
# split 6.4 : 1 rare : mythic.
_WILDCARD_COMMON = 0.700
_WILDCARD_UNCOMMON = 0.175
_WILDCARD_RARE = 0.108  # ~10.8%
_WILDCARD_MYTHIC = 0.017  # ~1.7%

# Land-slot upgrade chances. Most packs show a common-cycle or basic
# land; a minority upgrade to a rarer land.
_LAND_RARE_UPGRADE = 0.03   # 3% rare/mythic land
_LAND_UNCOMMON_UPGRADE = 0.07  # 7% uncommon land
# Remaining ~90% splits between common-cycle lands and basic lands.
_LAND_COMMON_CYCLE_SHARE = 0.70


@dataclass
class DraftCard:
    """Minimal card representation for the simulator."""

    name: str
    mana_cost: str
    cmc: float
    type_line: str
    oracle_text: str
    rarity: str
    colors: list[str]
    color_identity: list[str]
    power: str
    toughness: str
    set_code: str


def load_set_cards(scryfall_path: Path, set_code: str) -> dict[str, list[DraftCard]]:
    """Load cards from a Scryfall JSON file grouped by rarity.

    Args:
        scryfall_path: Path to the per-set Scryfall JSON (e.g. ``tmt_cards.json``).
        set_code: Uppercase set code.

    Returns:
        Dict mapping rarity → list of :class:`DraftCard`.
    """
    with open(scryfall_path, encoding="utf-8") as f:
        raw = json.load(f)

    by_rarity: dict[str, list[DraftCard]] = {
        "common": [],
        "uncommon": [],
        "rare": [],
        "mythic": [],
    }
    for c in raw:
        rarity = c.get("rarity", "common")
        if rarity not in by_rarity:
            continue
        card = DraftCard(
            name=c["name"],
            mana_cost=c.get("mana_cost") or "",
            cmc=c.get("cmc", 0.0),
            type_line=c.get("type_line") or "",
            oracle_text=c.get("oracle_text") or "",
            rarity=rarity,
            colors=c.get("colors") or [],
            color_identity=c.get("color_identity") or [],
            power=c.get("power") or "",
            toughness=c.get("toughness") or "",
            set_code=set_code,
        )
        by_rarity[rarity].append(card)

    return by_rarity


class DraftEngine:
    """Simulates an 8-player booster draft with simple bot opponents.

    Args:
        cards_by_rarity: Card pool grouped by rarity.
        set_code: The draft set code.
        pack_size: Cards per pack (default 15).
        num_packs: Number of pack rounds (default 3).
        num_players: Total drafters including the human (default 8).
        seed: Random seed for reproducibility.
    """

    def __init__(
        self,
        cards_by_rarity: dict[str, list[DraftCard]],
        set_code: str,
        *,
        pack_size: int = _PACK_SIZE,
        num_packs: int = 3,
        num_players: int = 8,
        seed: int | None = None,
    ) -> None:
        self._set_code = set_code
        self._pack_size = pack_size
        self._num_packs = num_packs
        self._num_players = num_players
        self._cards = cards_by_rarity
        self._rng = random.Random(seed)
        self._partition_pools()

        # State
        self._seats: list[list[DraftCard]] = [[] for _ in range(num_players)]
        self._bot_pools: list[list[DraftCard]] = [[] for _ in range(num_players)]
        self._player_pool: list[DraftCard] = []
        self._pack_round = 0
        self._pick_in_round = 0
        self._draft_complete = False

        # Pre-generate all packs.
        self._all_packs: list[list[list[DraftCard]]] = []
        self._generate_all_packs()
        self._deal_round()

    # -- public properties ---------------------------------------------------

    @property
    def set_code(self) -> str:
        return self._set_code

    @property
    def pack_number(self) -> int:
        return self._pack_round

    @property
    def pick_number(self) -> int:
        return self._pick_in_round

    @property
    def player_pool(self) -> list[DraftCard]:
        return self._player_pool

    @property
    def is_draft_complete(self) -> bool:
        return self._draft_complete

    @property
    def total_picks(self) -> int:
        return self._num_packs * self._pack_size

    # -- public API ----------------------------------------------------------

    def get_current_pack(self) -> list[DraftCard] | None:
        """Return the pack currently available to the human player."""
        if self._draft_complete:
            return None
        return list(self._seats[0])

    def player_pick(self, card_name: str) -> bool:
        """Execute the human player's pick and advance the draft.

        Returns:
            *True* if the pick was valid, *False* otherwise.
        """
        if self._draft_complete:
            return False

        pack = self._seats[0]
        picked_card: DraftCard | None = None
        for i, c in enumerate(pack):
            if c.name == card_name:
                picked_card = pack.pop(i)
                break
        if picked_card is None:
            return False

        self._player_pool.append(picked_card)

        # Bots pick from their packs.
        for idx in range(1, self._num_players):
            bot_pack = self._seats[idx]
            if bot_pack:
                choice = self._bot_pick(bot_pack, self._bot_pools[idx])
                bot_pack.remove(choice)
                self._bot_pools[idx].append(choice)

        # Rotate packs.
        passes_left = (self._pack_round % 2) == 0
        old = self._seats
        if passes_left:
            self._seats = [old[(i + 1) % self._num_players] for i in range(self._num_players)]
        else:
            self._seats = [old[(i - 1) % self._num_players] for i in range(self._num_players)]

        self._pick_in_round += 1

        # Check if round is finished.
        if self._pick_in_round >= self._pack_size:
            self._pack_round += 1
            self._pick_in_round = 0
            if self._pack_round >= self._num_packs:
                self._draft_complete = True
            else:
                self._deal_round()

        return True

    # -- internals -----------------------------------------------------------

    def _partition_pools(self) -> None:
        """Split the rarity pools into land / non-land subsets.

        Arena Play Boosters route lands through a dedicated slot, so the
        common/uncommon main slots should exclude lands, and the land
        slot should be able to find the right candidates in one lookup.
        """
        self._non_land: dict[str, list[DraftCard]] = {}
        self._lands_by_rarity: dict[str, list[DraftCard]] = {}
        self._basics: list[DraftCard] = []

        for rarity, cards in self._cards.items():
            non_land: list[DraftCard] = []
            lands: list[DraftCard] = []
            for c in cards:
                is_land = "Land" in c.type_line
                is_basic = "Basic" in c.type_line
                if is_basic:
                    self._basics.append(c)
                elif is_land:
                    lands.append(c)
                else:
                    non_land.append(c)
            self._non_land[rarity] = non_land
            self._lands_by_rarity[rarity] = lands

    def _sampler(self, used_names: set[str]):
        """Return a closure that samples *n* unique cards from a pool.

        The closure mutates the shared ``used_names`` set so that every
        slot in a pack draws from the same no-duplicates constraint.
        """

        def sample(pool: list[DraftCard], n: int = 1) -> list[DraftCard]:
            if not pool or n <= 0:
                return []
            picked: list[DraftCard] = []
            attempts = 0
            max_attempts = max(50, n * 20)
            while len(picked) < n and attempts < max_attempts:
                c = self._rng.choice(pool)
                if c.name not in used_names:
                    picked.append(c)
                    used_names.add(c.name)
                attempts += 1
            return picked

        return sample

    def _pick_land_slot(self, sample) -> list[DraftCard]:
        """Pick one card for the dedicated land slot.

        Most sets publish a common dual-land cycle used in the land
        slot; if the set has none, fall back to basics.
        """
        common_lands = self._lands_by_rarity.get("common") or []
        uncommon_lands = self._lands_by_rarity.get("uncommon") or []
        rare_lands = self._lands_by_rarity.get("rare") or []
        mythic_lands = self._lands_by_rarity.get("mythic") or []

        r = self._rng.random()

        if r < _LAND_RARE_UPGRADE and (rare_lands or mythic_lands):
            if mythic_lands and self._rng.random() < _MYTHIC_RATE:
                return sample(mythic_lands, 1)
            if rare_lands:
                return sample(rare_lands, 1)
            return sample(mythic_lands, 1)

        if r < _LAND_RARE_UPGRADE + _LAND_UNCOMMON_UPGRADE and uncommon_lands:
            return sample(uncommon_lands, 1)

        # Common band: split between the common land-cycle and basics.
        if common_lands and self._basics:
            if self._rng.random() < _LAND_COMMON_CYCLE_SHARE:
                return sample(common_lands, 1)
            return sample(self._basics, 1)
        if common_lands:
            return sample(common_lands, 1)
        if self._basics:
            return sample(self._basics, 1)
        # Sets with no land data at all — return nothing; caller pads.
        return []

    def _pick_wildcard_slot(self, sample) -> list[DraftCard]:
        """Pick one card for a wildcard slot (any rarity from the set).

        Uses the published Play Booster wildcard rarity distribution.
        Lands are allowed (the land slot isn't the only place they
        can appear), but basics are excluded — basics don't appear in
        wildcard slots in real packs.
        """
        r = self._rng.random()

        if r < _WILDCARD_MYTHIC:
            pool = self._cards.get("mythic") or []
            return sample(pool, 1) or self._pick_wildcard_fallback(sample, "rare")

        if r < _WILDCARD_MYTHIC + _WILDCARD_RARE:
            pool = self._cards.get("rare") or []
            return sample(pool, 1) or self._pick_wildcard_fallback(sample, "uncommon")

        if r < _WILDCARD_MYTHIC + _WILDCARD_RARE + _WILDCARD_UNCOMMON:
            pool = self._cards.get("uncommon") or []
            return sample(pool, 1) or self._pick_wildcard_fallback(sample, "common")

        # Common wildcard — exclude basics.
        pool = self._non_land.get("common") or self._cards.get("common") or []
        pool = [c for c in pool if "Basic" not in c.type_line] or pool
        return sample(pool, 1)

    def _pick_wildcard_fallback(self, sample, rarity: str) -> list[DraftCard]:
        """Fallback when a wildcard rarity pool is exhausted."""
        pool = self._cards.get(rarity) or []
        return sample(pool, 1)

    def _generate_pack(self) -> list[DraftCard]:
        """Generate a single 14-card Arena Play Booster.

        Slots: 7 non-land commons, 3 uncommons, 1 rare/mythic, 1 land,
        2 wildcards.
        """
        used_names: set[str] = set()
        sample = self._sampler(used_names)
        pack: list[DraftCard] = []

        # Seven commons — strictly non-land. Fall back to any common
        # only if the set lists no non-land commons at all.
        common_pool = self._non_land.get("common") or self._cards.get("common") or []
        pack.extend(sample(common_pool, 7))

        # Three uncommons. Prefer non-land uncommons so the land slot
        # remains the primary source for uncommon lands, mirroring real
        # Play Boosters.
        uncommon_pool = self._non_land.get("uncommon") or self._cards.get("uncommon") or []
        pack.extend(sample(uncommon_pool, 3))

        # Rare or mythic slot (1 in 7.4 mythic).
        if self._rng.random() < _MYTHIC_RATE and self._cards.get("mythic"):
            pack.extend(sample(self._cards["mythic"], 1))
        else:
            rare_pool = self._cards.get("rare") or self._cards.get("mythic") or []
            pack.extend(sample(rare_pool, 1))

        # Dedicated land slot.
        pack.extend(self._pick_land_slot(sample))

        # Two wildcards (Arena replaces tabletop's foil slot with a
        # second wildcard).
        pack.extend(self._pick_wildcard_slot(sample))
        pack.extend(self._pick_wildcard_slot(sample))

        # Pad if any slot came up empty on tiny card pools.
        while len(pack) < self._pack_size:
            any_pool = (
                self._non_land.get("common")
                or self._cards.get("common")
                or self._cards.get("uncommon")
                or []
            )
            extra = sample(any_pool, 1)
            if not extra:
                break
            pack.extend(extra)

        self._rng.shuffle(pack)
        return pack

    def _generate_all_packs(self) -> None:
        for _ in range(self._num_packs):
            round_packs = [self._generate_pack() for _ in range(self._num_players)]
            self._all_packs.append(round_packs)

    def _deal_round(self) -> None:
        for i in range(self._num_players):
            self._seats[i] = list(self._all_packs[self._pack_round][i])

    def _bot_pick(self, pack: list[DraftCard], pool: list[DraftCard]) -> DraftCard:
        """Simple bot AI: prefer higher rarity + colour synergy."""
        pool_colors: set[str] = set()
        for c in pool:
            pool_colors.update(c.colors or [])

        def score(card: DraftCard) -> float:
            s = _RARITY_WEIGHT.get(card.rarity, 1.0)
            card_colors = set(card.colors or [])
            if pool_colors and card_colors:
                if card_colors & pool_colors:
                    s += 1.5
                elif len(pool_colors) >= 2:
                    s -= 0.5
            return s + self._rng.random() * 0.3

        return max(pack, key=score)
