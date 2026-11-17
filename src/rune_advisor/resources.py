"""Bonus-XP resources the planner can spend instead of grinding: lamps, stars, penguin points,
weekly/daily D&Ds, etc.

A resource has a small acquisition cost (hours to go claim it) and gives xp, either a fixed
amount or scaled by the target skill's level. The planner spends resources first when they
beat grinding xp/hr, so a goal like "level 90 Woodcutting for achievement X" can be mostly
paid for by lamps sitting in the bank.

Definitions come from src/rune_advisor/data/xp_resources.toml plus [[resources]] in rune_advisor.toml.
Stock (how many units you have) comes from the definition, or from the exported inventories
(bank + backpack + worn...) via `item`. Two kinds need no definition: task-set antique lamps held as items
("Antique lamp (Elite Ardougne Tasks)", the wiki's name, which data/item_names.json uses) take their xp and
level floor from data/achievement_xp.json; and penguin points (PlayerState.penguin_points, from
overrides.toml: no exported var holds them, TOKEN_DD_PENGUIN_HIDE_SEEK reads 0) buy Spy Club lamps at
Larry/Chuck, 1/2/4/8 points for a small/medium/large/huge prismatic lamp's xp, so a point is a small lamp
(or 10,000 coins, not modelled).

Bonus xp (https://runescape.wiki/w/Bonus_experience) works differently from lamps: it isn't xp
itself but a per-skill pool paid out 1:1 on top of trained xp until it runs out, so it can at
most halve a grind. Sources are the player's banked pool (PlayerState.bonus_xp), fallen stars in
the bank, and Fort Forinthry rested xp (PlayerState.rested_xp: 100 points = one small prismatic
star in the chosen skill, all cashed in at once with Aster). Free-to-play players can't use it.
"""

from __future__ import annotations

import json
import math
import re
import tomllib
from functools import cache
from dataclasses import dataclass, field
from . import datapkg
from typing import Any, Callable

from .models import PlayerState
from .xp import SKILL_ALIASES, lamp_xp, level_at_xp, star_xp


@dataclass
class Resource:
    name: str
    source: str = ""
    # xp per unit = xp_fixed + xp_per_level * current level of the skill it is used on.
    xp_fixed: float = 0.0
    xp_per_level: float = 0.0
    # Standard lamp size (small/medium/large/huge/giant): xp comes from the wiki lamp table.
    lamp: str | None = None
    # Fallen star size: gives *bonus* xp (star_xp(level, size) * scale per unit), see module doc.
    star: str | None = None
    scale: float = 1.0
    # Every unit is used in one go (rested xp is cashed in all at once).
    all_at_once: bool = False
    units: int = 0
    hours_per_unit: float = 0.0
    skills: set[str] | None = None  # None = any skill
    cadence: str = "stock"  # stock | daily | weekly | once
    item: str | None = None  # item name to count across the player's exported inventories
    # Only count the item when the export's item id matches (variants sharing a name: the two Effigy lamps).
    item_id: int | None = None
    # The skill must be at least this level to take the xp (task-set antique lamps: 85 for an elite set).
    min_level: int = 1
    verified: bool = False  # False until the xp formula has been checked against the wiki
    # Bonus xp that should be placed now, not kept for a later grind: the free_xp track picks its skill and level
    # plans leave it alone. Black pearls cannot be banked (a backpack slot until cracked); rested xp stops
    # accruing at the Town Hall's cap.
    spend_first: bool = False
    # xp per unit at a level, for rewards with a curve of their own (reward books); beats xp_fixed/xp_per_level.
    xp_fn: Callable[[int], float] | None = None

    def xp_per_unit(self, level: int, skill: str | None = None) -> float:
        if self.xp_fn:
            return self.xp_fn(level)
        if self.star:
            return star_xp(level, self.star) * self.scale
        if self.lamp:
            return lamp_xp(level, self.lamp)
        # No elite-skill discount: the wiki's Invention page says fixed rewards are halved there, but an Antique lamp
        # (Elite Ardougne Tasks) rubbed on Invention 85 paid the full 50,000 (tested on the beta, 2026-11-11). `skill`
        # is kept for resources that may yet pay by skill.
        return self.xp_fixed + self.xp_per_level * level

    @property
    def bonus(self) -> bool:
        return self.star is not None

    def applies_to(self, skill: str) -> bool:
        return self.skills is None or skill in self.skills


@dataclass
class ResourcePool:
    resources: list[Resource] = field(default_factory=list)

    def for_skill(self, skill: str) -> list[Resource]:
        return [r for r in self.resources if r.units > 0 and r.applies_to(skill)]


def _parse(entry: dict[str, Any]) -> Resource:
    skills = entry.get("skills")
    return Resource(
        name=entry["name"], source=entry.get("source", ""),
        xp_fixed=float(entry.get("xp_fixed", 0)), xp_per_level=float(entry.get("xp_per_level", 0)),
        units=int(entry.get("units", 0)), hours_per_unit=float(entry.get("hours_per_unit", 0)),
        skills={SKILL_ALIASES.get(s, s) for s in skills} if skills else None, cadence=entry.get("cadence", "stock"),
        item=entry.get("item"), item_id=entry.get("item_id"), lamp=entry.get("lamp"), star=entry.get("star"),
        min_level=int(entry.get("min_level", 1)),
        # Lamp and star xp come from the wiki table, so those are verified by construction.
        verified=bool(entry.get("verified", entry.get("lamp") is not None or entry.get("star") is not None)),
    )


def load_pool(player: PlayerState, extra: list[dict[str, Any]] | None = None) -> ResourcePool:
    raw = tomllib.loads(datapkg.path("xp_resources.toml").read_text())
    pool = [_parse(e) for e in raw.get("resources", []) + (extra or [])]
    for r in pool:
        if r.item and r.item in player.holdings:
            if r.item_id is not None and player.item_ids.get(r.item) != r.item_id:
                continue
            r.units = player.holdings[r.item]
    pool += star_resources(player.holdings)
    pool += book_resources(player)
    pool += set_lamp_resources(player.holdings)
    if player.penguin_points:
        pool.append(Resource(name="Penguin points", source="Larry/Chuck at the Ardougne Zoo (Spy Club lamps)",
                             lamp="small", units=player.penguin_points, hours_per_unit=PENGUIN_POINT_HOURS,
                             verified=True))
    if player.rested_xp:
        pool.append(Resource(name="rested xp", source="Aster (Fort Forinthry Town Hall)", star="small",
                             scale=0.01, units=player.rested_xp, all_at_once=True, cadence="regen",
                             spend_first=player.rested_xp >= RESTED_CAPS[0],
                             hours_per_unit=0.1 / player.rested_xp, verified=True))
    return ResourcePool(pool)


# A completed task set's antique lamps, named as the wiki does ("Antique lamp (Elite Ardougne Tasks)").
_SET_LAMP = re.compile(r"^Antique lamp \((?P<tier>Easy|Medium|Hard|Elite) (?P<area>.+) Tasks\)$")
LAMP_RUB_HOURS = 0.002  # a click
# One Spy Club trip spread over a full balance (250 points): 1 point = a small lamp, 8 = a huge one, bought in bulk.
PENGUIN_POINT_HOURS = 0.1 / 250


@cache
def _achievement_xp() -> dict[str, dict]:
    return json.loads(datapkg.path("achievement_xp.json").read_text())


def set_lamp_resources(holdings: dict[str, int]) -> list[Resource]:
    """Task-set antique lamps among the player's items: a fixed xp in any skill at or above the set's level
    floor (data/achievement_xp.json, the same rows task_sets values a set's reward by)."""
    found = []
    for name, count in holdings.items():
        if not (m := _SET_LAMP.match(name)):
            continue
        rows = (_achievement_xp().get(f"{m['area']} Set Tasks - {m['tier']}") or {}).get("choice") or []
        if not rows:
            continue
        skills = rows[0].get("skills")
        found.append(Resource(name=name, source=f"{m['area']} {m['tier'].lower()} tasks", xp_fixed=float(rows[0]["xp"]),
                              min_level=int(rows[0].get("min_level", 1)), units=count, hours_per_unit=LAMP_RUB_HOURS,
                              skills=None if skills in (None, "any") else {SKILL_ALIASES.get(x, x) for x in skills},
                              verified=True))
    return found


# "Small prismatic fallen star", "Bonus XP star (huge)", "Large fallen star (Attack)"...
_STAR_NAMES = [
    re.compile(r"^(?P<size>small|medium|large|huge|giant) prismatic fallen star$", re.I),
    re.compile(r"^bonus xp star \((?P<size>small|medium|large|huge)\)$", re.I),
    re.compile(r"^(?P<size>small|medium|large|huge) fallen star \((?P<skill>[a-z]+)\)$", re.I),
]


# Items that pay a fallen star's bonus xp under another name: the black pearls from the weekly Agoroth fight
# ("experience equal to that of a medium fallen star", the wiki's Black pearl page; any skill). The quest's
# own pearls, "Black pearl (A Shadow over Ashdale)", pay differently and are not counted.
STAR_LIKE = {"Black pearl": "medium"}
UNBANKABLE = {"Black pearl"}  # the wiki's infobox: bankable = No
# Rested xp held stops growing at the Town Hall's cap (tier 1: 500, tier 2: 1,000, tier 3: 1,500; the wiki's Town
# Hall tiers). The tier is not exported, so from the lowest cap up the balance may be full.
RESTED_CAPS = (500, 1_000, 1_500)


def star_resources(holdings: dict[str, int]) -> list[Resource]:
    """Bonus-xp stars among the player's items (prismatic ones work on any skill)."""
    found = []
    for name, count in holdings.items():
        if name in STAR_LIKE:
            found.append(Resource(name=name, star=STAR_LIKE[name], units=count, verified=True,
                                  spend_first=name in UNBANKABLE, hours_per_unit=LAMP_RUB_HOURS))
            continue
        for pattern in _STAR_NAMES:
            if m := pattern.match(name):
                skill = (m.groupdict().get("skill") or "").capitalize()
                skill = SKILL_ALIASES.get(skill, skill)
                found.append(Resource(name=name, star=m["size"].lower(), units=count, verified=True,
                                      skills={skill} if skill else None))
                break
    return found


def troll_book_xp(level: int) -> float:
    """Troll Invasion's reward book after a full defence: 8 x (L^2 - 2L + 100) in the chosen skill (the wiki's
    formula; a partial run pays its share of the waves)."""
    return 8 * (level * level - 2 * level + 100)


# Rush of Blood's reward book: the wiki gives three figures only, at level 99 after all 20 waves (bronze 7,500,
# silver 13,153, platinum 23,294) and says the xp depends on the challenge, the waves and the level. The item
# does not say which run it came from, so it counts as the least of them, and the level curve is assumed to be
# the Troll Invasion book's shape.
RUSH_BOOK_XP_AT_99 = 7_500


def rush_book_xp(level: int) -> float:
    return RUSH_BOOK_XP_AT_99 * troll_book_xp(level) / troll_book_xp(99)


# The two books are both "Reward book" in game; the export's item id tells them apart.
TROLL_BOOK_IDS, RUSH_BOOK_IDS = {20935, 20936}, {32193}


def book_resources(player: PlayerState) -> list[Resource]:
    """D&D reward books among the player's items: xp in any skill, read whenever the player likes."""
    found = []
    for name, count in player.holdings.items():
        item_id = player.item_ids.get(name)
        if item_id in TROLL_BOOK_IDS or (item_id is None and name == "Reward book"):
            found.append(Resource(name="Reward book (Troll Invasion; a full defence assumed)", source="Troll Invasion",
                                  xp_fn=troll_book_xp, units=count, hours_per_unit=LAMP_RUB_HOURS, verified=True))
        elif item_id in RUSH_BOOK_IDS or (item_id is None and name == "Reward book (Rush of Blood)"):
            found.append(Resource(name="Reward book (Rush of Blood; at least this: a bronze run assumed)",
                                  source="Rush of Blood", xp_fn=rush_book_xp, units=count,
                                  hours_per_unit=LAMP_RUB_HOURS))
    return found


@dataclass
class LevelPlan:
    hours: float
    steps: list[str]
    resource_xp: float = 0.0
    # Bonus xp paid out during the grind (the grind itself is that much shorter in actions).
    bonus_xp: float = 0.0
    grind_hours: float = 0.0
    # Part of the grind needs materials we have no recipe data for (so hours are a lower bound).
    materials_unmodelled: bool = False
    # Gathering the missing materials: coins spent in shops, and xp other skills get along the way.
    gather_coins: float = 0.0
    alch_gp: float = 0.0  # coins from high-alching the training's drops that this plan spent on shops
    calendar_hours: float = 0.0  # farming growth time the plan waits on (not play time)
    reputation: dict[str, float] = field(default_factory=dict)  # Menaphos reputation earned, by faction
    byproduct_xp: dict[str, float] = field(default_factory=dict)
    # What the plan uses up (for planning several goals in a row): resource units by name, and items.
    resources_used: dict[str, int] = field(default_factory=dict)
    items_used: dict[str, int] = field(default_factory=dict)
    # Materials the grind needs beyond the player's holdings (gathering isn't modelled yet).
    materials_short: dict[str, int] = field(default_factory=dict)
    # Urn payouts during the grind (boosts.best_urn): like bonus xp, the grind is that much shorter in actions.
    urn_xp: float = 0.0


def plan_level(cost, pool: ResourcePool | None, skill: str, current_xp: int, current_level: int,
               target_xp: int, banked_bonus: float = 0.0, use_bonus: bool = True, urn=None) -> LevelPlan:
    """Cheapest mix of resources + grinding to go from current_xp to target_xp.

    Lamps (and other direct-xp resources) are spent first when they beat grinding; the rest is
    trained, with bonus xp (`banked_bonus` already in the skill, then rested xp and stars as
    needed) paid out on top. `use_bonus=False` for free-to-play, where bonus xp can't be used.
    `urn` (a boosts.Urn) pays `urn.share` of the trained xp on top; making the urns costs time.

    Each call assumes the whole pool is available (goals are ranked independently);
    a future multi-goal planner should deduct resources as it commits to goals.
    """
    need = target_xp - current_xp
    if need <= 0:
        return LevelPlan(0.0, [])
    hours, steps, used_xp = 0.0, [], 0.0
    used: dict[str, int] = {}
    grind_rate = cost.rate(skill, current_level)
    options = []
    for r in (pool.for_skill(skill) if pool else []):
        xp = r.xp_per_unit(current_level, skill)
        if r.bonus or xp <= 0 or current_level < r.min_level:
            continue
        per_hour = xp / r.hours_per_unit if r.hours_per_unit > 0 else math.inf
        if per_hour > grind_rate:
            options.append((per_hour, r))
    for _, r in sorted(options, key=lambda o: o[0], reverse=True):
        if need <= 0:
            break
        # One unit at a time: level-scaled xp grows as each unit raises the level.
        n, gained = 0, 0.0
        while n < r.units and need > 0:
            xp = r.xp_per_unit(level_at_xp(skill, current_xp + used_xp), skill)
            n, gained, used_xp, need = n + 1, gained + xp, used_xp + xp, need - xp
        hours += n * r.hours_per_unit
        used[r.name] = used.get(r.name, 0) + n
        flag = "" if r.verified else " (xp unverified)"
        steps.append(f"use {n} x {r.name}{f' from {r.source}' if r.source else ''} on {skill} (~{gained:,.0f} xp){flag}")
    plan = LevelPlan(hours, steps, used_xp, resources_used=used)
    if need <= 0:
        return plan
    start = int(current_xp + used_xp)
    bonus = _gather_bonus(plan, pool, skill, level_at_xp(skill, start), need, banked_bonus) if use_bonus else 0.0
    if banked_bonus and not use_bonus:
        steps.append(f"{banked_bonus:,.0f} banked {skill} bonus xp can't be used on free-to-play")
    # Each trained xp is worth 1 + bonus (while bonus lasts) + urn share. Bonus xp pays 1:1 on the
    # first `bonus` trained xp, so it covers (2 + u) * bonus of the climb; after that, 1 + u per xp.
    u = urn.share if urn else 0.0
    boosted_to = min(target_xp, int(start + (2 + u) * bonus))
    grind = cost.hours_for_xp(skill, start, boosted_to) / (2 + u) + cost.hours_for_xp(skill, boosted_to, target_xp) / (1 + u)
    trained = (boosted_to - start) / (2 + u) + (target_xp - boosted_to) / (1 + u)
    plan.bonus_xp, plan.grind_hours = (boosted_to - start) / (2 + u), grind
    plan.hours += grind
    if urn:
        plan.urn_xp = trained * u
        n = urn.count(trained)
        # Getting the urns' inputs can train this skill itself (mining clay for mining urns): that xp is
        # trained xp too (and fills urns), so the grind shrinks by it. Other skills' xp is a by-product.
        own = n * urn.material_xp.get(skill, 0.0)
        if own > 0 and trained > 0:
            keep = max(0.0, 1 - own / trained)
            plan.hours -= grind * (1 - keep)
            plan.grind_hours = grind = grind * keep
        plan.hours += n * urn.hours_each
        if urn.rune:
            plan.items_used[urn.rune] = plan.items_used.get(urn.rune, 0) + n
        how = f"make them (~{n * urn.hours_each:.1f}h incl. clay)" if urn.hours_each else "buy or make them"
        if urn.crafting_xp:
            plan.byproduct_xp["Crafting"] = plan.byproduct_xp.get("Crafting", 0) + n * urn.crafting_xp
            how += f", ~{n * urn.crafting_xp:,.0f} Crafting xp"
        for s_, v in urn.material_xp.items():
            plan.byproduct_xp[s_] = plan.byproduct_xp.get(s_, 0) + n * v
        if urn.material_xp:
            how += ", their inputs give " + ", ".join(f"~{n * v:,.0f} {s_} xp" for s_, v in sorted(urn.material_xp.items()))
            if own > 0:
                how += f" ({skill}: counted toward the goal)"
        if urn.enhanced:
            how += ", urn enhancer: +25%, 20 charges each"
        steps.append(f"carry {n:,} {urn.name}s ({how}"
                     + (f", {n:,} {urn.rune}" if urn.rune else "") + f"): +{plan.urn_xp:,.0f} xp")
    if used_xp or plan.bonus_xp or urn:
        steps.append(f"then train {skill} ~{grind:.1f}h")
    return plan


def _gather_bonus(plan: LevelPlan, pool: ResourcePool | None, skill: str, level: int, need: float,
                  banked: float) -> float:
    """Bonus xp available for a grind of `need` xp: what's banked, topped up from rested xp and
    stars until it covers half the grind (more can't be paid out)."""
    bonus = banked
    if banked:
        plan.steps.append(f"{banked:,.0f} banked {skill} bonus xp pays out during the grind")
    # Rested xp first: it refills for free while logged out, stars don't come back.
    extras = sorted((r for r in (pool.for_skill(skill) if pool else []) if r.bonus),
                    key=lambda r: (not r.all_at_once, r.skills is None))
    for r in extras:
        if bonus >= need / 2:
            break
        n = r.units if r.all_at_once else min(r.units, math.ceil((need / 2 - bonus) / r.xp_per_unit(level)))
        gained = n * r.xp_per_unit(level)
        bonus += gained
        plan.hours += n * r.hours_per_unit
        plan.resources_used[r.name] = plan.resources_used.get(r.name, 0) + n
        if r.all_at_once:
            refill = " (refills 4/h while logged out)" if r.cadence == "regen" else ""
            plan.steps.append(f"cash in {n:,} {r.name} with {r.source} for {skill} bonus xp (~{gained:,.0f}){refill}")
        else:
            plan.steps.append(f"use {n} x {r.name} on {skill} (~{gained:,.0f} bonus xp)")
    return bonus
