"""Recorded play sessions (the dev plugin's Start/Stop recorder, plugins/rune_advisor_dev/lib/recorder.lua).

A session is the player's own xp gains, net item changes and nearby object changes over a stretch of
play, timed in active seconds (logged-out time excluded). The planner never reads sessions: they are
evidence for working out mechanics (crack swings, respawn times, drop mixes) and for checking the sims,
and a curated summary of one can be pasted into rune_advisor.toml as a `[[methods]]` row for content the
wiki has no mechanics for (Croesus nodes, new content). From a session we get:
- xp/h per skill, items gained and used per hour;
- per object (loc) name, how long it stays at a tile before the tile changes, i.e. a node's lifetime
  ("COPPER_ROCK" -> depleted) and its depleted/respawn time ("COPPER_ROCK_DEPLETED" -> back);
- how AFK it is: game actions (clicks/menu choices) and key presses per hour, the median gap between
  inputs, and the share of the session spent in idle stretches of IDLE_GAP_S or more;
- a `[[methods]]` entry for rune_advisor.toml (main skill, level, xp/h, yields, side xp, clicks/h, input gap).
The recorder only sees successes, never attempts, so this measures what the player got, not a chance.
"""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from .sources.plugin_db import SKILLTYPE_TO_NAME

SESSION_PREFIX = "session:"  # session:<player>:<n>
OREBOX_VAR = "var:MINING_OREBOX_"
IDLE_GAP_S = 60  # an input gap at least this long counts as idle (AFK) time


_TAG = re.compile(r"<[^>]+>")


@dataclass
class Session:
    player: str
    number: int
    label: str | None
    seconds: float
    ended: bool
    xp: dict[str, float]  # skill -> xp gained
    gained: dict[str, float]  # item -> total gained
    used: dict[str, float]  # item -> total lost (used up, sold, dropped)
    start_levels: dict[str, int]
    worn: list[str]
    loc_lifetimes: dict[str, list[float]] = field(default_factory=dict)  # loc name -> seconds at a tile
    clicks: int = 0
    keys: int = 0
    input_times: list[float] = field(default_factory=list)  # clicks and key presses, seconds
    actions: Counter = field(default_factory=Counter)  # (verb, target) -> times chosen
    clicks_seq: list[tuple[float, str, str]] = field(default_factory=list)  # (seconds, verb, target), in order
    started_at: float | None = None  # Unix time of Start, from the first chat message's timestamp
    # The game's own chat messages: (seconds, chat type, text verbatim). Empty without chatAccess.
    chat: list[tuple[float, str, str]] = field(default_factory=list)
    # Own movement (see movement.py): (seconds, level, x, z) whenever the tile changed, and the client's
    # route to each new destination as (seconds, level, [(x, z), ...]) waypoints.
    moves: list[tuple[float, int, int, int]] = field(default_factory=list)
    routes: list[tuple[float, int, list[tuple[int, int]]]] = field(default_factory=list)
    # A clicked object's MultiLoc variable changing (a rock depleting/respawning fires no Loc event, so the
    # recorder polls it): (seconds, var name, new value, x, z).
    loc_vars: list[tuple[float, str, int, int, int]] = field(default_factory=list)

    @property
    def input_gaps(self) -> list[float]:
        """Seconds between inputs, including Start -> first input and last input -> Stop."""
        times = [0.0, *sorted(self.input_times), self.seconds]
        return [b - a for a, b in zip(times, times[1:])]

    @property
    def median_input_gap(self) -> float | None:
        gaps = self.input_gaps
        return statistics.median(gaps) if self.input_times else None

    @property
    def idle_share(self) -> float:
        """Share of the session spent in input gaps of IDLE_GAP_S or more."""
        return sum(g for g in self.input_gaps if g >= IDLE_GAP_S) / self.seconds if self.seconds else 0.0

    @property
    def hours(self) -> float:
        return self.seconds / 3600

    def per_hour(self, amounts: dict[str, float]) -> dict[str, float]:
        return {k: v / self.hours for k, v in amounts.items()} if self.hours else {}

    @property
    def main_skill(self) -> str | None:
        return max(self.xp, key=self.xp.get) if self.xp else None

    @property
    def activity(self) -> str | None:
        """What the session was spent on: the most chosen action's target ("Essence deposit")."""
        if not self.actions:
            return self.label
        (verb, target), _ = self.actions.most_common(1)[0]
        return target or self.label

    def method(self, skill: str | None = None, name: str | None = None) -> dict[str, Any]:
        """A [[methods]] row for rune_advisor.toml from this session's rates."""
        skill = skill or self.main_skill
        xp_h = self.per_hour(self.xp)
        row: dict[str, Any] = {
            "skill": skill,
            "method": name or self.label or f"Recorded session {self.number}",
            "level_min": self.start_levels.get(skill, 1),
            "xp_per_hour": round(xp_h.get(skill, 0)),
        }
        net = {i: self.gained.get(i, 0) - self.used.get(i, 0) for i in set(self.gained) | set(self.used)}
        yields = {i: round(n / self.hours, 1) for i, n in sorted(net.items()) if n > 0}
        if yields:
            row["yields"] = yields
        side = {s: round(v) for s, v in sorted(xp_h.items()) if s != skill and v > 0}
        if side:
            row["side_xp"] = side
        if self.input_times and self.hours:
            row["clicks_per_hour"] = round(self.clicks / self.hours)
            row["input_gap_s"] = round(self.median_input_gap, 1)
        if self.activity:
            row["activity"] = self.activity
        return row


@cache
def _ores() -> dict[str, str]:
    """Ore box var suffix (COPPER, CALCIFIED_FUNGUS...) -> ore item name, from the Mining sim's rocks."""
    from .sim import data
    out = {}
    for rock in data()["rocks"]:
        ore = rock.get("ore") or ""
        norm = re.sub(r"[^A-Z0-9]+", "_", ore.upper()).strip("_")
        out[norm] = ore
        out.setdefault(norm.removesuffix("_ORE"), ore)
    return out


def _item_name(key: str, names: dict[str, str]) -> str:
    if key.startswith(OREBOX_VAR):
        suffix = key[len(OREBOX_VAR):]
        return _ores().get(suffix) or f"{suffix.replace('_', ' ').capitalize()} (ore box)"
    return names.get(key, f"item {key}")


def _loc_lifetimes(events: list[list]) -> dict[str, list[float]]:
    """Seconds each loc name stayed at a tile before that tile changed again. Only spans with a
    known start and end count (the first sight of a tile has no start)."""
    at: dict[tuple, tuple[str, float]] = {}
    out: dict[str, list[float]] = defaultdict(list)
    for e in events:
        if e[0] == "l":
            _, t, name, x, z = (e + [None])[:5]
            tile = (x, z)
        elif e[0] == "r":
            _, t, x, z = (e + [None])[:4]
            tile, name = (x, z), None
        else:
            continue
        if tile in at:
            prev, since = at.pop(tile)
            out[prev].append(t - since)
        if name:
            at[tile] = (name, t)
    return dict(out)


def parse(raw: dict[str, Any]) -> Session:
    names = raw.get("items") or {}
    xp: dict[str, float] = defaultdict(float)
    gained: dict[str, float] = defaultdict(float)
    used: dict[str, float] = defaultdict(float)
    actions: Counter = Counter()
    inputs: list[float] = []
    chat: list[tuple[float, str, str]] = []
    clicks_seq: list[tuple[float, str, str]] = []
    moves: list[tuple[float, int, int, int]] = []
    routes: list[tuple[float, int, list[tuple[int, int]]]] = []
    loc_vars: list[tuple[float, str, int, int, int]] = []
    epoch = None
    keys = 0
    for e in raw.get("events") or []:
        if e[0] == "p" and len(e) >= 5:
            moves.append((float(e[1]), int(e[2]), int(e[3]), int(e[4])))
            continue
        if e[0] == "w" and len(e) >= 4 and isinstance(e[3], list):
            flat = [int(v) for v in e[3]]
            routes.append((float(e[1]), int(e[2]), list(zip(flat[0::2], flat[1::2]))))
            continue
        if e[0] == "m" and len(e) >= 6:
            loc_vars.append((float(e[1]), str(e[2]), e[3], int(e[4]), int(e[5])))
            continue
        if e[0] == "a":
            actions[(e[2], e[3])] += 1
            inputs.append(e[1])
            clicks_seq.append((float(e[1]), str(e[2]), _TAG.sub("", str(e[3]))))
        elif e[0] == "k":
            keys += 1
            inputs.append(e[1])
        elif e[0] == "x":
            xp[SKILLTYPE_TO_NAME.get(e[2], e[2])] += e[3]
        elif e[0] == "i":
            for key, delta in e[2].items():
                (gained if delta > 0 else used)[_item_name(key, names)] += abs(delta)
        elif e[0] == "c" and len(e) >= 4:
            chat.append((float(e[1]), str(e[2]), str(e[3])))
            if len(e) >= 5 and e[4] and epoch is None:
                epoch = float(e[4]) - float(e[1])  # wall-clock time of the session's start
    return Session(
        player=raw.get("player", ""), number=int(raw.get("number") or 0), label=raw.get("label"),
        seconds=float(raw.get("seconds") or 0), ended=bool(raw.get("ended")), xp=dict(xp),
        gained=dict(gained), used=dict(used),
        start_levels={SKILLTYPE_TO_NAME.get(k, k): v.get("level", 1) for k, v in (raw.get("startSkills") or {}).items()},
        worn=list(raw.get("worn") or []), loc_lifetimes=_loc_lifetimes(raw.get("events") or []),
        clicks=sum(actions.values()), keys=keys, input_times=inputs, actions=actions, chat=chat, clicks_seq=clicks_seq, started_at=epoch,
        moves=moves, routes=routes, loc_vars=loc_vars,
    )


_NUMBER = re.compile(r"\d[\d,.]*")


def target_changes(s: "Session") -> list[tuple[float, str, str]]:
    """(seconds, old target, new target) whenever the same action ("Mine") moves to a differently named
    target: an object changing state ("Essence deposit" -> "Essence deposit (cracked)"). The server
    doesn't send loc events for these swaps, so the clicked names are the record of them."""
    out, last = [], {}
    for t, verb, target in s.clicks_seq:
        prev = last.get(verb)
        # Only names that extend each other ("Essence deposit" / "Essence deposit (cracked)"): switching
        # between different targets (moth colours) isn't a state change.
        if prev is not None and target and prev != target and (target.startswith(prev) or prev.startswith(target)):
            out.append((t, prev, target))
        if target:
            last[verb] = target
    return out


def message_pattern(text: str) -> str:
    """A chat message with colour tags dropped and numbers as "#", so "You get 3 logs" and
    "You get 5 logs" count as one kind of message."""
    return _NUMBER.sub("#", _TAG.sub("", text)).strip()


@dataclass
class MessageCadence:
    pattern: str
    count: int
    per_hour: float
    median_gap: float | None  # seconds between consecutive ones
    first: float  # seconds into the session
    example: str  # one verbatim occurrence


def message_cadence(s: "Session") -> list[MessageCadence]:
    """Per kind of game message: how often it came, and the usual gap between two of them (the
    activity's cadence: a swing message per attempt, a success message per log)."""
    times: dict[str, list[float]] = defaultdict(list)
    example: dict[str, str] = {}
    for t, _, text in s.chat:
        key = message_pattern(text)
        times[key].append(t)
        example.setdefault(key, text)
    out = []
    for key, ts in times.items():
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]))
        out.append(MessageCadence(key, len(ts), len(ts) / s.hours if s.hours else 0.0,
                                  gaps[len(gaps) // 2] if gaps else None, ts[0], example[key]))
    return sorted(out, key=lambda m: -m.count)


TICK_S = 0.6
START_MESSAGE = re.compile(r"You (swing|cast|begin|start)\b")  # the game's "started gathering" messages
STALL_ODDS = 0.05  # a session's runs of misses this unlikely in all (at the measured chance) are stalls, not bad luck


@dataclass
class Stall:
    start: float  # seconds: the last success (or the start click) before the gap
    end: float  # the success that ended it
    swings: int
    after: str  # the input or game message inside the gap that came first ("start" for the first gap)


@dataclass
class Swings:
    """Per-attempt success from a gathering session: the success messages land on a fixed swing grid
    (whole ticks apart), so the gaps between them count the misses too."""
    message: str  # the success message's pattern
    interval_ticks: int
    successes: int
    swings: int  # from each start to its successes, stalls included (walks between starts aren't)
    stalls: list[Stall]

    @property
    def chance(self) -> float:
        """Success per swing outside the stalls."""
        n = self.swings - sum(st.swings for st in self.stalls)
        return (self.successes - len(self.stalls)) / n if n > 0 else 0.0

    @property
    def chance_all(self) -> float:
        return self.successes / self.swings if self.swings else 0.0


def swings(s: "Session", min_successes: int = 10) -> Swings | None:
    """Swing analysis on the session's most frequent game message (taken as the success message), or None
    when it's too rare or its gaps aren't whole multiples of one swing interval."""
    cad = message_cadence(s)
    if not cad or cad[0].count < min_successes:
        return None
    key = cad[0].pattern
    hits = [t for t, _, text in s.chat if message_pattern(text) == key]
    # Where each run of swings starts: the game's start message ("You swing your hatchet at the tree.")
    # comes a tick after the first swing is queued; without one, the clicks on the activity's target.
    # Time from the last success to the next start (walking to another tree, a full backpack) isn't counted.
    starts = [t - TICK_S for t, _, text in s.chat if START_MESSAGE.match(_TAG.sub("", text))]
    if not starts:
        first = next(((v, g) for t, v, g in s.clicks_seq if t < hits[0]), None)
        starts = [t for t, v, g in s.clicks_seq if (v, g) == first] if first else []
    # Gaps within one run of swings (no new start between them): walks to the next tree aren't swings.
    ticks = [round((b - a) / TICK_S) for a, b in zip(hits, hits[1:]) if not any(a < st < b for st in starts)]
    # The swing interval: the largest whole-tick step that 90% of those gaps are multiples of.
    interval = next((k for k in range(8, 1, -1) if sum(g % k == 0 for g in ticks) >= 0.9 * len(ticks)), 1)
    step = interval * TICK_S
    gaps, prev = [], None
    for h in hits:
        anchor = max((a for a in starts if a < h and (prev is None or a > prev)), default=None)
        a = anchor if anchor is not None else prev
        if a is not None:
            gaps.append((a, h, max(1, round((h - a) / step)), anchor is not None))
        prev = h
    total = sum(g[2] for g in gaps)
    # Test each run against the chance measured over all gaps (no stalls assumed), with the odds threshold
    # split across the gaps: one unlucky run in a long session isn't a stall. (Estimating the chance without
    # the long runs first is self-confirming: it raises the chance, which makes the long runs look impossible.)
    p = len(gaps) / total if total else 1.0
    stalled = [g for g in gaps if (1 - p) ** (g[2] - 1) < STALL_ODDS / len(gaps)]
    events = [(t, f"{verb} {target}".strip()) for t, verb, target in s.clicks_seq] + \
             [(t, message_pattern(text)) for t, _, text in s.chat if message_pattern(text) != key]
    out = []
    for a, b, n, from_start in stalled:
        if from_start:
            after = "start"
        else:
            inside = sorted(e for e in events if a < e[0] < b)
            after = f"{inside[0][1]} ({inside[0][0] - a:.1f}s in)" if inside else "nothing logged"
        out.append(Stall(a, b, n, after))
    return Swings(key, interval, len(hits), total, out)


def sessions(entries: dict[str, Any], player: str | None = None) -> list[Session]:
    """Sessions in the exporter's PersistentDB entries, oldest first."""
    out = []
    for key, val in entries.items():
        if key.startswith(SESSION_PREFIX) and isinstance(val, dict):
            s = parse(val)
            if player is None or s.player.lower() == player.lower():
                out.append(s)
    return sorted(out, key=lambda s: (s.player.lower(), s.number))


def lifetime_summary(s: Session, min_samples: int = 2) -> list[tuple[str, int, float]]:
    """(loc name, samples, median seconds) for locs seen changing at least min_samples times."""
    rows = [(name, len(v), statistics.median(v)) for name, v in s.loc_lifetimes.items() if len(v) >= min_samples]
    return sorted(rows, key=lambda r: -r[1])


def to_toml(row: dict[str, Any]) -> str:
    def val(v):
        return f'"{v}"' if isinstance(v, str) else (f"{v:g}" if isinstance(v, float) else str(v))

    def table(d):
        return "{ " + ", ".join(f'"{k}" = {val(v)}' for k, v in d.items()) + " }"

    lines = ["[[methods]]"]
    for k, v in row.items():
        lines.append(f"{k} = {table(v) if isinstance(v, dict) else val(v)}")
    return "\n".join(lines)
