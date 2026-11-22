"""Achievement sets: open achievements made of sub-achievements (area task sets, Mastery/Mysteries, boss
"perfect" feats, capes), ranked by hours to finish, with what each open sub-achievement still needs
("Ardougne Set Tasks - Elite: 1 of 5 left; Abyssal Valet: 93 Summoning (have 92, ~1.5h)").

Value 1 per set: the track compares sets with each other by hours to finish. Set rewards (cloaks,
unlocks) are listed by name, not valued; task-set lamps net off the cost like other earned xp.
"""

from __future__ import annotations

from collections import Counter

from ..base import Candidate, Context
from ..plans import achievement_plan, achievement_xp

MIN_SET_SIZE = 3  # fewer sub-achievements is a prerequisite chain, not a set


class _TaskSets:
    name = "task_sets"
    description = "Achievement sets (task sets, masteries, boss feats, capes) by hours to finish, with what's left"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p, out = ctx.player, []
        todo = [a for a in p.achievements.values() if not a.completed
                and sum(1 for t in a.requirements if t.get("kind") == "achievement" and t.get("achievement")) >= MIN_SET_SIZE]
        for i, a in enumerate(todo):
            ctx.report(i, len(todo))
            subs = [t for t in a.requirements if t.get("kind") == "achievement" and t.get("achievement")]
            if not p.members and a.name in ctx.members_only_achievements:
                continue
            plan = achievement_plan(ctx, a.name)
            if plan.needs_membership:
                continue
            left = [t["achievement"] for t in subs if not t.get("complete")]
            why = [f"{len(subs) - len(left)} of {len(subs)} done", f"{plan.runescore} RS"]
            if lamps := achievement_xp().get(a.name):
                counts = Counter((c["xp"], c["min_level"]) for c in lamps["choice"])
                why.append("lamps: " + ", ".join(f"{n} x {xp:,}" + (f" (skill {lvl}+)" if lvl > 1 else "")
                                                 if isinstance(xp, int) else f"{n} x {xp}"
                                                 for (xp, lvl), n in counts.items()))
            if note := plan.reward_note():
                why.append(note)
            out.append(Candidate(
                track=self.name, title=f"{a.name}: {len(left)} of {len(subs)} left", value=1, unit="set",
                cost_hours=plan.net_hours, play_hours=plan.hours, details=[d for d in (a.category,) if d] + plan.steps,
                uncertain=plan.uncertain, why="; ".join(why), **plan.guide_fields(),
            ))
        return out


TaskSetsTrack = _TaskSets()
