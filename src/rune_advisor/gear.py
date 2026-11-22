"""Gear picker: the best combat loadout a player owns, per combat style.

Equipment stats come from the wiki (data/equipment.json, scripts/build_equipment.py). Owned means
in the exported inventories (bank, backpack, worn), matched by item id (the in-game "Death guard" is
the wiki's "Death guard (tier 90)"), else by name with charge/degrade suffixes stripped
("Ferocious ring (5)"); owning one variant of a free style-switch family (the Sunspear)
counts as owning all of them. Items with unmet level requirements are skipped.

Weapons are compared by ability damage: a 2h weapon against the best main hand + off-hand pair
(the wiki's off-hand figures are already the off-hand share). Other slots take the highest damage
bonus for the style, then the most armour.

The loadout feeds kill-rate estimates for monsters the training guides don't cover
(`style_rate`): the hand-written rate for the style's skill, scaled by how the weapon's ability
damage compares with a typical weapon of the player's level. Hit chance isn't modelled.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from . import datapkg

from .models import PlayerState

STYLES = {"melee": "Attack", "ranged": "Ranged", "magic": "Magic", "necromancy": "Necromancy"}
ARMOUR_SLOTS = ("head", "torso", "legs", "hands", "feet", "back", "neck", "ring", "pocket")
# Gear scaling of the hand-written rate is kept within these bounds (it's a rough model).
MIN_GEAR_FACTOR, MAX_GEAR_FACTOR = 0.4, 1.2
_SUFFIX = re.compile(r"\s*\((?:\d+|worn|a|e|t|i|charged|uncharged|damaged)\)$")


@cache
def equipment() -> dict:
    path = datapkg.path("equipment.json")
    return json.loads(path.read_text()) if path.is_file() else {"items": {}, "switchable": []}


@dataclass
class Item:
    name: str
    slot: str
    cls: str
    tier: int
    damage: float
    accuracy: float
    bonus: float
    armour: float
    reqs: dict[str, int]


def _item(name: str) -> Item | None:
    row = equipment()["items"].get(name)
    if row is None:
        return None
    slot, cls, tier, damage, accuracy, bonus, armour, _lp, reqs, _style, _range = row
    return Item(name, slot or "", cls, tier, damage, accuracy, bonus, armour, reqs)


@cache
def _base(name: str) -> str:
    return re.sub(r"\s*\(.*?\)", "", name).strip().lower()


def _same_item(held: str, page: str) -> bool:
    a, b = _base(held), _base(page)
    return a == b or a.startswith(b) or b.startswith(a)


def owned_equipment(player: PlayerState) -> list[Item]:
    """The equipment the player holds and meets the requirements of. Cached on the player until its holdings or
    levels change: kill-rate estimates ask for it thousands of times a plan."""
    key = (player.holdings_state, tuple((s, k.level) for s, k in player.skills.items()), id(player.item_ids))
    cached = player.__dict__.get("_owned_equipment")
    if cached is None or cached[0] != key:
        cached = (key, _owned_equipment(player))
        player.__dict__["_owned_equipment"] = cached
    return list(cached[1])


def _owned_equipment(player: PlayerState) -> list[Item]:
    names = set()
    items, ids = equipment()["items"], equipment().get("ids", {})
    for held in player.holdings:
        # The export's item id names the exact wiki page (tiered items share an in-game name), as
        # long as the page is about the same item (a few wiki id lists are wrong).
        name = ids.get(str(player.item_ids.get(held)))
        if name is not None and not _same_item(held, name):
            name = None
        if name is None:
            name = held if held in items else _SUFFIX.sub("", held)
        if name in items:
            names.add(name)
    for family in equipment()["switchable"]:
        if names & set(family):
            names |= set(family)
    items = [_item(n) for n in sorted(names)]
    return [i for i in items if i and all(player.level(s) >= lvl for s, lvl in i.reqs.items())]


@dataclass
class Loadout:
    style: str
    slots: dict[str, Item] = field(default_factory=dict)

    @property
    def ability_damage(self) -> float:
        return sum(i.damage for s, i in self.slots.items() if "weapon" in s)

    @property
    def weapon_tier(self) -> int:
        return max((i.tier for s, i in self.slots.items() if "weapon" in s), default=0)

    @property
    def damage_bonus(self) -> float:
        return sum(i.bonus for i in self.slots.values())

    @property
    def armour(self) -> float:
        return sum(i.armour for i in self.slots.values())


def best_loadout(player: PlayerState, style: str, extra: list[Item] = ()) -> Loadout:
    """The best owned loadout for a style; `extra` adds hypothetical items (gear goals)."""
    gear = owned_equipment(player) + list(extra)
    fits = [i for i in gear if i.cls in (style, "hybrid", "all", "none")]
    weapons = [i for i in gear if i.cls == style]
    out = Loadout(style)

    def best(slot: str) -> Item | None:
        return max((i for i in weapons if i.slot == slot), key=lambda i: (i.damage, i.accuracy), default=None)

    two_h, main, off = best("2h weapon"), best("main hand weapon"), best("off-hand weapon")
    pair = (main.damage if main else 0) + (off.damage if off else 0)
    if two_h and two_h.damage >= pair:
        out.slots["2h weapon"] = two_h
    else:
        out.slots.update({s: i for s, i in (("main hand weapon", main), ("off-hand weapon", off)) if i})
    for slot in ARMOUR_SLOTS:
        if item := max((i for i in fits if i.slot == slot), key=lambda i: (i.bonus, i.armour), default=None):
            out.slots[slot] = item
    if "off-hand weapon" not in out.slots and "2h weapon" not in out.slots:
        if shield := max((i for i in fits if i.slot == "off-hand"), key=lambda i: i.armour, default=None):
            out.slots["off-hand"] = shield
    return out


def damage_score(player: PlayerState, loadout: Loadout, target: dict | None = None) -> float:
    """Expected damage per second (mechanics/combat.py: the wiki's ability damage, hit chance and crit
    formulas) against `target`, by default a neutral tier-90 reference monster. Ratios compare loadouts."""
    from .mechanics.combat import loadout_dps

    return loadout_dps(player, loadout, target)


@cache
def typical_damage(style: str, tier: int) -> float:
    """Ability damage of a typical 2h weapon of this style at about this tier (median of the wiki's)."""
    rows = [r for r in equipment()["items"].values() if r[0] == "2h weapon" and r[1] == style and r[3]]
    near = sorted(r[3] for r in rows if abs(r[2] - tier) <= 5) or sorted(r[3] for r in rows)
    return near[len(near) // 2] if near else 1.0


def gear_factor(player: PlayerState, style: str) -> float:
    skill = STYLES[style]
    loadout = best_loadout(player, style)
    if not loadout.ability_damage:
        return MIN_GEAR_FACTOR
    ratio = loadout.ability_damage / typical_damage(style, min(player.level(skill), 99))
    return max(MIN_GEAR_FACTOR, min(MAX_GEAR_FACTOR, ratio))


def best_style(player: PlayerState, baseline_rate) -> tuple[str, str, float]:
    """(style, skill, estimated xp/h) of the player's strongest combat style for a generic spot.
    `baseline_rate(skill, level)` is the cost model's hand-written rate."""
    options = []
    for style, skill in STYLES.items():
        options.append((style, skill, baseline_rate(skill, player.level(skill)) * gear_factor(player, style)))
    return max(options, key=lambda o: o[2])
