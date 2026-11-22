"""Fishing, from the mechanics the RuneScape Wiki documents (its Fishing chance calculator and data table; the
numbers are in mechanics.json).

Each fish has a low (level 1) and high (level 99) chance out of 256. Boosts multiply the high value
(floored after each): shark outfits, rod-o-matics, crystal/Tavia's rods, the dragon harpoon,
skillchompas, familiars, juju potions, Menaphos spots; the Honed perk multiplies both. Virtual levels
(the ring, familiars) add to the level the chance is interpolated at. Fish at one spot are rolled in
the data's order and the first success is caught, so P(fish i) = (1 - P(earlier)) x chance_i. A catch
attempt is every 4 ticks (5 for the slow fish). xp/h = attempts/h x sum(P x xp), times the xp outfit.

Bait (BAIT, hand-written from the fish pages: the calculator has none): one a catch, carried as the row's
`consumes`, so a karambwan costs the karambwanji it was caught with (the planner took karambwan as free supply).
The Prawnbroker's perks (`prawn_perks`) drop bait for rod fishing, feathers for fly fishing and both for barbarian
fishing; karambwanji and the Living Rock Caverns' living minerals are always needed.

Not in the wiki data (assumptions, listed on the rows): spots move every ~4 minutes (SPOT_MOVE_SECONDS
walking each time) and bank trips for the fish (trips.py). Fish the module lacks (new Havenhythe spots)
aren't simulated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..models import PlayerState
from . import bands, data, interpolate_by_level, profile, row, trips

ATTEMPTS_PER_HOUR = 3600 / 0.6  # per tick; divided by the fish's delay
# Transcribed from Module:Fishing chance calculator (multipliers_list).
SKILLCHOMPA = {"Cobalt": 1.01, "Viridian": 1.02, "Azure": 1.03, "Crimson": 1.04, "Crystal": 1.05}
OUTFIT = {"Fury shark": 1.07, "Shark gloves": 1.05}  # item that marks the outfit -> multiplier
ROD = {"Fishing rod-o-matic": 1.03, "Enhanced fishing rod-o-matic": 1.04, "Crystal fishing rod": 1.08,
       "Tavia's fishing rod": 1.14}
HARPOON = {"Fishing rod-o-matic": 1.03, "Enhanced fishing rod-o-matic": 1.04, "Dragon harpoon": 1.09}
CAGE_NET = {"Fishing rod-o-matic": 1.03, "Enhanced fishing rod-o-matic": 1.04}
JUJU = 1.10
MENAPHOS = 1.1
RING_LEVELS = 3  # the calculator's ring option: +3 invisible levels (Ring of Whispers assumed)
FAMILIAR = {"Granite crab": (1.01, 1), "Ibis": (1.02, 3), "Granite lobster": (1.03, 4)}
# Bait used up a catch, by fish (the fish's wiki page; one each). Fish not listed need none.
BAIT = {"Raw karambwan": "Raw karambwanji",
        **dict.fromkeys(("Raw sardine", "Raw herring", "Raw pike", "Raw cave eel", "Slimy eel", "Raw cavefish",
                         "Raw lava eel"), "Fishing bait"),
        **dict.fromkeys(("Raw trout", "Raw salmon", "Leaping trout", "Leaping salmon", "Leaping sturgeon"), "Feather"),
        "Raw rainbow fish": "Stripy feather", "Raw rocktail": "Living minerals"}
# Which Prawnbroker perk makes a bait unnecessary, by (bait, fish): the Prawnbroker page.
BARBARIAN = {"Leaping trout", "Leaping salmon", "Leaping sturgeon"}
PRAWN_PERKS = ("bait", "feathers", "barbarian")
DEFAULT_DELAY = 4


def prawn_perks(player: PlayerState) -> set[str]:
    """The bait-saving prawn perks owned: all of them with "Almost as Perky as a Prawn" (every perk unlocked), else
    `prawn_perks = ["bait", "feathers", "barbarian"]` under [players.X.boosts] (the perk vars aren't exported)."""
    a = player.achievements.get("Almost as Perky as a Prawn")
    if a is not None and a.completed:
        return set(PRAWN_PERKS)
    return {str(x).lower() for x in profile.boosts(player).get("prawn_perks") or []}


def bait_for(player: PlayerState, fish: str) -> str | None:
    """The bait a catch of `fish` uses up, or None (no bait, or a prawn perk covers it)."""
    bait = BAIT.get(fish)
    if bait is None or bait in ("Raw karambwanji", "Living minerals"):
        return bait
    perks = prawn_perks(player)
    if fish in BARBARIAN:
        return None if "barbarian" in perks else bait
    return None if ("feathers" if bait.endswith("eather") else "bait") in perks else bait
SPOT_LIFETIME_SECONDS = 252  # ~4m12s average (wiki prose)
SPOT_MOVE_SECONDS = 10  # walking to the spot's new place (assumption)


def _owned_mult(player: PlayerState, table: dict[str, float]) -> tuple[float, str | None]:
    best = (1.0, None)
    for name, mult in table.items():
        if (name in player.holdings or f"Augmented {name[0].lower()}{name[1:]}" in player.holdings) and mult > best[0]:
            best = (mult, name)
    return best


def _tool_table(tool: str) -> dict[str, float]:
    if tool in ("Fishing rod", "Fly fishing rod", "Barbarian rod"):
        return ROD
    if tool == "Harpoon":
        return HARPOON
    return CAGE_NET if tool in ("Crayfish cage", "Lobster pot", "Small fishing net", "Big fishing net") else {}


@dataclass
class _Angler:
    """What a player brings to every fishing spot: multipliers on a fish's level-99 threshold, multipliers on
    both thresholds, invisible levels, the xp bonus, and the names of what gives them."""
    top: list[float] = field(default_factory=list)
    both: list[float] = field(default_factory=list)
    levels: int = 0
    xp_bonus: float = 0.0
    notes: list[str] = field(default_factory=list)


def _angler(player: PlayerState) -> _Angler:
    a = _Angler()
    outfit, outfit_name = _owned_mult(player, OUTFIT)
    if outfit_name:
        a.top.append(outfit)
        a.notes.append(outfit_name)
    if chompa := (profile.boosts(player).get("skillchompa") or {}).get("Fishing"):
        a.top.append(SKILLCHOMPA.get(chompa, 1.0))
        a.notes.append(f"{chompa} skillchompas")
    if familiar := profile.familiar(player, "Fishing"):
        multiplier, levels = FAMILIAR.get(familiar, (1.0, 0))
        a.top.append(multiplier)
        a.levels += levels
        a.notes.append(familiar)
    if profile.juju(player, "Fishing"):
        a.top.append(JUJU)
        a.notes.append("juju fishing")
    if honed := profile.perks(player, "fishing").get("Honed"):
        a.top.append(1 + 0.02 * honed)
        a.both.append(1 + 0.02 * honed)
        a.notes.append(f"Honed {honed}")
    if profile.has(player, "Ring of Whispers"):
        a.levels += RING_LEVELS
        a.notes.append("Ring of Whispers (+3 levels)")
    xp = profile.profile(player, "Fishing", "fishing")
    a.xp_bonus = xp.xp_bonus
    a.notes += xp.notes
    return a


def _bite_chance(fish: dict, level: int, angler: _Angler, tool: float | None) -> float:
    """Chance that an attempt at a spot lands this fish, were it the only one there. Every multiplier is
    floored as it is applied (the player's first, then the tool's and a Menaphos spot's on the top value)."""
    bottom, top = fish["low"], fish["high"]
    for multiplier in angler.top:
        top = math.floor(top * multiplier)
    for multiplier in angler.both:
        bottom = math.floor(bottom * multiplier)
    if tool is not None:
        top = math.floor(top * tool)
    if fish.get("is_menaphos"):
        top = math.floor(top * MENAPHOS)
    return max(0.0, min(1.0, interpolate_by_level(bottom, top, level + angler.levels) / 256))


def spot_rates(player: PlayerState, level: int | None = None) -> list[dict]:
    """Per spot: {spot, fish: [(item, P, per hour, xp)], xp_per_hour, notes}."""
    level = level or player.level("Fishing")
    angler = _angler(player)
    # Fish the level can catch, by the spot they share (a fish with no spot of its own name is its own spot).
    at_spot: dict[str, list[dict]] = {}
    for fish in data().get("fishing", []):
        if not fish.get("is_gws_toggle") and level >= fish["level"]:
            at_spot.setdefault(fish.get("spot") or fish["name"], []).append(fish)
    out = []
    for spot, present in at_spot.items():
        tool, tool_name = _owned_mult(player, _tool_table(present[0]["tool"]))
        # An attempt tries the spot's fish in order and stops at the first that bites.
        still_trying, catches, xp_per_hour = 1.0, [], 0.0
        for fish in present:
            landed = still_trying * _bite_chance(fish, level, angler, tool if tool_name else None)
            still_trying -= landed
            per_hour = ATTEMPTS_PER_HOUR / (fish.get("delay") or DEFAULT_DELAY) * landed
            xp = fish["xp"] * (1 + angler.xp_bonus)
            catches.append((fish["link"], landed, per_hour, xp))
            xp_per_hour += per_hour * xp
        out.append({"spot": spot, "fish": catches, "xp_per_hour": xp_per_hour, "tool": present[0]["tool"],
                    "notes": angler.notes + ([tool_name] if tool_name else []),
                    "top": max(present, key=lambda fish: fish["level"])})
    return out


def method_rows(player: PlayerState) -> list[dict]:
    from ..recommend.catalogue import members_items

    move = SPOT_LIFETIME_SECONDS / (SPOT_LIFETIME_SECONDS + SPOT_MOVE_SECONDS)
    rows = []
    for band in bands(player, "Fishing"):
        for s in spot_rates(player, band):
            if s["xp_per_hour"] <= 0 or (not player.members and any(i in members_items() for i, *_ in s["fish"])):
                continue
            total = sum(ph for _, _, ph, _ in s["fish"]) * move
            trip = trips.gathering(player, "Fishing", place="nearby")
            banked = trip.per_hour(total) / total if total else 1.0
            top = s["top"]
            fish = {i: ph * move * banked for i, _, ph, _ in s["fish"] if ph > 0}
            bait: dict[str, float] = {}
            for i, n in fish.items():
                if b := bait_for(player, i):
                    bait[b] = bait.get(b, 0) + n
            rows.append(row("Fishing", f"{top['text']} ({s['spot']}, simulated)", max(band, top["level"]), band + 4,
                            s["xp_per_hour"] * move,
                            yields=fish,
                            assumptions=[f"spot moves every ~{SPOT_LIFETIME_SECONDS // 60} min, "
                                         f"{SPOT_MOVE_SECONDS}s walk", trip.how]
                            + ([f"bait: {', '.join(sorted(bait))}, one a catch"] if bait else []),
                            activity=top["text"], boosts=s["notes"], **({"consumes": bait} if bait else {})))
    return rows
