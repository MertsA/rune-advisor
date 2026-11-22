"""Significant skill levels. 99 Hunter is worth more than 90 Attack, but costs far more from 93."""

from __future__ import annotations

from ...xp import level_cap
from ..base import Candidate, Context

# Points per milestone level. Tune freely; only the ratios matter within this track.
MILESTONE_VALUE = {
    50: 0.5, 70: 0.75, 80: 1.5, 90: 3.0, 92: 2.0, 99: 10.0,
    104: 2.0, 110: 3.0, 120: 12.0, 150: 15.0,
}
# How many upcoming milestones per skill to offer (e.g. next one, plus the cap).
PER_SKILL = 2
MAX_CAPE_BONUS = 25.0
# A free player's cap on a members' skill (20 Necromancy, 20 Archaeology) is as far as that skill goes for them, so
# it is a goal in its own right even though no milestone level is that low. Worth a 50. The level-5 caps are a few
# minutes each and would top every ranking at ~0h, so they're left out.
F2P_CAP_VALUE = 0.5
MIN_F2P_CAP = 20


class _Milestones:
    name = "milestones"
    description = "Skill milestones (90, 99, 120...) weighted by significance vs. hours to reach"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p, out = ctx.player, []
        for i, (skill, s) in enumerate(p.skills.items()):
            ctx.report(i, len(p.skills))
            cap = level_cap(skill, p.members)
            upcoming = [lvl for lvl in sorted(MILESTONE_VALUE) if s.level < lvl <= cap]
            # Always include the 99 (or real cap) even if it's not among the next few.
            picks = upcoming[:PER_SKILL] + [l for l in (99, cap) if l in upcoming[PER_SKILL:]]
            f2p_cap = not p.members and cap not in MILESTONE_VALUE and MIN_F2P_CAP <= cap and s.level < cap
            if f2p_cap:
                picks.append(cap)
            for lvl in dict.fromkeys(picks):
                plan = ctx.level_plan(skill, lvl)
                title = f"{lvl} {skill}" + (" (free-to-play cap)" if lvl == cap and f2p_cap else "")
                out.append(Candidate(
                    track=self.name, title=title, value=MILESTONE_VALUE.get(lvl, F2P_CAP_VALUE), unit="pts",
                    cost_hours=plan.hours, details=[f"{s.level} -> {lvl}", *plan.steps],
                    # Missing materials take gathering time we don't model yet.
                    uncertain=bool(plan.materials_short) or plan.materials_unmodelled,
                    why=f"{s.level} -> {lvl}", needs=[f"{lvl} {skill} (have {s.level}, ~{plan.hours:.1f}h)"],
                    skills={skill}, unknowns=[f"{skill}: supplies not fully costed"]
                    if plan.materials_short or plan.materials_unmodelled else [],
                ))
        below_99 = [(sk, s) for sk, s in p.skills.items() if s.level < 99]
        if below_99 and p.members:  # the max cape needs every skill at 99, members' skills included
            hours = sum(ctx.level_plan(sk, 99).hours for sk, _ in below_99)
            out.append(Candidate(
                track=self.name, title="Max cape (all 99)", value=MAX_CAPE_BONUS, unit="pts", cost_hours=hours,
                details=[f"{len(below_99)} skills below 99: " + ", ".join(sk for sk, _ in below_99)],
                why=f"{len(below_99)} skills below 99", skills={sk for sk, _ in below_99},
                needs=[f"99 {sk} (have {s.level})" for sk, s in below_99],
            ))
        return out


MilestoneTrack = _Milestones()
