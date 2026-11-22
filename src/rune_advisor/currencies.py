"""Reward-shop currencies: what they buy (data/currency_shops.json, scripts/build_shops.py) and how the player earns
them, so an item sold only for reaper points or thaler has a source like any other.

Each currency shop line becomes a conversion recipe (`shop_recipes`: N of the currency -> the item), and the
currency itself is earned (`earning`) at a rate from its wiki page:

  * Reaper points: Soul Reaper tasks (data/reaper_tasks.json, scripts/build_reaper_tasks.py): at the boss that
    pays the most points an hour at the player's kill rate (a boss with a money making guide, its rate scaled for
    the player's gear; TASK_OVERHEAD_MINUTES a task), with the task's
    Slayer xp; at most 300 a week (Reaper points page), so a big purchase waits weeks of calendar time.
  * Thaler: 1 every 5 minutes in a Minigame Spotlight minigame (1 a minute on the spotlighted one, not assumed).
  * Runespan points: ~3,000 an hour at 99 Runecrafting (Runespan points page), scaled down below 99.
  * Void knight commendations: 4 a Veteran boat win (Pest Control page); a game every PEST_CONTROL_MINUTES.
  * Dungeoneering tokens, Slayer points and pilfer points: the rates the toolbelt track uses.
  * Chimes: selling Arc commodities at the Waiko market (`arc_recipes`, data/arc_commodities.json, base prices);
    the 20,000-a-day trade cap isn't modelled.

Currencies that only buy cosmetics or come from Treasure Hunter or events are left out (`SKIPPED`). Shop stock
limits on currency shops aren't modelled, and neither are balances except where the export has them (Slayer points).
Everything else a shop takes stays an unsourced input (`radv gaps` names it).
"""

from __future__ import annotations

import json
from functools import cache
from typing import TYPE_CHECKING

from . import datapkg

if TYPE_CHECKING:
    from .recommend.base import Context

# Paid, Treasure Hunter, league and holiday-event currencies: their shops sell cosmetics an hour of play can't buy.
SKIPPED = {"credits", "RuneCoins", "Double XP Tokens", "oddments", "Oddments", "H'oddments", "League points",
           "heartments", "fayre prize tickets", "Christmas spirit", "candy corn", "spooky tokens", "Fallen Leaves",
           "frozen tokens", "warped tokens", "shattered anima", "Rusty coins"}  # Rusty coins: Daemonheim only
WEEK_HOURS = 7 * 24
THALER_PER_HOUR = 12.0  # 1 per 5 minutes (Thaler page)
RUNESPAN_POINTS_AT_99 = 3_000.0  # "roughly 3,000 points ... per hour" at 99 Runecrafting (Runespan points page)
PEST_CONTROL_POINTS = 4  # a Veteran boat win
PEST_CONTROL_MINUTES = 7.0  # a 2-3 minute game, the lander's 3-minute wait and the walk back: an assumption
SLAYER_POINTS_PER_HOUR = 70.0  # as tracks/toolbelt.py
PILFER_POINTS_PER_HOUR = 900.0  # as tracks/toolbelt.py
TASK_OVERHEAD_MINUTES = 3.0  # getting a reaper task from Death and reaching the boss: an assumption


@cache
def shops() -> dict[str, list]:
    path = datapkg.path("currency_shops.json")
    return json.loads(path.read_text())["items"] if path.is_file() else {}


@cache
def reaper_tasks() -> dict:
    path = datapkg.path("reaper_tasks.json")
    return json.loads(path.read_text()) if path.is_file() else {"bosses": [], "weekly_cap": 300}


@cache
def arc_commodities() -> dict:
    path = datapkg.path("arc_commodities.json")
    return json.loads(path.read_text()) if path.is_file() else {"chimes": {}}


def arc_recipes() -> list[dict]:
    """Selling an Arc commodity to the Waiko merchants for chimes (data/arc_commodities.json), as conversions: the
    gatherer then earns chimes from whichever commodity is cheapest to get (bamboo, golden idols, fish oil...)."""
    return [{"skill": "", "output": "chimes", "output_qty": float(each), "level": 1, "xp": 0.0, "ticks": 1.0,
             "materials": [[item, 1.0]], "members": True, "quests": [], "other_skills": [],
             "facility": "Waiko market", "conversion": True, "tools": []}
            for item, each in arc_commodities()["chimes"].items()]


def _canonical() -> dict[str, str]:
    """Currency names as most shop lines spell them ("Chimes" on one line is "chimes" on the rest)."""
    counts: dict[str, dict[str, int]] = {}
    for offers in shops().values():
        for o in offers:
            for c in o[2]:
                counts.setdefault(c.lower(), {}).setdefault(c, 0)
                counts[c.lower()][c] += 1
    return {name: max(forms, key=forms.get) for forms in counts.values() for name in forms}


def shop_recipes() -> list[dict]:
    """A conversion recipe per currency shop line (catalogue.catalogue merges them): the prices are materials."""
    canonical = _canonical()
    out = []
    for item, offers in shops().items():
        if "#" in item:  # "Bonecrusher#Upgrade": an unlock on the item, not an item
            continue
        for shop, _stock, prices, members in offers:
            if any(c in SKIPPED for c in prices):
                continue
            out.append({"skill": "", "output": item, "output_qty": 1.0, "level": 1, "xp": 0.0, "ticks": 1.0,
                        "materials": [["Coins" if c == "coins" else canonical.get(c, c), float(q)]
                                      for c, q in prices.items()],
                        "members": members, "quests": [], "other_skills": [], "facility": shop,
                        "conversion": True, "tools": [], "currency_shop": True})
    return out


def earning(ctx: Context, currency: str) -> tuple[float, str, dict[str, float], float] | None:
    """(hours per unit, how, side xp per unit, calendar hours per unit) for a currency the player can earn."""
    p = ctx.player
    currency = currency.lower()
    if currency == "reaper points":
        return _reaper(ctx)
    if currency == "thaler":
        return 1 / THALER_PER_HOUR, "play Minigame Spotlight minigames (1 thaler per 5 minutes)", {}, 0.0
    if currency == "runespan points":
        lvl = p.level("Runecraft")
        per_hour = RUNESPAN_POINTS_AT_99 * min(1.0, lvl / 99)
        return (1 / per_hour, f"siphon in the Runespan (~{per_hour:,.0f} points an hour; the wiki's figure is ~3,000 at 99)",
                {"Runecraft": ctx.cost.rate("Runecraft", lvl) / per_hour}, 0.0)
    if currency == "void knight commendations":
        per_hour = PEST_CONTROL_POINTS * 60 / PEST_CONTROL_MINUTES
        return (1 / per_hour, f"win Pest Control on the Veteran boat ({PEST_CONTROL_POINTS} a game, a game every "
                              f"~{PEST_CONTROL_MINUTES:.0f} minutes assumed)", {}, 0.0)
    if currency == "dungeoneering tokens":
        rate = ctx.cost.rate("Dungeoneering", p.level("Dungeoneering"))
        return 1 / (0.2 * rate), "Dungeoneering floors (tokens are a tenth of floor xp, x2 with the ring of kinship)", \
            {"Dungeoneering": 5.0}, 0.0
    if currency == "pilfer points":
        return 1 / PILFER_POINTS_PER_HOUR, "Safecracking (Misthalin three-dial safes)", {}, 0.0
    if currency == "slayer points":
        return (1 / SLAYER_POINTS_PER_HOUR, "Slayer tasks (Laniakea: 22 a task, 110 every 10th, 330 every 50th)",
                {"Slayer": ctx.cost.rate("Slayer", p.level("Slayer")) / SLAYER_POINTS_PER_HOUR}, 0.0)
    return None


def _reaper(ctx: Context) -> tuple[float, str, dict[str, float], float] | None:
    """Reaper points at the boss paying the most an hour: points / (kills a task / kills an hour)."""
    from .drops import boss_kills_per_hour, estimated_kills_per_hour

    if ctx.player.level("Slayer") < 45:  # Death assigns tasks from 45 Slayer (Reaper task page)
        return None
    data = reaper_tasks()
    best = None
    for b in data["bosses"]:
        # Guide-rated bosses only: a combat-sim rate on a minigame's last wave (Har-Aken, TzTok-Jad) is no task time.
        if boss_kills_per_hour(ctx.player, b["boss"]) is None:
            continue
        est = estimated_kills_per_hour(ctx.player, b["boss"], ctx.combat_style)
        if not est or est[0] <= 0:
            continue
        hours = b["kills"] / est[0] + TASK_OVERHEAD_MINUTES / 60
        per_hour = b["points"] / hours
        if best is None or per_hour > best[0]:
            best = (per_hour, b, hours)
    if best is None:
        return None
    per_hour, b, hours = best
    cap = data.get("weekly_cap") or 300
    return (1 / per_hour, f"Soul Reaper tasks on {b['boss']} ({b['points']:g} points a task of ~{b['kills']:g} kills, "
                          f"~{hours * 60:.0f} min; {cap} points a week at most)",
            {"Slayer": b["slayer_xp"] / b["points"]}, WEEK_HOURS / cap)
