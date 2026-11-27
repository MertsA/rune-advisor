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

From the export (plugin 0.5.0; beta 2026-11-26): the coffers (MISC_COFFERS, gp) are pay already deposited, so their
subject-days cost no new coins (`Ledger.pay`); approval (MISC_APPROVAL, of 127: 107 showed as 84%), each category's
subjects (MISC_POINTS_<category>) and its setting (MISC_HERBS_OR_FLAX 0 = herbs, MISC_COOKED 0 = raw; the hardwood
setting's encoding is unknown) are shown in the profile (`note`): plans choose their own split.
"""

from __future__ import annotations

from . import workforce
from .models import PlayerState
from .workforce import Workforce

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


COFFERS_VAR, APPROVAL_VAR, APPROVAL_MAX = "MISC_COFFERS", "MISC_APPROVAL", 127
WORKER_VARS = {"wood": "MISC_POINTS_WOOD", "mining": "MISC_POINTS_MINE", "fishing": "MISC_POINTS_FISH",
               "herbs": "MISC_POINTS_HERB", "hardwood": "MISC_POINTS_RAREWOOD", "farm": "MISC_POINTS_FARM"}


def _var(player: PlayerState, name: str) -> int | None:
    value = (getattr(player, "vars", None) or {}).get(name)
    return value if isinstance(value, int) and value >= 0 else None


def coffers(player: PlayerState) -> int | None:
    return _var(player, COFFERS_VAR)


def current_setting(player: PlayerState, category: str) -> str | None:
    """The setting a category runs on now, from the export: herbs or flax, raw or cooked fish; the one-setting
    categories as they are. None for hardwood (MISC_RAREWOOD_TYPE's encoding is unknown) and when not exported."""
    if category == "herbs":
        v = _var(player, "MISC_HERBS_OR_FLAX")
        return None if v is None else ("flax" if v else "herbs")
    if category == "fishing":
        v = _var(player, "MISC_COOKED")
        return None if v is None else ("cooked" if v else "raw")
    if category == "hardwood":
        return None
    settings = CATEGORIES.get(category) or {}
    return next(iter(settings)) if len(settings) == 1 else None


def note(player: PlayerState) -> str | None:
    """The kingdom as the export shows it, for the profile."""
    if not managed(player) or (approval := _var(player, APPROVAL_VAR)) is None:
        return None
    setting = {"herbs": "flax" if _var(player, "MISC_HERBS_OR_FLAX") else "herbs",
               "fishing": "cooked" if _var(player, "MISC_COOKED") else "raw"}
    workers = ", ".join(f"{n} {c}" + (f" ({setting[c]})" if c in setting else "")
                        for c, v in WORKER_VARS.items() if (n := _var(player, v)))
    gp = coffers(player) or 0
    pct = approval / APPROVAL_MAX
    text = (f"Miscellania: approval {pct:.0%}, coffers {gp:,} gp (~{gp / COINS_PER_SUBJECT_DAY:,.0f} subject-days of "
            f"pay, used first), subjects: {workers or 'none assigned'}")
    if pct < 0.95:
        text += "; plans assume approval near 100%: top it up"
    return text


def items() -> frozenset[str]:
    return frozenset(i for settings in CATEGORIES.values() for items in settings.values() for i in items)


class _Kingdom(Workforce):
    """Managing Miscellania as a workforce (workforce.py): 10 or 15 subjects, at most 10 a category, 5,000 coins of pay
    a subject-day, the exported coffers already paid."""

    name, worker, cost_per_worker_day = "Managing Miscellania", "subject", COINS_PER_SUBJECT_DAY

    def workers(self, player: PlayerState) -> int:
        return subjects(player)

    def per_category(self, player: PlayerState) -> int:
        return SUBJECTS_PER_CATEGORY

    def output(self, player: PlayerState, category: str, setting: str) -> dict[str, float]:
        return per_subject_day(player, category, setting)

    def prepaid(self, player: PlayerState) -> float:
        return (coffers(player) or 0) / COINS_PER_SUBJECT_DAY


KINGDOM = _Kingdom()


class Ledger(workforce.Ledger):
    """The kingdom's bookings (workforce.Ledger): subject-days per (category, setting) over a plan's window, at most 10
    subjects a category and the player's subjects in all; items that come together (tuna and swordfish) share the
    subject-days bought for either; the coffers pay first."""

    def __init__(self, player: PlayerState, days: float):
        super().__init__(KINGDOM, player, days)
