"""Dungeoneering floors, from the floor experience the wiki documents (the WINterface page; Dungeoneering Remastered,
11 May 2026).

A floor's base xp is floor x 100 plus 60 x e^(0.05 x level). Its modifiers add up: dungeon size (large +500%),
bonus rooms opened (large: up to +360%), solo (large +300%), enemies killed (large: -10% to +30%), the floor-buff
"Large Floor XP Boost" (+25% a buff, up to +300%); complexity and deaths multiply. The total is capped at 1,020%
(the Remastered patch notes). A solo large high-complexity floor left once its boss is down (about half the bonus
rooms opened, the required enemies killed: the page's "rush" averages) is over the cap before any floor buff, so it
pays the cap: base x 10.2. Floor n needs Dungeoneering 2n - 1 (Floors page): the highest floor the level opens is run.

Tokens: 20% of floor xp (currencies.TOKEN_SHARE). Daemonheim Rewards trades them for Dungeoneering xp 1:1 (Pay-to-play
Dungeoneering training), so each band has a twin row with the floors' own tokens traded back (x1.2). Tokens the player
already holds are never traded: they are for the shop (the user's rule, 2026-11-22). Large Dungeoneering token boxes
(two a Dragonkin V or VI completion, 1.25 x level^2 + 50 tokens each) are a recipe (`box_recipes`): a box opened and
traded is that much xp, and the gatherer costs the box (the collection, with its Archaeology xp). Not modelled: floor time (an
assumption: SOLO_LARGE_FLOOR_MINUTES, `dg_floor_minutes` under [players.X.boosts] replaces it), floor buffs other than
being past the cap, team floors (the guides' team rates), deaths.
"""

from __future__ import annotations

import math

from ..models import PlayerState
from . import bands, profile, row

MAX_FLOOR = 60
SIZE_LARGE = 5.00
SOLO_LARGE = 3.00
RUSH_BONUS_ROOMS = 1.80  # the page: rushing leaves ~180% of the large floor's 360% unopened
RUSH_MOBS = 0.10  # -20% of the large floor's +30%
MODIFIER_CAP = 10.20  # "to a maximum of 1020%" (patch notes)
SOLO_LARGE_FLOOR_MINUTES = 25.0  # an assumption until measured (the calculator's default is 10 for a team of five)


def highest_floor(level: int) -> int:
    return max(1, min(MAX_FLOOR, (level + 1) // 2))


def floor_xp(level: int, floor: int | None = None) -> float:
    """Xp for a solo large high-complexity floor, rushed to its boss, no deaths."""
    floor = floor or highest_floor(level)
    base = floor * 100 + 60 * math.exp(0.05 * level)
    total = 1.0 + SIZE_LARGE + RUSH_BONUS_ROOMS + RUSH_MOBS + SOLO_LARGE
    return base * min(MODIFIER_CAP, total)


def floor_minutes(player: PlayerState) -> tuple[float, bool]:
    """Minutes a solo large floor takes, and whether that's the player's own figure."""
    mine = profile.boosts(player).get("dg_floor_minutes")
    return (float(mine), True) if mine else (SOLO_LARGE_FLOOR_MINUTES, False)


def xp_per_hour(player: PlayerState, level: int | None = None) -> float:
    level = level or player.level("Dungeoneering")
    minutes, _ = floor_minutes(player)
    xp = floor_xp(level) * (1 + profile.profile(player, "Dungeoneering").xp_bonus)
    return xp * 60 / minutes


TOKEN_SHARE = 0.2  # tokens per floor xp (Dungeoneering tokens page)
TOKEN_BOX = "Large dungeoneering token box"
BOX_SECONDS = 3.6  # open a box and trade its tokens: an assumption, a few clicks


def box_tokens(level: int) -> float:
    return 1.25 * level ** 2 + 50


def box_recipes(player: PlayerState, level: int) -> list[dict]:
    """Opening a large token box and trading its tokens for xp, as a Dungeoneering recipe at `level`."""
    if not player.members:
        return []
    return [{"skill": "Dungeoneering", "output": "Dungeoneering experience (token box)", "output_qty": 1.0,
             "level": 1, "xp": box_tokens(level), "ticks": BOX_SECONDS / 0.6, "materials": [[TOKEN_BOX, 1.0]],
             "members": True, "quests": [], "other_skills": [], "facility": "Daemonheim Rewards",
             "boost": "tokens traded for xp 1:1"}]


def method_rows(player: PlayerState) -> list[dict]:
    if not player.members:
        return []  # free-to-play floors (1-35) are a different grind; the free-to-play guide's rows stand
    minutes, measured = floor_minutes(player)
    rows = []
    for band in bands(player, "Dungeoneering"):
        floor = highest_floor(band)
        rate = xp_per_hour(player, band)
        assumptions = [f"{minutes:g} min a solo large floor" + ("" if measured else
                       " (ASSUMED: set dg_floor_minutes under boosts)"),
                       "high complexity, rushed to the boss, no deaths; xp at the 1,020% cap"]
        rows.append(row("Dungeoneering", f"Solo large floors (floor {floor}, simulated)", band, band + 4,
                        rate, activity="Dungeoneering floors", assumptions=assumptions))
        rows.append(row("Dungeoneering", f"Solo large floors, their tokens traded for xp (floor {floor}, simulated)",
                        band, band + 4, rate * (1 + TOKEN_SHARE), activity="Dungeoneering floors (tokens traded)",
                        assumptions=assumptions + ["the floors' tokens traded for xp 1:1 at Daemonheim Rewards"]))
    return rows
