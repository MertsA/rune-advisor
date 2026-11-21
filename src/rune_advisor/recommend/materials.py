"""Material-aware grinding: turn "train Fletching to 99" into "fletch N Rune darts from the bank".

A wiki method row may carry a `recipe` (embedded in data/wiki_rates.json from the wiki's
Bucket:Recipe), with one or more variants:
    {"output": "Rune dart", "variants": [{"output_qty": 1, "xp": 18.8, "ticks": 1,
                                          "materials": [["Rune dart tip", 1], ["Feather", 1]]}]}
Variants are per final product, with same-skill intermediates already expanded into total xp and
raw materials (scripts/embed_wiki_recipes.py).
Gathering materials isn't modelled yet: shortfalls are reported as explicit dependencies.

In a Group Ironman group, materials the player lacks can come from groupmates' holdings: the
player's own stock is used first, and each borrowed amount becomes a step for the groupmate
("put N X in group storage or trade them").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import cache

from ..cost import typical_rate


@dataclass
class MaterialRun:
    method: str
    output: str
    actions: int
    produced: int
    rate: float
    needs: list[tuple[str, int, int]] = field(default_factory=list)  # (material, needed, have incl. group)
    transfers: list[tuple[str, str, int]] = field(default_factory=list)  # (from member, material, count)
    recipient: str = ""
    from_level: int = 0  # the level band this run starts at (when a grind is split by level)

    @property
    def shortfall(self) -> dict[str, int]:
        return {name: need - have for name, need, have in self.needs if need > have}

    @property
    def covered(self) -> bool:
        return not self.shortfall

    def describe(self) -> list[str]:
        steps = [f"{member}: put {n:,} {name} in group storage (or trade them) for {self.recipient or 'this player'}"
                 for member, name, n in self.transfers]
        uses = ", ".join(f"{need:,} {name} (have {have:,})" for name, need, have in self.needs)
        band = f"from level {self.from_level}, " if self.from_level else ""
        steps.append(f"{band}{self.method}: make {self.produced:,} {self.output}, using {uses}")
        if self.shortfall:
            steps.append("still need " + ", ".join(f"{n:,} {name}" for name, n in self.shortfall.items())
                         + " (gathering not modelled)")
        return steps


@cache
def members_only_items() -> frozenset[str]:
    """Members' items (the wiki's item data), plus items whose every recipe is a members' recipe."""
    from .catalogue import catalogue, members_items
    free, members = set(), set()
    for r in catalogue():
        (members if r["members"] else free).add(r["output"])
    return frozenset((members - free) | members_items())


def _borrow(name: str, short: int, group: dict[str, dict[str, int]]) -> list[tuple[str, str, int]]:
    """Take `short` of an item from groupmates, biggest stack first."""
    out = []
    for member, stock in sorted(group.items(), key=lambda kv: kv[1].get(name, 0), reverse=True):
        if short <= 0:
            break
        if take := min(short, stock.get(name, 0)):
            out.append((member, name, take))
            short -= take
    return out


def material_run(row: dict, xp_needed: float, holdings: dict[str, int],
                 group: dict[str, dict[str, int]] | None = None, recipient: str = "",
                 members: bool = True) -> MaterialRun | None:
    """Best-covered recipe variant for getting `xp_needed` from this method, or None without recipe data.

    `group` is the other Group Ironman members' holdings, used for whatever `holdings` lacks."""
    group = group or {}
    recipe = row.get("recipe")
    if not recipe or xp_needed <= 0:
        return None
    best = None
    for v in recipe["variants"]:
        # Free-to-play players can't use variants made from members-only materials.
        if not members and any(name in members_only_items() for name, _ in v["materials"]):
            continue
        if not v.get("xp"):
            continue
        actions = math.ceil(xp_needed / v["xp"])
        needs, transfers = [], []
        for name, qty in v["materials"]:
            need, own = math.ceil(qty * actions), holdings.get(name, 0)
            borrowed = _borrow(name, need - own, group)
            transfers += borrowed
            needs.append((name, need, own + sum(n for _, _, n in borrowed)))
        coverage = min((have / need for _, need, have in needs if need), default=1.0)
        run = MaterialRun(method=row["method"], output=recipe["output"], actions=actions,
                          produced=round(actions * v.get("output_qty", 1)), rate=typical_rate(row), needs=needs,
                          transfers=transfers, recipient=recipient)
        if best is None or coverage > best[0]:
            best = (coverage, run)
    return best[1] if best else None


def choose_run(methods: list[dict], xp_needed: float, holdings: dict[str, int],
               group: dict[str, dict[str, int]] | None = None, recipient: str = "",
               members: bool = True) -> MaterialRun | None:
    """Fastest method the player's (and group's) holdings fully cover; else the fastest method with recipe data."""
    runs = [r for r in (material_run(m, xp_needed, holdings, group, recipient, members) for m in methods) if r]
    covered = [r for r in runs if r.covered]
    if covered:
        return max(covered, key=lambda r: r.rate)
    return max(runs, key=lambda r: r.rate) if runs else None
