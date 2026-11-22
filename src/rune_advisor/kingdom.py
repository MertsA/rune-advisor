"""Managing Miscellania: the kingdom's subjects gather resources for the player (the wiki's Managing Miscellania page).

Subjects: 10 after Throne of Miscellania, 15 after Royal Trouble, split across six categories, at most 10 a category.
Each category has a setting (herbs or flax, raw or cooked fish, mahogany / teak / both), and what a category brings in
is the setting at collection, so a plan can split a category's days between settings by collecting in between. The
fishing category gives tuna and swordfish together.

Pay: the coffers pay out 10% a day up to 50,000 (75,000 after Royal Trouble), shared by the assigned subjects, so a
subject-day costs 5,000 coins either way; Royal Trouble makes each coin gather 50% more (the page's example: 10 maple
subjects on 50,000 a day give 594 logs before it, and the same 594 on two thirds of that money after it). The table's
daily figures are 10 subjects at 100% approval and full pay with Royal Trouble; without it, 2/3 of that per subject.

Assumed: approval kept near 100% (a visit a week, or the Fort's Command Centre table), hardwood and the farm need
Royal Trouble (they are Etceteria's), and the by-products (gems, nests, caskets, herb seeds) aren't counted.
"""

from __future__ import annotations

from .models import PlayerState

SUBJECTS_PER_CATEGORY = 10
COINS_PER_SUBJECT_DAY = 5_000
# One visit a day's worth of collecting and approval upkeep, in hours of play per day of 10 subjects' work.
VISIT_HOURS_PER_DAY = 0.05

_HERBS = {"Grimy tarromin": 13.5, "Grimy harralander": 14, "Grimy ranarr": 3.5, "Grimy irit": 9,
          "Grimy avantoe": 9.5, "Grimy kwuarm": 5, "Grimy cadantine": 3.5, "Grimy lantadyme": 3,
          "Grimy dwarf weed": 3.5}
_FARM = {
    # allotment
    "Potato seed": 93.5, "Onion seed": 69, "Cabbage seed": 34.5, "Tomato seed": 35, "Sweetcorn seed": 13,
    "Strawberry seed": 2.5, "Watermelon seed": 1.5,
    # hops
    "Barley seed": 17.5, "Hammerstone seed": 28.5, "Asgarnian seed": 16.5, "Jute seed": 20.5, "Yanillian seed": 11,
    "Krandorian seed": 6.5, "Wildblood seed": 4.5,
    # flowers
    "Marigold seed": 27, "Rosemary seed": 9.5, "Nasturtium seed": 12, "Woad seed": 4.5, "Limpwurt seed": 7,
    "Starbloom flower seed": 2.5,
    # herbs
    "Guam seed": 6.5, "Marrentill seed": 7, "Tarromin seed": 3, "Irit seed": 2, "Ranarr seed": 1, "Avantoe seed": 1,
    "Snapdragon seed": 0.5, "Torstol seed": 0.5,
    # bushes
    "Redberry seed": 19.5, "Cadavaberry seed": 11.5, "Dwellberry seed": 5, "Jangerberry seed": 2.5,
    "Poison ivy seed": 0.5,
    # special, trees (one of five), fruit trees (one of two), spirit
    "Bittercap mushroom spore": 1.5, "Cactus seed": 0.5, "Cave nightshade seed": 1.5,
    "Acorn": 0.2, "Willow seed": 0.2, "Maple seed": 0.2, "Yew seed": 0.2, "Magic seed": 0.2,
    "Apple tree seed": 0.5, "Orange tree seed": 0.5, "Spirit seed": 1,
}
# category -> setting -> items a day from 10 subjects (Royal Trouble, 100% approval, full pay). Herbs scaled to the
# page's 61 a day from its ranges' midpoints; farm seeds are the ranges' midpoints.
CATEGORIES: dict[str, dict[str, dict[str, float]]] = {
    "mining": {"coal": {"Coal": 546}},
    "wood": {"maple": {"Maple logs": 892}},
    "herbs": {"herbs": {k: v * 61 / sum(_HERBS.values()) for k, v in _HERBS.items()}, "flax": {"Flax": 1250}},
    "fishing": {"raw": {"Raw tuna": 440, "Raw swordfish": 131}, "cooked": {"Tuna": 440, "Swordfish": 131}},
    "hardwood": {"mahogany": {"Mahogany logs": 223}, "teak": {"Teak logs": 301},
                 "both": {"Teak logs": 121, "Mahogany logs": 121}},
    "farm": {"seeds": _FARM},
}
ROYAL_TROUBLE_ONLY = {"hardwood", "farm"}


def _done(player: PlayerState, quest: str) -> bool:
    q = player.quests.get(quest)
    return q is not None and q.done


def managed(player: PlayerState) -> bool:
    return player.members and _done(player, "Throne of Miscellania")


def subjects(player: PlayerState) -> int:
    return 15 if _done(player, "Royal Trouble") else 10


def per_subject_day(player: PlayerState, category: str, setting: str) -> dict[str, float]:
    """Items one subject-day brings in (5,000 coins of pay)."""
    share = 1.0 if _done(player, "Royal Trouble") else 2 / 3
    return {i: n * share / SUBJECTS_PER_CATEGORY for i, n in CATEGORIES[category][setting].items()}


def settings_for(player: PlayerState, item: str) -> list[tuple[str, str, float]]:
    """(category, setting, per subject-day) for each way the player's kingdom brings `item` in, best first."""
    if not managed(player):
        return []
    rt = _done(player, "Royal Trouble")
    out = [(c, s, per_subject_day(player, c, s)[item]) for c, settings in CATEGORIES.items()
           if rt or c not in ROYAL_TROUBLE_ONLY for s, items in settings.items() if item in items]
    return sorted(out, key=lambda t: -t[2])


def items() -> frozenset[str]:
    return frozenset(i for settings in CATEGORIES.values() for items in settings.values() for i in items)


class Ledger:
    """One plan's use of the kingdom over a window of `days`: subject-days per (category, setting), at most 10
    subjects a category and the player's subjects in all, and what each item has drawn from them. Items that come
    together (tuna and swordfish) share the subject-days bought for either."""

    def __init__(self, player: PlayerState, days: float):
        self.player, self.days = player, days
        self.alloc: dict[tuple[str, str], float] = {}
        self.drawn: dict[tuple[str, str, str], float] = {}

    def copy(self) -> Ledger:
        out = Ledger(self.player, self.days)
        out.alloc, out.drawn = dict(self.alloc), dict(self.drawn)
        return out

    def adopt(self, other: Ledger) -> None:
        """Take on another ledger's bookings (a plan's working copy, once the plan is kept)."""
        self.alloc, self.drawn = dict(other.alloc), dict(other.drawn)

    def capacity(self, item: str, category: str, setting: str) -> float:
        """How many of `item` this setting can still bring in: partner subject-days already paid, then free subjects."""
        per = per_subject_day(self.player, category, setting)[item]
        key = (category, setting)
        spare = max(0.0, self.alloc.get(key, 0.0) * per - self.drawn.get((*key, item), 0.0))
        return spare + self._room(category) * per

    def _room(self, category: str) -> float:
        cat = sum(v for (c, _), v in self.alloc.items() if c == category)
        total = sum(self.alloc.values())
        return max(0.0, min(SUBJECTS_PER_CATEGORY * self.days - cat, subjects(self.player) * self.days - total))

    def take(self, item: str, n: float, category: str, setting: str) -> tuple[float, float, float]:
        """Draw up to `n` of `item`: (units from subject-days already bought, units from new ones, new subject-days)."""
        per = per_subject_day(self.player, category, setting)[item]
        key = (category, setting)
        made = self.alloc.get(key, 0.0) * per
        spare = max(0.0, made - self.drawn.get((*key, item), 0.0))
        free = min(n, spare)
        new_days = min((n - free) / per, self._room(category))
        self.alloc[key] = self.alloc.get(key, 0.0) + new_days
        self.drawn[(*key, item)] = self.drawn.get((*key, item), 0.0) + free + new_days * per
        return free, new_days * per, new_days
