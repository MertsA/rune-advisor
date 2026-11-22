"""Archaeology Guild shop unlocks not yet bought (archaeology_guild.unlocks_left), each costed in chronotes at what
the gatherer says a chronote takes (handing restored artefacts to collectors: the training loop earns them as it goes).
Value 1 per unlock: listed by name, not valued in hours (the user's rule for unlocks). Once every unlock is bought,
chronotes go to the research team (mechanics/archaeology.hand_in credits its offline xp to the training loop)."""

from __future__ import annotations

from ... import archaeology_guild as ag
from ..base import Candidate, Context


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
        return out


ArchaeologyGuildTrack = _ArchaeologyGuild()
