"""Archaeology Guild shop unlocks not yet bought (archaeology_guild.unlocks_left), each costed in chronotes at what
the gatherer says a chronote takes (handing restored artefacts to collectors: the training loop earns them as it goes).
Value 1 per unlock: listed by name, not valued in hours (the user's rule for unlocks). Once every unlock is bought,
chronotes go to the research team (mechanics/archaeology.hand_in credits its offline xp to the training loop).
Research consultants not yet unlocked are listed too: their dig site's mysteries left (not costed: flagged) and the
Archaeology level they want (costed)."""

from __future__ import annotations

from ... import archaeology_guild as ag
from ..base import Candidate, Context
from ..plans import Plan, skill_plan


class _ArchaeologyGuild:
    name = "archaeology_guild"
    description = "Archaeology Guild shop unlocks still to buy, by hours of chronotes (listed, not valued)"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        if not p.members or p.level("Archaeology") < ag.QUALIFICATIONS["Assistant"]:
            return []
        chronote = ctx.gatherer.complete_source("chronotes")
        per = ctx.gatherer.net_cost(chronote) if chronote else None
        out = []
        for u, known in ag.unlocks_left(p):
            need = ag.QUALIFICATIONS.get(u["qualification"], 1)
            hours = u["chronotes"] * per if per is not None else 0.0
            details = [f"{u['chronotes']:,} chronotes at the Archaeology Guild shop ({u['qualification']} qualification)"]
            if chronote:
                details.append(f"chronotes: {chronote.how}")
            if not known:
                details.append("not in the export: may already be bought (`archaeology_unlocks` in overrides.toml)")
            out.append(Candidate(
                track=self.name, title=u["name"], value=1, unit="unlock", cost_hours=round(hours, 2),
                details=details, uncertain=not known or per is None,
                why=f"{u['chronotes']:,} chronotes" + (f"; needs {need} Archaeology" if p.level("Archaeology") < need else ""),
                needs=[f"{need} Archaeology"] if p.level("Archaeology") < need else [],
                unknowns=[] if known else ["ownership not exported"], skills={"Archaeology"},
            ))
        out.extend(self._consultants(ctx))
        return out

    def _consultants(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        hired = ag.unlocked(p)
        if hired is None:
            return []
        out = []
        for i, (place, need) in ag.CONSULTANTS.items():
            if i in hired:
                continue
            name, speed, cost, perk = ag.RESEARCHERS[i]
            left = ag.mysteries_left(p, place) or []
            plan = Plan()
            if p.level("Archaeology") < need:
                plan.add(skill_plan(ctx, "Archaeology", need))
            if left:
                plan.uncertain = True
                plan.unknowns.append(f"the {place} mysteries' work isn't costed")
            details = [f"research consultant: +{speed}% speed, {cost:+}% cost" + (f", {perk}" if perk else "")]
            details += [f"{place} mystery: {m}" for m in left] + plan.steps
            out.append(Candidate(
                track=self.name, title=f"Unlock {name} for the research team", value=1, unit="unlock",
                cost_hours=plan.net_hours, play_hours=plan.hours, details=details, uncertain=plan.uncertain,
                why="; ".join(([f"{len(left)} {place} mysteries left"] if left else [])
                              + ([f"Archaeology {need}"] if p.level("Archaeology") < need else [])),
                **plan.guide_fields()))
        return out


ArchaeologyGuildTrack = _ArchaeologyGuild()
