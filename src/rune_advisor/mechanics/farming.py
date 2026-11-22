"""Farming yields from the wiki's Farming yield calculator (Module:Farming yield calculator,
Module:Farming harvest chart, Module:SkillUtils/SeedData) and seed data (Bucket:Infobox_seed).

Patches with harvest lives (herbs, allotments, hops...): each harvest keeps a life with chance
(interp(low, high, level) + 1) / 256 from the seed's roll pair, so the expected yield is
lives / (1 - that chance). Lives = the seed's harvest life + compost (1 / 2 / 3 for compost /
supercompost / ultracompost). Magic secateurs raise low and high by 10%, the Ring of Whispers by 3%.
Fixed-harvest patches (trees, bushes, cacti): max harvests x yield per harvest.

Farming xp per hour: patches x (plant + check + harvest xp x yield) per run, one run per growth cycle
(the run's minutes if longer), i.e. farming continuously. Patch counts and run times aren't on the wiki: they come from [farming] in rune_advisor.toml,
else gathering.PATCHES (assumptions). Growth is calendar time, not play time.
"""

from __future__ import annotations

from ..models import PlayerState
from . import data, interpolate_by_level, profile, row

COMPOST_LIVES = {"None": 0, "Compost": 1, "Supercompost": 2, "Ultracompost": 3}
DEFAULT_COMPOST = "Supercompost"  # assumption unless `compost` is set in the boosts table
SECATEURS = 0.10
RING = 0.03
# Crops per harvest where the seed infobox gives "Varies" (yield_per_harvest -1) or a figure the seed page
# contradicts: the page's statement (averaged where it gives a range).
SEED_YIELDS = {"Harmony moss seed": 3.0,  # "1 to 5 harmony moss" (Harmony moss seed page)
               "Starbloom flower seed": 12.0}  # "Harvesting yields 12 starbloom flowers" (infobox: 3)


def _roll_key(seed: str) -> str:
    return seed.lower().replace(" ", "_").replace("'", "")


def expected_yield(player: PlayerState, seed: dict, level: int | None = None) -> float | None:
    """Expected crops per patch for a seed record (recipe catalogue `seeds`), or None if unknown."""
    level = min(99, level or player.level("Farming"))
    if seed.get("harvest_life"):
        roll = (data().get("seed_rolls") or {}).get(_roll_key(seed["seed"]))
        if not roll:
            return None
        mod = (SECATEURS if profile.has(player, "Magic secateurs", "*magic secateurs*") else 0) + \
              (RING if profile.has(player, "Ring of Whispers") else 0)
        low, high = int(roll[0] * (1 + mod)), int(roll[1] * (1 + mod))
        keep = min(0.999, (interpolate_by_level(low, high, level) + 1) / 256)
        compost = profile.boosts(player).get("compost") or DEFAULT_COMPOST
        lives = seed["harvest_life"] + COMPOST_LIVES.get(compost, 0)
        return lives / (1 - keep)
    if seed.get("max_harvests"):
        per = SEED_YIELDS.get(seed["seed"]) or seed.get("yield_per_harvest") or 1
        if per <= 0:
            return None
        return seed["max_harvests"] * per
    return None


def method_rows(player: PlayerState) -> list[dict]:
    """Best seed per patch type as Farming xp per hour of run time (gathering.PATCHES or [farming])."""
    from ..recommend.gathering import PATCHES, seeds

    level = player.level("Farming")
    best: dict[str, tuple[float, dict, float]] = {}
    for seed in seeds():
        if seed["level"] > level:
            continue
        patch = next((p for p in seed["patch"] if p in PATCHES), None)
        crop = expected_yield(player, seed)
        if patch is None or crop is None:
            continue
        patches, _, minutes = PATCHES[patch]
        xp_run = patches * (seed["plant_xp"] + seed.get("check_xp", 0) + seed["harvest_xp"] * crop)
        # One run per growth cycle: xp per hour farming continuously (play time between runs included).
        xp_h = xp_run * 60 / max(minutes, seed["grow_minutes"])
        if patch not in best or xp_h > best[patch][0]:
            best[patch] = (xp_h, seed, crop)
    rows = []
    patch_data = data().get("farming") or {}
    for patch, (xp_h, seed, crop) in best.items():
        patches, _, minutes = PATCHES[patch]
        per_patch = (patch_data.get(seed["seed"]) or {}).get("seed_amount") or 1
        xp_patch = seed["plant_xp"] + seed.get("check_xp", 0) + seed["harvest_xp"] * crop
        rows.append(row("Farming", f"{seed['seed']} runs ({patch}, simulated yield)", seed["level"], None, xp_h,
                        # No `yields`: crops use up seeds (Gatherer._farm_source grows them).
                        # Seeds are the material: ironman plans gather them like any recipe input.
                        recipe={"output": seed["crops"][0], "variants": [
                            {"output_qty": crop, "xp": xp_patch, "ticks": None, "materials": [[seed["seed"], per_patch]]}]},
                        assumptions=[f"{patches} patches, {minutes} min per run (gathering.PATCHES)",
                                     f"{profile.boosts(player).get('compost') or DEFAULT_COMPOST}",
                                     f"grows in {seed['grow_minutes']} min (calendar time)"],
                        activity=f"{seed['seed']} runs"))
    return rows
