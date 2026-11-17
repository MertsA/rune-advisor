"""Necromancy rituals, from the mechanics the RuneScape Wiki documents (its ritual calculator and data tables;
the numbers are in mechanics.json).

A ritual takes `ticks` ritual ticks (two game ticks each) and gives its xp. Its glyphs wear out after
`glyphduration` rituals (by type and tier, at Um), so per ritual it uses amount / durability of each
glyph's ink recipe, plus the focus item its recipe consumes; the outputs come from the same recipe.

Alteration glyphs fill the glyph slots the ritual's own (foundational) glyphs leave free (5 slots at 1, 7 at 60,
11 at 90; a 12th from the Necromancy cape's perk at 99, which costs no ink). Each
Speed glyph cuts the ritual's ticks by its percentage (capped at -50%), each Attraction glyph adds soul attraction
(base 100), each Multiply glyph adds to the outputs; every alteration glyph also adds its own soul attraction. The
Alteration necklace makes each glyph's effect x1.2. Protection glyphs only widen the window to catch a
disturbance (no xp) and are left out. `ritual_rates` tries every mix of the highest Speed/Attraction/Multiply
glyph the level allows in the free slots (and on the cape) and keeps the best xp per hour.

Disturbances, as the calculator at its default "Average" handling: every 12 ritual ticks (after the first 2)
a disturbance may spawn (the chance by soul attraction band); which one by attraction and ritual tier
(`disturbance_probability`); each gives its xp multiplier x the level's small-lamp xp (data/lamp_xp.json,
as Module:Lamp/data), +3% per Underworld Grimoire tier held, Defile a 5% tome (x1.2 with the Ring of Kayazu,
which also adds 80 attraction); handling one costs its restart and effort ticks. The grimoire also adds 5% x
(tier - 1) to necroplasm outputs. Um assumed (Ungael's 0.8 site multiplier isn't applied); no xp boosts here
(outfits and the like are the profile's business).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import cache
from .. import datapkg
from itertools import product

from ..models import PlayerState
from . import bands, data, row

RITUAL_TICKS_PER_HOUR = 3600 / 1.2  # one ritual tick = two game ticks
BASE_ATTRACTION = 100
EFFORT = "Average"  # the calculator's default disturbance speed
ALTERATIONS = ("Speed", "Attraction", "Multiply")  # Protection: no xp effect
TIERS = ("I", "II", "III")
NECKLACE = "Alteration necklace"
NECKLACE_MULT = 1.2
KAYAZU = "Ring of Kayazu"
KAYAZU_ATTRACTION = 80
GRIMOIRE_XP = 0.03  # disturbance xp per grimoire tier
_GRIMOIRE = re.compile(r"^Underworld Grimoire (\d)$")
_CAPE = re.compile(r"necromancy (master )?cape", re.I)
CAPE_LEVEL = 99
MAX_SPEED = -50


@dataclass(frozen=True)
class Gear:
    necklace: bool = False
    kayazu: bool = False
    grimoire: int = 0
    cape: bool = False  # the skillcape perk: one alteration glyph free of ink

    def describe(self) -> str:
        parts = []
        if self.necklace:
            parts.append(NECKLACE)
        if self.kayazu:
            parts.append(KAYAZU)
        if self.grimoire:
            parts.append(f"Underworld Grimoire {self.grimoire}")
        if self.cape:
            parts.append("Necromancy cape perk")
        return ", ".join(parts) or "no ritual gear"


def gear(player: PlayerState, level: int) -> Gear:
    held = player.holdings
    grimoire = max((int(m[1]) for name in held if (m := _GRIMOIRE.match(name))), default=0)
    return Gear(necklace=NECKLACE in held, kayazu=KAYAZU in held, grimoire=grimoire,
                cape=level >= CAPE_LEVEL and any(_CAPE.search(name) for name in held))


@cache
def _lamp_xp() -> list[float]:
    path = datapkg.path("lamp_xp.json")
    return json.loads(path.read_text())["base_xp"] if path.is_file() else []


def _spawn_chance(attraction: float) -> float:
    """Chance that a disturbance appears at a check: set by the highest soul-attraction band reached that
    states one (1 when none does)."""
    reached = [band["disturbance_chance"] for band in data()["rituals"]["attractionnames"]
               if attraction >= band["2"] and band.get("disturbance_chance") is not None]
    return reached[-1] if reached else 1.0


def disturbance_probability(d: dict, attraction: float, tier: int) -> float:
    """Chance that a disturbance which appears is `d`, for a ritual of this tier at this soul attraction.

    The game rolls a number from 1 up to the soul attraction and the disturbance is the one whose range holds
    it: each starts at its own attraction (`d["3"]`) and ends just below the next higher start. A ritual
    can't roll into the range of a disturbance of the tier above its own (tiers 1 and 2), and the
    disturbances that start at 1 share the first range equally."""
    table = data()["rituals"]["disturbances"]
    start = d["3"]
    # The top of the roll: the attraction, held below where the next tier's disturbances begin.
    top = attraction
    if tier < 3:
        above = [other["3"] for other in table if other["2"] == tier + 1]
        if above:
            top = min(top, above[0] - 1)
    if top <= 0 or d["2"] > tier or start > top:
        return 0.0
    following = next((other["3"] for other in table if other["3"] > start), None)
    end = top if following is None else min(top, following - 1)
    if end < start:
        return 0.0
    share = (end - start + 1) / top
    if start == 1:
        sharing = next((n for n, other in enumerate(table) if other["3"] != 1), len(table))
        if sharing:
            share /= sharing
    return share


def disturbances(ritual: dict, level: int, ticks: int | None = None, attraction: float = BASE_ATTRACTION,
                 g: Gear = Gear()) -> tuple[float, float]:
    """(extra xp, extra game ticks) per ritual of `ticks` ritual ticks from disturbances handled at EFFORT."""
    d = data()["rituals"]
    lamps = _lamp_xp()
    if not lamps:
        return 0.0, 0.0
    ticks = ritual["ticks"] if ticks is None else ticks
    lamp = lamps[min(max(level, 1), len(lamps)) - 1] * (1 + GRIMOIRE_XP * g.grimoire)
    tome = d["tome_xp"][min(max(level, 1), len(d["tome_xp"])) - 1] * (1.2 if g.kayazu else 1.0)
    chances = (ticks - 2) // 12
    spawn = _spawn_chance(attraction)
    interaction = d.get("disturbance_interaction_time") or {}
    effort = (interaction.get("effort") or {}).get(EFFORT, {})
    restart = interaction.get("restart_ticks", 0)
    xp = extra = 0.0
    for dist in d["disturbances"]:
        p = disturbance_probability(dist, attraction, ritual.get("tier", 1))
        xp += p * (dist["xp_multiplier"] * lamp + dist.get("xp_tome_chance", 0) * tome)
        extra += p * (restart + effort.get(dist["1"], 0))
    return xp * spawn * chances, extra * spawn * chances


def best_glyph(kind: str, level: int) -> str | None:
    """The highest tier of an alteration glyph kind the level allows ("Speed II"), or None."""
    glyphs = data()["rituals"]["glyphs"]
    found = None
    for tier in TIERS:
        name = f"{kind} {tier}"
        if name in glyphs and glyphs[name]["lvl"] <= level:
            found = name
    return found


def _add_effect(effects: dict[str, float], glyph: str, mult: float) -> None:
    for k, v in (data()["rituals"]["glyphs"][glyph].get("alteration") or {}).items():
        effects[k] = math.floor(effects.get(k, 0) + v * mult + 1e-9)  # the calculator's add_counts


def evaluate(name: str, ritual: dict, level: int, loadout: dict[str, int], cape_glyph: str | None = None,
             g: Gear = Gear()) -> dict:
    """One ritual with `loadout` alteration glyphs ({"Speed II": 3}) in its free slots and `cape_glyph` on the
    cape perk: xp, rituals per hour, materials per ritual (inks by durability, the focus) and outputs."""
    d = data()["rituals"]
    mult = NECKLACE_MULT if g.necklace else 1.0
    effects: dict[str, float] = {"attraction": BASE_ATTRACTION}
    for glyph, n in loadout.items():
        for _ in range(n):
            _add_effect(effects, glyph, mult)
    if cape_glyph:
        _add_effect(effects, cape_glyph, mult)
    speed = max(effects.get("speed", 0), MAX_SPEED)
    ticks = math.ceil(ritual["ticks"] * (100 + speed) / 100)
    attraction = effects["attraction"] + (KAYAZU_ATTRACTION if g.kayazu else 0)
    extra_xp, extra_ticks = disturbances(ritual, level, ticks, attraction, g)
    materials: dict[str, float] = {}
    for glyph, n in list(ritual.get("glyphs", {}).items()) + list(loadout.items()):
        info = d["glyphs"].get(glyph)
        if not info:
            continue
        durability = d["glyphduration"][info["type"]][info["tier"] - 1]
        for ink, q in (info.get("recipe") or {}).items():
            materials[ink] = materials.get(ink, 0) + n * q / durability
    focus, output = (ritual.get("recipes") or [[{}, {}]])[0]
    for item, q in focus.items():
        materials[item] = materials.get(item, 0) + q
    pct = effects.get("output", 0)
    necro = 5 * (g.grimoire - 1) if ritual.get("type") == "necroplasm" and g.grimoire else 0
    outputs = {item: math.floor(q * (100 + pct + (0 if item == "Ectoplasm" else necro)) / 100 + 1e-9)
               for item, q in output.items()}
    per_hour = 3600 / ((ticks * 2 + extra_ticks) * 0.6)
    xp = ritual["xp"] + extra_xp
    glyph_text = ", ".join(f"{glyph} x{n}" for glyph, n in sorted(loadout.items()) if n)
    if cape_glyph:
        glyph_text += f"{'; ' if glyph_text else ''}cape: {cape_glyph}"
    return {"ritual": name, "per_hour": per_hour, "xp": xp, "xp_per_hour": per_hour * xp, "materials": materials,
            "outputs": outputs, "ticks": ticks, "attraction": attraction, "glyphs": glyph_text or "no alteration glyphs",
            "loadout": dict(loadout), "cape_glyph": cape_glyph, "gear": g.describe()}


def ritual_rates(player: PlayerState, level: int | None = None, alterations: bool = True) -> list[dict]:
    """Every ritual the level allows at its best xp/h alteration loadout (or bare, `alterations=False`)."""
    d = data().get("rituals") or {}
    level = level or player.level("Necromancy")
    g = gear(player, level) if alterations else Gear()
    slots = max((n for lv, n in d.get("glyphspots", []) if level >= lv), default=0)
    options = [glyph for kind in ALTERATIONS if (glyph := best_glyph(kind, level))] if alterations else []
    out = []
    for name, r in (d.get("rituals") or {}).items():
        glyphs = r.get("glyphs") or {}
        need = max([r["lvl"]] + [d["glyphs"][x]["lvl"] for x in glyphs if x in d["glyphs"]])
        used = sum(glyphs.values())
        if level < need or used > slots:
            continue
        free = slots - used
        best = None
        for counts in product(range(free + 1), repeat=len(options)):
            if sum(counts) > free:
                continue
            loadout = {glyph: n for glyph, n in zip(options, counts) if n}
            for cape_glyph in ([None] + options) if g.cape else [None]:
                cand = evaluate(name, r, level, loadout, cape_glyph, g)
                if best is None or cand["xp_per_hour"] > best["xp_per_hour"] + 1e-6:
                    best = cand
        if best is None:
            best = evaluate(name, r, level, {}, None, g)
        best["level"] = need
        out.append(best)
    return out


def method_rows(player: PlayerState) -> list[dict]:
    rows = []
    for band in bands(player, "Necromancy"):
        for r in ritual_rates(player, band):
            rows.append(row("Necromancy", f"{r['ritual']} ritual ({r['glyphs']}) (simulated)", max(band, r["level"]),
                            band + 4, r["xp_per_hour"],  # no `yields`: outputs use up the focus and inks
                            recipe={"output": f"{r['ritual']} ritual", "variants": [
                                {"output_qty": 1, "xp": r["xp"], "ticks": None,
                                 "materials": [[k, v] for k, v in r["materials"].items()]}]},
                            assumptions=[f"alteration glyphs: {r['glyphs']}; {r['gear']}",
                                         "disturbances handled at the calculator's Average speed; Um"],
                            activity=f"{r['ritual']} ritual"))
    return rows
