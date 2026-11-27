"""Fort Forinthry buildings not yet at tier 3 (fort.py): the next tier of each, costed and valued.

Cost: the Construction level for the blueprint, the quests it waits on, the frames and stone wall segments (the
gatherer for an ironman, the cheaper of the Grand Exchange and gathering otherwise) and the hours at the hotspots;
less what its experience is worth: the tier's Construction experience (with the Workshop's bonus) and the
experience made processing the materials (planks, refined planks, frames, bricks), as training hours saved.
Value: the hours the tier's modelled perks take off the player's planned training (fort.AFFECTS: the Chapel's
Prayer, the Kitchen's Cooking, the Ranger's Workroom's Fletching, the Command Centre's research), re-planned with the
tier built as the scrolls track does; other perks are listed with the wiki's wording, not valued (the user's rule
for unlocks). Nothing is offered while the export doesn't carry the buildings' tiers (plugin before 0.5.0).
"""

from __future__ import annotations

import dataclasses

from ... import fort
from .. import gathering
from ..base import Candidate, Context
from ..plans import Plan, goal_levels, quests_plan, skill_plan, xp_hours_saved
from ...models import QuestStatus

EASTERN_WALL = "Eastern border wall"


def what_if(ctx: Context, building: str, tier: int) -> Context:
    """The context with `building` at `tier`: a player copy and fresh planner caches (tracks/scrolls.what_if)."""
    from ..joint import _copy_player

    player = _copy_player(ctx.player)
    player.vars = {**ctx.player.vars, fort.buildings()[building]["var"]: tier}
    cost = ctx.build_cost(player) if ctx.build_cost else ctx.cost
    return dataclasses.replace(ctx, player=player, cost=cost, _needs=None, _gatherer=None, _finding_needs=False,
                               _activities=None, _urn_options={}, _urn_xp={})


def saved_hours(ctx: Context, building: str, tier: int, goals: dict[str, int]) -> tuple[float, list[str]]:
    total, lines, alt = 0.0, [], None
    for skill in fort.AFFECTS.get(building, ()):
        target = goals.get(skill)
        if not target or ctx.player.level(skill) >= target:
            continue
        alt = alt or what_if(ctx, building, tier)
        before, after = ctx.level_plan(skill, target).hours, alt.level_plan(skill, target).hours
        if before - after > 0.05:
            total += before - after
            lines.append(f"{skill} {target}: {before:.1f}h -> {after:.1f}h")
    return total, lines


def materials_plan(ctx: Context, need: dict[str, int]) -> Plan:
    """The materials less what the player holds: gathered (ironman) or the cheaper of buying and gathering."""
    p = ctx.player
    short = {item: n - p.holdings.get(item, 0) for item, n in need.items() if n > p.holdings.get(item, 0)}
    held = [f"{min(n, p.holdings.get(item, 0)):,} {item}" for item, n in need.items() if p.holdings.get(item, 0)]
    plan = Plan(steps=[f"use {', '.join(held)} from the bank"] if held else [])
    if not short:
        return plan
    g = ctx.gatherer
    if p.ironman:
        got = g.plan(short)
        coins, transfers = ctx.cover_coins(got.coins)
        hours = got.hours + coins / gathering.COINS_PER_HOUR
        plan.add(Plan(hours=hours, uncertain=bool(got.short), steps=got.steps + transfers,
                      unknowns=[f"no source known for {', '.join(got.short)}"] if got.short else [],
                      reward_hours=sum(xp_hours_saved(ctx, s, xp) for s, xp in got.xp.items()),
                      needs=[f"{', '.join(f'{n:,} {i}' for i, n in short.items())} (~{hours:.1f}h)"]))
        return plan
    for item, n in short.items():
        v = ctx.prices.value(item) if ctx.prices is not None else None
        src = g.complete_source(item)
        buy_h = v.gp * n / gathering.COINS_PER_HOUR if v else None
        gather_h = n * g.net_cost(src) if src else None
        if gather_h is not None and (buy_h is None or gather_h < buy_h):
            plan.add(Plan(hours=gather_h, steps=[f"make {n:,} {item}: {src.how} (~{gather_h:.1f}h)"],
                          reward_hours=sum(xp_hours_saved(ctx, s, per * n) for s, per in src.xp_per_unit.items())))
        elif buy_h is not None:
            plan.add(Plan(hours=buy_h, steps=[f"buy {n:,} {item} (~{v.gp * n:,.0f} gp, ~{buy_h:.1f}h of money making)"]))
        else:
            plan.add(Plan(uncertain=True, unknowns=[f"no source or price for {item}"]))
    return plan


def tier_plan(ctx: Context, building: str, tier: int) -> Plan:
    """Everything building `tier` of `building` takes, from the tier below it."""
    p = ctx.player
    row = fort.tier_row(building, tier)
    plan = skill_plan(ctx, fort.SKILL, row["level"])
    done, start = fort.quests_needed(building, tier)
    unfinished = [q for q in done if (s := p.quests.get(q)) is None or s.status != QuestStatus.COMPLETED]
    unstarted = [q for q in start if (s := p.quests.get(q)) is None or s.status == QuestStatus.NOT_STARTED]
    if unfinished or unstarted:
        qp = quests_plan(ctx, unfinished + unstarted)
        if unstarted:
            qp.unknowns.append(f"only starting {', '.join(unstarted)} is needed for tier 1: costed to the end")
            qp.uncertain = True
        plan.add(qp)
    need = dict(row["materials"])
    if building == "Grove" and tier == 1:
        extra = fort.data()["extras"][EASTERN_WALL]
        for item, n in extra["materials"].items():
            need[item] = need.get(item, 0) + n
        plan.add(Plan(hours=0.0, unknowns=[f"the {EASTERN_WALL} comes first: assumed not built (its materials "
                                           "are included)"]))
    plan.add(materials_plan(ctx, need))
    hours = fort.build_hours(building, tier)
    xp = fort.build_xp(p, building, tier)
    plan.add(Plan(hours=hours, skills={fort.SKILL}, reward_hours=xp_hours_saved(ctx, fort.SKILL, xp),
                  steps=[f"build {building} tier {tier} at the Fort Forinthry blueprints: ~{hours:.2f}h at the "
                         f"hotspots for {xp:,.0f} Construction xp"]))
    return plan


class _Fort:
    name = "fort"
    description = "Fort Forinthry buildings' next tiers: hours their perks save on your training, against building them"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        if not p.members or not fort.known(p):
            return []
        goals = goal_levels(ctx)
        out = []
        for building, have in fort.tiers(p).items():
            if have is None or have >= 3:
                continue
            tier = have + 1
            row = fort.tier_row(building, tier)
            plan = tier_plan(ctx, building, tier)
            saved, lines = saved_hours(ctx, building, tier, goals)
            perks = [b for b in row["benefits"] if not b.startswith("Everything from")]
            why = (f"saves ~{saved:.1f}h on " + ", ".join(lines)) if lines else \
                ("; ".join(b.strip() for b in perks[:2]) or "next tier")
            if note := plan.reward_note():
                why += f"; {note}"
            out.append(Candidate(
                track=self.name, title=f"Build the {building} to tier {tier}", value=round(saved, 1), unit="h saved",
                cost_hours=plan.net_hours, details=[*lines, *perks, *plan.steps], why=why, uncertain=plan.uncertain,
                play_hours=plan.hours, **plan.guide_fields()))
        return sorted(out, key=lambda c: (-(c.value - c.cost_hours) if c.value else float("inf"), c.cost_hours))


FortTrack = _Fort()
