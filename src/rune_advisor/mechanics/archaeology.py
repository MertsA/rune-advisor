"""Archaeology excavation, from the mechanics behind the wiki's hotspot calculator (Module:Skill calc/Archaeology/data
for the numbers), the Material cache, Soil and Screening pages.

Excavation only: restoring an artefact is a workbench recipe (the recipe catalogue: the damaged artefact plus
materials), and screening soil a recipe the gatherer adds (`sift_yields`). The user's observation (2026-11-21):
excavation plus screening usually leaves a shortfall of materials for restoring; material caches cover it.

Hotspots: the AFK method's 1,000-1,175 hits an hour (average used), less an eighth for banking without a porter.
Each hit finds a material with 0.21 + 1.11% per point of the mattock's Focus (times the Honed perk), soil with
SOIL_CHANCE independently, and adds the mattock's precision to the artefact progress: an artefact every
hitpoints / precision hits. Precision counts the Archaeology Guild's upgrades (+2 a qualification, assumed bought
unless `mattock_upgrades` is set) and the master outfit. xp/h = misses x fail xp + materials x success xp +
artefacts x find xp, times the xp boosts. Time sprites (a second row): SPRITE_ACTIVITY of 1,500 hits/h, +5%
success, a crit every 20 hits worth 2-5 more hits by level (`sprite_precision`), +20% xp.

Material caches (data/archaeology_caches.json): one material at the same success rate, soil only inside a dig
site, no artefacts; depletion isn't modelled.

Left out: the sashbrush, monocle, tea and pylons, walking between hotspots. Precision and the hit rate are the
places to look if real sessions find artefacts faster than this (the user's experience: they do).
"""

from __future__ import annotations

import json
import math
from functools import cache

from .. import datapkg
from ..models import PlayerState
from . import bands, data, profile, row

AFK_HITS = (1000 + 1175) / 2
SPRITE_ACTIVITY = 0.95  # assumption: share of the 1,500 hits/h maximum while chasing sprites
SPRITE_HITS = 1500 * SPRITE_ACTIVITY
SPRITE_SUCCESS = 0.05
SPRITE_FOCUS_NEEDED, SPRITE_FOCUS_PER_HIT = 40, 2  # sprite focus % for a crit, % gained a hit
SPRITE_XP = 1.2
NO_PORTER_SHARE = 7 / 8
SOIL_CHANCE = 0.14  # soil a hit, independent of the material roll (hotspot calculator data)
QUALIFICATION_LEVELS = (12, 42, 70, 98)  # Assistant, Associate, Professor, Guildmaster: a precision upgrade each
MASTER_OUTFIT = ("Master archaeologist's hat", "Master archaeologist's jacket", "Master archaeologist's trousers",
                 "Master archaeologist's gloves", "Master archaeologist's boots")
SIFT_HIGH = ((37, 75), (70, 88), (92, 105), (110, 140), (999, 194))  # screening's "high" by level (Screening page)
SIFT_XP = ((111, 20.0), (93, 12.0), (71, 5.0), (38, 3.0), (1, 1.0))
SIFT_ARTEFACT = 1 / 1000
SIFT_TICKS = 3  # a heap at the screening station (2 with the master outfit or Sift Soil)
EXCLUDED = {"Dragonkin remains (Desperate Measures)", "Orthen rubble (Desperate Measures)", "Fort debris",
            "Forum entrance", "Barricade (Archaeology)", "Orthen rubble", "Dragonkin remains"}


def mattock(player: PlayerState, level: int) -> tuple[dict | None, str, bool]:
    """(mattock data, name, assumed?): overrides.toml `mattock`, an owned one, else the best for the level."""
    table = (data().get("archaeology") or {}).get("mattocks") or {}
    named = (player.tools.get("mattock") or "").lower().removesuffix(" mattock")
    usable = {k: v for k, v in table.items() if v.get("Level", 999) <= level}
    for k, v in usable.items():
        if k.lower() == named or f"{k} mattock".lower() in {h.lower() for h in player.holdings}:
            return v, k, False
    if not usable:
        return None, "", True
    k = max(usable, key=lambda k: (usable[k]["Precision"], usable[k]["Focus"]))
    return usable[k], k, True


def precision_upgrades(player: PlayerState, level: int) -> tuple[int, bool]:
    """(precision added, assumed?): the Archaeology Guild's mattock precision upgrades, +2 each, sold at the Assistant,
    Associate, Professor and Guildmaster qualifications (Mattocks page). Not exported: `mattock_upgrades = N` under
    [players.X.boosts] sets how many are bought, else one per qualification the level reaches is assumed."""
    set_ = profile.boosts(player).get("mattock_upgrades")
    if set_ is not None:
        return 2 * int(set_), False
    return 2 * sum(1 for lvl in QUALIFICATION_LEVELS if level >= lvl), True


def sprite_precision(level: int) -> float:
    """Precision factor chasing time sprites: a crit every SPRITE_FOCUS_NEEDED / SPRITE_FOCUS_PER_HIT hits counts as
    that many hits plus the crit bonus for the level (2, 3, 4, 5 extra hits from level 1, 43, 82, 101), and each hit
    carries 1/(4 x crits per 200 hits) more for the sprite focus (the hotspot calculator's sprite mechanics)."""
    a = data().get("archaeology") or {}
    bonus = max((b for b, lvl in zip(a.get("crit_bonus") or [2], a.get("crit_bonus_level") or [1]) if level >= lvl),
                default=2)
    per_crit = math.ceil(SPRITE_FOCUS_NEEDED / SPRITE_FOCUS_PER_HIT)
    return (1 + 1 / (4 * math.ceil(200 / per_crit))) * (per_crit + bonus) / per_crit


def _excavation(player: PlayerState, level: int, sprites: bool) -> dict | None:
    """What one hour of hits does at this level: hits, success chance, precision, xp boost, mattock."""
    a = data().get("archaeology") or {}
    m, mname, assumed = mattock(player, level)
    if m is None:
        return None
    honed = (a.get("honed") or {}).get(f"Honed {profile.perks(player, 'mattock').get('Honed', 0)}", 0)
    success = (0.21 + m.get("Focus", 0) * 0.0111 + (SPRITE_SUCCESS if sprites else 0)) * (1 + honed / 100)
    hits = (SPRITE_HITS if sprites else AFK_HITS) * (1 if profile.porter(player) else NO_PORTER_SHARE)
    upgrades, upgrades_assumed = precision_upgrades(player, level)
    master = a.get("masteroutfitbuff", 1.0) if all(profile.has(player, piece) for piece in MASTER_OUTFIT) else 1.0
    precision = (m["Precision"] + upgrades) * master * (sprite_precision(level) if sprites else 1)
    notes = [f"{mname} mattock" + (", assumed" if assumed else "")]
    if upgrades:
        notes.append(f"+{upgrades} precision upgrades" + (" (assumed: one per qualification)" if upgrades_assumed else ""))
    return {"hits": hits, "success": success, "precision": precision, "find_boost": SPRITE_XP if sprites else 1,
            "boost": 1 + profile.profile(player, "Archaeology", "mattock").xp_bonus, "notes": notes}


def _xp(h: dict) -> tuple[float, float]:
    fail = h.get("failxp") or 10 ** (0.0000965 * h["level"] ** 2)
    return fail, h.get("successxp") or fail * 9.3


def hotspot_rates(player: PlayerState, level: int | None = None, sprites: bool = False) -> list[dict]:
    """Excavating each hotspot for an hour: materials (hits x success, by the hotspot's weights), damaged artefacts
    (a find every hitpoints / precision hits, by its artefact weights), soil (SOIL_CHANCE a hit, independent of
    success) and the excavation xp (finding, not restoring: restoration is a workbench recipe)."""
    a = data().get("archaeology") or {}
    level = level or player.level("Archaeology")
    e = _excavation(player, level, sprites)
    if e is None:
        return []
    out = []
    for name, h in (a.get("hotspots") or {}).items():
        if name in EXCLUDED or h.get("level", 999) > level or not h.get("hitpoints"):
            continue
        fail, succ = _xp(h)
        art = h.get("artefactxp") or 100 * fail
        artefacts = e["hits"] / (h["hitpoints"] / e["precision"])
        mats = e["hits"] * e["success"]
        xp = (e["hits"] * (1 - e["success"]) * fail + mats * succ + artefacts * art) * e["find_boost"] * e["boost"]
        weights = sum(w for _, w in h.get("materials") or []) or 1
        art_weights = sum(w for _, w in h.get("artefacts") or []) or 1
        yields = {mat: mats * w / weights for mat, w in h.get("materials") or []}
        yields.update({art_name: artefacts * w / art_weights for art_name, w in h.get("artefacts") or []})
        if h.get("soil"):
            yields[h["soil"]] = yields.get(h["soil"], 0) + e["hits"] * SOIL_CHANCE
        out.append({"hotspot": name, "level": h["level"], "xp_per_hour": xp, "artefacts_per_hour": artefacts,
                    "yields": yields, "mattock": ", ".join(e["notes"])})
    return out


@cache
def caches() -> list[dict]:
    path = datapkg.path("archaeology_caches.json")
    return json.loads(path.read_text())["caches"] if path.is_file() else []


def cache_rates(player: PlayerState, level: int | None = None) -> list[dict]:
    """Excavating a material cache for an hour: its one material at the hotspot success rate (Material cache page:
    caches yield at the same rate whatever their level), soil only for caches inside a dig site (Soil page), and the
    level's fail/success xp. Depletion and respawn (a minute or more) aren't modelled: a dig site has two or more
    caches of each of its materials."""
    level = level or player.level("Archaeology")
    e = _excavation(player, level, sprites=False)
    if e is None:
        return []
    out = []
    for c in caches():
        if c["level"] > level:
            continue
        fail, succ = _xp({"level": c["level"]})
        mats = e["hits"] * e["success"]
        yields = {c["material"]: mats}
        if c.get("soil"):
            yields[c["soil"]] = e["hits"] * SOIL_CHANCE
        xp = (e["hits"] * (1 - e["success"]) * fail + mats * succ) * e["boost"]
        out.append({"cache": c["cache"], "level": c["level"], "xp_per_hour": xp, "yields": yields,
                    "mattock": ", ".join(e["notes"])})
    return out


def sift_chance(player: PlayerState, level: int | None = None) -> float:
    """Chance a heap of soil gives anything when screened (Screening page): m x floor(31 + (level - 1) x (high - 31)
    / 98) out of 2,550,000, high by level band, m 10,700 in the master archaeologist's outfit, else 10,000."""
    level = level or player.level("Archaeology")
    high = next(h for lvl, h in SIFT_HIGH if level <= lvl)
    m = 10_700 if all(profile.has(player, piece) for piece in MASTER_OUTFIT) else 10_000
    return min(1.0, m * math.floor(31 + (level - 1) * (high - 31) / 98) / 2_550_000)


def sift_yields(player: PlayerState, soil: str, level: int | None = None) -> dict[str, float]:
    """What one heap of `soil` gives when screened: on a success, 1 in 1,000 a damaged artefact, else a material from
    one of the soil's hotspots the player can dig (equally likely), by that hotspot's material weights."""
    a = data().get("archaeology") or {}
    level = level or player.level("Archaeology")
    spots = [h for n, h in (a.get("hotspots") or {}).items()
             if h.get("soil") == soil and h.get("level", 999) <= level and n not in EXCLUDED]
    if not spots:
        return {}
    p = sift_chance(player, level)
    out: dict[str, float] = {}
    for h in spots:
        weights = sum(w for _, w in h.get("materials") or []) or 1
        for mat, w in h.get("materials") or []:
            out[mat] = out.get(mat, 0) + p * (1 - SIFT_ARTEFACT) / len(spots) * w / weights
        for art, _ in h.get("artefacts") or []:
            out[art] = out.get(art, 0) + p * SIFT_ARTEFACT / len(spots) / len(h["artefacts"])
    return out


def sift_xp(level: int) -> float:
    """Screening xp per heap (Screening page): 1, 3 from 38, 5 from 71, 12 from 93, 20 from 111."""
    return next(xp for lvl, xp in SIFT_XP if level >= lvl)


def restoration(damaged: str) -> dict | None:
    """The workbench recipe restoring a damaged artefact (recipe catalogue), or None."""
    from ..recommend.catalogue import by_output

    restored = damaged.removesuffix(" (damaged)").replace("(damaged)", "").strip()
    return next((r for r in by_output().get(restored, []) if r["skill"] == "Archaeology"
                 and any(m == damaged for m, _ in r["materials"])), None)


def restoration_mixes(level: int) -> list[dict]:
    """Restoring whatever a hotspot digs up, as one workbench recipe per hotspot: its artefacts' restoration recipes
    weighted as the hotspot finds them (a player restores every artefact found, not one kind). Materials are per
    artefact restored, damaged artefacts included at their shares, so one visit to the hotspot feeds the whole mix."""
    out = []
    for name, h in ((data().get("archaeology") or {}).get("hotspots") or {}).items():
        if name in EXCLUDED or h.get("level", 999) > level:
            continue
        found = [(r, w) for d, w in h.get("artefacts") or [] if (r := restoration(d)) is not None and r["level"] <= level]
        total = sum(w for _, w in found)
        if not found or not total:
            continue
        materials: dict[str, float] = {}
        for r, w in found:
            for m, q in r["materials"]:
                materials[m] = materials.get(m, 0) + q * w / total
        out.append({"skill": "Archaeology", "output": f"artefacts restored from {name}", "output_qty": 1.0,
                    "level": max(h["level"], max(r["level"] for r, _ in found)),
                    "xp": sum(r["xp"] * w / total for r, w in found), "ticks": 10.0,
                    "materials": [[m, q] for m, q in materials.items()], "members": True, "quests": [],
                    "other_skills": [], "facility": "Archaeologist's workbench", "mix": name})
    return out


def soils() -> set[str]:
    return {h["soil"] for h in ((data().get("archaeology") or {}).get("hotspots") or {}).values() if h.get("soil")}


def screened(player: PlayerState, level: int, yields: dict[str, float], xp: float) -> tuple[dict[str, float], float] | None:
    """An hour's excavation with its soil screened afterwards (SIFT_TICKS a heap), per hour of both: the soil's
    materials and artefacts join the excavated ones, and the screening xp the excavation xp. None without soil."""
    soil = next((s for s in yields if s in soils()), None)
    if soil is None:
        return None
    heaps = yields[soil]
    total = 1 + heaps * SIFT_TICKS * 0.6 / 3600
    out = {k: v for k, v in yields.items() if k != soil}
    for k, v in sift_yields(player, soil, level).items():
        out[k] = out.get(k, 0) + heaps * v
    return {k: v / total for k, v in out.items()}, (xp + heaps * sift_xp(level)) / total


def method_rows(player: PlayerState) -> list[dict]:
    """Excavation only: a row per hotspot (AFK and time sprites) and per material cache, yielding materials, damaged
    artefacts and soil, and a twin of each soil-giving row that screens the soil afterwards (`screened`). Restoring
    is a workbench recipe (the recipe catalogue); screening soil on its own a recipe the gatherer adds."""
    rows = []

    def add(name: str, level: int, band: int, xp: float, yields: dict, assumptions: list[str], activity: str, afk: bool):
        rows.append(row("Archaeology", f"{name} (simulated, {assumptions[-1]})", level, band + 4, xp, yields=yields,
                        assumptions=assumptions[:-1], activity=activity, afk=afk))
        if (s := screened(player, band, yields, xp)) is not None:
            rows.append(row("Archaeology", f"{name}, soil screened (simulated, {assumptions[-1]})", level, band + 4,
                            s[1], yields=s[0], assumptions=assumptions[:-1] + ["soil screened at the station"],
                            activity=activity, afk=afk))

    for band in bands(player, "Archaeology"):
        for sprites in (False, True):
            for h in hotspot_rates(player, band, sprites):
                how = "time sprites, " if sprites else ""
                add(h["hotspot"], max(band, h["level"]), band, h["xp_per_hour"], h["yields"],
                    [f"time sprites at {SPRITE_ACTIVITY:.0%} activity" if sprites else "AFK hit rate", how + h["mattock"]],
                    h["hotspot"], not sprites)
        for c in cache_rates(player, band):
            add(c["cache"], max(band, c["level"]), band, c["xp_per_hour"], c["yields"],
                ["AFK hit rate", "depletion not modelled", c["mattock"]], c["cache"].split(" at ")[0], True)
    return rows
