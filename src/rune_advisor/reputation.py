"""Menaphos reputation: a by-product of skilling in the city, toward the tier-10 faction achievements.

From the wiki's Reputation (Menaphos) page: skilling inside the walls earns reputation for the
chosen faction, more for the methods released with Menaphos (REP_PER_ITEM, base amounts, no boosts).
Tier 10 in a faction ("Reputation - Class Crown" etc.) needs 3,000,000 reputation; progress comes from
that achievement's tally in the plugin export.

Reputation is valued in hours by the best general source the wiki names: soul obelisks, ~20k per hour
for any faction (limited by a daily cap, so it's a rate, not a plan for millions).
"""

from __future__ import annotations

from .models import PlayerState

FACTION_ACHIEVEMENTS = {
    "Imperial": "Reputation - Class Crown",
    "Port": "Reputation - Seagullible",
    "Merchant": "Reputation - Lock, Stock and Barrel",
    "Worker": "Reputation - To the Bone",
}
# Item gathered in its district -> (faction, base reputation per item).
REP_PER_ITEM = {
    "Acadia logs": ("Imperial", 4.5),
    "Raw desert sole": ("Port", 3.0),
    "Raw catfish": ("Port", 4.5),
    "Raw beltfish": ("Port", 5.6),
    "Sandstone": ("Worker", 2.7),
}
BEST_REP_PER_HOUR = 20_000  # soul obelisks (wiki), daily-capped


def remaining(player: PlayerState, faction: str) -> float:
    """Reputation still needed for the faction's tier-10 achievement (0 if done or unknown)."""
    a = player.achievements.get(FACTION_ACHIEVEMENTS.get(faction, ""))
    if a is None or a.completed:
        return 0.0
    tests = [t for t in a.requirements if (t.get("totalValue") or 0) > 1]
    if not tests:
        return 0.0
    t = tests[0]
    return max(0.0, t["totalValue"] - (t.get("currentValue") or 0))


def value_per_rep(player: PlayerState, faction: str) -> float:
    """Hours one reputation point saves (it would otherwise come from soul obelisks), if still needed."""
    return 1 / BEST_REP_PER_HOUR if remaining(player, faction) > 0 else 0.0


def add(player: PlayerState, faction: str, amount: float) -> float:
    """Credit reputation to the faction's achievement tally (on a copy of the player); returns what landed."""
    a = player.achievements.get(FACTION_ACHIEVEMENTS.get(faction, ""))
    if a is None or a.completed:
        return 0.0
    for t in a.requirements:
        if (t.get("totalValue") or 0) > 1:
            landed = min(amount, t["totalValue"] - (t.get("currentValue") or 0))
            t["currentValue"] = (t.get("currentValue") or 0) + landed
            if t["currentValue"] >= t["totalValue"]:
                t["complete"] = True
                a.completed = all(x.get("complete") for x in a.requirements)
            return landed
    return 0.0
