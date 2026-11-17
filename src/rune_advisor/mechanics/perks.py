"""Gizmo odds: the exact chance of every perk outcome for materials in a gizmo shell (data/perks.json).

The game's perk generation, as the RuneScape Wiki documents it (Calculator:Perks), implemented from that
description of the mechanics and checked against the percentages the wiki's perk pages publish
(tests/fixtures/perk_checks.json):

  1. Invent budget: 5 rolls (6 in an ancient gizmo) of random(0 .. level // 2 + 19), summed; never below the
     Invention level (boosts count).
  2. Each material adds `base + random(0 .. roll - 1)` to every perk it feeds for this gizmo type, in slot order
     (middle, top, left, right, bottom, then the ancient gizmo's four corners). A normal material in an ancient
     gizmo counts 80% of base and of roll (both floored); the four ancient materials only fit an ancient gizmo.
  3. A perk's rank is the highest threshold its total reaches (ancient-only ranks need an ancient gizmo); rank 0
     costs 0 and can't be generated but still takes part in the sort.
  4. The perks are sorted by rank cost with the game's own quicksort (`quicksort`: its tie-breaking decides which of
     two equal-cost perks wins, which is why slot order matters), then walked from the dearest down: a perk whose
     cost is strictly below the budget is generated and paid for, up to two.
  5. A two-slot perk (Enhanced Devoted, Enhanced Efficient) among them leaves only the first one generated.
     No perk at all: nothing is consumed, the player just tries again.

`gizmo_odds` enumerates every rank combination of the perks in play (their rank chances are independent). For
one combination the outcome is a step function of the budget, changing only where the budget passes a cost or
a sum of two costs, so each outcome's chance is read off the budget's cumulative distribution as a range
(`_generated`). A common material fills a slot with 5 units, anything else with 1 (`slot_units`).
"""

from __future__ import annotations

import json
from bisect import bisect_right
from functools import cache, lru_cache
from itertools import accumulate, product
from math import inf, prod

from .. import datapkg

GIZMOS = ("weapon", "armour", "tool")
ANCIENT_SCALE = 0.8
SLOTS, ANCIENT_SLOTS = 5, 9
BUDGET_ROLLS, ANCIENT_BUDGET_ROLLS = 5, 6
MAX_PERKS = 20  # perks one gizmo can have in play; materials feeding a further one are ignored for it
COMMON_UNITS = 5  # units of a common material ("... parts") to fill one slot
# The Extreme invention potion boosts by one more level at each of these Invention levels (+17 from 52).
EXTREME_BOOST_LEVELS = (25, 30, 33, 36, 38, 40, 42, 43, 44, 45, 47, 48, 49, 49, 50, 51, 52)
MAX_COMBINATIONS = 200_000  # rank combinations gizmo_odds will enumerate before giving up (None)

Outcome = tuple[tuple[str, int], ...]  # ((perk, rank), ...) sorted by perk name; () = no perk generated


@cache
def data() -> dict:
    path = datapkg.path("perks.json")
    return json.loads(path.read_text()) if path.is_file() else {"materials": {}, "perks": {}, "shells": {}}


def materials() -> dict[str, dict]:
    return data()["materials"]


def perks() -> dict[str, dict]:
    return data()["perks"]


def is_ancient_material(material: str) -> bool:
    return bool(materials().get(material, {}).get("ancient"))


def slot_units(material: str) -> int:
    """Units of the material one gizmo slot takes: 5 of a common one (parts), 1 of anything else."""
    return COMMON_UNITS if material.endswith(" parts") else 1


def shell(gizmo: str, ancient: bool = False) -> dict[str, int]:
    """Materials of the gizmo shell itself."""
    return dict(data()["shells"].get(f"ancient {gizmo}" if ancient else gizmo, {}))


def potion_level(level: int, potion: str) -> int:
    """The Invention level with a potion: none, normal (+3), super (+5) or extreme (+1 for every entry of
    EXTREME_BOOST_LEVELS the level has reached)."""
    kind = potion.lower()
    if kind == "extreme":
        return level + bisect_right(EXTREME_BOOST_LEVELS, level)
    return level + {"normal": 3, "super": 5}.get(kind, 0)


def quicksort(arr: list, key=lambda x: x) -> None:
    """The game's sort, in place, ascending by `key`. It has to be this exact procedure, not just any sort: the
    middle element becomes the pivot and is parked at the end, and an element moves in front of the pivot when
    its key is below the pivot's, or equal to it at an odd position. That odd/even rule decides which of two
    equal-cost perks comes out ahead, so nothing simpler gives the game's results."""
    pending = [(0, len(arr) - 1)]
    while pending:
        first, last = pending.pop()
        if first >= last:
            continue
        middle = (first + last) // 2
        arr[middle], arr[last] = arr[last], arr[middle]
        pivot_key = key(arr[last])
        split = first
        for at in range(first, last):
            before = key(arr[at]) < pivot_key or (at % 2 == 1 and key(arr[at]) == pivot_key)
            if before:
                arr[at], arr[split] = arr[split], arr[at]
                split += 1
        arr[split], arr[last] = arr[last], arr[split]
        pending.append((first, split - 1))
        pending.append((split + 1, last))


def _roll_sum(sizes: list[int]) -> list[float]:
    """Probability of each total (index = total) of independent rolls, one per size, each uniform on
    0 .. size - 1. Adding a uniform roll turns a distribution into a moving average of itself, which is a
    difference of two prefix sums."""
    pmf = [1.0]
    for size in sizes:
        if size < 2:
            continue  # a roll that can only be 0
        prefix = [0.0, *accumulate(pmf)]
        top = len(pmf)
        pmf = [(prefix[min(n + 1, top)] - prefix[max(n + 1 - size, 0)]) / size for n in range(top + size - 1)]
    return pmf


def budget_distribution(level: int, ancient: bool = False) -> list[float]:
    """Chance of each invent budget (index = budget). The budget is the rolled total or the level, whichever is
    higher, so everything the rolls put at or below the level sits on the level itself."""
    rolled = _roll_sum([level // 2 + 20] * (ANCIENT_BUDGET_ROLLS if ancient else BUDGET_ROLLS))
    return [0.0] * level + [sum(rolled[:level + 1])] + rolled[level + 1:]


def rank_chances(gizmo: str, mats: tuple[str, ...], ancient: bool = False) -> list[tuple[str, list[tuple[int, int, float]]]]:
    """Per perk the materials feed, in the order they first appear: [(rank, cost, chance)] for every rank with a
    chance, rank 0 (cost 0) included."""
    # What each perk gets from the slots: a fixed part and one roll per contributing material.
    fixed: dict[str, int] = {}
    rolls: dict[str, list[int]] = {}
    for name in mats:
        material = materials().get(name)
        if material is None:
            continue
        weakened = ancient and not material.get("ancient")  # a normal material in an ancient gizmo
        for perk, base, roll in material[gizmo]:
            if perk not in fixed:
                if len(fixed) == MAX_PERKS:
                    continue  # the game tracks no more perks than this
                fixed[perk], rolls[perk] = 0, []
            fixed[perk] += int(base * ANCIENT_SCALE) if weakened else base
            rolls[perk].append(int(roll * ANCIENT_SCALE) if weakened else roll)
    result = []
    for perk, base in fixed.items():
        pmf = _roll_sum(rolls[perk])
        # reach[n]: chance the rolled part is n or more; the perk's value is base + rolled part.
        reach = list(accumulate(reversed(pmf)))[::-1] + [0.0]

        def at_least(value: int) -> float:
            return reach[min(max(value - base, 0), len(pmf))]

        tiers = [(threshold, cost) for threshold, cost, ancient_only in perks()[perk]["ranks"] if ancient or not ancient_only]
        marks = [0, *(threshold for threshold, _ in tiers)]
        costs = [0, *(cost for _, cost in tiers)]
        chances = []
        for rank, mark in enumerate(marks):
            beyond = at_least(marks[rank + 1]) if rank + 1 < len(marks) else 0.0
            chance = at_least(mark) - beyond
            if chance > 0:
                chances.append((rank, costs[rank], chance))
        result.append((perk, chances))
    return result


class _Budget:
    """The invent budget of one attempt as a cumulative distribution, for "how likely is a budget in this range"."""

    def __init__(self, level: int, ancient: bool):
        self.upto = list(accumulate(budget_distribution(level, ancient)))  # upto[b]: chance the budget is <= b

    def between(self, above: float, upto: float) -> float:
        """Chance that above < budget <= upto (upto may be infinity)."""
        def cumulative(b: float) -> float:
            return 0.0 if b < 0 else self.upto[int(min(b, len(self.upto) - 1))]
        return max(cumulative(upto) - cumulative(above), 0.0)


@lru_cache(maxsize=64)
def _budget(level: int, ancient: bool) -> _Budget:
    return _Budget(level, ancient)


def _generated(costs: tuple[int, ...], double: list[bool], budget: _Budget) -> list[tuple[tuple[int, ...], float]]:
    """Which perks (indexes into `costs`) one attempt generates, with the chance of each case, for perks at
    these rank costs.

    The game sorts the perks by cost and offers them dearest first; a perk is generated when its cost is
    strictly below what is left of the budget, and at most two are. Walking a list of falling costs, the first
    perk generated is the one whose cost the budget just exceeds, so each case is a range of budgets:
    perk `a` first when the budget is above its cost but not above the cost of the perk offered before it, and
    perk `b` second when what is left after paying for `a` falls in the same kind of range among the perks
    offered after `a`. The chance of a case is the chance of a budget in its range."""
    offered = list(range(len(costs)))
    quicksort(offered, key=lambda i: costs[i])
    offered = [i for i in reversed(offered) if costs[i] > 0]  # rank 0 is sorted with the rest but never generated
    cases: dict[tuple[int, ...], float] = {}

    def add(chosen: tuple[int, ...], chance: float) -> None:
        if chance > 0:
            if any(double[i] for i in chosen):
                chosen = chosen[:1]  # a two-slot perk leaves room for nothing else: only the first stays
            cases[chosen] = cases.get(chosen, 0.0) + chance

    if not offered:
        return [((), 1.0)]
    add((), budget.between(-1, costs[offered[-1]]))  # not above the cheapest: nothing
    for n, a in enumerate(offered):
        ceiling = costs[offered[n - 1]] if n else inf  # above this the perk offered before `a` goes first
        later = offered[n + 1:]
        # No second perk: what is left does not exceed the cheapest perk still on offer.
        alone_upto = costs[a] + costs[later[-1]] if later else inf
        add((a,), budget.between(costs[a], min(ceiling, alone_upto)))
        for k, b in enumerate(later):
            upto = costs[a] + costs[later[k - 1]] if k else inf
            add((a, b), budget.between(costs[a] + costs[b], min(ceiling, upto)))
    return list(cases.items())


def gizmo_odds(level: int, gizmo: str, mats: tuple[str, ...] | list[str], ancient: bool = False) -> dict[Outcome, float] | None:
    """Chance of each outcome of one attempt, () = no perk (nothing consumed). None when the materials put more
    rank combinations in play than MAX_COMBINATIONS (many common parts each feeding several perks)."""
    return _odds(level, gizmo, tuple(mats), bool(ancient))


@lru_cache(maxsize=50_000)
def _odds(level: int, gizmo: str, mats: tuple[str, ...], ancient: bool) -> dict[Outcome, float] | None:
    in_play = rank_chances(gizmo, mats, ancient)
    if not in_play:
        return {(): 1.0}
    if prod(len(options) for _, options in in_play) > MAX_COMBINATIONS:
        return None
    names = [perk for perk, _ in in_play]
    double = [bool(perks()[perk].get("doubleslot")) for perk in names]
    budget = _budget(level, ancient)
    by_costs: dict[tuple[int, ...], list[tuple[tuple[int, ...], float]]] = {}  # many rank mixes share their costs
    odds: dict[Outcome, float] = {}
    # The perks' ranks are independent, so every mix of ranks has the product of their chances; what it then
    # generates depends on the budget alone.
    for mix in product(*(options for _, options in in_play)):
        weight = prod(chance for _, _, chance in mix)
        costs = tuple(cost for _, cost, _ in mix)
        if costs not in by_costs:
            by_costs[costs] = _generated(costs, double, budget)
        for chosen, chance in by_costs[costs]:
            outcome = tuple(sorted((names[i], mix[i][0]) for i in chosen))
            odds[outcome] = odds.get(outcome, 0.0) + weight * chance
    return odds


def per_gizmo(odds: dict[Outcome, float]) -> dict[Outcome, float]:
    """The odds per gizmo made: the no-perk attempts (free retries) taken out."""
    made = 1.0 - odds.get((), 0.0)
    return {o: p / made for o, p in odds.items() if o} if made > 0 else {}


def chance_of(odds: dict[Outcome, float], want: list[tuple[str, int, int]]) -> float:
    """Chance per gizmo made that it holds every wanted perk at a rank within [low, high]."""
    total = 0.0
    for outcome, p in per_gizmo(odds).items():
        have = dict(outcome)
        if all(low <= have.get(perk, 0) <= high for perk, low, high in want):
            total += p
    return total


def describe(outcome: Outcome) -> str:
    """"Precise 4 + Equilibrium 2"; single-rank perks without the number."""
    if not outcome:
        return "no perk"
    return " + ".join(perk if len(perks()[perk]["ranks"]) == 1 else f"{perk} {rank}" for perk, rank in outcome)
