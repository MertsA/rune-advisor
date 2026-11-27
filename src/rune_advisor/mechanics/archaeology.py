"""Archaeology excavation, from the mechanics behind the wiki's hotspot calculator (Module:Skill calc/Archaeology/data
for the numbers), the Material cache, Soil and Screening pages.

Excavation only: restoring an artefact is a workbench recipe (the recipe catalogue: the damaged artefact plus
materials), and screening soil a recipe the gatherer adds (`sift_yields`). The user's observation (2026-11-21):
excavation plus screening usually leaves a shortfall of materials for restoring; material caches cover it.

Hotspots: the AFK method's 1,000-1,175 hits an hour (average used), less the time spent emptying the backpack
(`_deposit`). The user (2026-11-21): hotspots and caches inside a dig site have material deposit spots all around, so
a full backpack is a few steps away from storage (DEPOSIT_TRIP_SECONDS); only caches out in the world mean a bank trip
(BANK_TRIP_SECONDS), which is where signs of the porter pay. A porter sends materials only: damaged artefacts still
fill the backpack, and soil goes to the soil box, never the porter. Rows say how many materials they excavate an
hour (`items_per_hour`: the porter's charges). Screened materials don't count: the screening station is next to a
deposit spot, so nobody keeps a porter on there (the user).
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
from .. import traits
from ..models import PlayerState
from . import bands, data, profile, row

AFK_HITS = (1000 + 1175) / 2
SPRITE_ACTIVITY = 0.95  # assumption: share of the 1,500 hits/h maximum while chasing sprites
SPRITE_HITS = 1500 * SPRITE_ACTIVITY
SPRITE_SUCCESS = 0.05
SPRITE_FOCUS_NEEDED, SPRITE_FOCUS_PER_HIT = 40, 2  # sprite focus % for a crit, % gained a hit
SPRITE_XP = 1.2
INVENTORY_SLOTS = 27  # the backpack less the soil box
DEPOSIT_TRIP_SECONDS = 6.0  # to a material deposit spot at a dig site and back: an assumption (they're everywhere)
BANK_TRIP_SECONDS = 60.0  # from a cache out in the world to a bank and back: an assumption
# The Material cache page: the Archaeology level, as well as focus, raises the chance of a material, and caches give
# "400+ materials per hour" at 80+ with an imcando mattock. The hotspot calculator has no level term; this one is
# fitted to that figure (0.21 + 6 x 1.11% + 80 x LEVEL_SUCCESS at ~1,090 hits an hour = ~400) until recorded
# sessions can fit it properly.
LEVEL_SUCCESS = 0.0015
SOIL_CHANCE = 0.14  # soil a hit, independent of the material roll (hotspot calculator data)
QUALIFICATION_LEVELS = (12, 42, 70, 98)  # Assistant, Associate, Professor, Guildmaster: a precision upgrade each
MASTER_OUTFIT = ("Master archaeologist's hat", "Master archaeologist's jacket", "Master archaeologist's trousers",
                 "Master archaeologist's gloves", "Master archaeologist's boots")
SIFT_HIGH = ((37, 75), (70, 88), (92, 105), (110, 140), (999, 194))  # screening's "high" by level (Screening page)
SIFT_XP = ((111, 20.0), (93, 12.0), (71, 5.0), (38, 3.0), (1, 1.0))
SIFT_ARTEFACT = 1 / 1000
SIFT_TICKS = 3  # a heap at the screening station (2 with the master outfit or Sift Soil)
SOIL_BOX = (50, 100, 250, 500)  # heaps of each soil, by ARCH_SOIL_BOX_UPGRADE (Archaeological soil box page)
SOIL_BACKPACK = 20  # heaps carried in the backpack beside the box: an assumption
SOIL_TRIP_MINUTES = 2.0  # to the Archaeology Guild's screening station and back: an assumption
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
    Associate, Professor and Guildmaster qualifications (Mattocks page). The export counts them (ARCH_PRECISION_BASE_UPGRADE,
    plugin 0.5.0); `mattock_upgrades = N` under [players.X.boosts] wins, and without either one per qualification
    the level reaches is assumed."""
    set_ = profile.boosts(player).get("mattock_upgrades")
    if set_ is not None:
        return 2 * int(set_), False
    exported = (getattr(player, "vars", None) or {}).get("ARCH_PRECISION_BASE_UPGRADE")
    if isinstance(exported, int) and exported >= 0:
        return 2 * exported, False
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
    success = (0.21 + m.get("Focus", 0) * 0.0111 + LEVEL_SUCCESS * level
               + (SPRITE_SUCCESS if sprites else 0)) * (1 + honed / 100)
    hits = SPRITE_HITS if sprites else AFK_HITS
    upgrades, upgrades_assumed = precision_upgrades(player, level)
    master = a.get("masteroutfitbuff", 1.0) if all(profile.has(player, piece) for piece in MASTER_OUTFIT) else 1.0
    precision = (m["Precision"] + upgrades) * master * (sprite_precision(level) if sprites else 1)
    notes = [f"{mname} mattock" + (", assumed" if assumed else "")]
    if upgrades:
        notes.append(f"+{upgrades} precision upgrades" + (" (assumed: one per qualification)" if upgrades_assumed else ""))
    return {"hits": hits, "success": success, "precision": precision, "find_boost": SPRITE_XP if sprites else 1,
            "boost": 1 + profile.profile(player, "Archaeology", "mattock").xp_bonus, "notes": notes}


def _deposit(player: PlayerState, hits: float, materials_per_hit: float, artefacts_per_hit: float,
             trip_seconds: float) -> float:
    """Hits an hour once trips to empty the backpack are paid for: every INVENTORY_SLOTS items (materials, damaged
    artefacts) cost a trip. With a porter the materials go straight to storage and only artefacts fill the backpack."""
    items = (0 if profile.porter(player) else materials_per_hit) + artefacts_per_hit
    return hits / (1 + hits * items / INVENTORY_SLOTS * trip_seconds / 3600)


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
        # Hotspots are at dig sites: material deposit spots close by (`_deposit`).
        hits = _deposit(player, e["hits"], e["success"], e["precision"] / h["hitpoints"], DEPOSIT_TRIP_SECONDS)
        artefacts = hits / (h["hitpoints"] / e["precision"])
        mats = hits * e["success"]
        xp = (hits * (1 - e["success"]) * fail + mats * succ + artefacts * art) * e["find_boost"] * e["boost"]
        weights = sum(w for _, w in h.get("materials") or []) or 1
        art_weights = sum(w for _, w in h.get("artefacts") or []) or 1
        yields = {mat: mats * w / weights for mat, w in h.get("materials") or []}
        yields.update({art_name: artefacts * w / art_weights for art_name, w in h.get("artefacts") or []})
        if h.get("soil"):
            yields[h["soil"]] = yields.get(h["soil"], 0) + hits * SOIL_CHANCE
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
        # Inside a dig site a deposit spot is close by; out in the world a full backpack is a bank trip.
        hits = _deposit(player, e["hits"], e["success"], 0.0,
                        DEPOSIT_TRIP_SECONDS if c.get("dig_site") else BANK_TRIP_SECONDS)
        mats = hits * e["success"]
        yields = {c["material"]: mats}
        if c.get("soil"):
            yields[c["soil"]] = hits * SOIL_CHANCE
        xp = (hits * (1 - e["success"]) * fail + mats * succ) * e["boost"]
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


def soil_box(player: PlayerState) -> int:
    """Heaps of each soil the archaeological soil box holds: 50, upgraded to 100, 250, 500 (the export's
    ARCH_SOIL_BOX_UPGRADE, 0-3); the backpack adds what's free of it (SOIL_BACKPACK)."""
    level = int((getattr(player, "vars", None) or {}).get("ARCH_SOIL_BOX_UPGRADE") or 0)
    return SOIL_BOX[min(level, len(SOIL_BOX) - 1)] + SOIL_BACKPACK


def screened(player: PlayerState, level: int, yields: dict[str, float], xp: float) -> tuple[dict[str, float], float, float]:
    """Excavating with the soil screened as it piles up (the user: screening can't be put off for long, the soil box
    is emptied only by screening, so it caps the time at a site), per hour of the whole loop: (yields, xp/h, the
    loop's hours per excavation hour). Screening is SIFT_TICKS a heap at the station; each full soil box costs a
    SOIL_TRIP_MINUTES trip to the Archaeology Guild and back. The soil's materials (and rare artefacts) join the
    excavated ones; the soil itself isn't kept."""
    soil = next((s for s in yields if s in soils()), None)
    if soil is None:
        return dict(yields), xp, 1.0
    heaps = yields[soil]
    total = 1 + heaps * SIFT_TICKS * 0.6 / 3600 + heaps / soil_box(player) * SOIL_TRIP_MINUTES / 60
    out = {k: v for k, v in yields.items() if k != soil}
    for k, v in sift_yields(player, soil, level).items():
        out[k] = out.get(k, 0) + heaps * v
    return {k: v / total for k, v in out.items()}, (xp + heaps * sift_xp(level)) / total, total


def restore_loop(player: PlayerState, level: int, hotspot: dict, yields: dict[str, float], xp: float,
                 sources: dict[str, tuple[float, float]]) -> tuple[dict[str, float], float, list[str], dict[str, float]]:
    """Training at a hotspot the way it's done: excavate (soil screened), restore every artefact found with the
    materials dug up, and top up a material the excavation runs short of where it comes fastest (`sources`: a material
    cache, or another hotspot for materials no cache gives). Per hour of
    the loop: (restored artefacts, xp/h, notes). An artefact whose recipe needs a material nothing gives at the
    player's level stays damaged and earns no restoration xp."""
    hours, total_xp = 1.0, xp
    have = dict(yields)
    restored: dict[str, float] = {}
    short: list[str] = []
    consumes: dict[str, float] = {}  # inputs from outside Archaeology (molten glass, diamonds): costed by the planner
    digs = materials()
    own = {a for a, _ in hotspot.get("artefacts") or []}  # screening's rare finds stay damaged
    for damaged in [k for k in yields if k in own]:
        n = yields[damaged]
        r = restoration(damaged)
        if r is None or r["level"] > level:
            continue
        share = traits.restoration_share(player)  # the restoration scroll: 2% use no materials
        need = {m: q * n * (share if m in digs else 1.0) for m, q in r["materials"] if m != damaged}
        extra_hours, extra_xp, ok, used = 0.0, 0.0, True, {}
        for m, q in need.items():
            gap = q - have.get(m, 0)
            if gap <= 0:
                continue
            if m not in digs:
                used[m] = q
                continue
            if m not in sources:
                ok = False
                break
            per_hour, cache_xp = sources[m]
            extra_hours += gap / per_hour
            extra_xp += gap / per_hour * cache_xp
        if not ok:
            short.append(damaged)
            continue
        for m, q in used.items():
            consumes[m] = consumes.get(m, 0) + q
        for m, q in need.items():
            have[m] = max(0.0, have.get(m, 0) - q)
        ticks = 8 if all(profile.has(player, piece) for piece in MASTER_OUTFIT) else 10
        hours += extra_hours + n * ticks * 0.6 / 3600
        total_xp += extra_xp + n * r["xp"] * (1 + profile.profile(player, "Archaeology").xp_bonus)
        restored[r["output"]] = restored.get(r["output"], 0) + n * r["output_qty"]
    notes = [f"kept damaged (a material nothing gives at this level): {', '.join(short)}"] if short else []
    return ({k: v / hours for k, v in restored.items()}, total_xp / hours, notes,
            {k: v / hours for k, v in consumes.items()})


@cache
def materials() -> frozenset[str]:
    """Every Archaeology material a hotspot or cache gives."""
    hs = (data().get("archaeology") or {}).get("hotspots") or {}
    return frozenset({m for h in hs.values() for m, _ in h.get("materials") or []} | {c["material"] for c in caches()})


def _material_count(yields: dict[str, float]) -> float:
    """Materials an hour: what a porter sends (a charge each), never damaged artefacts or soil."""
    return sum(v for k, v in yields.items() if k in materials())


def hand_in(player: PlayerState, level: int, restored: dict[str, float], xp: float) -> tuple[dict[str, float], float, list[str]]:
    """The loop's restored artefacts handed to collectors: chronotes at their collection value (each artefact's
    chronotes, doubled by the set bonus as collections fill: archaeology_guild.COLLECTION_FACTOR). Once every Guild
    shop unlock is bought the chronotes fund the research team (offline, one expedition at a time: at most
    24 / PLAY_HOURS_PER_DAY research hours per hour played), whose xp joins the loop's. Returns (yields:
    the chronotes left, xp/h, notes)."""
    from .. import archaeology_guild as ag
    from ..recommend.catalogue import archaeology_collections
    from ..recommend import gathering

    values = archaeology_collections().get("artefact_chronotes") or {}
    chronotes = sum(n * values.get(a, 0) for a, n in restored.items()) * ag.COLLECTION_FACTOR
    notes = ["restored artefacts handed to collectors (chronotes at collection value)"]
    best = ag.best_research(level)
    if chronotes and best and not ag.unlocks_left(player):
        site, info = best
        cost = ag.research_cost(level, info["factor"], player)
        hours = min(24 / gathering.PLAY_HOURS_PER_DAY, chronotes / cost)
        xp += hours * ag.research_xp(level, info["factor"], player)
        chronotes -= hours * cost
        notes.append(f"chronotes fund {hours:.1f}h of {site} research a played hour (offline)")
    return ({"chronotes": chronotes} if chronotes > 1e-9 else {}), xp, notes


def method_rows(player: PlayerState) -> list[dict]:
    """Excavation: a row per hotspot (AFK and time sprites) and per material cache, the soil screened as it piles up
    (`screened`), yielding materials and damaged artefacts. Restoring is a workbench recipe (the recipe catalogue).
    Training: a row per hotspot for the whole loop (`restore_loop`: excavate, screen, restore everything found, top
    up materials at caches), yielding the restored artefacts, which is what the training guides rate."""
    rows = []
    for band in bands(player, "Archaeology"):
        caches_ = []
        for c in cache_rates(player, band):
            yields, xp, total = screened(player, band, c["yields"], c["xp_per_hour"])
            caches_.append((c, yields, xp))
            rows.append(row("Archaeology", f"{c['cache']} (simulated, {c['mattock']})", max(band, c["level"]), band + 4,
                            xp, yields=yields, items_per_hour=_material_count(c["yields"]) / total,
                            assumptions=["AFK hit rate", "depletion not modelled",
                                         "deposit spot close by" if c.get("dig_site") else "bank trip per backpack"]
                            + (["soil screened as it piles up"] if c.get("soil") or any(s in c["yields"] for s in soils()) else []),
                            activity=c["cache"].split(" at ")[0], afk=True))
        for sprites in (False, True):
            how = "time sprites, " if sprites else ""
            kind = f"time sprites at {SPRITE_ACTIVITY:.0%} activity" if sprites else "AFK hit rate"
            dug = [(h, *screened(player, band, h["yields"], h["xp_per_hour"])) for h in hotspot_rates(player, band, sprites)]
            # Where a material the loop runs short of comes fastest: a cache, else another hotspot (rarer materials).
            best: dict[str, tuple[float, float]] = {}
            for yields, xp in [(y, x) for _, y, x in caches_] + [(y, x) for _, y, x, _ in dug]:
                for m, per_hour in yields.items():
                    if not m.endswith("(damaged)") and per_hour > best.get(m, (0, 0))[0]:
                        best[m] = (per_hour, xp)
            for h, yields, xp, total in dug:
                excavated = _material_count(h["yields"]) / total  # porter charges: not the screened materials
                level = max(band, h["level"])
                rows.append(row("Archaeology", f"{h['hotspot']} (simulated, {how}{h['mattock']})", level, band + 4, xp,
                                yields=yields, items_per_hour=excavated,
                                assumptions=[kind, "soil screened as it piles up"],
                                activity=h["hotspot"], afk=not sprites))
                hotspot = (data().get("archaeology") or {}).get("hotspots", {}).get(h["hotspot"], {})
                restored, loop_xp, notes, consumes = restore_loop(player, band, hotspot, yields, xp, best)
                if restored:
                    restored, loop_xp, guild_notes = hand_in(player, band, restored, loop_xp)
                    notes = notes + guild_notes
                    rows.append(row("Archaeology", f"{h['hotspot']}: excavate, screen and restore (simulated, {how}"
                                    f"{h['mattock']})", level, band + 4, loop_xp, yields=restored,
                                    assumptions=[kind, "soil screened as it piles up",
                                                 "materials short at the hotspot topped up where they come fastest"] + notes,
                                    activity=h["hotspot"], afk=not sprites,
                                    items_per_hour=excavated,  # per hour at the hotspot: a little over the loop's
                                    # Inputs from other skills: the row is costed with them (cost.consuming_row).
                                    **({"consumes": consumes} if consumes else {})))
    return rows
