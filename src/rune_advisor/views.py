"""Planner views as data: what `radv guide`, `radv next` and `radv focus` compute, before any formatting.

The CLI prints these results and the desktop app (`rune_advisor.app`) serialises them, so neither parses the
other's output. Slow steps take an optional `progress(done, total, message)` callback that may raise to
cancel (the app's jobs do), and nothing here prints.
"""

from __future__ import annotations

import dataclasses
from typing import Callable

from .recommend.guide import GuideEntry, afk_skills
from .recommend.guide import guide as make_guide
from .recommend.tracks import ALL_TRACKS

Progress = Callable[[float, float, str], None]

# Typical seconds per step (desktop, one maxed-ish ironman, 2026-11-22). They only weight the progress bar: done and
# total are in these seconds, so a caller can tell the time left from how fast a run gets through them (app.js).
STEP_SECONDS = {"priors": 67.0, "quest_points": 0.7, "milestones": 3.5, "runescore": 8.5, "gear": 1.2,
                "free_xp": 0.9, "task_sets": 25.0, "collection_logs": 0.7, "dnd_tokens": 0.5, "scrolls": 15.0,
                "ranking": 0.2}
DEFAULT_STEP_SECONDS = 0.2


class Steps:
    """A run's steps on one progress bar, each weighted by STEP_SECONDS; slow loops inside a step move the bar
    through it (Context.report)."""

    def __init__(self, ctx, progress: Progress | None, keys: list[str]):
        self.ctx, self.progress = ctx, progress
        self.total = sum(STEP_SECONDS.get(k, DEFAULT_STEP_SECONDS) for k in keys)
        self.done = 0.0

    def run(self, key: str, message: str, fn: Callable):
        weight = STEP_SECONDS.get(key, DEFAULT_STEP_SECONDS)
        if self.progress is None:
            return fn()
        start = self.done
        self.progress(start, self.total, message)
        outer, self.ctx._report = self.ctx._report, \
            (lambda i, n: self.progress(start + weight * i / n, self.total, message))
        try:
            return fn()
        finally:
            self.ctx._report = outer
            self.done = start + weight


def _keys(ctx, names: list[str]) -> list[str]:
    """The steps of planning these tracks: the category priors first when an achievement track needs them."""
    priors = ctx._category_priors is None and any(n in ("runescore", "task_sets", "quest_points") for n in names)
    return (["priors"] if priors else []) + list(names)


def _priors(ctx, steps: Steps) -> None:
    from .recommend.plans import category_priors
    if ctx._category_priors is None:
        steps.run("priors", "estimating typical achievement costs", lambda: category_priors(ctx))


def charge_steps(ctx, combat_hours: float, session: float | None = None) -> list[str]:
    """Prep steps when the charge pack won't last a plan's combat time (its expected time, or the session's when
    shorter) in the player's augmented gear; nothing when it will, or when nothing augmented is worn."""
    from . import charge
    if combat_hours <= 0:
        return []
    hours = charge.trip_hours(combat_hours, session)
    cache = ctx.__dict__.setdefault("_charge_preps", {})
    key = round(hours, 2)
    if key not in cache:
        cache[key] = charge.prep(ctx, hours)
    return cache[key].steps


def guide_sections(ctx, names: list[str] | None = None, mood: str | None = None, session: float | None = None,
                   progress: Progress | None = None) -> dict[str, list[GuideEntry]]:
    """`radv guide`: each track's entries, best first. Candidates are built track by track so a caller can show progress."""
    from .player import rock_halved_skills
    names = names or list(ALL_TRACKS)
    steps = Steps(ctx, progress, _keys(ctx, names) + ["ranking"])
    if "priors" in _keys(ctx, names):
        _priors(ctx, steps)
    per_track = {}
    for n in names:
        per_track[n] = steps.run(n, f"planning {n}", lambda n=n: ALL_TRACKS[n].candidates(ctx))
    return steps.run("ranking", "ranking", lambda: make_guide(per_track, mood, session, afk_skills(ctx),
                                                                rock_halved_skills(ctx.player)))


def next_picks(ctx, hours: float, mood: str | None = None, replan: bool = False, max_replans: int | None = None,
               progress: Progress | None = None):
    """`radv next`: the session's picks (cash-ins first, then each track's best that fits)."""
    from .player import rock_halved_skills
    from .recommend.picker import candidates_for_picking, pick, pick_replanned

    def build(c, tracks=None):
        # The lamps and points go where free_xp says; the other tracks plan their levels without them. A re-plan
        # after a pick (--replan) starts a new bar for the tracks it re-plans.
        names = list(tracks or ALL_TRACKS)
        steps = Steps(c, progress, _keys(c, names))
        if "priors" in _keys(c, names):
            _priors(c, steps)
        found = candidates_for_picking(c, ALL_TRACKS, tracks,
                                       step=lambda n, fn: steps.run(n, f"planning {n}", fn))
        return make_guide(found, mood, hours, afk_skills(c), rock_halved_skills(c.player))

    return pick_replanned(ctx, build, hours, max_replans) if replan else pick(build(ctx), hours)


def session_prep(ctx, picks, hours: float) -> list[str]:
    """One prep for the session's combat: the picks share the charge pack."""
    combat = sum(p.extra_hours for p in picks if "combat" in p.entry.tags)
    return charge_steps(ctx, combat, hours)


# ---- JSON-able view models (the app's API) ----

def profile_view(ctx) -> dict:
    p = ctx.player
    return {
        "name": p.name, "ironman": p.ironman, "members": p.members,
        "quest_points": p.quest_points, "max_quest_points": p.max_quest_points,
        "journal_quest_points": p.journal_quest_points, "runescore": p.runescore,
        "skills": [{"name": s.name, "level": s.level, "xp": s.xp, "rank": s.rank,
                    "bonus_xp": p.bonus_xp.get(s.name) or None}
                   for s in sorted(p.skills.values(), key=lambda s: s.xp)],
        "rested_xp": p.rested_xp, "notes": list(p.notes),
    }


def entry_view(e: GuideEntry, prep: list[str] | None = None) -> dict:
    return {"track": e.track, "headline": e.headline, "hours": e.hours, "play_hours": e.time_to_play,
            "value": e.value, "why": e.why, "left": list(e.remaining), "unsure": list(e.unsure),
            "steps": list(e.steps), "tags": sorted(e.tags), "confidence": e.confidence, "prep": prep or []}


def guide_view(ctx, names=None, mood=None, session=None, top: int = 10, progress: Progress | None = None) -> dict:
    sections = guide_sections(ctx, names, mood, session, progress)
    out = []
    for n, entries in sections.items():
        shown = [entry_view(e, charge_steps(ctx, e.time_to_play if "combat" in e.tags else 0.0, session))
                 for e in entries[:top]]
        out.append({"track": n, "description": ALL_TRACKS[n].description, "total": len(entries), "entries": shown})
    return {"tracks": out, "notes": list(ctx.player.notes)}


def next_view(ctx, hours: float = 2.0, mood=None, replan=False, max_replans=None,
              progress: Progress | None = None) -> dict:
    picks = next_picks(ctx, hours, mood, replan, max_replans, progress)
    return {"hours": hours, "planned_hours": sum(p.extra_hours for p in picks),
            "picks": [{**entry_view(p.entry), "shares": list(p.shares), "extra_hours": p.extra_hours} for p in picks],
            "prep": session_prep(ctx, picks, hours), "notes": list(ctx.player.notes)}


def focus_view(ctx, goal: str, progress: Progress | None = None) -> dict:
    """Raises ValueError for a goal that doesn't parse."""
    from .recommend.focus import focus, parse_goal
    g = parse_goal(ctx, goal)
    if progress:
        progress(0, 0, f"planning {g.name}")
    return {"focus": dataclasses.asdict(focus(ctx, g)), "notes": list(ctx.player.notes)}
