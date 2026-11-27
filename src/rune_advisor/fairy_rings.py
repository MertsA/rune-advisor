"""Fairy rings that must be built or repaired before they work (the wiki's Fairy rings page), and whether the export
says they are: five rings need five mushrooms "used on the Enchanted lands spot" with a spade on the tool belt, and
Mos Le'Harmless's ring needs both halves of its bridge repaired. Hand-written; the flags are the export's (0.5.0):
FAIRYRING_<PLACE>_UNLOCKED in FAIRYRINGS_MULTI and the two FT3_MOS_LE_HARMLESS_BRIDGE_* bits (the test account, who built every
ring, 2026-11-26). The Ancient Cavern's ring (BJQ) has no variable found by name yet: listed as unknown.
The planner doesn't model travel, so these are unlocks to list, not to value.
"""

from __future__ import annotations

from .models import PlayerState

RINGS = {
    "CJS": {"place": "Kharazi Jungle", "vars": ["FAIRYRING_KHARAZI_UNLOCKED"], "items": {"Bittercap mushroom": 5},
            "tools": ["Spade"], "quests_started": ["Legends' Quest"]},
    "BJS": {"place": "The Lost Grove", "vars": ["FAIRYRING_THE_LOST_GROVE_UNLOCKED"],
            "items": {"Bittercap mushroom": 5}, "tools": ["Spade"]},
    "BJQ": {"place": "Ancient Cavern", "vars": [], "items": {"Bittercap mushroom": 5}, "tools": ["Spade"],
            "note": "Barbarian Training up to pyre ships first"},
    "DIQ": {"place": "Fenmoor", "vars": ["FAIRYRING_FENMOOR_UNLOCKED"], "items": {"Bogwart": 5}, "tools": ["Spade"],
            "quests_started": ["Heralds of Crimson"], "levels": {"Agility": 79}},
    "DIP": {"place": "Mos Le'Harmless (its bridge, both halves)",
            "vars": ["FT3_MOS_LE_HARMLESS_BRIDGE_1", "FT3_MOS_LE_HARMLESS_BRIDGE_2"],
            "items": {"Plank": 8, "Bronze nails": 16}, "tools": ["Hammer"],
            "quests_started": ["A Fairy Tale III - Battle at Ork's Rift"]},
}


def built(player: PlayerState, code: str) -> bool | None:
    """True when every flag of the ring is set, False when one is clear, None when the export doesn't say."""
    names = RINGS[code]["vars"]
    values = [(getattr(player, "vars", None) or {}).get(n) for n in names]
    if not names or any(not isinstance(v, int) for v in values):
        return None
    return all(v > 0 for v in values)


def open_rings(player: PlayerState) -> list[tuple[str, dict, bool | None]]:
    """(code, ring, built: False or None) for every ring not known to be built."""
    return [(code, ring, state) for code, ring in RINGS.items() if (state := built(player, code)) is not True]
