"""This week's Advance Time casts (advance_time.py): the target worth the most hours per cast, with the others for
comparison. Value: hours saved a week (3 casts). Cost: the runes (15 cosmic, 20 time a cast) as the gatherer makes or
buys them, a minute a cast, and 93 Magic when the player is short of it (the spell can be cast on a boost; boosting
isn't costed here, the level is). The same allocation feeds the base camp's station supply.
"""

from __future__ import annotations

from ... import advance_time as at
from ..base import Candidate, Context
from ..plans import Plan, item_plan, skill_plan

CAST_HOURS = 1 / 60


class _AdvanceTime:
    name = "advance_time"
    description = "Advance Time: which activity this week's three casts are worth the most on"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        if not p.members:
            return []
        found = at.targets(ctx)
        chosen = at.allocation(ctx)
        if not chosen:
            return []
        best = next(t for t in found if t.name in chosen)
        casts = chosen[best.name]
        plan = Plan(hours=casts * CAST_HOURS)
        if p.level("Magic") < at.MAGIC_LEVEL:
            plan.add(skill_plan(ctx, "Magic", at.MAGIC_LEVEL))
        for rune, n in at.RUNES.items():
            short = n * casts - p.holdings.get(rune, 0)
            if short > 0:
                got = item_plan(ctx, rune, short)
                plan.add(got if got is not None else Plan(uncertain=True, unknowns=[f"no source known for {rune}"]))
        others = [f"{t.name}: ~{t.hours:.1f}h a cast ({t.how})" for t in found if t is not best]
        return [Candidate(
            track=self.name, title=f"Cast Advance Time {casts}x on {best.name}", value=round(best.hours * casts, 1),
            unit="h saved a week", cost_hours=plan.net_hours, play_hours=plan.hours, uncertain=plan.uncertain,
            why=f"~{best.hours:.1f}h a cast: {best.how}",
            details=[best.how, *others, *(f"not valued: {u}" for u in at.UNVALUED), *plan.steps],
            **plan.guide_fields())]


AdvanceTimeTrack = _AdvanceTime()
