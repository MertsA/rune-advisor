"""Dungeoneering reward scrolls: permanent skilling traits bought with Dungeoneering tokens at the Daemonheim Rewards
shop (the wiki's scroll pages, 2026-11-22). Which ones the player has read comes from the achievement "Scrolling with
Power", whose requirement tests are one per scroll; `scrolls_read = [...]` under [players.X.boosts] in overrides.toml
adds more (an export without the achievement), and the planner's own what-if valuation adds the one it is pricing
(`assume`).

Effects, as recipes are made (`mechanics.processing.effective`, `mechanics.archaeology.restore_loop`, the farm
model):
  * efficiency (Smithing): every bar a smithed item uses has a 2% chance to be refunded.
  * proficiency (Construction): furniture of 3+ planks rolls once per 3 planks of a type to keep one: 50% planks,
    25% oak, 20% teak, 10% mahogany (x1.25 with an Artificer's measure); not refined or eternal planks.
  * dexterity (Crafting): an item needing 3+ of one material has a chance to save one of it (imphide and weaving 50%,
    spider silk 40%, carapace 35%, batwing and snakeskin 30%, green/blue/red/black/royal dragon leather 25/20/15/10/5%).
  * cleansing (Herblore): 10% chance to save the secondaries (one of them at random when there are several; never the
    first ingredient: the vial, flask or base potion), and 1 in 8 potions made a tick faster. Not tars or tea.
  * restoration (Archaeology): 2% chance a restoration uses none of its archaeological materials.
  * life (Farming): a seed back from a patch: 10% (5% tree and fruit tree patches; none for flowers, spirit trees,
    calquats).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import PlayerState

ACHIEVEMENT = "Scrolling with Power"


@dataclass(frozen=True)
class Scroll:
    name: str
    trait: str  # the key effects test for
    skill: str  # the skill whose recipes it changes
    level: int  # Dungeoneering and that skill, both needed to buy and read it
    tokens: int  # Daemonheim Rewards price
    effect: str  # one line, for the guide
    # Skills whose plans it can shorten (the life scroll's seeds feed the crops other skills use).
    affects: tuple[str, ...] = ()


SCROLLS = {s.trait: s for s in (
    Scroll("Scroll of efficiency", "efficiency", "Smithing", 55, 20_000, "2% of the bars a smithed item uses come back"),
    Scroll("Scroll of proficiency", "proficiency", "Construction", 60, 20_000,
           "furniture of 3+ planks keeps a plank per 3: 50% planks, 25% oak, 20% teak, 10% mahogany"),
    Scroll("Scroll of dexterity", "dexterity", "Crafting", 60, 20_000,
           "items of 3+ of one material save one: 5-50% by material (hides, silk, weaving)"),
    Scroll("Scroll of cleansing", "cleansing", "Herblore", 49, 20_000,
           "10% of potions keep their secondaries; 1 in 8 potions a tick faster"),
    Scroll("Scroll of restoration", "restoration", "Archaeology", 44, 20_000,
           "2% of restorations use none of their materials"),
    Scroll("Scroll of life", "life", "Farming", 25, 10_000, "a seed back from 10% of patches (5% trees)",
           affects=("Farming", "Herblore", "Cooking")),
)}
_BY_NAME = {s.name: s for s in SCROLLS.values()}

EFFICIENCY_REFUND = 0.02
RESTORATION_SAVE = 0.02
CLEANSING_SAVE = 0.10
CLEANSING_FASTER = 1 / 8  # a potion made one tick faster
PROFICIENCY = {"Plank": 0.50, "Oak plank": 0.25, "Teak plank": 0.20, "Mahogany plank": 0.10}
ARTIFICERS_FOCUS = 1.25
DEXTERITY = {"Imphide": 0.50, "Spider silk": 0.40, "Carapace": 0.35, "Batwing": 0.30, "Snakeskin": 0.30,
             "Green dragon leather": 0.25, "Blue dragon leather": 0.20, "Red dragon leather": 0.15,
             "Black dragon leather": 0.10, "Royal dragon leather": 0.05}
LIFE_SEED_BACK = 0.10
LIFE_TREE_SEED_BACK = 0.05
LIFE_TREE_PATCHES = {"Tree patch", "Fruit Tree Patch"}
LIFE_NO_SEED = {"Flower Patch", "Spirit Tree Patch", "Calquat patch"}
NOT_POTIONS = re.compile(r"\btar\b|^Cup of tea|\btea\b", re.I)


def read(player: PlayerState) -> set[str]:
    """Traits the player has: the achievement's tests, overrides.toml's `scrolls_read`, and a what-if `assume`.
    Worked out once per player object (recipes ask for every action they cost)."""
    key = frozenset(player.assumed_traits)
    cached = player.__dict__.get("_traits_read")
    if cached is not None and cached[0] == key:
        return cached[1]
    out = _read(player) | set(key)
    player.__dict__["_traits_read"] = (key, out)
    return out


def _read(player: PlayerState) -> set[str]:
    out: set[str] = set()
    a = player.achievements.get(ACHIEVEMENT)
    if a is not None:
        for t in a.requirements:
            s = _BY_NAME.get(str(t.get("description") or ""))
            if s and (t.get("complete") or a.completed):
                out.add(s.trait)
    for name in (player.boosts.get("scrolls_read") or []) if isinstance(player.boosts, dict) else []:
        s = _BY_NAME.get(name) or SCROLLS.get(str(name).lower().removeprefix("scroll of ").strip())
        if s:
            out.add(s.trait)
    return out


def has(player: PlayerState, trait: str) -> bool:
    return trait in read(player)


def unread(player: PlayerState) -> list[Scroll]:
    """Scrolls the player hasn't read, whether or not the levels to buy them are met yet (members only)."""
    if not player.members:
        return []
    got = read(player)
    return [s for s in SCROLLS.values() if s.trait not in got]


def materials(player: PlayerState, recipe: dict) -> list[list] | None:
    """The recipe's materials per action after the player's traits, or None when none changes them."""
    skill, mats = recipe.get("skill"), recipe.get("materials") or []
    got = read(player)
    if not got or not mats:
        return None
    if skill == "Smithing" and "efficiency" in got and _smithed(recipe):
        return [[m, q * (1 - EFFICIENCY_REFUND) if m.endswith(" bar") else q] for m, q in mats]
    if skill == "Construction" and "proficiency" in got:
        focus = ARTIFICERS_FOCUS if any(k.startswith("Artificer's measure") for k in player.holdings) else 1.0
        out, changed = [], False
        for m, q in mats:
            p = PROFICIENCY.get(m)
            if p and q >= 3:
                out.append([m, q - (q // 3) * min(1.0, p * focus)])
                changed = True
            else:
                out.append([m, q])
        return out if changed else None
    if skill == "Crafting" and "dexterity" in got:
        out, changed = [], False
        for m, q in mats:
            p = DEXTERITY.get(m)
            if p and q >= 3:
                out.append([m, q - p])
                changed = True
            else:
                out.append([m, q])
        return out if changed else None
    if skill == "Herblore" and "cleansing" in got and len(mats) > 1 and not NOT_POTIONS.search(recipe["output"]):
        # Each of k secondaries is kept 10%/k of the time; a kept one can be kept again, so 1 - p per potion holds.
        share = CLEANSING_SAVE / (len(mats) - 1)
        return [mats[0]] + [[m, q * (1 - share)] for m, q in mats[1:]]
    if skill == "Archaeology" and "restoration" in got and recipe.get("facility", "").lower().startswith(("archaeolog", "workbench")):
        from .mechanics.archaeology import materials as dug
        return [[m, q * (1 - RESTORATION_SAVE) if m in dug() else q] for m, q in mats]
    return None


def ticks(player: PlayerState, recipe: dict) -> float | None:
    """Ticks per action after traits (cleansing: 1 in 8 potions a tick faster), or None when unchanged."""
    if recipe.get("skill") == "Herblore" and has(player, "cleansing") and recipe.get("ticks") \
            and len(recipe.get("materials") or []) > 1 and not NOT_POTIONS.search(recipe["output"]):
        return max(1.0, recipe["ticks"] - CLEANSING_FASTER)
    return None


def restoration_share(player: PlayerState) -> float:
    """Of a restoration's archaeological materials, the share actually used (restore_loop)."""
    return 1 - RESTORATION_SAVE if has(player, "restoration") else 1.0


def seeds_per_planting(player: PlayerState, patch: str) -> float:
    """Seeds a planting really uses, once the life scroll's seeds back are counted."""
    if not has(player, "life") or patch in LIFE_NO_SEED:
        return 1.0
    return 1 - (LIFE_TREE_SEED_BACK if patch in LIFE_TREE_PATCHES else LIFE_SEED_BACK)


def _smithed(recipe: dict) -> bool:
    """A smithed item (bars at an anvil), not a smelted bar (ores at a furnace)."""
    return any(m.endswith(" bar") for m, _ in recipe["materials"]) and not recipe["output"].endswith(" bar")
