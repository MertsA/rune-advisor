"""Fort Forinthry: the nine buildings' tiers, what building the next tier takes, and the perks the planner models.

Data: data/fort_forinthry.json (scripts/build_fort_forinthry.py, from the wiki's "<Building> tiers" templates): each
tier's Construction level, base experience, materials (frames of one wood and stone wall segments), benefits and the
typical experience an hour of building it (the Construction training guide). A building's tier is the export's
PLAYER_FORT_BUILDING_<NAME> (plugin 0.5.0; 0 = not built; all nine read 3 on a fort built to tier 3, beta 2026-11-26).
An older export says nothing (None): nothing is assumed built or missing then.

Building: the blueprint table starts a tier at its Construction level (boosts allowed: only the start is checked)
once the previous tier stands, and the quests allow it (`quests_needed`). Building it takes the materials and
`xp / xp_per_hour` hours at the hotspots; it pays the tier's experience, plus the Workshop's 5/10/15% for building in
the fort (`build_xp`). The optimal hotspot's extra progress and experience are in the guide's figures as typical play.

Perks the planner models (the rest are listed by the fort track, not valued):
  * Town Hall: the rested xp cap (resources.rested_cap).
  * Chapel: bones and ashes at its altar give 200/250/300% Prayer experience by tier; the guide's rows assume tier 3
    (`adjust_row` scales them to the player's tier, or drops them while the Chapel isn't built).
  * Kitchen: tier 2 burns 3% less (taken as 3% of the burn chance), tier 3 +5% base Cooking experience, cooking on
    its range (it has a bank chest beside it) (`recipe_perks`).
  * Ranger's Workroom: tier 1 saves 2% of the materials of ammunition (arrows, bolts, darts) and fletches 1% more
    arrow shafts; tier 2 makes 1% more ammunition and headless arrows (with their experience); tier 3 +5% base
    Fletching experience, at its fletching workbench (bank chest beside it) (`recipe_perks`).
  * Command Centre: Archaeology research costs 3% fewer chronotes from tier 2 and runs 3% faster at tier 3
    (`research_cost_factor`, `research_speed_factor`).
Source: RuneScape Wiki, Fort Forinthry (CC BY-NC-SA 3.0).
"""

from __future__ import annotations

import re
from functools import cache

from . import datapkg
from .models import PlayerState

SKILL = "Construction"
WORKSHOP = "Workshop"
WORKSHOP_XP_BONUS = 0.05  # a tier: 5/10/15% Construction experience at the fort's hotspots
CHAPEL_XP = {1: 2.0, 2: 2.5, 3: 3.0}  # Prayer experience at the Chapel's altar, by tier
CHAPEL_ROW = re.compile(r"Fort Forinthry chapel \(tier (\d)\)", re.I)
KITCHEN_BURN_CUT = 0.03  # tier 2
KITCHEN_XP = 0.05  # tier 3, added to base experience
RANGERS_SAVE = 0.02  # tier 1, ammunition materials
RANGERS_SHAFTS = 0.01  # tier 1, extra arrow shafts
RANGERS_AMMO = 0.01  # tier 2, extra ammunition and headless arrows (full experience)
RANGERS_XP = 0.05  # tier 3, added to base experience
RESEARCH_COST_CUT = 0.03  # Command Centre tier 2
RESEARCH_SPEED = 0.03  # Command Centre tier 3
AMMO = re.compile(r"(arrows?|bolts?|darts?)( \(unf\))?$", re.I)


@cache
def data() -> dict:
    import json

    return json.loads(datapkg.path("fort_forinthry.json").read_text())


def buildings() -> dict[str, dict]:
    return data()["buildings"]


def tier(player: PlayerState, building: str) -> int | None:
    """The building's tier from the export (0 = not built), None when the export doesn't carry it."""
    value = getattr(player, "vars", {}).get(buildings()[building]["var"])
    return value if isinstance(value, int) and 0 <= value <= 3 else None


def tiers(player: PlayerState) -> dict[str, int | None]:
    return {name: tier(player, name) for name in buildings()}


def known(player: PlayerState) -> bool:
    return any(t is not None for t in tiers(player).values())


def tier_row(building: str, n: int) -> dict:
    return buildings()[building]["tiers"][n - 1]


def quests_needed(building: str, n: int) -> tuple[list[str], list[str]]:
    """(quests to finish, quests only to start) before tier `n` of `building` can be built."""
    b = buildings()[building]
    done, start = list(b.get("done", [])), list(b.get("start", []))
    return (done + start, []) if n >= 2 else (done, start)


def build_xp(player: PlayerState, building: str, n: int) -> float:
    """Construction experience building tier `n` pays: the base, plus the Workshop's bonus at the Workshop's tier
    while building (the Workshop's own previous tier when building the Workshop)."""
    workshop = tier(player, WORKSHOP) or 0
    if building == WORKSHOP:
        workshop = min(workshop, n - 1)
    return tier_row(building, n)["xp"] * (1 + WORKSHOP_XP_BONUS * workshop)


def build_hours(building: str, n: int) -> float:
    """Hours at the hotspots: the base experience at the guide's typical experience an hour for that tier."""
    row = tier_row(building, n)
    rate = row.get("xp_per_hour") or 0
    return row["xp"] / rate if rate else 0.0


def adjust_row(row: dict, player: PlayerState) -> tuple[dict | None, str | None]:
    """A training row as this player can use it: the guide's Chapel rows (tier 3) follow the exported Chapel tier.
    (row, None) unchanged or adjusted; (None, why) when the player can't use it."""
    m = CHAPEL_ROW.search(row.get("method", ""))
    if not m:
        return row, None
    have = tier(player, "Chapel")
    if have is None:
        return row, None
    if have == 0:
        return None, "Fort Forinthry Chapel not built"
    written = int(m.group(1))
    scale = CHAPEL_XP[have] / CHAPEL_XP[written]
    out = {**row, "requirements": None,  # built already: the Construction level was only to build it
           "method": CHAPEL_ROW.sub(f"Fort Forinthry chapel (tier {have})", row["method"])}
    for key in ("xp_per_hour_low", "xp_per_hour_high"):
        if out.get(key):
            out[key] = out[key] * scale
    return out, None


def recipe_perks(player: PlayerState, recipe: dict) -> tuple[float, float, float, float, list[str]]:
    """(xp share added, output factor, material factor, burn factor, notes) the Kitchen and Ranger's Workroom give a
    recipe made there; (0, 1, 1, 1, []) when they don't apply."""
    skill, output = recipe.get("skill"), recipe.get("output") or ""
    facility = (recipe.get("facility") or "").lower()
    if skill == "Cooking" and "range" in facility:
        t = tier(player, "Kitchen") or 0
        xp = KITCHEN_XP if t >= 3 else 0.0
        burn = 1 - KITCHEN_BURN_CUT if t >= 2 else 1.0
        if xp or burn < 1:
            return xp, 1.0, 1.0, burn, [f"Fort Forinthry Kitchen tier {t}"]
    if skill == "Fletching":
        t = tier(player, "Ranger's Workroom") or 0
        if t < 1:
            return 0.0, 1.0, 1.0, 1.0, []
        xp = RANGERS_XP if t >= 3 else 0.0
        out, mats = 1.0, 1.0
        if output.lower().startswith("arrow shaft"):
            out += RANGERS_SHAFTS
        if AMMO.search(output) or output.lower().startswith("headless arrow"):
            mats -= RANGERS_SAVE if AMMO.search(output) else 0.0
            if t >= 2:
                out += RANGERS_AMMO
        if xp or out > 1 or mats < 1:
            return xp, out, mats, 1.0, [f"Ranger's Workroom tier {t}"]
    return 0.0, 1.0, 1.0, 1.0, []


def research_cost_factor(player: PlayerState) -> float:
    return 1 - RESEARCH_COST_CUT if (tier(player, "Command Centre") or 0) >= 2 else 1.0


def research_speed_factor(player: PlayerState) -> float:
    return 1 + RESEARCH_SPEED if (tier(player, "Command Centre") or 0) >= 3 else 1.0


# Which skills' plans a building's next tier can shorten (the tracks re-plan these with the tier built).
# The Grove's tree patch (tier 2) isn't one: the gatherer doesn't farm ordinary trees.
AFFECTS = {"Chapel": ("Prayer",), "Kitchen": ("Cooking",), "Ranger's Workroom": ("Fletching",),
           "Command Centre": ("Archaeology",)}
