"""Processing skills (Herblore, Fletching, Crafting, Cooking, Smithing at furnaces, Construction,
Summoning, Runecrafting, Prayer...) from recipe ticks plus inventory trips.

A recipe's time per action is its `ticks` (Bucket:Recipe) plus its share of a trip: one trip per
backpack of materials (stackables take one slot), at a round-trip time set by where the recipe is made
(FACILITY_PLACE; the times are assumptions in trips.BANK_SECONDS). Recipes without ticks fall back to
DEFAULT_TICKS and are still capped at the skill's rate (they're flagged unknown).

`effective(player, recipe)` applies what changes a recipe's xp or yield for this player:
- xp outfits, the Wise perk, clan avatar (profile.py);
- Summoning pouches at the Shrine of Inanna (boosts.shrine_recipe);
- Cooking burns: a share of the food burns, so xp and output per material drop. Foods whose page has a
  success chart (data `burn_charts`, 36) use it; the rest rise linearly from BURN_FLOOR (the charts'
  median at the recipe's level) to 100% at the no-burn level (with Cooking gauntlets if owned); "Never"
  stops at NEVER_CEILING;
- Herblore: Brooch of the Gods and the Botanist's amulet each give a 5% chance of an extra potion
  (Module:Skill calc/Herblore/data `brooch`, `botanist_amulet`);
- Runecrafting: runes per essence from the level (Module:Multiple runes), essence per trip from
  pouches (an altar crafts the whole inventory at once).
"""

from __future__ import annotations

from ..models import PlayerState
from . import data, profile, trips

TICK = 0.6
DEFAULT_TICKS = 2
# A recipe computing faster than this many times the skill's rate is taken as a data error (a batch
# spell recorded as 1 tick per action) and capped at the rate.
MAX_OVER_RATE = 2.0
ALTAR_TICKS = 3  # crafting an inventory at an altar (one action)
EXALTED_TICKS = 3
OBELISK_TICKS = 3  # infusing an inventory of pouches
BURN_FLOOR = 0.76  # median success at the recipe's level over the foods with a success chart (34)
NEVER_CEILING = 0.95
NEVER_SPAN = 30  # levels from the recipe's level to NEVER_CEILING when burning never stops
# Where a facility is relative to a bank (assumptions; see trips.BANK_SECONDS).
FACILITY_PLACE = {
    "Runecrafting altar": "remote", "Ectofuntus": "remote", "Sawmill": "nearby", "Tanner": "remote",
    "Anvil": "nearby", "Burial Anvil": "nearby", "Dragonkin anvil": "nearby", "Obelisk": "nearby",
    "Summoning obelisk": "nearby", "Furniture workbench": "nearby", "Soul forge": "nearby",
    "Creation kiln": "nearby", "Offering cradle": "nearby", "Bone grinder": "remote",
}
# Transcribed from Module:Multiple runes.
POUCHES = {"Small pouch": 2, "Medium pouch": 7, "Large pouch": 15, "Giant pouch": 26, "Massive pouch": 43,
           "Expansive pouch": 69}
RUNE_LEVELS = {
    "Air rune": [1, 11, 22, 33, 44, 55, 66, 77, 88, 99, 110, 121, 132, 143],
    "Spirit rune": [1, 11, 22, 33, 44, 55, 66, 77, 88, 99, 110, 121, 132, 143],
    "Mind rune": [1, 14, 28, 42, 56, 70, 84, 98, 112, 126, 140],
    "Water rune": [5, 19, 38, 57, 76, 95, 114, 133, 152], "Earth rune": [9, 26, 52, 78, 104, 130],
    "Fire rune": [14, 35, 70, 105, 140], "Body rune": [20, 46, 92, 138], "Bone rune": [20, 46, 92, 138],
    "Cosmic rune": [27, 59, 118, 177], "Chaos rune": [35, 74, 148], "Astral rune": [40, 82, 164],
    "Flesh rune": [40, 82, 164], "Nature rune": [44, 91, 182], "Law rune": [54, 110, 220],
    "Miasma rune": [60, 120, 180], "Death rune": [65, 131], "Blood rune": [77, 154], "Time rune": [100, 200],
}
HERBLORE_EXTRA = {"Brooch of the Gods": "brooch", "Botanist's amulet": "botanist_amulet"}


def capped(rate: float, recipe: dict, skill_rate: float) -> float:
    """The recipe's rate, or the skill's rate when its ticks are unknown or implausibly fast."""
    if recipe.get("ticks") is None or rate > MAX_OVER_RATE * skill_rate:
        return min(rate, skill_rate)
    return rate


def place(recipe: dict) -> str:
    facility = (recipe.get("facility") or "").split(",")[0].strip()
    for key, where in FACILITY_PLACE.items():
        if facility.startswith(key):
            return where
    return "bank"


def essence_per_trip(player: PlayerState) -> int:
    pouch = max((n for name, n in POUCHES.items() if name in player.holdings), default=0)
    return trips.INVENTORY + pouch


def runes_per_essence(rune: str, level: int) -> int:
    return max(1, sum(1 for lv in RUNE_LEVELS.get(rune, [1]) if level >= lv))


def action_seconds(recipe: dict, player: PlayerState | None = None) -> float:
    """Seconds per action including its share of the trip."""
    skill = recipe["skill"]
    where = place(recipe)
    if skill == "Runecraft" and "altar" in (recipe.get("facility") or "").lower():
        per_trip = essence_per_trip(player) if player else trips.INVENTORY
        essence = sum(q for name, q in recipe["materials"] if "essence" in name.lower()) or 1
        if any(name == "Exalted essence" for name, _ in recipe["materials"]):
            # Exalted essence is bound one at a time, 3 ticks each (Exalted essence page).
            return EXALTED_TICKS * TICK * essence + trips.BANK_SECONDS[where] * essence / per_trip
        return (ALTAR_TICKS * TICK + trips.BANK_SECONDS[where]) * essence / per_trip
    if skill == "Summoning" and "belisk" in (recipe.get("facility") or "") and (recipe.get("ticks") or 1) <= 1:
        n = trips.actions_per_inventory(recipe["materials"])
        return (OBELISK_TICKS * TICK + trips.BANK_SECONDS[where]) / n
    ticks = recipe.get("ticks")
    if ticks is None and skill == "Smithing" and player is not None and "nvil" in (recipe.get("facility") or ""):
        # Forging is progress-based (no recipe ticks): the forging model's ticks for this item or "+k" step.
        from .smithing import step_ticks

        ticks = step_ticks(player, recipe["output"])
    ticks = max(ticks or DEFAULT_TICKS, 1)
    n = trips.actions_per_inventory(recipe["materials"])
    return ticks * TICK + trips.BANK_SECONDS[where] / n


def cooking_success(player: PlayerState, recipe: dict) -> float:
    level, req = player.level("Cooking"), recipe["level"]
    chart = (data().get("burn_charts") or {}).get(recipe["output"])
    if chart:  # the food page's success chart: (interp(low, high) + 1) / 256
        from . import interpolate_by_level
        return min(1.0, (interpolate_by_level(chart[0], chart[1], min(level, 99)) + 1) / 256)
    stop = recipe.get("noburn")
    if recipe.get("gauntlets") and profile.has(player, "Cooking gauntlets", "*cooking gauntlets*"):
        stop = recipe["gauntlets"]
    try:
        stop = int(stop)
    except (TypeError, ValueError):
        stop = None
    if stop is None:
        if not recipe.get("noburn"):
            return 1.0  # no burn data: the recipe doesn't burn (most non-food steps)
        return min(NEVER_CEILING, BURN_FLOOR + (NEVER_CEILING - BURN_FLOOR) * max(0, level - req) / NEVER_SPAN)
    if level >= stop:
        return 1.0
    return BURN_FLOOR + (1 - BURN_FLOOR) * max(0, level - req) / max(1, stop - req)


def effective(player: PlayerState, recipe: dict, for_xp: bool = True) -> dict:
    """The recipe as this player makes it: xp, output and materials per action after their boosts.
    `for_xp`: the output is made for the xp and can be traded back (Summoning pouches: boosts.pouch_swap);
    False when the output itself is wanted (the gatherer making an item)."""
    from ..boosts import pouch_swap, shrine_recipe

    r = shrine_recipe(player, recipe)
    if for_xp:
        r = pouch_swap(player, recipe, r)
    skill = r["skill"]
    if not skill:
        return r
    notes = [r["boost"]] if r.get("boost") else []
    xp_mult, out_mult = 1.0, 1.0
    p = profile.profile(player, skill)
    if p.xp_bonus:
        xp_mult += p.xp_bonus
        notes += p.notes
    if skill == "Cooking":
        success = cooking_success(player, r)
        if success < 1:
            xp_mult *= success
            out_mult *= success
            notes.append(f"~{1 - success:.0%} burnt (assumed burn curve)")
    if skill == "Herblore" and ("potion" in r["output"].lower() or r["output"].endswith(("(3)", "(4)", "(6)"))):
        table = data().get("herblore") or {}
        for item, key in HERBLORE_EXTRA.items():
            if profile.has(player, item, f"{item}*") and key in table:
                out_mult *= 1 + table[key].get("p", 0)
                notes.append(f"{item} (+{table[key].get('p', 0):.0%} potions)")
    if skill == "Runecraft" and r["output"] in RUNE_LEVELS and r["output_qty"] == 1:
        out_mult *= runes_per_essence(r["output"], player.level("Runecraft"))
    if skill == "Smithing" and r["output"].endswith(" bar") and not any(m.endswith(" bar") for m, _ in r["materials"]):
        from .smithing import double_bar_chance
        if double := double_bar_chance(player, r["output"]):
            out_mult *= 1 + double  # the extra bar gives no xp: xp stays per smelt
            notes.append(f"+{double:.1%} double bars")
    from .. import traits
    mats, ticks = traits.materials(player, r), traits.ticks(player, r)
    if mats is not None or ticks is not None:
        notes.append("Dungeoneering scroll")
    if xp_mult == 1 and out_mult == 1 and not notes:
        return r
    return {**r, "xp": r["xp"] * xp_mult, "output_qty": r["output_qty"] * out_mult,
            **({"materials": mats} if mats is not None else {}), **({"ticks": ticks} if ticks is not None else {}),
            "boost": "; ".join(notes) if notes else r.get("boost")}
