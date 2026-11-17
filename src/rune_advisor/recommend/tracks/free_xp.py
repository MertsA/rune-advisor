"""Xp the player already holds and can cash in within minutes: no grind, just a trip and a click.

Value is the grinding time it saves (xp / the planner's xp/h for that skill at the player's level),
so a cash-in worth an hour of Mining outranks one worth ten minutes of Ranged.

Statue rocks (player.held_rocks: the collection bag plus rock items in any inventory): each full pair of
golden rocks goes on the Statue of Rhiannon in Prifddinas' Tower of Voices for ~1.188 x level^2 xp in its
skill (fit to 10 pairs placed on 2026-10-29: Agility 96: 10,948; Ranged 90: 9,624; Mining 104: 12,849, all
within a few xp; needs 75 in all 16 golden-rock skills and Constitution, met by anyone holding pairs since
rocks only drop once Auron Ithell has switched them on). Each pair of strange rocks goes on the Statue of
Dahmaroc in the Varrock Museum for level^2 - level^2 // 100 xp (2026-10-31: Agility 96: 9,124; Herblore
100: 9,900, exact).

Choice lamps and penguin points (resources.load_pool: task-set antique lamps held as items, penguin points from
overrides.toml) go to the skill where their xp replaces the most work, among the skills at the lamp's level floor.
"Work" is the planner's full cost of training that xp without the lamps (Context.level_plan with the pool off:
for an ironman that includes gathering the supplies, so Summoning's charms and shards count, not just its xp/h),
never Hitpoints, which comes on its own with every other combat skill and unlocks nothing (PASSIVE_SKILLS).
Level plans spend the same pool when it beats grinding, so a session that takes this cash-in and a level goal in
the same skill counts the lamps twice; the picker's totals are a little optimistic then.

Invention discoveries (invention.py): every blueprint the player hasn't discovered and could, boosted by the best
Invention potion in the bank, is a one-off xp at an inventor's workbench; the entry is worth the Invention training
it replaces. Discovery materials (a few parts per blueprint) aren't costed.
"""

from __future__ import annotations

import math

from ...invention import GATE_TEXT, discoveries
from ...player import held_rocks
from ...resources import load_pool
from ...xp import level_at_xp, level_cap, xp_at
from ..base import Candidate, Context
from ..plans import goal_levels, xp_hours_saved

GOLDEN_XP_PER_LEVEL_SQ = 1.188
STATUE_TRIP_HOURS = 0.05  # a lodestone, a short walk, one click on the plinth
# Skills a lamp is wasted on: trained on the side of everything else, with nothing to unlock.
PASSIVE_SKILLS = {"Hitpoints"}
DISCOVERY_HOURS = 1.5 / 60  # the optimisation puzzle, per blueprint
# kind -> (statue, how to get there)
STATUES = {"Golden": ("Statue of Rhiannon", "Prifddinas lodestone -> Tower of Voices plinth, one click"),
           "Strange": ("Statue of Dahmaroc", "Varrock lodestone -> Varrock Museum plinth, one click")}
# Combat rocks let the player pick the skill, like a limited lamp.
ROCK_CHOICES = {"Melee": ("Attack", "Strength", "Defence"), "Ranged": ("Ranged", "Defence"),
                "Magic": ("Magic", "Defence")}


def golden_rock_xp(level: int) -> int:
    return math.floor(GOLDEN_XP_PER_LEVEL_SQ * level * level)


def strange_rock_xp(level: int) -> int:
    return level * level - level * level // 100


ROCK_XP = {"Golden": golden_rock_xp, "Strange": strange_rock_xp}


class _FreeXp:
    name = "free_xp"
    description = "Xp already in hand: cash-ins that take minutes (value = grinding hours saved)"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        held = held_rocks(p)
        out = []
        for kind, (statue, route) in STATUES.items():
            pairs = sorted(skill for skill, n in held.get(kind, {}).items() if n >= 2)
            if pairs:
                out.append(self._statue(ctx, kind, statue, route, pairs))
        goals = goal_levels(ctx)  # xp past the last level an unlock needs is worth nothing here
        for r in load_pool(p).resources:
            if r.units > 0 and r.skills is None and not r.bonus and not r.all_at_once and r.xp_per_unit(99) > 0:
                if c := self._choice(ctx, r, goals):
                    out.append(c)
        if c := self._discoveries(ctx, goals):
            out.append(c)
        return out

    def _discoveries(self, ctx: Context, goals: dict[str, int] | None = None) -> Candidate | None:
        p = ctx.player
        d = discoveries(p)
        if d is None or not d.ready or "Invention" not in p.skills:
            return None
        hours_saved = training_hours(ctx, "Invention", d.xp, goals)
        if hours_saved <= 0:
            return None
        n = len(d.ready)
        article = "an" if d.potion and d.potion[0] in "AEIOU" else "a"
        boost = f" after {article} {d.potion} (+{d.boost}: Invention {d.level}, have {p.level('Invention')})" if d.potion else ""
        top = "; ".join(f"{b['name']} (lvl {b['level']}, {b['xp']:,.0f} xp)" for b in d.ready[:6])
        details = [f"{d.xp:,.0f} Invention xp from {n} discoveries{boost}: {top}" + (" ..." if n > 6 else ""),
                   "each is a short optimisation puzzle (a perfect arrangement gives the full xp); the discovery "
                   "materials, a few parts each, aren't costed"]
        if d.gated:
            by_gate: dict[str, list[str]] = {}
            for b in d.gated:
                by_gate.setdefault(GATE_TEXT.get(b.get("gate", "other"), "another unlock"), []).append(b["name"])
            details.append(f"{len(d.gated)} more at your level ({sum(b['xp'] for b in d.gated):,.0f} xp) need something first: "
                           + "; ".join(f"{gate}: {', '.join(names)}" for gate, names in by_gate.items()))
        if d.locked:
            nxt = min(b["level"] for b in d.locked)
            details.append(f"{len(d.locked)} more ({sum(b['xp'] for b in d.locked):,.0f} xp) need Invention {nxt}+")
        if not d.potion:
            details.append("an Extreme invention potion (+1 per 150,000 Invention xp, up to +17) would open higher blueprints")
        if d.unknown:
            details.append("blueprints the data lacks: " + ", ".join(d.unknown))
        return Candidate(
            track=self.name, title=f"Discover {n} Invention blueprints at an inventor's workbench{boost and f' with {article} {d.potion}'}",
            value=round(hours_saved, 2), unit="h saved", cost_hours=round(n * DISCOVERY_HOURS, 2), details=details,
            why=f"~{hours_saved:.1f}h of Invention work for ~{n * DISCOVERY_HOURS * 60:.0f} min of puzzles",
            needs=["an inventor's workbench"] + ([d.potion] if d.potion else []), skills={"Invention"},
        )

    def _choice(self, ctx: Context, r, goals: dict[str, int] | None = None) -> Candidate | None:
        """A lamp (or lamp-like resource) usable on any skill: spend it where it saves the most training toward
        something it unlocks (goal_levels: never a skill with nothing left before 120)."""
        p = ctx.player
        goals = goal_levels(ctx) if goals is None else goals
        options = [s for s in p.skills if p.skills[s].level >= r.min_level and s not in PASSIVE_SKILLS]
        if not options:
            return None
        weights = getattr(ctx, "skill_weights", None) or {}
        # hours saved x the skill's usefulness weight ([skill_weights] in rune_advisor.toml, 1.0 unset): the ranking
        # value; the hours themselves are still shown.
        saved = {s: (training_hours(ctx, s, xp := r.units * r.xp_per_unit(p.skills[s].level, s), goals), xp) for s in options}
        score = {s: saved[s][0] * weights.get(s, 1.0) for s in options}
        ranked = sorted((s for s in options if score[s] > 0), key=lambda s: score[s], reverse=True)[:3]
        if not ranked:
            return None
        hours_saved, xp = saved[ranked[0]]

        def line(s: str) -> str:
            w = weights.get(s, 1.0)
            weighted = f", x{w:g} = {score[s] * 60:.0f}" if w != 1.0 else ""
            return f"{s} {saved[s][1]:,.0f} xp ({saved[s][0] * 60:.0f}{weighted} min of work toward {goals[s]})"

        lines = [line(s) for s in ranked]
        floor = f" (skills at {r.min_level}+)" if r.min_level > 1 else ""
        details = [f"from {r.source}" if r.source else "in the bank", f"best skills{floor}: " + "; ".join(lines)]
        if weights:
            details.append("usefulness weights ([skill_weights] in rune_advisor.toml): "
                           + ", ".join(f"{s} x{w:g}" for s, w in sorted(weights.items())))
        if idle := sorted(s for s in options if s not in goals):
            details.append("nothing left to unlock before 120 (xp there counts for nothing): " + ", ".join(idle))
        return Candidate(
            track=self.name, title=f"Spend {r.units} x {r.name} on {ranked[0]}",
            value=round(score[ranked[0]], 2), unit="h saved", cost_hours=round(r.units * r.hours_per_unit, 3),
            details=details,
            why=f"~{hours_saved:.1f}h of {ranked[0]} work (supplies included) for a click", needs=[r.source or r.name],
            skills={ranked[0]},
        )

    def _statue(self, ctx: Context, kind: str, statue: str, route: str, pairs: list[str]) -> Candidate:
        p = ctx.player

        def saved(skill: str) -> tuple[float, int]:
            level = p.skills[skill].level
            xp = ROCK_XP[kind](level)
            return xp_hours_saved(ctx, skill, xp), xp

        hours_saved, lines = 0.0, []
        for rock_skill in pairs:
            # A combat rock goes where it saves the most training time.
            options = [s for s in ROCK_CHOICES.get(rock_skill, (rock_skill,)) if s in p.skills]
            if not options:
                continue
            skill = max(options, key=lambda s: saved(s)[0])
            hours, xp = saved(skill)
            hours_saved += hours
            pick = f" (chosen from {'/'.join(options)})" if len(options) > 1 else ""
            lines.append(f"{skill} {xp:,} xp{pick} ({hours * 60:.0f} min of training)")
        plural = "pair" if len(pairs) == 1 else "pairs"
        return Candidate(
            track=self.name, title=f"Place {len(pairs)} {kind.lower()} rock {plural} on the {statue}",
            value=round(hours_saved, 2), unit="h saved", cost_hours=STATUE_TRIP_HOURS,
            details=[route, *lines, "xp grows with level^2: later pays slightly more, but rocks keep dropping"],
            why=f"~{hours_saved:.1f}h of training for a few minutes' trip", needs=[f"{statue} plinth"],
        )


def training_hours(ctx: Context, skill: str, xp: float, goals: dict[str, int] | None = None) -> float:
    """The planner's hours for the next `xp` of `skill` with no lamps to spend: the level plan to the level that xp
    reaches (supplies and all), scaled to the xp itself. Xp past the skill's cap is worth nothing, and with `goals`
    (plans.goal_levels) neither is xp past the last level an open unlock needs: a skill absent from `goals` has
    nothing left before 120 and saves 0 h."""
    p = ctx.player
    cap = level_cap(skill, p.members)
    if goals is not None:
        if skill not in goals:
            return 0.0
        cap = min(cap, goals[skill])
    now = p.xp(skill)
    xp = min(xp, max(0.0, xp_at(skill, cap) - now))
    if xp <= 0:
        return 0.0
    target = min(cap, level_at_xp(skill, now + xp) + 1)
    span = xp_at(skill, target) - now
    pool, ctx.resources = ctx.resources, None  # the lamps must not pay for the training they would replace
    try:
        hours = ctx.level_plan(skill, target).hours
    finally:
        ctx.resources = pool
    return hours * xp / span if span > 0 else 0.0


FreeXpTrack = _FreeXp()
