"""The Anachronia base camp's resources, workers and buildings, and the Dream of Iaia's skilling stations they stock.

Data: data/anachronia_base_camp.json (scripts/build_base_camp.py, from the wiki). State, from the export (0.5.0):
DINO_BASE_CAMP_RESOURCE_<R> (in store), DINO_BASE_CAMP_WORKER_ASSIGNED_<R>, DINO_BASE_CAMP_BUILDING_TIER_<B> and the
one-off structures' flags, QUEST_EGW_FINALE_REWARD_STATION_STATE_<SKILL> (built) and _XP_<SKILL> (xp still stored:
it counts down, 175 an action). The test account on 2026-11-26: 150,000 of each resource (the tier 3 Storehouse's cap), 70
workers (12 each, 11 on hides and stone), every building at tier 3 but the remote totem access point.

Spend or wait (the user, 2026-11-26: the planner weighs it, no fixed rule). An upgrade that can't go ahead yet (a
level, an item such as the Remototem, or an earlier tier missing) holds nothing back: its resources are spent, and only
once it can go ahead does it compete. A ready upgrade is then worth `Context.unlock_value_hours` (the user: "some
inherent boost towards unlocks even if the rewards are unmodeled") plus what its perks save where the planner models
them, against the training its resources would buy at the stations (2 an xp, as training hours saved). It is waited
for (its resources reserved, `reserved`) when it is worth more; otherwise they go to the stations. Without a Context
(no valuation possible) every ready upgrade is waited for. Only what is left (`surplus`) stocks a station, at 2
resources an xp, up to 350,000 stored. Each station takes one resource (wood -> Fletching, vines -> Hunter, leaves ->
Herblore, hides -> Crafting, stone -> Construction, clay -> Fishing).

Supply in hours played, like Managing Miscellania, and through the same workforce ledger (workforce.py, `BASE_CAMP`):
workers gather 60 an hour each around the clock, so a plan's window (its hours played, plus earlier goals' in a joint
plan, at play_hours_per_day) is days of the camp's whole workforce, plus the base camp's Advance Time days
(`advance_days`). A level plan with a station (Context._plan_with_station) counts its stored xp, the spare store and
what all the workers could gather on its resource over that window, at 175 xp every 3 ticks (350,000 an hour); the
kept plan books the worker-days (the step says how many workers to move) and adds the compacted resources its actions
drop to the store, so later goals of a joint plan can stock their stations with them (Context.base_camp). Station xp
is trained xp: banked bonus xp pays out on it (resources.Resource.trained). `worker_values` / `allocation_advice`
are the standing advice between plans: where the workers save the most while no plan says otherwise.
Every xp drop at a station also rolls for rewards (the Dream of Iaia page): a compacted resource 1/50 (400 of one of
the other five resources with a tier 3 Town Hall), Anachronia resource packs, strange and golden rocks, skill pets,
dinosaur farm animals, laboratory relics and the lore pages (more likely with the lorehound): listed, not planned.
"""

from __future__ import annotations

import json
from functools import cache

from . import datapkg
from .models import PlayerState
from .workforce import Ledger, Workforce

RESOURCES = ("WOOD", "VINES", "LEAVES", "PELTS", "STONE", "CLAY")
NAMES = {"WOOD": "wood", "VINES": "vines", "LEAVES": "leaves", "PELTS": "hides", "STONE": "stone", "CLAY": "clay"}
RESOURCE_VAR = "DINO_BASE_CAMP_RESOURCE_{}"
WORKER_VAR = "DINO_BASE_CAMP_WORKER_ASSIGNED_{}"
STATION_XP_VAR = "QUEST_EGW_FINALE_REWARD_STATION_XP_{}"
STATION_STATE_VAR = "QUEST_EGW_FINALE_REWARD_STATION_STATE_{}"
TICK_SECONDS = 0.6


@cache
def data() -> dict:
    return json.loads(datapkg.path("anachronia_base_camp.json").read_text())


def _var(player: PlayerState, name: str) -> int | None:
    value = (getattr(player, "vars", None) or {}).get(name)
    return value if isinstance(value, int) and value >= 0 else None


def known(player: PlayerState) -> bool:
    return any(_var(player, RESOURCE_VAR.format(r)) is not None for r in RESOURCES)


def stock(player: PlayerState) -> dict[str, int]:
    return {r: _var(player, RESOURCE_VAR.format(r)) or 0 for r in RESOURCES}


def workers(player: PlayerState) -> dict[str, int]:
    return {r: _var(player, WORKER_VAR.format(r)) or 0 for r in RESOURCES}


def tier(player: PlayerState, building: str) -> int:
    return _var(player, data()["buildings"][building]["var"]) or 0


def open_upgrades(player: PlayerState) -> list[tuple[str, dict]]:
    """Every building tier and one-off structure not built yet: (name, its row), cheapest tiers first."""
    out = []
    for name, b in data()["buildings"].items():
        have = tier(player, name)
        out += [(f"{name} (tier {row['tier']})", row) for row in b["tiers"] if row["tier"] > have]
    for name, row in data()["one_off"].items():
        if not _var(player, row["var"]):
            out.append((name, row))
    return out


def ready(player: PlayerState, row: dict) -> bool:
    """Whether an upgrade could be built now but for its base camp resources: earlier tiers, levels and items in place."""
    if any(tier(player, b) < t for b, t in (row.get("needs") or {}).items()):
        return False
    if any(player.level(skill) < level for skill, level in (row.get("levels") or {}).items()):
        return False
    return all(player.holdings.get(item, 0) >= n for item, n in (row.get("items") or {}).items())


STATION_OF = {}  # resource -> the station skill it stocks, filled below


def _station_of(resource: str) -> str | None:
    if not STATION_OF:
        STATION_OF.update({r: s for s, r in data()["dream_of_iaia"]["stations"].items()})
    return STATION_OF.get(resource)


SPA_AGILITY = {1: 0.05, 2: 0.07, 3: 0.10}  # base Agility xp on Anachronia, by Spa tier
ANACHRONIA_AGILITY = "Anachronia Agility Course"


def adjust_row(row: dict, player: PlayerState) -> dict:
    """A training row as this player trains it: the Anachronia agility courses pay the Spa's bonus (the guide's figures
    taken as without it)."""
    if row.get("skill") != "Agility" or ANACHRONIA_AGILITY not in row.get("method", ""):
        return row
    bonus = SPA_AGILITY.get(tier(player, "Spa"), 0.0)
    if not bonus:
        return row
    out = {**row}
    for key in ("xp_per_hour_low", "xp_per_hour_high"):
        if out.get(key):
            out[key] = out[key] * (1 + bonus)
    return out


def _upgraded(player: PlayerState, name: str) -> PlayerState | None:
    """A copy of the player with the upgrade built (the new workers of a Sleeping Quarters tier spread evenly over the
    resources whose station is built), or None for an upgrade whose perks aren't modelled."""
    from .recommend.joint import _copy_player

    building, _, rest = name.partition(" (tier ")
    if building not in ("Sleeping Quarters", "Spa") or not rest:
        return None
    n = int(rest.rstrip(")"))
    copy = _copy_player(player)
    copy.vars = {**player.vars, data()["buildings"][building]["var"]: n}
    if building == "Sleeping Quarters":
        extra = data()["workers"]["Sleeping Quarters"][n - 1]
        targets = [r for s, r in data()["dream_of_iaia"]["stations"].items() if station_built(player, s)] or list(RESOURCES)
        for i, r in enumerate(targets):
            key = WORKER_VAR.format(r)
            copy.vars[key] = (copy.vars.get(key) or 0) + extra // len(targets) + (1 if i < extra % len(targets) else 0)
    return copy


def perk_hours(ctx, name: str) -> float:
    """Hours an upgrade's perks save, where the planner models them: re-planned with it built (the scrolls track's
    way) over the skills it touches, to their goal levels. Modelled: Sleeping Quarters (more workers: more station xp an
    hour played) and the Spa (Agility xp on Anachronia). The Storehouse's cap, the lodges' combat and Big Game Hunter
    perks and the Town Hall's gate aren't: their worth is the unlock value."""
    import dataclasses

    from .recommend.plans import goal_levels

    copy = _upgraded(ctx.player, name)
    if copy is None:
        return 0.0
    skills = ("Agility",) if name.startswith("Spa") else tuple(data()["dream_of_iaia"]["stations"])
    goals = goal_levels(ctx)
    alt = dataclasses.replace(ctx, player=copy, cost=ctx.build_cost(copy) if ctx.build_cost else ctx.cost, _needs=None,
                              _gatherer=None, _finding_needs=False, _activities=None, _urn_options={}, _urn_xp={})
    saved = 0.0
    for skill in skills:
        target = goals.get(skill)
        if target and ctx.player.level(skill) < target:
            saved += max(0.0, ctx.level_plan(skill, target).hours - alt.level_plan(skill, target).hours)
    return saved


def station_hours(ctx, need: dict[str, int]) -> float:
    """Training hours the resources would save at their stations (2 an xp; nothing for a station not built)."""
    from .recommend.plans import xp_hours_saved

    per_xp = data()["dream_of_iaia"]["resources_per_xp"]
    total = 0.0
    for r, n in need.items():
        skill = _station_of(r)
        if skill and station_built(ctx.player, skill):
            total += xp_hours_saved(ctx, skill, n / per_xp)
    return total


def decisions(player: PlayerState, ctx=None) -> list[tuple[str, dict, str, str]]:
    """(name, row, "wait" | "spend", why) for every open upgrade."""
    out = []
    for name, row in open_upgrades(player):
        if not ready(player, row):
            out.append((name, row, "spend", "can't go ahead yet: its resources are spent meanwhile"))
            continue
        if ctx is None:
            out.append((name, row, "wait", "ready: its resources are held for it"))
            continue
        worth = ctx.unlock_value_hours + perk_hours(ctx, name)
        lost = station_hours(ctx, row["resources"])
        if worth >= lost:
            out.append((name, row, "wait", f"ready: worth ~{worth:.1f}h, its resources ~{lost:.1f}h at the stations"))
        else:
            out.append((name, row, "spend", f"ready, but its resources save more at the stations (~{lost:.1f}h vs "
                                            f"~{worth:.1f}h)"))
    return out


def reserved(player: PlayerState, ctx=None) -> dict[str, int]:
    """Resources held back for the upgrades waited for (`decisions`)."""
    out = {r: 0 for r in RESOURCES}
    for _, row, choice, _ in decisions(player, ctx):
        if choice == "wait":
            for r, n in row["resources"].items():
                out[r] += n
    return out


def surplus(player: PlayerState, ctx=None) -> dict[str, int]:
    """Resources in store beyond what waited-for upgrades need: what a Dream of Iaia station may take."""
    have, need = stock(player), reserved(player, ctx)
    return {r: max(0, have[r] - need[r]) for r in RESOURCES}


def station_built(player: PlayerState, skill: str) -> bool:
    return bool(_var(player, STATION_STATE_VAR.format(skill.upper())))


def station_xp(player: PlayerState, skill: str, ctx=None) -> tuple[int, int] | None:
    """(xp stored in the skill's station, xp the spare stock can add now), or None when the station isn't built or
    the export doesn't say. The 350,000 cap is on what is stored at once; a plan trains and restocks as it goes, so
    it isn't applied to the stock here."""
    iaia = data()["dream_of_iaia"]
    resource = iaia["stations"].get(skill)
    if resource is None or not station_built(player, skill):
        return None
    stored = _var(player, STATION_XP_VAR.format(skill.upper())) or 0
    return stored, surplus(player, ctx)[resource] // iaia["resources_per_xp"]


def seconds_per_action() -> float:
    return data()["dream_of_iaia"]["ticks_per_action"] * TICK_SECONDS


# Station rewards an action rolls (the Dream of Iaia page): a compacted resource (one of the five the station doesn't
# take) 1/50, worth 100/200/400 of it at Town Hall tier 1/2/3; an Anachronia resource pack 1/1,200; a common
# laboratory relic 1/34; an animal for the dinosaur farm 1/800. Strange and golden rocks, skill pets and the lore pages
# (more likely with the lorehound) roll too: named, not counted.
COMPACTED_CHANCE = 1 / 50
COMPACTED_RESOURCES = {1: 100, 2: 200, 3: 400}
DROP_CHANCES = {"Anachronia resource pack": 1 / 1200, "Laboratory relic (common)": 1 / 34,
                "dinosaur farm animal": 1 / 800}


def station_drops(player: PlayerState, skill: str, actions: int) -> tuple[dict[str, float], list[str]]:
    """Expected base camp resources (other stations' resources) and items from `actions` at a station, and a line each."""
    own = data()["dream_of_iaia"]["stations"].get(skill)
    per = COMPACTED_RESOURCES.get(tier(player, "Town Hall"), 0)
    others = [r for r in RESOURCES if r != own]
    gained = {r: actions * COMPACTED_CHANCE / len(others) * per for r in others} if per else {}
    lines = []
    if gained:
        lines.append(f"~{actions * COMPACTED_CHANCE:,.0f} compacted resources: ~{sum(gained.values()):,.0f} base camp "
                     f"resources for the other stations")
    lines += [f"~{actions * c:,.1f} {name}" for name, c in DROP_CHANCES.items() if actions * c >= 0.05]
    lines.append("and strange/golden rock, skill pet and lore page rolls on every action (lore pages more likely "
                 "with the lorehound)")
    return gained, lines


GATHER = "gather"  # the base camp workforce's one setting per resource


class _BaseCampWorkforce(Workforce):
    """The base camp's workers as a workforce (workforce.py): every worker on any resource (no limit a resource), 60
    of it an hour each, unpaid; the store's spare resources (`surplus`, after the spend-or-wait choice) are the stock."""

    name, worker = "Anachronia base camp", "worker"

    def workers(self, player: PlayerState) -> int:
        w = data()["workers"]
        by_tier = w["start"] + sum(w["Sleeping Quarters"][: tier(player, "Sleeping Quarters")])
        from . import fort

        cc = fort.tier(player, "Command Centre") or 0
        by_tier += sum(n for t, n in w["Command Centre"].items() if cc >= int(t))
        return max(by_tier, sum(workers(player).values()))

    def output(self, player: PlayerState, category: str, setting: str) -> dict[str, float]:
        return {category: data()["workers"]["per_hour"] * 24}

    def stock(self, player: PlayerState, ctx=None) -> dict[str, float]:
        return {r: float(n) for r, n in surplus(player, ctx).items()}


BASE_CAMP = _BaseCampWorkforce()


def ledger(player: PlayerState, ctx=None, days: float = 0.0) -> Ledger:
    """A fresh base camp ledger: no worker-days booked, the spare store as stock."""
    return Ledger(BASE_CAMP, player, days, ctx)


def advance_days(ctx, days: float) -> float:
    """Extra days of gathering the base camp's Advance Time casts add over a window of `days` (3 a week at most)."""
    from . import advance_time

    casts = advance_time.allocation(ctx).get("Anachronia base camp", 0) if ctx is not None else 0
    return casts * days / 7


def worker_values(ctx) -> dict[str, float]:
    """Training hours a worker's day of gathering saves, by resource: what its station xp replaces at the skill's
    training rate, less the station time (350,000 an hour), for a built station whose skill is short of a goal level."""
    from .recommend.plans import goal_levels

    p = ctx.player
    iaia = data()["dream_of_iaia"]
    station_rate = iaia["xp_per_action"] * 3600 / seconds_per_action()
    xp_a_day = data()["workers"]["per_hour"] * 24 / iaia["resources_per_xp"]
    goals = goal_levels(ctx)
    out = {}
    for skill, r in iaia["stations"].items():
        target = goals.get(skill)
        if not station_built(p, skill) or not target or p.level(skill) >= target:
            out[r] = 0.0
            continue
        rate = ctx.cost.rate(skill, p.level(skill))
        out[r] = max(0.0, xp_a_day * (1 / rate - 1 / station_rate)) if rate > 0 else 0.0
    return out


def allocation_advice(ctx) -> tuple[str, float, float] | None:
    """(resource, hours a week saved now, hours a week with every worker on it) when moving all the workers to the
    resource worth the most beats the current split; None otherwise."""
    values = worker_values(ctx)
    crew = workers(ctx.player)
    best = max(values, key=values.get, default=None)
    if best is None or values[best] <= 0:
        return None
    now = 7 * sum(crew[r] * values.get(r, 0.0) for r in RESOURCES)
    then = 7 * sum(crew.values()) * values[best]
    return (best, now, then) if then - now > 0.05 else None


def note(player: PlayerState) -> str | None:
    """The base camp as the export shows it, for the profile (no valuation: ready upgrades counted as waited for)."""
    if not known(player):
        return None
    have, spare, crew = stock(player), surplus(player), workers(player)
    per_day = {r: crew[r] * data()["workers"]["per_hour"] * 24 for r in RESOURCES}
    parts = [f"{NAMES[r]} {have[r]:,}" + (f" ({spare[r]:,} spare)" if spare[r] != have[r] else "")
             + (f", +{per_day[r]:,}/day" if per_day[r] else "") for r in RESOURCES]
    text = "base camp: " + "; ".join(parts)
    waiting = [name for name, _, choice, _ in decisions(player) if choice == "wait"]
    blocked = [name for name, row, choice, _ in decisions(player) if choice == "spend"]
    if waiting:
        text += "; held back for " + ", ".join(waiting)
    if blocked:
        text += "; spent meanwhile (can't go ahead yet): " + ", ".join(blocked)
    return text
