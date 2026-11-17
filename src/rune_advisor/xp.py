"""XP curves and per-skill level caps."""

from __future__ import annotations

import json
import math
from functools import cache
from . import datapkg

MAX_XP = 200_000_000

ELITE = {"Invention"}  # separate xp table, cap 150

# In-game skill names that the hiscores (and PlayerState) spell differently.
SKILL_ALIASES = {"Constitution": "Hitpoints", "Runecrafting": "Runecraft"}


@cache
def xp_for_level(level: int) -> int:
    """Standard RuneScape curve (valid through 120 virtual)."""
    points = 0
    for lvl in range(1, level):
        points += int(lvl + 300 * 2 ** (lvl / 7))
    return points // 4


@cache
def _elite_table() -> list[int]:
    """Total xp at elite levels 1..150, from the wiki (data/elite_xp.json)."""
    return json.loads(datapkg.path("elite_xp.json").read_text())["xp"]


def xp_at(skill: str, level: int) -> int:
    if skill in ELITE:
        table = _elite_table()
        return table[min(max(level, 1), len(table)) - 1]
    return xp_for_level(level)


@cache
def _lamp_data() -> dict:
    return json.loads(datapkg.path("lamp_xp.json").read_text())


def lamp_xp(level: int, size: str) -> float:
    """XP from one lamp of the given size (small/medium/large/huge/giant) at a skill level."""
    data = _lamp_data()
    base = data["base_xp"][min(max(level, 1), len(data["base_xp"])) - 1]
    return base * data["multipliers"][size.lower()]


def star_xp(level: int, size: str) -> float:
    """Bonus xp from one fallen star (prismatic or skill) of the given size: 1.2x the same-size
    lamp, rounded down to 0.1 (wiki Module:Star)."""
    return math.floor(lamp_xp(level, size) * 1.2 * 10) / 10


def level_at_xp(skill: str, xp: float) -> int:
    level = 1
    while level < 150 and xp_at(skill, level + 1) <= xp:
        level += 1
    return level


@cache
def _skills() -> dict:
    return json.loads(datapkg.path("skills.json").read_text())


def is_members_skill(skill: str) -> bool:
    return _skills()["skills"].get(skill, {}).get("members", True)


def level_cap(skill: str, members: bool = True) -> int:
    """Highest trainable level (data/skills.json, from the wiki). Free players can only train
    members' skills to a low cap (5, or 20 for a few; Invention not at all)."""
    data = _skills()
    cap = data["skills"].get(skill, {}).get("max", 99)
    if not members and is_members_skill(skill):
        cap = min(cap, data["f2p_members_skill_cap_exceptions"].get(skill, data["f2p_members_skill_cap"]))
    return cap
