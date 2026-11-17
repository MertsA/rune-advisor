"""Focus mode: the player picks one goal, and gets the activities that advance it, ranked by what else each
one advances (the user's pattern, 2026-10-29: work on the Thieving outfit, and pick the Thieving method
that also progresses something else).

Goals (`parse_goal`):
  * an elite outfit ("Thieving outfit"): fragments come per minute of any training in the skill, so every
    method advances it equally and the ranking is by extras alone;
  * a level ("Thieving:99"): hours to the level per method;
  * an achievement or task set: its open skill needs become level goals, its other needs are listed;
  * an item ("item:Dragon pickaxe"): drop sources by hours per drop, and when it can be made or bought, the
    gatherer's whole route (recipes all the way down, levels to train first, inputs with no known source).

"Also advances" per activity: drops another plan needs or unowned uniques (collection log), open
achievements that count its kills/drops (Activities.achievements_at), and side xp. Things any method
in the skill gets (level-gated achievements on the way, outfit fragments) are listed once for the goal.

Ranking: activities within FAST_SHARE of the fastest go first, by how many things they also advance,
then by speed; slower ones follow by speed. Extras aren't valued in hours (tracks stay separate).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..cost import typical_rate
from ..drops import notable_drops, sources_of
from ..outfits import UNLOCK_LEVEL, fragments_per_hour, outfit_state, outfits, seen_pieces
from ..xp import SKILL_ALIASES, level_at_xp, level_cap, xp_at
from . import gathering
from .base import Context
from .plans import achievement_plan
from .tracks.outfits import level_unlocks

FAST_SHARE = 0.75  # an activity this fast (of the fastest) competes on what else it advances
MAX_EXTRAS = 5


@dataclass
class FocusOption:
    activity: str
    hours: float | None  # to finish the goal this way (None: same for every option, e.g. outfit fragments)
    rate: str  # "~95,000 xp/h", "~40 kills/h"
    extras: list[str] = field(default_factory=list)
    speed: float = 0.0  # goal progress per hour, for ranking (xp/h, drops/h)


@dataclass
class Focus:
    goal: str
    summary: str
    options: list[FocusOption] = field(default_factory=list)
    any_method: list[str] = field(default_factory=list)  # what every option also gets
    other_needs: list[str] = field(default_factory=list)  # parts of the goal no activity here covers
    route: list[str] = field(default_factory=list)  # the planner's route, when there's no method to pick
    prep: list[str] = field(default_factory=list)  # before leaving: divine charge for the fastest option's fight
    sections: list["Focus"] = field(default_factory=list)  # an achievement's per-skill sub-goals


@dataclass
class Goal:
    kind: str  # outfit, level, achievement, item
    name: str
    level: int | None = None


def parse_goal(ctx: Context, text: str) -> Goal:
    t = text.strip()
    if t.lower().startswith("item:"):
        return Goal("item", t[5:].strip())
    if ":" in t and t.rsplit(":", 1)[1].strip().isdigit():
        skill, level = t.rsplit(":", 1)
        return Goal("level", _player_skill(ctx, skill), int(level))
    if t.lower().endswith(" outfit"):
        skill = t[: -len(" outfit")].strip().title()
        wiki = next((w for w in outfits() if skill in (w, SKILL_ALIASES.get(w, w))), None)
        if wiki:
            return Goal("outfit", wiki)
    names = {n.lower(): n for n in ctx.player.achievements}
    if t.lower() in names:
        return Goal("achievement", names[t.lower()])
    raise ValueError(f"unknown goal {text!r}: try \"<Skill> outfit\", \"<Skill>:<level>\", an achievement name "
                     "or \"item:<name>\"")


def _player_skill(ctx: Context, name: str) -> str:
    name = name.strip().title()
    return SKILL_ALIASES.get(name, name)


def focus(ctx: Context, goal: Goal) -> Focus:
    if goal.kind == "outfit":
        return _outfit_focus(ctx, goal.name)
    if goal.kind == "level":
        return _level_focus(ctx, goal.name, goal.level)
    if goal.kind == "item":
        return _item_focus(ctx, goal.name)
    return _achievement_focus(ctx, goal.name)


def _extras(ctx: Context, row: dict, skill: str, rate: float) -> list[str]:
    """What a training method also advances besides its own skill."""
    out = []
    useful = [d for d in notable_drops(row, skill, rate, ctx.player, ctx.needs())
              if d.kind in ("need", "charm") or d.worth_chasing]
    out += [d.describe() for d in useful[:3]]
    # Simulated rows have no drop_sources: the method's own name names its target. Those achievements
    # must be about the same action ("pickpocket"), not about killing the target.
    listed = set(row.get("drop_sources") or [])
    verb = row["method"].split()[0].lower().rstrip(":")[:6]
    for src in sorted(listed | ctx.activities.named_sources(row["method"], need_verb=False)):
        for name, _rs in ctx.activities.achievements_at(src):
            line = f"achievement: {name}"
            if line in out or (src not in listed and verb not in ctx.achievement_descriptions.get(name, "").lower()):
                continue
            out.append(line)
    for s, v in (row.get("side_xp") or {}).items():
        out.append(f"+{float(v):,.0f} {s} xp/h")
    return out


def _skill_options(ctx: Context, skill: str, target_xp: float | None) -> list[FocusOption]:
    """Training methods for `skill`; with no target xp (outfit fragments, earned per minute of any method)
    every method is equally fast, so only what else it advances ranks them."""
    p = ctx.player
    options = []
    for row in ctx.cost.available_methods(skill, p.level(skill)):
        rate = typical_rate(row)
        if not rate:
            continue
        hours = max(0.0, target_xp - p.xp(skill)) / rate if target_xp is not None else None
        options.append(FocusOption(row["method"], hours, f"~{rate:,.0f} xp/h", _extras(ctx, row, skill, rate),
                                   rate if target_xp is not None else 1.0))
    # Reward-shop activities in this skill (Safecracking for pilfer points): their xp/h isn't modelled, so
    # they only compete when speed doesn't matter.
    from .tracks.toolbelt import currency_unlocks
    by_activity: dict[str, list[str]] = {}
    for item, s, how, h in currency_unlocks(ctx):
        if s == skill:
            activity = how.split(" from ", 1)[1]
            by_activity.setdefault(activity, []).append(f"tool belt: {item} ({how.split(' from ')[0]}, ~{h:.1f}h)")
    for activity, extras in by_activity.items():
        options.append(FocusOption(activity, None, "xp/h not modelled", extras, 1.0 if target_xp is None else 0.0))
    return rank_options(options)


def rank_options(options: list[FocusOption]) -> list[FocusOption]:
    """Fast options (within FAST_SHARE of the fastest) by what else they advance, then the rest by speed."""
    if not options:
        return []
    best = max(o.speed for o in options)

    def key(o: FocusOption):
        fast = o.speed >= FAST_SHARE * best
        return (not fast, -len(o.extras) if fast else 0, -o.speed)

    seen, out = set(), []
    for o in sorted(options, key=key):  # guide rows can repeat a method across level bands
        if o.activity not in seen:
            seen.add(o.activity)
            o.extras = o.extras[:MAX_EXTRAS] + ([f"+{len(o.extras) - MAX_EXTRAS} more"] if len(o.extras) > MAX_EXTRAS else [])
            out.append(o)
    return out


def _on_the_way(ctx: Context, skill: str, hours: float) -> list[str]:
    """Things any training in `skill` for `hours` gets: level-gated achievements, outfit fragments."""
    p = ctx.player
    level = p.level(skill)
    gained = hours * ctx.cost.rate(skill, level)
    end = min(level_cap(skill, p.members), level_at_xp(skill, p.xp(skill) + gained))
    out = [f"achievement: {u}" for u in level_unlocks(ctx, skill, level, end)]
    wiki = next((w for w in outfits() if SKILL_ALIASES.get(w, w) == skill), None)
    if wiki and level >= UNLOCK_LEVEL:
        state = outfit_state(p, wiki, seen_pieces(p), p.outfit_pieces_left)
        if state.status not in ("complete", "likely complete"):
            out.append(f"{state.outfit}: ~{fragments_per_hour(p, wiki) * hours:,.0f} of "
                       f"{state.fragments_needed:,} fragments still needed")
    return out


def _outfit_focus(ctx: Context, wiki_skill: str) -> Focus:
    p = ctx.player
    skill = SKILL_ALIASES.get(wiki_skill, wiki_skill)
    state = outfit_state(p, wiki_skill, seen_pieces(p), p.outfit_pieces_left)
    rate = fragments_per_hour(p, wiki_skill)
    hours = state.fragments_needed / rate
    f = Focus(goal=state.outfit, summary=f"{state.fragments_needed:,} fragments: ~{hours:.1f}h of any {skill} "
                                         f"training (~{rate:,.0f}/h), so pick by what else the method gives")
    if state.blocked:
        f.other_needs.append(f"first: {state.blocked}")
    if wiki_skill == "Thieving":
        f.other_needs.append("Shifting Tombs gives ~6x the fragments of other Thieving (wiki)")
    f.options = _skill_options(ctx, skill, None)
    f.any_method = [a for a in _on_the_way(ctx, skill, hours) if not a.startswith(state.outfit)]
    return f


def _level_focus(ctx: Context, skill: str, level: int) -> Focus:
    p = ctx.player
    if p.level(skill) >= level:
        return Focus(goal=f"{level} {skill}", summary=f"already {p.level(skill)} {skill}")
    plan = ctx.level_plan(skill, level)
    f = Focus(goal=f"{level} {skill}", summary=f"{p.level(skill)} -> {level}: the planner's route takes "
                                               f"~{plan.hours:.1f}h (lamps, bonus xp and banked materials first)")
    f.options = _skill_options(ctx, skill, xp_at(skill, level))
    if not f.options:  # no guide method (Summoning): the planner's own route is the activity
        f.route = [st for st in plan.steps if not st.startswith(" ")][:MAX_EXTRAS]
    f.any_method = _on_the_way(ctx, skill, plan.hours)
    return f


def _item_focus(ctx: Context, item: str) -> Focus:
    acts = ctx.activities
    options = []
    for src, chance, qty in sources_of(item):
        if not isinstance(chance, (int, float)) or not chance or not (r := acts.rate(src)):
            continue
        per_hour = r.per_hour * min(1.0, chance * qty)
        extras = [f"achievement: {n}" for n, _ in acts.achievements_at(src)]
        extras += [f"+{v:,.0f} {s} xp/h" for s, v in r.xp_per_hour.items()]
        options.append(FocusOption(f"{src} ({r.how})", 1 / per_hour, f"~{r.per_hour:,.0f}/h, 1/{1 / (chance * qty):,.0f}",
                                   extras, per_hour))
    f = Focus(goal=item, summary=f"{len(options)} sources the player can do" if options else
              "no drop source with a known kill rate")
    f.options = rank_options(options)
    if f.options and f.options[0].hours:
        # Kills in augmented gear drain the charge pack: prep for the expected time, a session at most.
        from .. import charge
        f.prep = charge.prep(ctx, charge.trip_hours(f.options[0].hours)).steps
    if (src := ctx.gatherer.source(item)) is not None and src.kind != "drop":
        g = ctx.gatherer.plan({item: 1})
        hours = g.hours + g.coins / gathering.COINS_PER_HOUR
        made = f"~{hours:,.1f}h to make or buy" + (f" ({g.unlock_hours:,.0f}h of it training levels first)"
                                                     if g.unlock_hours else "")
        f.summary = f"{made}; {f.summary}" if options else made
        f.route = g.steps
        f.other_needs = [f"{n:,} {name} (no known source: not costed)" for name, n in g.short.items()]
    return f


def _achievement_focus(ctx: Context, name: str) -> Focus:
    a = ctx.player.achievements[name]
    plan = achievement_plan(ctx, name)
    f = Focus(goal=name, summary="done" if a.completed else f"~{plan.net_hours:.1f}h by the planner's route")
    # Each open level need becomes a sub-goal with its own methods; the rest is listed.
    for need in plan.needs:
        f.other_needs.append(need)
    for skill in sorted(plan.skills):
        target = _target_level(plan.needs, skill)
        if target:
            f.sections.append(_level_focus(ctx, skill, target))
    return f


def _target_level(needs: list[str], skill: str) -> int | None:
    """The highest "<level> <skill>" mentioned in a plan's needs."""
    levels = [int(m) for n in needs for m in re.findall(rf"\b(\d+) {re.escape(skill)}\b", n)]
    return max(levels, default=None)
