"""What boosts a player trains with: detected from their items, or set in overrides.toml.

Items are detected from the export (bank, backpack, worn): xp outfits (the Experience-boosting set
table: each piece adds its share, the full set the listed %), Varrock armour, skillcapes, the Grace of
the Elves / signs of the porter, pocket-slot items. What the export can't see is set per player:

    [players.YourName.boosts]
    perks = { pickaxe = ["Honed 6", "Fortune 3"], hatchet = ["Honed 4"] }  # wiki calculator names
    familiar = { Mining = "Lava Titan", Fishing = "Granite lobster" }
    potions = { Mining = "Super mining potion" }   # consumables are only assumed when listed
    juju = ["Mining", "Woodcutting"]               # juju potions kept up
    avatar = 0.06                                   # clan avatar xp bonus
    porters = true                                  # signs of the porter while gathering
    stone_spirits = false

Consumables (potions, juju, familiars, porters) are never assumed unless listed: an ironman makes
each one. What's listed here is taken as always kept up, at no cost. Familiars, mining potions and juju not
listed are offered as costed options instead (consumables.py: "with <boost>" twins of the simulated rocks and
trees, taken where the speed is worth more than making what they use up).
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from dataclasses import dataclass, field

from ..models import PlayerState
from . import data


@dataclass
class Profile:
    skill: str
    xp_bonus: float = 0.0  # additive xp share (outfit 0.06, Wise 0.04, avatar 0.06)
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)


def boosts(player: PlayerState) -> dict:
    return getattr(player, "boosts", None) or {}


def perks(player: PlayerState, tool: str) -> dict[str, int]:
    """{"Honed": 6, "Fortune": 3} for a tool, from overrides.toml."""
    out = {}
    for p in (boosts(player).get("perks") or {}).get(tool, []):
        m = re.match(r"(.+?)\s+(\d+)$", str(p).strip())
        name, rank = (m.group(1), int(m.group(2))) if m else (str(p).strip(), 1)
        out[name.title()] = rank
    return out


def familiar(player: PlayerState, skill: str) -> str | None:
    return (boosts(player).get("familiar") or {}).get(skill)


def potion(player: PlayerState, skill: str) -> str | None:
    return (boosts(player).get("potions") or {}).get(skill)


def juju(player: PlayerState, skill: str) -> bool:
    return skill in (boosts(player).get("juju") or [])


def has(player: PlayerState, *names: str) -> str | None:
    """The first of `names` the player owns (exact item names, or globs like "*ore box")."""
    held = player.holdings
    # Glob lookups scan the bank: cache them per player and bank state (plans copy and change holdings).
    cache = player.__dict__.setdefault("_glob_cache", {})
    state = player.holdings_state
    for n in names:
        if "*" in n:
            key = (n, state)
            if key not in cache:
                cache[key] = next((h for h in held if fnmatchcase(h.lower(), n.lower())), None)
            if cache[key]:
                return cache[key]
        elif n in held:
            return n
    return None


def outfit_bonus(player: PlayerState, skill: str) -> tuple[float, str | None]:
    """(xp share, description) from the skill's xp outfit pieces owned (elite outfits inherit it)."""
    best = (0.0, None)
    held = None  # read once: each `player.holdings` is a fresh copy
    for o in data().get("outfits", []):
        if o["skill"] != skill and not (o["skill"] == "Runecrafting" and skill == "Runecraft"):
            continue
        held = player.holdings if held is None else held
        owned = sum(1 for p in o["pieces"] if p in held)
        if not owned:
            continue
        n = len(o["pieces"])
        share = o["boost"] if owned == n else owned * (o["boost"] - 0.01) / n
        if share > best[0]:
            best = (share, f"{o['set']} ({owned}/{n}: +{share:.0%} xp)")
    return best


def porter(player: PlayerState) -> str | None:
    """What sends gathered items to the bank, taken as always kept up at no cost: only with `porters = true`.
    Otherwise signs of the porter are a costed option (consumables.py: a charge per item, made with Divination).
    The Grace of the Elves holds charges but makes none, so owning it isn't a porter by itself."""
    if boosts(player).get("porters"):
        return "Grace of the Elves" if has(player, "Grace of the elves*") else "signs of the porter"
    return None


def profile(player: PlayerState, skill: str, tool: str | None = None) -> Profile:
    """The xp boosts that apply to any method of the skill."""
    p = Profile(skill)
    share, what = outfit_bonus(player, skill)
    if share:
        p.xp_bonus += share
        p.note(what)
    if tool and (wise := perks(player, tool).get("Wise")):
        p.xp_bonus += 0.01 * wise
        p.note(f"Wise {wise} (+{wise}% xp)")
    if avatar := float(boosts(player).get("avatar") or 0):
        p.xp_bonus += avatar
        p.note(f"clan avatar (+{avatar:.0%} xp)")
    return p
