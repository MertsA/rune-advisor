"""Distraction & Diversion reset tokens in the bank: each one buys another completion of a daily, weekly or monthly
D&D (dnd.rewards: xp in fixed skills, choice xp, items), worth the training it replaces toward something the skill
still unlocks (free_xp.training_hours with plans.goal_levels, weighted by [skill_weights]) plus the gathering time
of items other plans are short of, less the time the D&D itself takes: a reset that saves less than it costs to play
is not worth a token. A token resets one D&D once per reset period, so the tokens are scheduled into the periods
left before the tokens' removal (data/dnd_resets.json: 2026-11-23): the best (D&D, period) slots first, each D&D
once a period; the entry covers the current period (today / this week / this month) and says how many tokens to
hold back for the periods after. Tokens with no slot left are flagged, as is converting 7 daily -> 1 weekly / 4 weekly -> 1 monthly where
the higher token's best open slot beats the lower slots given up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from ...dnd import CADENCES, Reward, rewards, rules
from ...drops import SUMMONING_CHARMS
from ...xp import level_cap, xp_at
from ..base import Candidate, Context
from ..plans import goal_levels
from .free_xp import PASSIVE_SKILLS, training_hours

WEEKLY_RESET_WEEKDAY = 2  # Wednesday 00:00 UTC
REFERENCE_XP = 50_000.0  # hours per xp are measured on this much xp (a lamp's worth) and scaled


@dataclass
class Valued:
    reward: Reward
    hours: float  # training (and gathering) hours one reset saves
    lines: list[str]

    @property
    def net(self) -> float:
        """Hours saved less the time the D&D takes: what a token is worth here."""
        return self.hours - self.reward.minutes / 60


class Valuer:
    """Hours one xp saves per skill, memoised: training_hours runs a level plan per skill, so it is called once
    per skill at a reference xp and scaled (xp past the skill's goal is worth nothing, as in training_hours)."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.p = ctx.player
        self.goals = goal_levels(ctx)
        self.weights = getattr(ctx, "skill_weights", None) or {}
        self._rate: dict[str, float] = {}
        self._needs: dict[str, str] | None = None

    def room(self, skill: str) -> float:
        if skill not in self.goals or skill in PASSIVE_SKILLS:
            return 0.0
        cap = min(level_cap(skill, self.p.members), self.goals[skill])
        return max(0.0, xp_at(skill, cap) - self.p.xp(skill))

    def rate(self, skill: str) -> float:
        """Weighted hours saved per xp of `skill` (0 when nothing is left to unlock before 120)."""
        if skill not in self._rate:
            room = self.room(skill)
            ref = min(REFERENCE_XP, room)
            self._rate[skill] = (training_hours(self.ctx, skill, ref, self.goals) / ref if ref > 0 else 0.0) * self.weights.get(skill, 1.0)
        return self._rate[skill]

    def saved(self, skill: str, xp: float) -> float:
        return min(xp, self.room(skill)) * self.rate(skill)

    @property
    def needs(self) -> dict[str, str]:
        if self._needs is None:
            self._needs = self.ctx.needs()
        return self._needs

    def value(self, r: Reward) -> Valued:
        p = self.p
        hours, lines = 0.0, []
        for skill, xp in sorted(r.xp.items(), key=lambda kv: -kv[1]):
            h = self.saved(skill, xp)
            hours += h
            lines.append(f"{xp:,.0f} {skill} xp ({h * 60:.0f} min of work)" if h > 0 else f"{xp:,.0f} {skill} xp (nothing left to unlock)")
        for c in r.choice:
            options = [s for s in p.skills if p.skills[s].level >= c.min_level and s not in PASSIVE_SKILLS
                       and (c.skills is None or s in c.skills)]
            if not options:
                continue
            scored = {s: self.saved(s, c.units * c.xp(p.skills[s].level)) for s in options}
            best = max(options, key=lambda s: scored[s])
            hours += scored[best]
            xp = c.units * c.xp(p.skills[best].level)
            lines.append(f"{c.what}: {xp:,.0f} {best} xp ({scored[best] * 60:.0f} min of work)" if scored[best] > 0
                         else f"{c.what}: {xp:,.0f} xp, no skill with an unlock left")
        for item, qty in r.items.items():
            if item in self.needs and (g := self.ctx.gatherer.complete_source(item)):
                h = qty * self.ctx.gatherer.net_cost(g)
                hours += h
                lines.append(f"~{qty:g} {item} ({h * 60:.0f} min of gathering: {self.needs[item]})")
        if r.charm_buff_minutes and any(ch in self.needs for ch in SUMMONING_CHARMS):
            h = 2 * r.charm_buff_minutes / 60  # triple drops: 40 min yield what 120 min would
            hours += h
            lines.append(f"triple charms for {r.charm_buff_minutes:.0f} min (~{h * 60:.0f} min of charm farming saved, if your plans farm that long)")
        return Valued(r, hours, lines)


def periods(cadence: str, today: date, removal: date) -> list[tuple[date, str]]:
    """Reset periods that start before `removal` and haven't ended by `today`: (start, label)."""
    out: list[tuple[date, str]] = []
    if cadence == "daily":
        d = today
        while d < removal:
            out.append((d, "today" if d == today else "tomorrow" if d == today + timedelta(days=1) else d.strftime("%-d %b")))
            d += timedelta(days=1)
    elif cadence == "weekly":
        d = today - timedelta(days=(today.weekday() - WEEKLY_RESET_WEEKDAY) % 7)
        while d < removal:
            out.append((d, "this week" if d <= today else f"week of {d.strftime('%-d %b')}"))
            d += timedelta(days=7)
    else:
        d = today.replace(day=1)
        while d < removal:
            out.append((d, "this month" if d <= today else d.strftime("%B")))
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


@dataclass
class Slot:
    valued: Valued
    period: str

    @property
    def net(self) -> float:
        return self.valued.net


@dataclass
class Schedule:
    cadence: str
    tokens: int
    slots: list[Slot]  # where the tokens go, best first
    spare: list[Slot]  # open slots (net > 0) left after the tokens are placed
    unused: int  # tokens with no worthwhile slot before the removal

    @property
    def hours(self) -> float:
        return sum(s.valued.hours for s in self.slots)

    @property
    def minutes(self) -> float:
        return sum(s.valued.reward.minutes for s in self.slots)


def schedule(cadence: str, tokens: int, valued: list[Valued], today: date, removal: date) -> Schedule:
    slots = [Slot(v, label) for v in valued if v.net > 0 and not v.reward.blocked for _, label in periods(cadence, today, removal)]
    slots.sort(key=lambda s: -s.net)
    taken, spare = slots[:tokens], slots[tokens:]
    return Schedule(cadence, tokens, taken, spare, max(0, tokens - len(taken)))


def plan(ctx: Context, today: date | None = None) -> tuple[dict[str, list[Valued]], dict[str, Schedule], date]:
    """Every D&D's one-reset value for the player and the token schedule per cadence."""
    p = ctx.player
    data = rules()
    today = today or date.today()
    removal = date.fromisoformat(data["removal"]) if data.get("removal") else today + timedelta(days=365)
    valuer = Valuer(ctx)
    valued = {c: sorted((valuer.value(r) for r in rewards(p) if r.cadence == c), key=lambda v: -v.net) for c in CADENCES}
    tokens = {c: p.holdings.get(data.get("tokens", {}).get(c, {}).get("item", ""), 0) for c in CADENCES}
    return valued, {c: schedule(c, tokens[c], valued[c], today, removal) for c in CADENCES}, removal


def _ranking(c: str, valued: list[Valued]) -> list[str]:
    data = rules()
    lines = ["one reset's worth per D&D (training it replaces toward an unlock, items other plans are short of, less the D&D's own time):"]
    for v in valued:
        tag = f" [not yet: {', '.join(v.reward.blocked)}]" if v.reward.blocked else ""
        tag += f" [{v.reward.players}+ players]" if v.reward.players > 1 else ""
        what = "; ".join(v.lines) if v.lines else "nothing your plans value"
        lines.append(f"{v.reward.dnd}: ~{v.hours * 60:.0f} min saved for ~{v.reward.minutes:.0f} min of play"
                     f" (net {v.net * 60:+.0f}){tag}: {what}")
    for n_, why in data.get("not_valued", {}).items():
        if n_ in data["dnds"].get(c, []):
            lines.append(f"{n_}: not valued ({why})")
    return lines


def _conversion(c: str, plans: dict[str, Schedule]) -> str | None:
    """Whether converting this cadence's tokens up pays: the higher token's best open slot vs the slots given up."""
    conv = rules()["tokens"][c].get("convert")
    if not conv:
        return None
    to, n = conv
    s, up = plans[c], plans[to]
    if not up.spare:
        return None
    best = up.spare[0]
    given = s.slots[-n:] if len(s.slots) >= n else s.slots
    cost = sum(x.net for x in given)
    if s.unused >= n or best.net > cost:
        return (f"convert {n} {c} tokens into a {to} one: its best open slot, {best.valued.reward.dnd} ({best.period}), nets "
                f"~{best.net * 60:.0f} min vs ~{cost * 60:.0f} min from the {c} slots given up")
    return None


class _DndTokens:
    name = "dnd_tokens"
    description = "D&D reset tokens in the bank: which Distraction & Diversion to reset with each before they go"

    def candidates(self, ctx: Context, today: date | None = None) -> list[Candidate]:
        data = rules()
        today = today or date.today()
        valued, plans, removal = plan(ctx, today)
        if not any(s.tokens for s in plans.values()):
            return []
        idle = [f"{plans[c].tokens} x {data['tokens'][c]['item']}: no D&D worth a reset for you (each takes longer than the training "
                f"it would replace)" + (f"; {conv}" if (conv := _conversion(c, plans)) else "")
                for c in CADENCES if plans[c].tokens and not plans[c].slots]
        out = []
        for c in CADENCES:
            s = plans[c]
            if not s.slots:
                continue
            item = data["tokens"][c]["item"]
            n_periods = len(periods(c, today, removal))
            # One entry per cadence, for the current reset period: later periods' tokens are held back and listed.
            current = periods(c, today, removal)[0][1]
            now = [x for x in s.slots if x.period == current]
            later: dict[str, list[Slot]] = {}
            for x in s.slots:
                if x.period != current:
                    later.setdefault(x.period, []).append(x)
            details = [f"{x.valued.reward.dnd} ({x.period}): ~{x.valued.hours * 60:.0f} min saved for ~{x.valued.reward.minutes:.0f} min" for x in now]
            if later:
                details.append(f"hold {sum(len(v) for v in later.values())} back: " + "; ".join(
                    f"{label}: {', '.join(x.valued.reward.dnd for x in xs)}" for label, xs in later.items()))
            details += _ranking(c, valued[c])
            details.append(f"{n_periods} {c} reset period{'s' if n_periods != 1 else ''} before {removal:%-d %b}, one reset per D&D each; "
                           f"{s.tokens} tokens held")
            if s.unused:
                details.append(f"{s.unused} of the {s.tokens} tokens have no worthwhile slot left before the removal"
                               + (": the game converts leftovers to coins at a reduced rate" if not data["tokens"][c].get("convert") else ""))
            if conv := _conversion(c, plans):
                details.append(conv)
            details += idle
            for v in valued[c]:
                for a in v.reward.assumptions[:1]:
                    details.append(f"{v.reward.dnd}: assumes {a}")
            details += data.get("rules", [])
            if data.get("removal_note"):
                details.append(data["removal_note"])
            if not now:
                continue
            hours = sum(x.valued.hours for x in now)
            minutes = sum(x.valued.reward.minutes for x in now)
            dnds = [x.valued.reward.dnd for x in now]
            out.append(Candidate(
                track=self.name, title=f"Use {len(now)} x {item} {current} on {', '.join(dnds)}", value=round(hours, 2), unit="h saved",
                cost_hours=round(minutes / 60, 2), uncertain=True, details=details,
                why=f"~{hours:.1f}h of your plans' work for ~{minutes / 60:.1f}h of D&Ds; the tokens go on {removal:%-d %b}",
                needs=[f"reset {x.valued.reward.dnd} {x.period} (~{x.valued.reward.minutes / 60:.1f}h)" for x in now],
                skills={sk for x in now for sk in x.valued.reward.xp},
            ))
        return out


DndTokensTrack = _DndTokens()
