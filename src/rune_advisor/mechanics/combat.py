"""Combat from the wiki's damage formulas: ability damage, hit chance, crits, then kill time.

- Ability damage (Ability damage page): with the level term f(L) = 145 ln(1 + 0.6 L / 145) / ln 1.6
  (Strength for melee, else the style's skill), a main hand gives floor(2.5 f) + floor(9.6 t + b), an
  off-hand half of that, a two-hander floor(2.5 f) + floor(1.25 f) + floor(14.4 t + 1.5 b); t is the
  weapon tier (capped by the spell or ammo tier: assumed to match the weapon), b the style's bonus.
- Hit chance (Hit chance page): affinity% x accuracy / armour rating, at most 100%. Accuracy =
  g(level) + 2.5 g(weapon tier) with g(x) = x^3/1250 + 4x + 40 (Attack for melee); armour rating =
  armour + g(Defence). Affinity: the monster's affinity for the style (its weakness: 90), Necromancy
  the middle of the three (Bucket:Infobox_monster). In PvM hit chance scales damage.
- Crits: 10% chance; bonus damage rising from 10% at level 1 to 50% at 90.
- DPS = AD x AVG_ABILITY_MULT x hit chance x (1 + 0.1 x crit bonus) / 1.8 s (one ability per global
  cooldown). AVG_ABILITY_MULT stands in for rotations, prayers, perks and auras: it's fitted to the money
  making guides' boss kill rates (scripts/calibrate_combat.py). Comparisons between loadouts and monsters,
  and guide-anchored spots (whose overhead absorbs it), don't depend on it.
- Kill time = life points / DPS + an overhead per kill (respawns, finding the next target, looting),
  else DEFAULT_OVERHEAD_SECONDS. For spots a training guide names (method_rows), the guide's kill rate
  anchors it: slower than a typical setup's single-target kill means overhead, faster means several
  targets per kill time (AoE), and the player's DPS scales from there.
"""

from __future__ import annotations

import math
from functools import cache

from ..models import PlayerState
from . import data, row

GCD_SECONDS = 1.8
# Mean ability multiplier per global cooldown, fitted to 34 solo boss guides (scripts/calibrate_combat.py:
# the median model damage time per kill equals the guide's time per kill).
AVG_ABILITY_MULT = 2.05
CRIT_CHANCE = 0.10
DEFAULT_OVERHEAD_SECONDS = 6.0  # assumption: respawn/next target per kill
MAX_KILLS_PER_HOUR = 300  # single-target spots
# The gear a guide's rate is assumed to use when backing out its overhead: weapon tier = the guide's level
# (at most REF_MAX_TIER), REF_BONUS_PER_TIER bonus per tier, at the guide's level.
REF_MAX_TIER = 90
REF_BONUS_PER_TIER = 1.0
# A neutral target for comparing loadouts (gear scores): a tier-90-ish monster at neutral affinity.
REFERENCE = {"armour": 2500, "defence_level": 90, "melee_affinity": 60, "ranged_affinity": 60,
             "magic_affinity": 60, "life_points": 50_000}
DAMAGE_SKILL = {"melee": "Strength", "ranged": "Ranged", "magic": "Magic", "necromancy": "Necromancy"}
ACCURACY_SKILL = {"melee": "Attack", "ranged": "Ranged", "magic": "Magic", "necromancy": "Necromancy"}


def g(x: float) -> float:
    return x ** 3 / 1250 + 4 * x + 40


def level_term(level: float) -> float:
    return 145 * math.log(1 + 0.6 * level / 145) / math.log(1.6)


def ability_damage(level: int, two_h: float | None = None, main: float | None = None, off: float | None = None,
                   bonus: float = 0.0) -> float:
    """Ability damage from the style level, weapon tiers (2h, or main + off-hand) and the style bonus."""
    f = level_term(level)
    if two_h:
        return math.floor(2.5 * f) + math.floor(1.25 * f) + math.floor(14.4 * two_h + 1.5 * bonus)
    # No main hand (unarmed, or only an off-hand): the level term with a tier-0 main hand.
    mh = math.floor(2.5 * f) + math.floor(9.6 * (main or 0) + bonus)
    oh = math.floor(0.5 * (math.floor(2.5 * f) + math.floor(9.6 * off + bonus))) if off else 0
    return mh + oh


def crit_bonus(level: int) -> float:
    return 0.10 + 0.40 * (min(level, 90) - 1) / 89


def affinity(monster: dict, style: str) -> float:
    vals = [monster.get(f"{s}_affinity") or 60 for s in ("melee", "ranged", "magic")]
    if style == "necromancy":
        return sorted(vals)[1]
    if monster.get("weakness_by_class") == style and monster.get("weakness_affinity"):
        return max(monster["weakness_affinity"], monster.get(f"{style}_affinity") or 60)
    return monster.get(f"{style}_affinity") or 60


def hit_chance(accuracy: float, monster: dict, style: str) -> float:
    rating = math.floor((monster.get("armour") or 0) + g(monster.get("defence_level") or 1))
    return min(1.0, affinity(monster, style) / 100 * accuracy / max(1, rating))


def loadout_stats(player: PlayerState, loadout) -> tuple[float, float]:
    """(ability damage, accuracy) of a gear.Loadout for the player."""
    style = loadout.style
    slots = loadout.slots
    tier = lambda s: slots[s].tier if s in slots else None  # noqa: E731
    ad = ability_damage(player.level(DAMAGE_SKILL[style]), tier("2h weapon"), tier("main hand weapon"),
                        tier("off-hand weapon"), loadout.damage_bonus)
    acc = g(player.level(ACCURACY_SKILL[style])) + 2.5 * g(loadout.weapon_tier or 1)
    return ad, acc


def dps(ad: float, acc: float, level: int, monster: dict, style: str) -> float:
    return ad * AVG_ABILITY_MULT * hit_chance(acc, monster, style) * (1 + CRIT_CHANCE * crit_bonus(level)) / GCD_SECONDS


def loadout_dps(player: PlayerState, loadout, monster: dict | None = None) -> float:
    ad, acc = loadout_stats(player, loadout)
    return dps(ad, acc, player.level(DAMAGE_SKILL[loadout.style]), monster or REFERENCE, loadout.style)


def reference_dps(level: int, tier: int, monster: dict, style: str) -> float:
    """DPS of a typical setup at a level and weapon tier (a two-hander, REF_BONUS_PER_TIER bonus)."""
    tier = min(tier, REF_MAX_TIER)
    ad = ability_damage(level, two_h=tier, bonus=REF_BONUS_PER_TIER * tier)
    return dps(ad, g(level) + 2.5 * g(tier), level, monster, style)


@cache
def _monsters() -> dict[str, dict]:
    return data().get("monsters") or {}


def monster(name: str) -> dict | None:
    m = _monsters().get(name)
    if m is None:
        page = name.split(" (")[0]
        m = _monsters().get(page) or next((v for v in _monsters().values() if v.get("page") == page), None)
    return m


def best_style(player: PlayerState, target: dict) -> tuple[str, float]:
    """(style, DPS) of the player's best owned loadout against a target."""
    from ..gear import STYLES, best_loadout

    return max(((s, loadout_dps(player, best_loadout(player, s), target)) for s in STYLES), key=lambda t: t[1])


def kills_per_hour(player: PlayerState, name: str, overhead: float = DEFAULT_OVERHEAD_SECONDS
                   ) -> tuple[float, str, float] | None:
    """(kills/h, style, seconds per kill) on a monster with the player's best style."""
    m = monster(name)
    if not m or not m.get("life_points"):
        return None
    style, d = best_style(player, m)
    if d <= 0:
        return None
    seconds = m["life_points"] / d + overhead
    return min(MAX_KILLS_PER_HOUR, 3600 / seconds), style, seconds


def anchor_from_guide(name: str, guide_kph: float, level: int, style: str) -> tuple[float, float]:
    """(overhead seconds per kill, AoE factor) implied by a guide's kill rate, with a typical setup at
    the guide's level: slower than that setup's single-target kill time means overhead; faster means
    several targets die per kill time (AoE), by the ratio."""
    m = monster(name)
    if not m or not m.get("life_points") or guide_kph <= 0:
        return DEFAULT_OVERHEAD_SECONDS, 1.0
    kill = m["life_points"] / max(1e-9, reference_dps(level, level, m, style))
    per_kill = 3600 / guide_kph
    return (per_kill - kill, 1.0) if per_kill >= kill else (0.0, kill / per_kill)


# A guide's rate already reflects a good setup and the spot's spawns: better gear doesn't beat it by much.
MAX_OVER_GUIDE = 1.1


def anchored_kph(player_dps: float, life_points: float, overhead: float, aoe: float, guide_kph: float) -> float:
    if player_dps <= 0:
        return 0.0
    seconds = (life_points / player_dps) / aoe + overhead
    return min(3600 / seconds, MAX_OVER_GUIDE * guide_kph)


GUIDE_STYLE = {"Attack": "melee", "Strength": "melee", "Defence": "melee", "Melee": "melee", "Ranged": "ranged",
               "Magic": "magic", "Necromancy": "necromancy"}


def method_rows(player: PlayerState) -> list[dict]:
    """Combat and Slayer training spots the guides name, re-rated for the player's gear: the guide sets
    the spot and (through its kill rate) the overhead per kill; the kill time is the player's."""
    from ..cost import _load_wiki_rates, typical_rate, usable_wiki_rows
    from ..drops import tables

    monsters = tables()["monsters"]
    rows = []
    for g_row in usable_wiki_rows(_load_wiki_rates(player.members)):
        skill = g_row["skill"]
        sources = [s for s in g_row.get("drop_sources") or [] if s in monsters]
        if skill not in GUIDE_STYLE and skill != "Slayer" or not sources or g_row.get("actions_per_hour"):
            continue
        try:
            rate = typical_rate(g_row)
        except (TypeError, ValueError):
            continue
        src = sources[0]
        key = "slayer_xp" if skill == "Slayer" else "xp"
        per_kill = monsters[src].get(key)
        if not per_kill or not rate:
            continue
        guide_kph = rate / per_kill
        style = GUIDE_STYLE.get(skill) or "melee"
        m = monster(src)
        if not m or not m.get("life_points"):
            continue
        overhead, aoe = anchor_from_guide(src, guide_kph, g_row["level_min"], style)
        from ..gear import best_loadout
        # The guide's style when it names one (Ranged, Magic...), else the player's best against it.
        d = loadout_dps(player, best_loadout(player, GUIDE_STYLE[skill]), m) if skill in GUIDE_STYLE \
            and skill != "Melee" else best_style(player, m)[1]
        kph = anchored_kph(d, m["life_points"], overhead, aoe, guide_kph)
        if kph <= 0:
            continue
        how = f"~{aoe:.1f} targets per kill time (AoE, from the guide)" if aoe > 1 else \
            f"{overhead:.0f}s overhead per kill (from the guide)"
        rows.append(row(skill, g_row["method"], g_row["level_min"], g_row.get("level_max"), kph * per_kill,
                        requirements=g_row.get("requirements", ""), drop_sources=g_row.get("drop_sources"),
                        assumptions=[how, f"average ability x{AVG_ABILITY_MULT} per {GCD_SECONDS}s, "
                                          f"guide gear = tier {min(g_row['level_min'], REF_MAX_TIER)}"],
                        activity=g_row["method"], kills_per_hour=kph,
                        # A best-case guide figure (Slayer's per-task maxima) stays one when re-rated.
                        figure=g_row.get("figure", "typical")))
    return rows
