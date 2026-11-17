"""Deathtouched darts: one dart kills a boss outright, so each is worth the time one kill of the best target would
take this player (or a kill they can't make at all). The wiki's rules (data/deathtouched_dart.json) shape the
targets: many bosses are immune (Hermod, Nakatra, Zuk, Raksha, the Mazcab raids, Nex AoD...), some only lose a phase
(Telos, Seiryu, Kalphite Queen: PARTIAL_SHARE of a kill), and a dart kill doesn't count in the Beasts tab, so
kill-count achievements are no reason to throw one; collection-log drops and drops other plans need are (drops count),
plus the few achievements the wiki says a dart unlocks. A boss with no kill rate for the player (no guide, no
combat stats) is valued at UNKILLABLE_HOURS, flagged as a guess.
"""

from __future__ import annotations

import json
from functools import cache
from ... import datapkg

from ...drops import bosses, boss_kills_per_hour, boss_sim_kills_per_hour, drop_chance, tables
from ...gear import STYLES
from ..base import Candidate, Context
from .gear import MIN_GAIN, SUPPLY_TAX, _combat_gains, _tool_gains, upgrade_hours_saved

DART = "Deathtouched dart"
UNKILLABLE_HOURS = 2.0  # a kill the player can't make: worth this much, as a placeholder
PARTIAL_SHARE = 0.5  # a dart that clears one phase: worth this much of a kill (assumption)
TRIP_HOURS = 0.05  # getting to the boss and throwing


@cache
def rules() -> dict:
    path = datapkg.path("deathtouched_dart.json")
    return json.loads(path.read_text()) if path.is_file() else {}


def targets() -> set[str]:
    """Bosses a dart can be thrown at: boss pages with a monster record (life points), less the wiki's immune list.
    The boss category also holds collective and achievement pages ("The Barrows Brothers", "Bosses", "Death")."""
    monsters = tables()["monsters"]
    immune = set(rules().get("immune", []))
    return {b for b in bosses() if (monsters.get(b) or {}).get("life_points") and b not in immune}


def drop_value(ctx: Context) -> dict[str, tuple[float, list[str]]]:
    """{boss: (hours one kill's expected drops are worth to this player, [what])}: drops other plans are short of at
    the gathering time each replaces, and gear or tool upgrades at the training time they save. A collection log
    is not a reason: seven darts won't finish one. Only bosses a dart works on."""
    p = ctx.player
    valid = targets()
    wanted = ctx.needs()
    gains = {**_combat_gains(ctx), **_tool_gains(ctx)} if p.ironman else {}
    out: dict[str, tuple[float, list[str]]] = {}
    for boss in valid:
        hours, lines = 0.0, []
        for item, chance, qty, _ in tables()["sources"].get(boss, []):
            c, guessed = drop_chance(chance)
            if not c:
                continue
            per_kill = c * (qty or 1)
            if item in wanted and (g := ctx.gatherer.complete_source(item)):
                h = per_kill * ctx.gatherer.net_cost(g)
                hours += h
                lines.append(f"{item} (1/{1 / c:,.0f}{', chance assumed' if guessed else ''}): {wanted[item]}")
            elif item in gains and gains[item][1] >= MIN_GAIN and gains[item][0] in STYLES and not p.holdings.get(item):
                style, gain = gains[item]
                h = c * upgrade_hours_saved(ctx, style, gain)
                hours += h
                lines.append(f"{item} (1/{1 / c:,.0f}): +{gain:.0%} {style} damage")
            elif item in gains and gains[item][1] >= MIN_GAIN and not p.holdings.get(item):
                lines.append(f"{item} (1/{1 / c:,.0f}): +{gains[item][1]:.0%} {gains[item][0]} xp/h")
        for ach in [a for b, a in rules().get("achievements_unlocked", {}).items() if b == boss]:
            a = p.achievements.get(ach.split(" (")[0])
            if a is not None and not a.completed:
                lines.append(f"unlocks the achievement {ach}")
        if lines:
            out[boss] = (hours, lines)
    return out


def kill_hours(ctx: Context, boss: str) -> tuple[float | None, str]:
    """(hours one kill takes the player, how) or (None, why not) when no rate is known."""
    p = ctx.player
    if rate := boss_kills_per_hour(p, boss):
        h = 1 / rate.kills_per_hour
        return h * (1 + SUPPLY_TAX if p.ironman else 1), f"{rate.kills_per_hour:,.1f} kills/h ({rate.guide.removeprefix('Money making guide/')})"
    if sim := boss_sim_kills_per_hour(p, boss):
        h = 1 / sim[0]
        return h * (1 + SUPPLY_TAX if p.ironman else 1), f"~{sim[0]:,.1f} kills/h (combat sim)"
    return None, "no kill rate for you (no guide, no combat stats): a dart is the only way counted"


class _Darts:
    name = "darts"
    description = "Deathtouched darts in the bank: the boss kill each one replaces"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        n = p.holdings.get(DART, 0)
        if n <= 0:
            return []
        value = drop_value(ctx)
        if not value:
            return []
        access = rules().get("access", {})
        partial = rules().get("partial", {})
        ranked = []
        for boss, (hours, lines) in value.items():
            kill, how = kill_hours(ctx, boss)
            share = PARTIAL_SHARE if boss in partial else 1.0
            ranked.append((hours * share, boss, kill, how, lines))
        # A dart skips the boss, not the dungeon in front of it: bosses you can walk up to first, then by value.
        ranked.sort(key=lambda r: (r[1] in access, -r[0], r[2] is None))
        best_value, best, kill, how, lines = ranked[0]
        if best_value <= 0:
            return []

        def line(v: float, b: str, k: float | None, hw: str, ls: list[str]) -> str:
            text = f"{b}: ~{60 * n * v:.0f} min of your plans' work expected from {n} kills; " + "; ".join(ls[:3])
            if b in partial:
                text += f"; {partial[b]} (counted as {PARTIAL_SHARE:.0%})"
            if b in access:
                text += f"; {access[b]}"
            text += f" [{'~' + format(k, '.2f') + 'h a kill for you, ' if k else ''}{hw}]"
            return text

        details = ["targets by what their drops are worth to your plans (direct-access bosses first):",
                   *(line(*r) for r in ranked[:6]), *rules().get("rules", [])]
        if any(r[1] in access for r in ranked[:6]):
            details.append(rules().get("access_note", ""))
        if rules().get("removal"):
            details.append(rules()["removal"])
        guessed = kill is None
        return [Candidate(
            track=self.name, title=f"Throw {n} x {DART} at {best}", value=round(n * best_value, 2), unit="h saved",
            cost_hours=round(n * TRIP_HOURS, 2), details=details, uncertain=guessed,
            why=f"{n} kills' drops worth ~{n * best_value:.1f}h to your plans: " + "; ".join(lines[:2]),
            needs=[f"{best} (reachable; a dart kills it)"],
        )]


DartsTrack = _Darts()
