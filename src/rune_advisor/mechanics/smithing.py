"""Smithing, from the mechanics the RuneScape Wiki documents (its Smithing rate calculator and data tables; the
numbers are in mechanics.json).

Forging: an item needs `bars x metal progress` progress. Each strike (2 ticks) adds progress: base 10
(+1 from the metal's extra-progress level) x the method's multiplier (High Heat 2 while above 66% heat;
Superheat Form lets it drop to 33%), plus double-progress chances (Tinker perk, Varrock armour), times
the Rapid perk. Each strike loses 10 heat (x2 with Rapid); the heat pool is 300 + 3 x Smithing + 3 x
Firemaking (+50/+100 at the metal's milestones). Reheating takes the metal's reheat ticks (6, 4 or 2 by
level) for the share of heat used, plus tick waste (turning, delays). Items/h = 6000 / ticks per item.
Smelting: a bar every 4 / 3.5 / 3 ticks by level, with a 10% double-bar chance past the metal's level,
plus Varrock armour and Fortune.

xp: the metal's xp per bar x bars, times the Blacksmith's outfit, Wise and clan avatar (urns are the
planner's, boosts.best_urn). The method defaults to High Heat (`smithing_method` in the boosts table
picks another, e.g. "AFK with Autoheater"). Items with upgrades are rated as the whole chain from bars
to the metal's top "+k" (each step: progress x progress_mult, xp x bar_mult, bars x 2^(k-1)).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..models import PlayerState
from . import bands, data, profile, row

TICKS_PER_HOUR = 6000
DEFAULT_METHOD = "High Heat"
_LINK = re.compile(r"\[\[([^\]|]+)\]\]")


def _bar(metal: dict) -> str:
    name = metal["metal"]
    return name if name.endswith(("bar", "ore")) else f"{name} bar"


def _ores(metal: dict) -> list[list]:
    out = []
    for o in metal.get("ores") or []:
        names = [n for n in _LINK.findall(o.get("link", "")) if not n.startswith("File:")]
        if names:
            out.append([names[0], o.get("count", 1)])
    return out


def _boosts(player: PlayerState) -> dict:
    s = data().get("smithing") or {}
    perk = profile.perks(player, "hammer")
    varrock = next((t for t in (4, 3, 2, 1) if f"Varrock armour {t}" in player.holdings), 0)
    return {"tinker": (s.get("tinker") or {}).get(f"Tinker {perk['Tinker']}", 0) if perk.get("Tinker") else 0,
            "rapid": (s.get("rapid") or {}).get(f"Rapid {perk['Rapid']}", {"heat": 1, "progress": 1})
            if perk.get("Rapid") else {"heat": 1, "progress": 1},
            "varrock": varrock, "fortune": perk.get("Fortune", 0),
            "cape": player.level("Smithing") >= 99 and bool(profile.has(player, "Smithing cape*"))}


def step_ticks(player: PlayerState, item: str) -> float | None:
    """Ticks to forge one step of an item ("Rune platebody", "Rune platebody + 3") at the player's level,
    from the forging model; None if the item isn't a forgeable metal item."""
    base, _, k = item.partition(" + ")
    table = _step_table(player.level("Smithing"), player.level("Firemaking"), _profile_key(player))
    if not table.get("filled"):
        table["filled"] = True
        forging(player)  # fills the table for the player's level
    return table.get((base, int(k) if k.isdigit() else 0))


def _profile_key(player: PlayerState) -> tuple:
    b = _boosts(player)
    return (b["tinker"], tuple(sorted(b["rapid"].items())), b["varrock"], b["cape"],
            profile.boosts(player).get("smithing_method") or DEFAULT_METHOD)


_STEP_CACHE: dict[tuple, dict] = {}


def _step_table(smith_level: int, fm_level: int, key: tuple) -> dict:
    cache_key = (smith_level, fm_level, key)
    if cache_key not in _STEP_CACHE:
        _STEP_CACHE[cache_key] = {}
    return _STEP_CACHE[cache_key]


@dataclass
class _Anvil:
    """How one metal forges for one smith: what a strike adds and what keeping the item hot costs."""
    per_strike: float  # progress one strike adds
    strike_ticks: int
    per_heat: float  # progress made between two reheats (the heat the method lets the item lose)
    reheat_pause: int  # ticks one reheat takes out of forging, turning to the forge and back included
    overhead: float  # ticks every item costs besides striking and reheating

    def ticks(self, progress: float) -> float:
        """Ticks to put `progress` into an item: the strikes, a pause each time the heat runs out before the
        item is finished, and the fixed overhead."""
        pauses = max(0, math.ceil((progress - self.per_heat) / self.per_heat))  # heats needed beyond the first
        return math.ceil(progress / self.per_strike) * self.strike_ticks + pauses * self.reheat_pause + self.overhead


def _anvil(metal: dict, smith: int, firemaking: int, method: dict, b: dict, s: dict) -> _Anvil:
    """The forging terms for a metal at a Smithing level (the cape's +1 already counted)."""
    # A strike: the base progress (one more past the metal's own level for it), times the method, plus the
    # chance of double progress (Tinker, Varrock armour where it covers the metal), times Rapid.
    double = b["tinker"]
    if b["varrock"] and (metal.get("varrock") or [0] * 4)[b["varrock"] - 1] == 1:
        armour = ((data().get("mining") or {}).get("boosts") or {}).get("varrock") or {}
        double += armour.get(metal.get("varrock_no") or "", {}).get("effect", 0)
    strike = (s["base_progress"] + (smith >= (metal.get("extra_progress") or 999))) * method["progress"]
    strike = (strike + strike * double) * b["rapid"]["progress"]

    # Heat: a pool that grows with Smithing and Firemaking and at the metal's two milestones; the method works
    # only in the top part of it, and every strike costs heat (more with Rapid).
    pool = 300 + 3 * smith + 3 * firemaking
    milestones = metal.get("max_heat_inc") or []
    reached = sum(1 for at in milestones[:2] if smith >= at)
    if reached:
        pool += s["max_heat_inc"][reached - 1]
    usable = pool - math.floor(pool * s["high_heat"])
    strikes_per_heat = math.ceil(usable / (s["heat_loss"] * b["rapid"]["heat"]))

    # Reheating gets quicker at two levels per metal; a method that keeps the item hot by itself never pauses.
    quicker = metal.get("reheat_level") or [999, 999]
    reheat = s["reheats"][1] if smith >= quicker[1] else s["reheats"][0] if smith >= quicker[0] else s["reheat_base"]
    self_heating = method.get("refill", 1) == 0
    if self_heating:
        pause, overhead = 0, s["tickwaste_afk"]
    else:
        pause = s["tw_turn"] * 2 + s["tw_delay"] + max(1, math.ceil(reheat * s["high_heat"]))
        overhead = s["tickwaste_max"] if smith >= (metal.get("max_heat") or 999) else s["tickwaste"] + reheat
    return _Anvil(per_strike=strike, strike_ticks=s["strike"], per_heat=strikes_per_heat * s["strike"] * strike,
                  reheat_pause=pause, overhead=overhead)


def _steps(item: dict, metal: dict, s: dict) -> list[tuple[float, float, int]]:
    """(progress, xp, bars) for an item and, if it can be upgraded, each "+k" up to the metal's highest: +k is
    worked on the previous step's item with bars x 2^(k-1) more bars, and its progress and xp scale by tables."""
    bars = item["bars"]
    chain = [(bars * metal["progress"], bars * metal["xp"], bars)]
    if item.get("plus"):
        for k in range(1, int(metal.get("max") or 0) + 1):
            chain.append((bars * metal["progress"] * s["progress_mult"][k - 1], bars * metal["xp"] * s["bar_mult"][k - 1],
                          bars * 2 ** (k - 1)))
    return chain


def forging(player: PlayerState, level: int | None = None) -> list[dict]:
    """Per metal, its best base item: {metal, item, bars, items_per_hour, xp_per_hour, bar, level}."""
    s = data().get("smithing") or {}
    level = level or player.level("Smithing")
    b = _boosts(player)
    smith = level + (1 if b["cape"] else 0)
    methods = s.get("method_s") or {}
    method = methods.get(profile.boosts(player).get("smithing_method") or DEFAULT_METHOD) or methods.get(DEFAULT_METHOD)
    xp_mult = 1 + profile.profile(player, "Smithing", "hammer").xp_bonus
    # At the player's own level the per-step ticks are kept for `step_ticks`.
    table = _step_table(level, player.level("Firemaking"), _profile_key(player)) if level == player.level("Smithing") else None
    out = []
    for metal in s.get("metals", []):
        if metal.get("smelting_only") or smith < metal["level"] or not metal.get("items") or not metal.get("progress"):
            continue
        anvil = _anvil(metal, smith, player.level("Firemaking"), method, b, s)
        best = None
        for item in metal["items"]:
            chain = _steps(item, metal, s)
            each = [anvil.ticks(progress) for progress, _, _ in chain]
            if table is not None:
                table.update({(item["name"], k): t for k, t in enumerate(each)})
            ticks = sum(each)
            xp = sum(x for _, x, _ in chain) * xp_mult
            per_hour = TICKS_PER_HOUR / ticks
            if best is None or per_hour * xp > best["xp_per_hour"]:
                best = {"metal": metal["metal"], "item": item["name"] + (f" + {len(chain) - 1}" if len(chain) > 1 else ""),
                        "bars": sum(n for _, _, n in chain), "items_per_hour": per_hour, "xp_per_hour": per_hour * xp,
                        "bar": _bar(metal), "level": metal["level"], "xp": xp, "ticks": ticks}
        if best:
            out.append(best)
    return out


def smelting(player: PlayerState, level: int | None = None) -> list[dict]:
    s = data().get("smithing") or {}
    level = level or player.level("Smithing")
    b = _boosts(player)
    varrock = (((data().get("mining") or {}).get("boosts") or {}).get("varrock")) or {}
    xp_mult = 1 + profile.profile(player, "Smithing", "hammer").xp_bonus
    out = []
    for m in s.get("metals", []):
        ores = _ores(m)
        # Masterwork alloys (concentrated/enriched/immaculate) and glorious bars are made from other bars.
        if level < m["level"] or not m.get("smelt") or not ores or m["metal"] == "Glorious bar" \
                or "alloy" in m["metal"].lower() or any(o.endswith(" bar") for o, _ in ores):
            continue
        srl = m.get("smelt_rate_level") or [999, 999]
        rate = s["smelt_rate"][1] if level >= srl[1] else s["smelt_rate"][0] if level >= srl[0] else s["smelt_rate_base"]
        bars = TICKS_PER_HOUR / rate
        double = s["double_bars"] if level >= (m.get("double_smelt") or 999) else 0
        if b["varrock"] and (m.get("varrock") or [0] * 4)[b["varrock"] - 1] == 1:
            double += varrock.get(m.get("varrock_no") or "", {}).get("effect", 0)
        double += b["fortune"] / 10 * 0.1 if b["fortune"] else 0
        out.append({"metal": m["metal"], "bar": _bar(m), "level": m["level"], "bars_per_hour": bars * (1 + double),
                    "xp_per_hour": bars * m["smelt"] * xp_mult, "ores": ores, "xp": m["smelt"] * xp_mult, "ticks": rate})
    return out


def method_rows(player: PlayerState) -> list[dict]:
    rows = []
    for band in bands(player, "Smithing"):
        for f in forging(player, band):
            rows.append(row("Smithing", f"{f['item']} (forging, simulated)", max(band, f["level"]), band + 4,
                            f["xp_per_hour"], activity=f["item"],
                            recipe={"output": f["item"], "variants": [
                                {"output_qty": 1, "xp": f["xp"], "ticks": f["ticks"], "materials": [[f["bar"], f["bars"]]]}]},
                            assumptions=["bars at hand (an anvil beside a bank)"]))
        for sm in smelting(player, band):
            rows.append(row("Smithing", f"Smelting {sm['bar']} (simulated)", max(band, sm["level"]), band + 4,
                            # No `yields`: bars use up ores (the catalogue's smelting recipes are the source).
                            sm["xp_per_hour"], activity=f"Smelting {sm['bar']}",
                            recipe={"output": sm["bar"], "variants": [
                                {"output_qty": 1, "xp": sm["xp"], "ticks": sm["ticks"], "materials": sm["ores"]}]},
                            assumptions=["ores from a metal bank or ore box, no trips"]))
    return rows
