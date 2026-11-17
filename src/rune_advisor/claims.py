"""Weekly free claims: supplies an area task set (or a quest) makes an NPC hand over once a week.

data/weekly_claims.json (hand-written from the wiki's Repeatable events page): flax from Geoffrey, potato cacti
from the Weird Old Man, pineapples and cooking apples from Dell Monti, pure essence from Wizard Cromperty,
buckets of sand from Bert, planks from Razmire. Since the DailyScape Overhaul these are weekly, 7 days after the
last claim. `available(player)` gives what the player's completed task sets and quests entitle them to (the best
tier met); the gatherer offers each as a source that takes a minute a week and a week of calendar time per claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from . import datapkg

from .models import PlayerState


@dataclass(frozen=True)
class Claim:
    item: str
    qty: int  # a week
    npc: str
    why: str  # the task set / quest that unlocks this tier
    choice: str = ""  # claims sharing this label are one weekly pick


@cache
def data() -> list[dict]:
    path = datapkg.path("weekly_claims.json")
    return json.loads(path.read_text())["claims"] if path.is_file() else []


def _met(player: PlayerState, tier: dict) -> bool:
    if name := tier.get("achievement"):
        a = player.achievements.get(name)
        if a is None or not a.completed:
            return False
    if name := tier.get("quest"):
        q = player.quests.get(name)
        if q is None or not q.done:
            return False
    return True


def available(player: PlayerState) -> list[Claim]:
    """The weekly claims the player has unlocked, each at its best tier."""
    if not player.members:
        return []
    out = []
    for c in data():
        met = [t for t in c["tiers"] if _met(player, t)]
        if met:
            best = max(met, key=lambda t: t["qty"])
            why = " and ".join(x for x in (best.get("achievement"), best.get("quest")) if x)
            out.append(Claim(c["item"], best["qty"], c["npc"], why, c.get("choice", "")))
    return out
