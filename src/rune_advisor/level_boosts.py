"""Temporary level boosts as a way past a level gate (data/boosts.json, scripts/build_boosts.py).

For an ironman the point of a boost is reaching a recipe or resource a few levels early (smithing armour now
instead of after 30 hours of training), not training faster. So a boostable gate gets two prices and the
cheaper wins (recommend/gathering.py): train to the level, or boost for as long as the gated work takes.

Rules (Temporary skill boost page; the user, 2026-10-30):
  * boosts don't stack: the boosted level is the highest of base + each boost (god banner included);
  * a boost is flat levels plus a percentage of the base level, rounded down;
  * normal boosts decay 1 level a minute, so a +B boost over a gap of g levels lasts B - g + 1 minutes per dose;
    static boosts (overloads, wilder pies) hold for STATIC_MINUTES, or the row's `minutes` (Extreme invention: 6,
    then it resets to the base level); the god banner holds 30 minutes, once a day.
Potions are costed per dose: banked doses first, else the gatherer's cost of making them. The banner needs
`banner = "<god>"` under [players.<name>.boosts] in overrides.toml. Invisible boosts (familiars, crystal saws)
aren't modelled yet.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import cache
from . import datapkg
from typing import TYPE_CHECKING

from .models import PlayerState

if TYPE_CHECKING:
    from .recommend.gathering import Gatherer

STATIC_MINUTES = 5  # an overload's boost is reapplied for five minutes (Temporary skill boost page)
DRINK_SECONDS = 1.8  # a dose: one action plus taking it out of the bank (rough)
_DOSE = re.compile(r"^(.*) \((\d)\)$")


@cache
def boosts() -> list[dict]:
    path = datapkg.path("boosts.json")
    return json.loads(path.read_text())["boosts"] if path.is_file() else []


def boosted(base: int, boost: dict, xp: int | None = None) -> int:
    """The level `boost` takes `base` to. A row with `xp_per_level` (Extreme invention) grows a level per that much
    skill xp up to `flat`; without the xp it is taken at its cap."""
    flat = boost["flat"]
    if xp is not None and boost.get("xp_per_level"):
        flat = min(flat, xp // boost["xp_per_level"])
    return base + flat + math.floor(base * boost["percent"] / 100)


def has_banner(player: PlayerState) -> bool:
    return bool((player.boosts or {}).get("banner"))


def reach(player: PlayerState, skill: str) -> int:
    """The highest level any single boost the player could use takes `skill` to (base if none)."""
    base = player.level(skill)
    return max([base] + [boosted(base, b) for b in boosts() if skill in b["skills"]
                         and (not b.get("daily_minutes") or has_banner(player))])


@dataclass
class BoostPlan:
    boost: str
    level: int  # the boosted level
    doses: int  # 0 for the banner
    item: str | None  # the dose item ("Super woodcutting potion (4)"), None for the banner
    held_doses: int
    hours: float  # play-time equivalent: doses still to make (gatherer cost) and drinking them, or banner days
    calendar_hours: float  # banner sessions spread over days
    steps: list[str]
    to_make: dict[str, int] = field(default_factory=dict)  # dose items still to get, for the plan to gather
    make_hours: float = 0.0  # the gatherer's per-unit estimate for them (part of `hours`)


def _dose_item(name: str, holdings: dict[str, int], made: set[str]) -> tuple[str, int, int] | None:
    """(item to get, doses per item, doses already held) for a boost named `name`: the dose a recipe actually
    makes ("Super woodcutting potion (3)": Herblore; decanting doesn't make doses), or the item itself (a stout)."""
    held = 0
    family = []
    for item, n in holdings.items():
        if (m := _DOSE.match(item)) and m[1] == name:
            held += n * int(m[2])
    for item in made:
        if (m := _DOSE.match(item)) and m[1] == name:
            family.append((int(m[2]), item))
    if family:
        doses, item = max(family)
        return item, doses, held
    if name in made or name in holdings:
        return name, 1, holdings.get(name, 0)
    return None


def plan_boost(gatherer: Gatherer, skill: str, level: int, work_hours: float) -> BoostPlan | None:
    """The cheapest way to hold `skill` at `level` or more for `work_hours` of gated work, or None when no boost
    the player can use reaches it."""
    from .recommend.catalogue import by_output
    from .recommend.gathering import CALENDAR_HOUR_COST

    p = gatherer.ctx.player
    base = p.level(skill)
    if level <= base:
        return None
    holdings, best = p.holdings, None
    made = {o for o, rs in by_output().items() if any(not r.get("conversion") for r in rs)}
    for b in boosts():
        if skill not in b["skills"] or (top := boosted(base, b)) < level:
            continue
        if b.get("daily_minutes"):
            if not has_banner(p):
                continue
            days = math.ceil(work_hours * 60 / b["daily_minutes"])
            plan = BoostPlan(b["label"], top, 0, None, 0, days * 24 * CALENDAR_HOUR_COST, days * 24,
                             [f"boost {skill} to {top} with the {p.boosts['banner']} banner "
                              f"({days:,} day{'s' if days != 1 else ''} of 30-minute sessions)"])
        else:
            found = _dose_item(b["name"], holdings, made)
            if found is None:
                continue
            item, per_item, held = found
            minutes = b.get("minutes", STATIC_MINUTES) if b["static"] else top - level + 1
            doses = math.ceil(work_hours * 60 / minutes)
            to_make = math.ceil(max(0, doses - held) / per_item)
            make_hours = 0.0
            if to_make:
                # A chain with unsourced inputs still counts: the plan gathers it and lists what's missing.
                src = gatherer.source(item)
                if src is None or src.unlock:
                    continue
                make_hours = to_make * src.cost_per_unit
            how = ", ".join(x for x in (f"{min(held, doses):,} banked" if held else "",
                                        f"{to_make:,} {item} to make" if to_make else "") if x)
            plan = BoostPlan(b["label"], top, doses, item, held, doses * DRINK_SECONDS / 3600 + make_hours, 0.0,
                             [f"boost {skill} to {top} with {b['label']} ({doses:,} dose{'s' if doses != 1 else ''}"
                              f", ~{minutes} min each; {how})"], {item: to_make} if to_make else {}, make_hours)
        if best is None or plan.hours < best.hours:
            best = plan
    return best
