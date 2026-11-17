"""Elite skilling outfits (rune_advisor/outfits.py): one goal per outfit not yet complete, costed as the training
time that earns the missing fragments, ranked by hours to finish (value 1 per outfit: the track compares
outfits with each other, not with other tracks).

Overlap: the same training raises the skill, so each entry lists the open achievements whose level
requirement in that skill the training reaches. That's progress the player gets for free on the way.
"""

from __future__ import annotations

from ...outfits import CRAFT_INVENTION, CRAFT_LEVEL, UNLOCK_LEVEL, fragments_per_hour, outfit_state, outfits, seen_pieces
from ...sources.plugin_db import SKILLTYPE_TO_NAME
from ...xp import SKILL_ALIASES, level_at_xp
from ..base import Candidate, Context
from ..plans import Plan, skill_plan

MAX_OVERLAP_LINES = 6


def level_unlocks(ctx: Context, skill: str, start: int, end: int) -> list[str]:
    """Open achievements with a single-skill level requirement in `skill` between start (exclusive) and end."""
    out = []
    for a in ctx.player.achievements.values():
        if a.completed:
            continue
        for t in a.requirements:
            names = [SKILLTYPE_TO_NAME.get(s, s) for s in t.get("skills", [])]
            if t.get("kind") == "skill" and not t.get("complete") and names == [skill] \
                    and start < (t.get("totalLevels") or 0) <= end:
                out.append(f"{a.name} (needs {t['totalLevels']})")
    return sorted(set(out))


class _Outfits:
    name = "outfits"
    description = "Elite skilling outfits from Invention fragments, by hours of training to finish"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        if not any(o["fragment_var"] in p.vars for o in outfits().values()):
            return []  # no fragment counts exported: nothing to go on
        seen = seen_pieces(p)
        out = []
        for wiki_skill in outfits():
            skill = SKILL_ALIASES.get(wiki_skill, wiki_skill)  # the player's name for it ("Runecraft")
            state = outfit_state(p, wiki_skill, seen, p.outfit_pieces_left)
            if state.status in ("complete", "likely complete"):
                continue
            plan = Plan()
            for need_skill, need in ((skill, CRAFT_LEVEL), ("Invention", CRAFT_INVENTION)):
                if p.level(need_skill) < need:
                    plan.add(skill_plan(ctx, need_skill, need))
            rate = fragments_per_hour(p, wiki_skill)
            hours = state.fragments_needed / rate
            details = [f"{state.fragments:,} fragments held; {state.fragments_needed:,} more at ~{rate:,.0f}/h "
                       f"of {skill} training (any method)"]
            if state.craftable:
                details.insert(0, f"craft {state.craftable} piece{'s' * (state.craftable > 1)} now at an "
                                  f"inventor's workbench ({state.craftable * 3600:,} fragments)")
            if state.status == "not started":
                details.append(f"fragments start at {UNLOCK_LEVEL} {skill}")
            if wiki_skill == "Thieving":
                details.append("Shifting Tombs gives ~6x the fragments of other Thieving (wiki)")
            # Overlap: what the same training unlocks on the way.
            level = p.level(skill)
            gained = hours * ctx.cost.rate(skill, level)
            end = level_at_xp(skill, (p.skills[skill].xp if skill in p.skills else 0) + gained)
            if unlocks := level_unlocks(ctx, skill, level, end):
                details.append(f"on the way ({level} -> {end} {skill}): " + "; ".join(unlocks[:MAX_OVERLAP_LINES])
                               + (f" and {len(unlocks) - MAX_OVERLAP_LINES} more" if len(unlocks) > MAX_OVERLAP_LINES else ""))
            if state.blocked:
                details.insert(0, f"blocked: buy the {state.blocked} first")
            details += state.notes + plan.steps
            left = "?" if state.pieces_left is None else state.pieces_left
            out.append(Candidate(
                track=self.name, title=("blocked: " if state.blocked else "")
                + f"{state.outfit} ({skill}): {left} of {state.pieces_max} pieces left",
                value=1, unit="outfit", cost_hours=round(hours + plan.hours, 2), details=details,
                uncertain=state.pieces_left is None or plan.uncertain or bool(state.blocked),
                why=details[0], skills={skill} | plan.skills,
                needs=([f"blueprint: {state.blocked}"] if state.blocked else []) + plan.needs
                + [f"~{hours:.1f}h of {skill} training (any method) for {state.fragments_needed:,} fragments"],
                unknowns=[n for n in state.notes if "unknown" in n or "status not exported" in n]
                + (["Dungeoneering fragments per floor: floors per hour assumed"] if wiki_skill == "Dungeoneering" else [])
                + plan.unknowns,
            ))
        return out


OutfitsTrack = _Outfits()
