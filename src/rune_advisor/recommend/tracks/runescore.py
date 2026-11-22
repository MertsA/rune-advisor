"""RuneScore from achievements. Needs per-achievement completion and requirement tests, which
only the in-game plugin export provides (the hiscores only give the RuneScore total).

Cost comes from the achievement's unmet requirements (skills via lamps or training, quests,
prerequisite achievements), so "spend lamps on Woodcutting, then claim X" plans fall out.
"""

from __future__ import annotations

from ..base import Candidate, Context
from ..plans import achievement_plan


class _RuneScore:
    name = "runescore"
    description = "Uncompleted achievements by RuneScore per hour, costed from their requirements (needs plugin export)"

    def candidates(self, ctx: Context) -> list[Candidate]:
        out = []
        todo = [a for a in ctx.player.achievements.values() if not a.completed and a.runescore > 0]
        for i, a in enumerate(todo):
            ctx.report(i, len(todo))
            if not ctx.player.members and a.name in ctx.members_only_achievements:
                continue
            plan = achievement_plan(ctx, a.name)
            if plan.needs_membership:
                continue
            # Value counts prerequisite achievements finished along the way, not just this one.
            why = ", ".join(w for w in (f"{a.progress:.0%} done" if a.progress else None,
                                        f"{plan.runescore - a.runescore} RS from prerequisites on the way"
                                        if plan.runescore > a.runescore else None, plan.reward_note()) if w)
            out.append(Candidate(
                track=self.name, title=a.name, value=plan.runescore, unit="RS", cost_hours=plan.net_hours, play_hours=plan.hours,
                details=[d for d in (a.category, f"{a.progress:.0%} done" if a.progress else None,
                                     plan.reward_note()) if d] + plan.steps,
                uncertain=plan.uncertain, why=why, **plan.guide_fields(),
            ))
        return out


RuneScoreTrack = _RuneScore()
