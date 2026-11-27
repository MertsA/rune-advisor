"""Fairy rings still to build or repair (fairy_rings.py): listed, not valued (the planner doesn't model travel), each
costed as what the player must do: the mushrooms or planks and nails, the quest it waits on and any level on the way.
A ring the export can't see (the Ancient Cavern's) is listed as unknown, in case it's already built.
"""

from __future__ import annotations

from ... import fairy_rings
from ..base import Candidate, Context
from ..plans import Plan, item_plan, quests_plan, skill_plan
from ...models import QuestStatus


class _FairyRings:
    name = "fairy_rings"
    description = "Fairy rings still to build or repair (listed, not valued)"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        if not p.members or not any(isinstance(p.vars.get(n), int) for r in fairy_rings.RINGS.values()
                                    for n in r["vars"]):
            return []  # an export without the flags says nothing either way
        out = []
        for code, ring, state in fairy_rings.open_rings(p):
            plan = Plan(hours=2 / 60)  # the build itself: a couple of minutes there
            unstarted = [q for q in ring.get("quests_started", [])
                         if (s := p.quests.get(q)) is None or s.status == QuestStatus.NOT_STARTED]
            if unstarted:
                qp = quests_plan(ctx, unstarted)
                qp.unknowns.append(f"only starting {', '.join(unstarted)} is needed: costed to the end")
                qp.uncertain = True
                plan.add(qp)
            for skill, level in ring.get("levels", {}).items():
                plan.add(skill_plan(ctx, skill, level))
            for item, n in ring["items"].items():
                short = n - p.holdings.get(item, 0)
                if short > 0:
                    got = item_plan(ctx, item, short)
                    plan.add(got if got is not None else Plan(uncertain=True, unknowns=[f"no source known for {item}"]))
            details = [f"{n} x {i}" for i, n in ring["items"].items()] + \
                      [f"{t} (tool belt or held)" for t in ring.get("tools", [])] + \
                      ([ring["note"]] if ring.get("note") else []) + plan.steps
            unknowns = ["not exported: may already be built"] if state is None else []
            out.append(Candidate(
                track=self.name, title=f"Build the {ring['place']} fairy ring ({code})", value=1, unit="unlock",
                cost_hours=plan.net_hours, play_hours=plan.hours, details=details,
                why=", ".join(f"{n} {i}" for i, n in ring["items"].items()),
                uncertain=plan.uncertain or state is None, **{**plan.guide_fields(),
                                                              "unknowns": plan.unknowns + unknowns}))
        return out


FairyRingsTrack = _FairyRings()
