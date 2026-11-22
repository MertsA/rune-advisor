"""A "next session" picker: pack guide entries into a session of a given length (`radv next`).

Tracks stay separate (no common score), so the picker doesn't trade one track off against another: it
takes cash-ins first (free_xp: minutes for banked xp), then goes round the tracks in turn, taking each one's
best remaining entry that fits the time left. Mostly guessed entries are only used when nothing modelled
fits. The same goal shown by two tracks (Ardougne Elite as RuneScore and as a task set) is taken once;
entries sharing an unmet requirement ("93 Summoning") are marked, and one whose requirements are all done for
earlier picks (a miniquest inside the miniquest set) takes no extra time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .guide import GuideEntry

FIRST_TRACKS = ("free_xp", "darts", "dnd_tokens")  # banked xp, banked kills, expiring tokens: always worth starting with


def without_choice(pool):
    """The pool without choice resources (lamps and points that can go on any skill): the free_xp track decides
    where those go, so other tracks' level plans must not spend them too (2026-11-11: with 184 penguin points in
    the pool, 92 Defence, 92 Hitpoints and 104 Thieving all read 0.0h beside "spend the points on Invention").
    Skill-bound resources (a Smithing-only lamp, banked bonus xp) stay; bonus xp free_xp places up front (black
    pearls, rested xp at its cap: Resource.spend_first) goes with the choice resources."""
    from ..resources import ResourcePool

    if pool is None:
        return None
    return ResourcePool([r for r in pool.resources if r.skills is not None or (r.bonus and not r.spend_first)])


def candidates_for_picking(ctx, tracks: dict, names=None, step=None) -> dict:
    """Each track's candidates for the picker: free_xp sees the whole pool, every other track the pool without
    the choice resources free_xp hands out (see without_choice). `step(name, fn)` runs each track (views.Steps)."""
    pool, out = ctx.resources, {}
    step = step or (lambda _name, fn: fn())
    try:
        for n in (names or tracks):
            ctx.resources = pool if n in FIRST_TRACKS else without_choice(pool)
            out[n] = step(n, lambda n=n: tracks[n].candidates(ctx))
    finally:
        ctx.resources = pool
    return out


@dataclass
class Pick:
    entry: GuideEntry
    shares: list[str] = field(default_factory=list)  # requirements another pick also needs
    extra_hours: float = 0.0  # session time it adds, in play hours (0 when earlier picks already do all it needs)


def goal_key(e: GuideEntry) -> str:
    """The goal behind an entry's headline: "Ardougne Set Tasks - Elite: 1 of 5 left" -> "ardougne set tasks - elite"."""
    return re.sub(r":.*$| \(.*\)$", "", e.headline).strip().lower()


def _requirement(need: str) -> str:
    """A need without its sub-achievement prefix and hours: "Abyssal Valet: 93 Summoning (have 92, ~1.5h)" -> "93 Summoning"."""
    return re.sub(r"\s*\(.*$", "", need.split(": ")[-1]).strip()


LEVEL = re.compile(r"^(\d+) ([A-Z][a-z]+)$")
HOURS = re.compile(r"~([\d.]+)h\)?$")


def _need_hours(need: str) -> float:
    m = HOURS.search(need)
    return float(m[1]) if m else 0.0


def pick(sections: dict[str, list[GuideEntry]], hours: float, exclude: set[str] | None = None,
         fallback: bool = True) -> list[Pick]:
    """Re-plans as it goes, cheaply: a requirement an earlier pick already meets (the same need, or a level
    at or below one an earlier pick reaches: "92 Defence" after "99 Defence") comes off a later pick's hours.
    The budget is play time (`GuideEntry.time_to_play`): an entry's ranking cost is net of the xp it earns,
    but a task set's lamps arrive after the session's work, not during it (2026-11-09: the Ardougne elite set
    read 0.2h net for 1.8h of play and a 2h session packed 3.6h)."""
    left, chosen, keys = hours, [], set(exclude or ())
    seen: dict[str, str] = {}  # requirement -> the pick that does it
    reached: dict[str, tuple[int, str]] = {}  # skill -> (level, pick) that earlier picks reach

    def covered(req: str) -> str | None:
        if req in seen:
            return seen[req]
        if (m := LEVEL.match(req)) and (r := reached.get(m[2])) and r[0] >= int(m[1]):
            return r[1]
        return None

    def take(e: GuideEntry) -> bool:
        nonlocal left
        if goal_key(e) in keys:
            return False
        reqs = [(_requirement(n), n) for n in e.remaining]
        shared = [(r, raw, by) for r, raw in reqs if (by := covered(r))]
        if reqs and len(shared) == len(reqs):
            extra = 0.0  # everything it needs is done for earlier picks
        else:
            extra = max(0.0, e.time_to_play - sum(_need_hours(raw) for _, raw, _ in shared))
        if extra > left:
            return False
        chosen.append(Pick(e, [f"{r} (also for {by})" for r, _, by in shared], extra))
        for r, _ in reqs + [(e.headline, e.headline)]:
            seen.setdefault(r, e.headline)
            if (m := LEVEL.match(r)) and int(m[1]) > reached.get(m[2], (0, ""))[0]:
                reached[m[2]] = (int(m[1]), e.headline)
        keys.add(goal_key(e))
        left -= extra
        return True

    for t in FIRST_TRACKS:
        for e in sections.get(t, []):
            take(e)
    queues = {t: [e for e in es if e.confidence != "mostly guessed"] for t, es in sections.items() if t not in FIRST_TRACKS}
    guessed = [e for t, es in sections.items() if t not in FIRST_TRACKS for e in es if e.confidence == "mostly guessed"]
    progress = True
    while progress and left > 0:
        progress = False
        for t, queue in queues.items():
            while queue:
                if take(queue.pop(0)):
                    progress = True
                    break
    if fallback and not any(p.entry.track not in FIRST_TRACKS for p in chosen):
        for e in guessed:
            take(e)
    return chosen


QUEST = re.compile(r"^quest (.+?)(?: \(~[\d.]+h\))?$")


def effects(e: GuideEntry) -> tuple[dict[str, int], set[str]]:
    """(levels reached, quests finished) by doing a pick: its own "99 Hunter" headline and its needs."""
    levels: dict[str, int] = {}
    quests: set[str] = set()
    for text in [e.headline, *e.remaining]:
        req = _requirement(text) if text is not e.headline else text
        if m := LEVEL.match(req):
            levels[m[2]] = max(levels.get(m[2], 0), int(m[1]))
        for part in text.split(": "):
            if q := QUEST.match(part.strip()):
                quests.add(q[1])
    return levels, quests


MAX_REPLANS = None  # re-plans per session (None: after every pick that changes a level or quest; minutes each)


def pick_replanned(ctx, build, hours: float, max_replans: int | None = MAX_REPLANS) -> list[Pick]:
    """`pick`, re-planning after picks that reach a level or finish a quest: those are applied to a copy of the
    player, and the tracks with an entry mentioning the changed skill or quest are costed again
    (`build(ctx, tracks)` -> guide sections), so a later pick's cost reflects what earlier ones did. The drop
    needs, category priors and gatherer are kept (they barely move with a level). `max_replans` caps the passes
    (after that the cheap shared-requirement check in `pick` carries on); None re-plans after every such pick."""
    import dataclasses

    from ..models import QuestStatus
    from ..xp import xp_at

    player = dataclasses.replace(ctx.player, skills=dict(ctx.player.skills), quests=dict(ctx.player.quests))
    ctx = dataclasses.replace(ctx, player=player)
    sections, chosen, keys, left, replans = build(ctx, None), [], set(), hours, 0
    while left > 0:
        nxt = pick(sections, left, keys, fallback=not any(c.entry.track not in FIRST_TRACKS for c in chosen))
        if not nxt:
            break
        p = nxt[0]
        chosen.append(p)
        keys.add(goal_key(p.entry))
        left -= p.extra_hours
        levels, quests = effects(p.entry)
        changed = []
        for skill, lvl in levels.items():
            s = player.skills.get(skill)
            if s and s.level < lvl:
                player.skills[skill] = dataclasses.replace(s, level=lvl, xp=max(s.xp, xp_at(skill, lvl)))
                changed.append(skill)
        for q in quests:
            if q in player.quests and not player.quests[q].done:
                player.quests[q] = dataclasses.replace(player.quests[q], status=QuestStatus.COMPLETED)
                changed.append(q)
        if not changed or (max_replans is not None and replans >= max_replans):
            continue
        stale = [t for t, es in sections.items()
                 if any(c in " ".join([e.headline, *e.remaining]) for e in es for c in changed)]
        if stale:
            replans += 1
            ctx = dataclasses.replace(ctx, _activities=None, _urn_options={})
            sections = {**sections, **build(ctx, stale)}
    return chosen
