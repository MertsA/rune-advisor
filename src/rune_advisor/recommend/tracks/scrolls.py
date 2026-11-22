"""Dungeoneering scrolls not read yet (traits.py): permanent savings bought once with tokens. Each is worth the hours
it takes off the player's planned training: the level plans of the skills it touches, to their next goal level
(plans.goal_levels), re-planned as if the scroll were read. That is measured, not a guess at what an unlock is
worth. The cost is a trip to the Daemonheim Rewards shop plus earning whatever tokens the player lacks
(`currencies = { "Dungeoneering tokens" = N }` in overrides.toml: the balance isn't exported) and the levels to
buy it. The user's aim (2026-11-22): surface the minute's trip that saves 2% of materials forever.
"""

from __future__ import annotations

import dataclasses

from ... import currencies, traits
from ..base import Candidate, Context
from ..plans import goal_levels

TOKENS = "Dungeoneering tokens"
SHOP_TRIP_HOURS = 2 / 60  # teleport to Daemonheim, buy, read


def what_if(ctx: Context, trait: str) -> Context:
    """The context with `trait` assumed read: a player copy and fresh planner caches (as joint plans make them)."""
    from ..joint import _copy_player

    player = _copy_player(ctx.player)
    player.assumed_traits = set(ctx.player.assumed_traits) | {trait}
    cost = ctx.build_cost(player) if ctx.build_cost else ctx.cost  # simulated rows use the traits too
    return dataclasses.replace(ctx, player=player, cost=cost, _needs=None, _gatherer=None, _finding_needs=False,
                               _activities=None, _urn_options={}, _urn_xp={})


def saved_hours(ctx: Context, scroll: traits.Scroll, goals: dict[str, int]) -> tuple[float, list[str]]:
    """Hours the scroll takes off the planned training of the skills it touches, and a line per skill."""
    total, lines = 0.0, []
    alt = None
    for skill in scroll.affects or (scroll.skill,):
        target = goals.get(skill)
        if not target or ctx.player.level(skill) >= target:
            continue
        alt = alt or what_if(ctx, scroll.trait)
        before, after = ctx.level_plan(skill, target).hours, alt.level_plan(skill, target).hours
        if before - after > 0.05:
            total += before - after
            lines.append(f"{skill} {target}: {before:.1f}h -> {after:.1f}h")
    return total, lines


class _Scrolls:
    name = "scrolls"
    description = "Dungeoneering scrolls not read yet: hours each saves on your planned training, against its tokens"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        if not p.members:
            return []
        goals = goal_levels(ctx)
        balance = p.currencies.get(TOKENS)
        earn = currencies.earning(ctx, TOKENS)
        out = []
        todo = traits.unread(p)
        for i, s in enumerate(todo):
            ctx.report(i, len(todo))
            saved, lines = saved_hours(ctx, s, goals)
            short = s.tokens - (balance or 0)
            cost, needs, unknowns = SHOP_TRIP_HOURS, [], []
            if short > 0 and earn is not None:
                cost += short * earn[0]
                needs.append(f"{short:,} more {TOKENS} ({earn[1]})" if balance is not None
                             else f"{s.tokens:,} {TOKENS} ({earn[1]})")
            if balance is None:
                unknowns.append(f"your {TOKENS} aren't exported: set currencies = {{ \"{TOKENS}\" = N }} in overrides.toml")
            for skill in dict.fromkeys(("Dungeoneering", s.skill)):
                if p.level(skill) < s.level:
                    plan = ctx.level_plan(skill, s.level)
                    cost += plan.hours
                    needs.append(f"{s.level} {skill} (have {p.level(skill)}, ~{plan.hours:.1f}h)")
            why = f"{s.effect}; saves ~{saved:.1f}h on " + ", ".join(lines) if lines \
                else f"{s.effect}; nothing planned it would shorten yet"
            out.append(Candidate(
                track=self.name, title=f"Buy and read the {s.name} ({s.tokens:,} tokens)", value=round(saved, 1), unit="h saved",
                cost_hours=cost, details=[s.effect, *lines], why=why, needs=needs, unknowns=unknowns,
                skills={s.skill}, play_hours=cost))
        return sorted(out, key=lambda c: -(c.value - c.cost_hours))


ScrollsTrack = _Scrolls()
