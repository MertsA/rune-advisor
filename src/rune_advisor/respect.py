"""Artisans' Workshop respect: the reward shop's unlocks, costed as Smithing xp to earn in the workshop.

Respect is a percentage, 1% per 10,000 Smithing xp from equipment (and ceremonial swords) finished at the
workshop's anvils in Falador; smelting, cannonballs, protean bars and masterwork components give none, bonus xp
doesn't count, and the Falador shield 4 adds 5% when worn. It caps at 100% at a time, so it has to be spent as it
fills. Egil's rewards (PRICES, in percent) are one-off unlocks; the achievements "Work on Your Artisan" (all
rewards but outfits and auras) and "Work on Your Artisan II" (the three outfits as well) test one flag per reward.
The balance is the export's ARTISAN_RESPECT (plugin 0.5.0), in thousandths of a percent: 17,556 -> 16,556 buying
a 1% consumable from Egil, which showed 17% -> 16% (beta, 2026-11-26); older exports count it as 0, and say so.

A plan for an open reward is the Smithing xp its respect takes at the player's Smithing rate; that xp is
training the player keeps (`Plan.reward_hours`), so the unlocks cost little beyond smithing in Falador rather
than elsewhere. `percent_for(xp)` is what a batch of smithing earns there, shown on the gatherer's anvil steps.

Source: RuneScape Wiki, Respect (Artisans' Workshop) and the reward shop's stock (CC BY-NC-SA 3.0).
"""

from __future__ import annotations

from .models import PlayerState

ACHIEVEMENTS = ("Work on Your Artisan", "Work on Your Artisan II")
XP_PER_PERCENT = 10_000
SHIELD = "Falador shield 4"
SHIELD_BONUS = 1.05
CAP = 100
VAR = "ARTISAN_RESPECT"
VAR_UNITS_PER_PERCENT = 1000
# Reward (as the achievements' tests name it) -> respect, in percent.
PRICES = {"Quick Repairs": 5, "Repair Expert": 15, "Solemn Smith I": 20, "Solemn Smith II": 40, "Solemn Smith III": 60,
          "Solemn Smith IV": 80, "Solemn Smith V": 100, "Ceremonial Swordsmith I": 30, "Ceremonial Swordsmith II": 30,
          "Ceremonial Swordsmith III": 30, "Ceremonial Swordsmith IV": 30, "Ceremonial Swordsmith V": 30,
          "Golden Cannon": 50, "Royale Cannon": 100, "Restocking Cannon": 100, "Ceremonial Sword Orders": 100,
          "Blacksmith's helmet": 50, "Blacksmith's top": 50, "Blacksmith's apron": 50, "Blacksmith's gloves": 50,
          "Blacksmith's boots": 50, "Blacksmith's helmet add-on": 50, "Necturion Outfit": 100,
          "Invictum Outfit": 100, "Aetherium Outfit": 100}


def bonus(player: PlayerState) -> float:
    return SHIELD_BONUS if SHIELD in player.holdings else 1.0


def held(player: PlayerState) -> float | None:
    """Respect held, in percent, or None when the export doesn't carry it."""
    value = player.vars.get(VAR)
    return value / VAR_UNITS_PER_PERCENT if isinstance(value, int) and value >= 0 else None


def xp_for(player: PlayerState, percent: float) -> float:
    """Smithing xp to earn at the workshop for `percent` respect."""
    return percent * XP_PER_PERCENT / bonus(player)


def percent_for(player: PlayerState, xp: float) -> float:
    """Respect a batch of smithing worth `xp` earns at the workshop."""
    return xp / XP_PER_PERCENT * bonus(player)


def open_rewards(player: PlayerState) -> dict[str, int]:
    """Rewards the achievements still want, with their prices."""
    out: dict[str, int] = {}
    for name in ACHIEVEMENTS:
        a = player.achievements.get(name)
        if a is None or a.completed:
            continue
        for r in a.requirements:
            desc = str(r.get("description") or "")
            if desc in PRICES and not r.get("complete"):
                out[desc] = PRICES[desc]
    return out
