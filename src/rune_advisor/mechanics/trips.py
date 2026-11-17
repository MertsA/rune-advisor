"""Inventory trips: how banking cuts into an action rate.

A method fills `carry` items (backpack plus storage like an ore box), then spends `bank_seconds` going
to a bank and back. Gathering into a porter (Grace of the Elves, signs of the porter) sends items to the
bank as they come, so there are no trips. Processing at a bank chest or with a bank preset spends a few
ticks per inventory.

None of the round-trip times are on the wiki: BANK_SECONDS are our assumptions per kind of place (the
rows that use them list it in `assumptions`), to be replaced by recorded sessions (radv session).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import PlayerState
from . import profile

INVENTORY = 28
TICK = 0.6
# Round trip to a bank and back, seconds (assumptions): standing at a bank chest with a preset; a bank a
# short walk away; a teleport out and back.
BANK_SECONDS = {"bank": 3.0, "nearby": 25.0, "remote": 60.0}
WOOD_BOX_CAPACITY = 70  # assumption: the smallest wood box's capacity per log type


@dataclass
class Trip:
    carry: int  # items per trip
    bank_seconds: float
    how: str

    def per_hour(self, items_per_hour: float) -> float:
        """Items per hour once the trips are counted."""
        if items_per_hour <= 0 or self.bank_seconds <= 0 or self.carry <= 0:
            return items_per_hour
        return self.carry / (self.carry / items_per_hour + self.bank_seconds / 3600)


ORE_BOX_BASE = 100
FIXED_ORE_BOX = {"Gold ore", "Silver ore", "Platinum ore"}  # always 100 (Ore boxes page)
ORE_BOX_ACHIEVEMENTS = {"Everything Is Oresome": 20, "Everything Is Still Oresome": 10}


def ore_box_capacity(player: PlayerState, ore: str, increase_level: int | None) -> int:
    """Ores of one kind an ore box holds (Ore boxes page): 100, +20 at the ore's Mining level milestone,
    +20 / +10 for the two Oresome achievements; gold, silver and platinum stay at 100. Assumes the box's
    tier can store the ore."""
    if ore in FIXED_ORE_BOX:
        return ORE_BOX_BASE
    cap = ORE_BOX_BASE + (20 if increase_level and player.level("Mining") >= increase_level else 0)
    for name, extra in ORE_BOX_ACHIEVEMENTS.items():
        a = player.achievements.get(name)
        if a is not None and a.completed:
            cap += extra
    return cap


def gathering(player: PlayerState, skill: str, ore_box: int | None = None, place: str = "nearby",
              slots: int = INVENTORY - 1) -> Trip:
    """The trip for a gathering method: no trips with a porter; else the backpack (less the tool slot)
    plus an ore box (`ore_box` = its capacity for this ore) or wood box if owned."""
    if who := profile.porter(player):
        return Trip(0, 0.0, f"{who} sends items to the bank")
    carry, extra = slots, []
    if skill == "Mining" and ore_box and profile.has(player, "*ore box"):
        carry += ore_box
        extra.append(f"ore box +{ore_box}")
    if skill == "Woodcutting" and profile.has(player, "*wood box"):
        carry += WOOD_BOX_CAPACITY
        extra.append(f"wood box +{WOOD_BOX_CAPACITY} (assumed capacity)")
    return Trip(carry, BANK_SECONDS[place], f"bank every {carry} ({', '.join(extra) or 'backpack'}; "
                                            f"{BANK_SECONDS[place]:.0f}s round trip assumed)")


def processing(actions_per_inventory: float, place: str = "bank") -> Trip:
    """A make-X method: one trip per inventory of materials."""
    return Trip(max(1, int(actions_per_inventory)), BANK_SECONDS[place],
                f"{BANK_SECONDS[place]:.0f}s per inventory of {int(actions_per_inventory)} (assumed)")


def actions_per_inventory(materials: list[list], slots: int = INVENTORY) -> float:
    """How many actions one backpack of materials makes: stackables (runes, shards, charms...) take one slot."""
    from . import is_stackable

    per_action = sum(q for name, q in materials if not is_stackable(name))
    free = slots - sum(1 for name, _ in materials if is_stackable(name))
    return max(1.0, free / per_action) if per_action > 0 else float(slots * 100)
