"""Daemonheim's once-a-day boss pick (an Easy Daemonheim reward): the boss whose floor advances the most open
achievements for the random floors it replaces (daemonheim.picks). One entry, today's pick, with the next best and
the floor-only lore in its details. Value is in floors replaced, not hours: the pick is a free daily choice, and
what it saves is luck, which the planner has no hours for (boss journals' drop chances are unknown).
"""

from __future__ import annotations

from ...daemonheim import floor_only, perk_unlocked, picks, rules
from ...mechanics.dungeoneering import floor_minutes
from ..base import Candidate, Context

SHOWN = 5  # other picks listed in the details


def _line(p) -> str:
    extra = f"; the floor also rolls {', '.join(p.floor_lore)}" if p.floor_lore else ""
    return (f"{p.boss} (floor {p.floor}, 1 in {p.rivals} without the pick): "
            f"{'; '.join(g.describe() for g in p.gains)}{extra}")


class _DgBoss:
    name = "dg_boss"
    description = "Daemonheim's daily boss pick: the boss that advances the most open lore and boss achievements"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        if not p.members or not perk_unlocked(p):
            return []
        reachable, later = picks(p)
        if not reachable:
            return []
        best = reachable[0]
        minutes, measured = floor_minutes(p)
        details = [_line(best), *best.notes]
        if len(reachable) > 1:
            details.append("next picks, one a day:")
            details += [f"  {_line(x)}" for x in reachable[1:1 + SHOWN]]
        if later:
            details.append("above your floors: " + "; ".join(
                f"{x.boss} (Dungeoneering {x.min_level}): {', '.join(g.describe() for g in x.gains)}" for x in later))
        if lore := floor_only(p):
            details.append("floor-only lore (any boss): " + "; ".join(lore))
        details += rules()["rules"]
        unknowns = ["each boss that can appear on a floor is assumed equally likely"]
        if not measured:
            unknowns.append(f"a floor is assumed to take {minutes:.0f} min (`dg_floor_minutes` under boosts)")
        return [Candidate(
            track=self.name, title=f"Daemonheim: pick {best.boss} for floor {best.floor}", value=best.worth,
            unit="floors replaced", cost_hours=round(minutes / 60, 2), uncertain=True, details=details,
            why=f"advances {len(best.gains)} open achievement item{'s' if len(best.gains) != 1 else ''}; without the pick "
                f"it turns up 1 floor in {best.rivals}",
            needs=[f"run floor {best.floor} and choose {best.boss}"], unknowns=unknowns, skills={"Dungeoneering"},
        )]


DgBossTrack = _DgBoss()
