"""Advance Time (standard spellbook, 93 Magic, boostable; 15 cosmic and 20 time runes): 3 casts a game week (reset
Wednesday 00:00), each pushing one chosen activity a day forward (the wiki's Advance Time page). The casts are one
budget shared by every target, so they are allocated once (`allocation`, cached on the Context) and the base camp's
share feeds its station supply (basecamp.advance_days).

Each target's worth per cast, in training or gathering hours saved:
  * Anachronia base camp: 24 hours of the workers' gathering (60 a worker an hour), each resource at what it buys at its
    Dream of Iaia station (2 an xp), as training hours saved in that skill. A resource whose station isn't built is
    worth nothing here (the base camp track lists the station).
  * Managing Miscellania: one day of the current subjects' output (kingdom.py; the export's allocation), each item at
    the gatherer's cost per unit when a plan needs it, less the day's pay (5,000 coins a subject) as money-making time.
  * Archaeology research: completes a running research. Counted as a 24-hour run at the best dig site the level allows
    (the wiki's suggestion), its xp as training hours saved, less the chronotes it costs.
  * Player-owned farms, player-owned port voyages, Arc contracts: listed, not valued (not modelled).
Which casts are already used this week isn't exported: 3 are assumed free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CASTS_PER_WEEK = 3
MAGIC_LEVEL = 93
RUNES = {"Cosmic rune": 15, "Time rune": 20}
DAY_HOURS = 24
UNVALUED = ("Player-owned farms (Manor Farm and Anachronia: a day of growth and breeding)",
            "Player-owned port (finishes every voyage)", "The Arc (resets the contract cap)")


@dataclass
class Target:
    name: str
    hours: float  # hours saved per cast
    how: str
    feeds: dict[str, float] = field(default_factory=dict)  # base camp resources a cast brings in


def _base_camp(ctx) -> Target | None:
    from . import basecamp
    from .recommend.plans import xp_hours_saved

    p = ctx.player
    if not basecamp.known(p):
        return None
    crew = basecamp.workers(p)
    per_hour = basecamp.data()["workers"]["per_hour"]
    stations = basecamp.data()["dream_of_iaia"]["stations"]
    gained = {r: crew[r] * per_hour * DAY_HOURS for r in basecamp.RESOURCES}
    hours, parts = 0.0, []
    for skill, r in stations.items():
        if basecamp.station_xp(p, skill) is None or not gained[r]:
            continue
        xp = gained[r] / basecamp.data()["dream_of_iaia"]["resources_per_xp"]
        h = xp_hours_saved(ctx, skill, xp)
        hours += h
        parts.append(f"{xp:,.0f} {skill} xp")
    how = f"a day of base camp gathering ({sum(gained.values()):,} resources: " + (", ".join(parts) or "no station built") + ")"
    return Target("Anachronia base camp", hours, how, gained)


def _miscellania(ctx) -> Target | None:
    from . import kingdom
    from .recommend import gathering

    p = ctx.player
    if not kingdom.managed(p) or kingdom.coffers(p) is None:
        return None
    needs = set(ctx.needs()) if not ctx._finding_needs else set()
    hours, parts, subjects = 0.0, [], 0
    for category, var in kingdom.WORKER_VARS.items():
        n = kingdom._var(p, var) or 0
        if not n:
            continue
        subjects += n
        setting = kingdom.current_setting(p, category)
        if setting is None:
            continue
        for item, per in kingdom.per_subject_day(p, category, setting).items():
            got = per * n
            src = ctx.gatherer.complete_source(item) if item in needs else None
            if src is not None:
                hours += got * ctx.gatherer.net_cost(src)
                parts.append(f"{got:,.0f} {item}")
    pay = subjects * kingdom.COINS_PER_SUBJECT_DAY / gathering.COINS_PER_HOUR
    return Target("Managing Miscellania", hours - pay,
                  f"a day of the kingdom's {subjects} subjects (" + (", ".join(parts) or "nothing a plan needs")
                  + f"), less {subjects * kingdom.COINS_PER_SUBJECT_DAY:,} coins of pay")


def _research(ctx) -> Target | None:
    from . import archaeology_guild as ag
    from .recommend.plans import xp_hours_saved

    p = ctx.player
    level = p.level("Archaeology")
    best = ag.best_research(level)
    if not best:
        return None
    site, info = best
    xp = ag.research_xp(level, info["factor"], p) * DAY_HOURS
    chronotes = ag.research_cost(level, info["factor"], p) * DAY_HOURS
    src = ctx.gatherer.complete_source("chronotes")
    cost = chronotes * ctx.gatherer.net_cost(src) if src else 0.0
    return Target("Archaeology research", xp_hours_saved(ctx, "Archaeology", xp) - cost,
                  f"a 24-hour research run at {site} finished at once ({xp:,.0f} Archaeology xp for "
                  f"{chronotes:,.0f} chronotes)")


def targets(ctx) -> list[Target]:
    """Every valued target, best first."""
    found = [t for t in (_base_camp(ctx), _miscellania(ctx), _research(ctx)) if t is not None]
    return sorted(found, key=lambda t: -t.hours)


def allocation(ctx) -> dict[str, int]:
    """Casts a week per target: all three to the best one (each cast's worth doesn't depend on the others).
    Empty when the player can't cast it (members only) or nothing is worth a cast."""
    if ctx._advance_time is not None:
        return ctx._advance_time
    out: dict[str, int] = {}
    ctx._advance_time = out  # placeholder: valuing Miscellania runs level plans, which ask for the base camp's share
    if ctx.player.members:
        best = next(iter(targets(ctx)), None)
        if best is not None and best.hours > 0:
            out[best.name] = CASTS_PER_WEEK
    ctx._advance_time = out
    return out
