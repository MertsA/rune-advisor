"""Colossus murals still to restore (colossi.py): each adds a teleport to the Stonevein amulet, listed as an account
unlock, not valued (the planner doesn't model travel). Cost: the deposits (assumed count and minutes, flagged), less
the Divination and Agility xp they pay, plus the Divination level and quest the colossi need."""

from __future__ import annotations

from ... import colossi
from ..base import Candidate, Context
from ..plans import Plan, quests_plan, skill_plan, xp_hours_saved
from ...models import QuestStatus


class _Colossi:
    name = "colossi"
    description = "Colossus murals to restore for Stonevein amulet teleports (listed, not valued)"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        state = colossi.restored(p)
        if not p.members or state is None:
            return []
        gate = Plan()
        if (q := p.quests.get(colossi.QUEST)) is None or q.status == QuestStatus.NOT_STARTED:
            gq = quests_plan(ctx, [colossi.QUEST])
            gq.unknowns.append(f"only starting {colossi.QUEST} is needed: costed to the end")
            gq.uncertain = True
            gate.add(gq)
        if p.level("Divination") < colossi.LEVEL:
            gate.add(skill_plan(ctx, "Divination", colossi.LEVEL))
        out = []
        for mural, (where, div10, agi10) in colossi.MURALS.items():
            if state[mural]:
                continue
            share = colossi.DEPOSITS / 10
            div, agi = div10 * share, agi10 * share
            plan = Plan(hours=colossi.DEPOSITS * colossi.DEPOSIT_MINUTES / 60, uncertain=True,
                        unknowns=[f"{colossi.DEPOSITS:g} deposits of {colossi.DEPOSIT_MINUTES:g} minutes assumed "
                                  "(the wiki: 8 to 11)"],
                        reward_hours=xp_hours_saved(ctx, "Divination", div) + xp_hours_saved(ctx, "Agility", agi))
            plan.add(gate)
            out.append(Candidate(
                track=self.name, title=f"Restore the {mural} colossus mural (Stonevein amulet teleport)", value=1,
                unit="unlock", cost_hours=plan.net_hours, play_hours=plan.hours, uncertain=True,
                why=f"~{colossi.DEPOSITS:g} charged memory stone deposits; {div:,.0f} Divination, {agi:,.0f} Agility xp",
                details=[f"the mural {where}", "its third restoration phase adds a teleport to it on the Stonevein "
                         "amulet (from Aurriel at Divine Fall)", *plan.steps],
                **plan.guide_fields()))
        return out


ColossiTrack = _Colossi()
