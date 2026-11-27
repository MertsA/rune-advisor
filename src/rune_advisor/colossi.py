"""The Divination colossi's murals (the wiki's Fates of the Colossi, Colossi Divination and Stonevein amulet pages):
seven murals restored by depositing charged memory stones (charged at the colossus remnants, 180 seconds a charge).
A mural's third restoration phase adds a teleport to it on the Stonevein amulet, an account unlock the planner lists
(it doesn't model travel, so not valued). Needs 77 Divination for the colossi and Heralds of Crimson started; the
murals take deposits without the level.

State: the achievement "Fates of the Colossi" tests each mural's COLOSSI_DIVINATION_MURAL_<NAME>_STATE for 3 ("Fully
restore the <name> colossus mural."); the tests travel in the export already. Taken as restored (teleport unlocked)
at 3. The test account on the beta (2026-11-27): one deposit made, every mural 0.

Per mural, the wiki's Colossi Divination table at level 99: Divination and Agility xp for 10 deposits; how many
deposits a mural takes is "between 8 and 11" there (unsourced): DEPOSITS assumed 9.5, each DEPOSIT_MINUTES (the
180-second charge plus the walk, assumed).
"""

from __future__ import annotations

from .models import PlayerState

ACHIEVEMENT = "Fates of the Colossi"
LEVEL = 77
QUEST = "Heralds of Crimson"
DEPOSITS = 9.5
DEPOSIT_MINUTES = 4.0
# mural -> (where, Divination xp and Agility xp for 10 deposits at 99)
MURALS = {
    "unicorn": ("south of the Shuruk-Ba entrance", 26_475, 5_170),
    "wolf": ("west of the Exalted Quarry", 30_446, 5_945.5),
    "bat": ("the southern half of Shuruk-Ba", 33_093, 6_462.5),
    "tree": ("west of Berylbrook", 33_093, 6_462.5),
    "fox": ("the southern edge of Fenmoor, west of Globin", 37_065, 7_238),
    "millipede": ("south of the Shrine of Inanna", 37_065, 7_238),
    "spider": ("the hilltop above Hollow Hill", 58_245, 11_374),
}


def restored(player: PlayerState) -> dict[str, bool] | None:
    """Each mural restored (its teleport unlocked) or not, from the achievement's tests; None when not exported."""
    a = player.achievements.get(ACHIEVEMENT)
    if a is None:
        return None
    out = {}
    for name in MURALS:
        test = next((r for r in a.requirements if f" {name} colossus mural" in (r.get("description") or "")), None)
        out[name] = bool(a.completed or (test is not None and test.get("complete")))
    return out
