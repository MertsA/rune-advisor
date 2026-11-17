"""Skilling boosts the planner applies on top of a method's rate.

Urns (data/urns.json, scripts/build_urns.py): held while training, they collect 1 xp per 5 base xp and
pay it out when full, so trained xp is worth 20% more (bonus xp payouts don't fill them). They aren't
free: each urn is 2 soft clay, a trip through the pottery wheel and oven, and a rune (elementals come
from a staff). The wiki's Urns page puts making at ~1,500 urns/h and clay mining at ~3,000 soft clay/h,
so an ironman sourcing everything gets ~750 urns/h; other accounts buy them (gp isn't counted yet).
The best tier is the one the player's Crafting level can make that accepts their resources.
Making them trains Crafting (unfired, fired and rune steps from the recipe catalogue): an ironman's urn
making counts that xp as a by-product. The urn enhancer (Invention, held in the pack) adds 25% to each
payout for 20 charge-pack charges per urn (charges aren't costed).
Exquisite urns (Crafting 102+, porcelain clay and a gem) pay more but cost what gathering their inputs
takes (Context._urn compares them with the best regular urn per trained xp). Prayer urns (impious, accursed,
infernal) fill with the Prayer xp of the ashes the player's kills drop and pay 1.2x that (`prayer_urn`):
Prayer xp on the side of combat training on demons.

Boon of Crondis (after Eclipse of the Heart): 2% chance of an extra gathered material, so gathering
sources yield 2% more items per hour.

Shrine of Inanna (Havenhythe): infusing pouches at its empowered obelisk stacks Spirit Plane Connection
(+2% per pouch, up to 100%): +50% Summoning xp and a 10% chance to save the materials at full stacks.
Spirit Communion (100 spirit shards to the spirit wolf) keeps the stacks for 100 pouches and makes
each pouch 3 ticks instead of 4, so the plan assumes full stacks and one extra shard per pouch. It is
one pouch at a time, slower than batch infusing, but an ironman's Summoning is limited by charms, not
time. The Voice of Seren's +20% only applies while skilling in that clan's district of Prifddinas, so
a pouch infused at the shrine (Havenhythe) never has it; the shrine's +50% wins anyway, and
recommend/catalogue.py leaves the Voice out of shrine recipes (`shrine` flag).
"""

from __future__ import annotations

import math

import json
from dataclasses import dataclass, field
from functools import cache
from . import datapkg

from .models import PlayerState

URN_SKILLS = {"Cooking", "Divination", "Farming", "Fishing", "Hunter", "Mining", "Runecraft", "Smithing", "Woodcutting"}
URN_HOURS_IRONMAN = 1 / 750  # mine and soften the clay, form, fire, add the rune
URN_ENHANCER = "Urn enhancer"
URN_ENHANCER_BONUS = 1.25
STAFF_RUNES = {"Air rune", "Water rune", "Earth rune", "Fire rune"}  # from an elemental staff
CRONDIS_QUEST = "Eclipse of the Heart"
CRONDIS_ITEM_BONUS = 1.02
# Skills whose actions gather a material the boon can double (method yields in other skills are products).
GATHERING_SKILLS = {"Woodcutting", "Mining", "Fishing", "Hunter", "Farming", "Divination", "Archaeology"}


@cache
def urns() -> list[dict]:
    path = datapkg.path("urns.json")
    return json.loads(path.read_text())["urns"] if path.is_file() else []


@dataclass
class Urn:
    name: str
    share: float  # extra xp per trained xp (0.2)
    fill_xp: float  # base xp per urn
    rune: str | None  # consumed per urn (None when a staff covers it)
    hours_each: float
    crafting_xp: float = 0.0  # Crafting xp from making one (only counted when the player makes them)
    enhanced: bool = False
    prereq: str | None = None  # a level to get first (exquisite urns below their Crafting level)
    # Xp per urn from getting its inputs (mining the clay and gem, cutting the gem), by skill; the urn-making
    # steps' own Crafting xp is `crafting_xp`. Set by the planner (Context._urn_material_xp) for ironmen.
    material_xp: dict[str, float] = field(default_factory=dict)

    def count(self, trained_xp: float) -> int:
        return -(-int(trained_xp) // int(self.fill_xp)) if trained_xp > 0 else 0


def best_urn(player: PlayerState, skill: str, level: int, exquisite: bool = False) -> Urn | None:
    """The highest-paying urn the player can make for training `skill` at `level`, if any (`exquisite`: only
    exquisite ones, whose making cost the caller sets)."""
    skill = "Runecraft" if skill == "Runecrafting" else skill
    if skill not in URN_SKILLS:
        return None
    crafting = player.level("Crafting")
    usable = [u for u in urns() if (u["skill"] == "Runecrafting" and skill == "Runecraft" or u["skill"] == skill)
              and u["exquisite"] == exquisite and crafting >= u["crafting"] and (player.members or not u["members"])
              and (u["accepts_level"] is None or u["accepts_level"] >= level)]
    if not usable:
        return None
    u = max(usable, key=lambda u: u["fill_xp"])
    enhanced = URN_ENHANCER in player.holdings
    return Urn(u["name"], u["payout_xp"] / u["fill_xp"] * (URN_ENHANCER_BONUS if enhanced else 1.0), u["fill_xp"],
               None if u["rune"] in STAFF_RUNES else u["rune"],
               URN_HOURS_IRONMAN if player.ironman else 0.0,
               urn_crafting_xp(u["name"]) if player.ironman else 0.0, enhanced)


# Prayer xp per ashes (wiki urn pages: what an ash fills a Prayer urn with).
ASH_XP = {"Impious ashes": 4.0, "Accursed ashes": 12.5, "Infernal ashes": 62.5, "Tortured ashes": 90.0,
          "Searing ashes": 200.0}


@dataclass
class PrayerUrn:
    name: str
    prayer_xp_per_hour: float
    urns_per_hour: float
    hours_each: float
    rune: str | None


def prayer_urn(player: PlayerState, row: dict, skill: str, xp_per_hour: float) -> PrayerUrn | None:
    """Prayer urns carried while a combat method kills ash-dropping monsters: Prayer xp per hour on the side."""
    from .drops import kills_per_hour, tables

    kph = kills_per_hour(row, skill, xp_per_hour)
    if not kph:
        return None
    ash_xp = max((sum(ASH_XP[i] * c * (q or 1) for i, c, q, _ in tables()["sources"].get(src, [])
                      if i in ASH_XP and isinstance(c, (int, float)))
                  for src in row.get("drop_sources") or []), default=0.0)
    usable = [u for u in urns() if u["skill"] == "Prayer" and player.level("Crafting") >= u["crafting"]
              and (player.members or not u["members"])]
    if not ash_xp or not usable:
        return None
    u = max(usable, key=lambda u: u["fill_xp"])
    mult = URN_ENHANCER_BONUS if URN_ENHANCER in player.holdings else 1.0
    fill_per_hour = kph * ash_xp
    return PrayerUrn(u["name"], fill_per_hour * u["payout_xp"] / u["fill_xp"] * mult, fill_per_hour / u["fill_xp"],
                     URN_HOURS_IRONMAN if player.ironman else 0.0, None if u["rune"] in STAFF_RUNES else u["rune"])


@cache
def urn_crafting_xp(name: str) -> float:
    """Crafting xp to make one urn: forming (unfired), firing (no rune) and adding the rune, from the recipe
    catalogue (each step once: the catalogue may list a step twice with different inputs)."""
    from .recommend.catalogue import catalogue

    steps = {f"{name} (unfired)", f"{name} (no rune)", name}
    xp = {}
    for r in catalogue():
        if r["skill"] == "Crafting" and r["output"] in steps:
            xp[r["output"]] = max(xp.get(r["output"], 0.0), r["xp"] / r["output_qty"])
    return sum(xp.values())


SHRINE_XP_MULTIPLIER = 1.5
SHRINE_MATERIAL_SHARE = 0.9  # 10% chance to save the materials
SHRINE_TICKS = 3.0  # with Spirit Communion
SHRINE_NOTE = ("infuse one at a time at the Shrine of Inanna's empowered obelisk, with Spirit Communion (100 spirit "
               "shards per 100 pouches): +50% xp and 10% of materials saved at full Spirit Plane Connection")
# Pouches made for xp are traded back afterwards: Lord Amlodd (Prifddinas, after Plague's End) and Bogrog
# (Gu'Tanoth) give 70% of a pouch's spirit shards for it (the wiki's figure; charms and secondaries are gone),
# so shards are a small net cost of training. The user asked for this on 2026-11-09: 393 Rune minotaur
# pouches read as 35,763 shards to buy, when the swap leaves ~21 net per pouch and the bank covers them.
POUCH_SWAP_REFUND = 0.7
POUCH_SWAP_NOTE = ("swap the pouches with Lord Amlodd (Prifddinas) or Bogrog (Gu'Tanoth) for 70% of their spirit "
                   "shards back: shards here are the net cost, charms and secondaries are spent")


def shrine_recipe(player: PlayerState, recipe: dict) -> dict:
    """A Summoning pouch recipe as made at the Shrine of Inanna (see the module docstring); other
    recipes unchanged."""
    if recipe["skill"] != "Summoning" or not recipe["output"].endswith(" pouch") or not player.members:
        return recipe
    mats = [[n, q * SHRINE_MATERIAL_SHARE] for n, q in recipe["materials"]]
    shards = next((m for m in mats if m[0] == "Spirit shards"), None)
    if shards:
        shards[1] += 1
    else:
        mats.append(["Spirit shards", 1.0])
    return {**recipe, "xp": recipe["xp"] * SHRINE_XP_MULTIPLIER, "materials": mats, "ticks": SHRINE_TICKS,
            "boost": SHRINE_NOTE, "shrine": True}  # `shrine`: made in Havenhythe, so no Voice of Seren


def pouch_swap(player: PlayerState, base: dict, recipe: dict) -> dict:
    """`recipe` (a pouch recipe as made, e.g. after `shrine_recipe`) with the shards Lord Amlodd/Bogrog hand back
    for the finished pouch taken off: 70% of the plain recipe's (`base`) shard count. Only for pouches made for
    xp: a pouch wanted as an item (processing.effective(for_xp=False)) keeps its full cost."""
    if base["skill"] != "Summoning" or not base["output"].endswith(" pouch") or not player.members:
        return recipe
    shards = next((q for n, q in base["materials"] if n == "Spirit shards"), 0)
    if not shards:
        return recipe
    refund = math.floor(shards * POUCH_SWAP_REFUND)
    mats = [[n, max(0.0, q - refund) if n == "Spirit shards" else q] for n, q in recipe["materials"]]
    note = (recipe["boost"] + "; " if recipe.get("boost") else "") + POUCH_SWAP_NOTE
    return {**recipe, "materials": mats, "boost": note, "swap": True}


def crondis_active(player: PlayerState) -> bool:
    q = player.quests.get(CRONDIS_QUEST)
    return bool(q and q.done)


def item_multiplier(player: PlayerState) -> float:
    """Gathered items per hour multiplier from always-on boosts."""
    return CRONDIS_ITEM_BONUS if crondis_active(player) else 1.0
