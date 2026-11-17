"""Elite skilling outfits made from Invention fragments (data/elite_outfits.json, scripts/build_elite_outfits.py).

Each skill has three component outfits (four for Dungeoneering), five pieces each. Crafting at an inventor's
workbench (80 in the skill, 20 Invention) turns 3,600 fragments into a random unowned component piece; one of
each component outfit's piece in a slot combine into that slot's combined piece (e.g. Nature's sentinel helm).

What the export shows: the fragment currency (CURRENCY_FRAGMENTS_*) and any pieces in exported inventories.
Pieces stored with Diango or in the house aren't visible, so:
  * a piece seen once is remembered (SEEN_FILE), since outfit pieces aren't lost;
  * no fragments at 70+ with nothing seen means the combined outfit is likely done (the game destroys
    leftover fragments and stops dropping them once it's made);
  * `outfit_pieces_left = { Thieving = 6 }` under [players.X] in overrides.toml states pieces still to craft;
  * `outfit_blueprints = ["Dungeoneering"]` marks a blueprint bought where no var shows it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from . import datapkg
from pathlib import Path

from .config import STATE_DIR
from .models import PlayerState
from .xp import SKILL_ALIASES

FRAGMENTS_PER_PIECE = 3600
UNLOCK_LEVEL = 70  # fragments start dropping
CRAFT_LEVEL = 80  # and pieces can be made (plus CRAFT_INVENTION Invention)
CRAFT_INVENTION = 20
# Each outfit's fragment page gives its drop size and interval (data: per_drop, minutes; x2 at 99, x3 at 120, x4 at
# 200m xp) and a 1/2,500 chance per drop of a bonus 3,600. The cooldown is shared by all outfits.
BONUS_FRAGMENTS, BONUS_CHANCE = 3600, 1 / 2500
FLOORS_PER_HOUR = 4  # Dungeoneering drops per floor: an assumption (large floors, ~15 min each)
SEEN_FILE = STATE_DIR / "seen_items.json"


@cache
def outfits() -> dict[str, dict]:
    return json.loads(datapkg.path("elite_outfits.json").read_text())


def all_piece_names() -> set[str]:
    return {piece for o in outfits().values() for c in [*o["components"], o["combined"]] for piece in c["pieces"]}


def fragments_per_hour(player: PlayerState, skill: str) -> float:
    data = outfits()[skill]
    s = player.skills.get(SKILL_ALIASES.get(skill, skill))
    level, xp = (s.level, s.xp) if s else (1, 0)
    mult = 4 if xp >= 200_000_000 else 3 if level >= 120 else 2 if level >= 99 else 1
    drops = 60 / data["minutes"] if data.get("minutes") else FLOORS_PER_HOUR
    return drops * (data["per_drop"] * mult + BONUS_FRAGMENTS * BONUS_CHANCE)


def seen_pieces(player: PlayerState, path: Path | None = None) -> set[str]:
    """Outfit pieces ever seen in this player's exports: the remembered ones plus those held now (saved back)."""
    path = path or SEEN_FILE
    try:
        store = json.loads(path.read_text())
    except (OSError, ValueError):
        store = {}
    key = player.name.lower()
    known = set(store.get(key, []))
    now = known | (set(player.holdings) & all_piece_names())
    if now != known:
        store[key] = sorted(now)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(store, indent=1) + "\n")
        except OSError:
            pass  # read-only home: still correct for this run
    return now


@dataclass
class OutfitState:
    skill: str
    outfit: str  # the combined outfit's name
    fragments: int
    status: str  # "complete", "likely complete", "in progress", "not started"
    pieces_left: int | None  # component pieces still to craft; None = unknown
    pieces_max: int  # every component piece
    craftable: int  # pieces the held fragments make now
    notes: list[str] = field(default_factory=list)
    blocked: str | None = None  # a blueprint still to buy before any piece can be crafted

    @property
    def fragments_needed(self) -> int:
        left = self.pieces_max if self.pieces_left is None else self.pieces_left
        return max(0, left * FRAGMENTS_PER_PIECE - self.fragments)


def outfit_state(player: PlayerState, skill: str, seen: set[str], pieces_left: dict[str, int] | None = None) -> OutfitState:
    data = outfits()[skill]
    combined = data["combined"]
    components = data["components"]
    fragments = int(player.vars.get(data["fragment_var"]) or 0)
    level = player.level(SKILL_ALIASES.get(skill, skill))
    pieces_max = len(components) * len(combined["pieces"])
    state = OutfitState(skill, combined["outfit"], fragments, "in progress", None, pieces_max,
                        fragments // FRAGMENTS_PER_PIECE)
    if all(piece in seen for piece in combined["pieces"]):
        state.status, state.pieces_left = "complete", 0
        return state
    # Per slot: done once the combined piece is seen, else each component piece not seen is still to make.
    seen_any = False
    left = 0
    for slot, piece in enumerate(combined["pieces"]):
        if piece in seen:
            seen_any = True
            continue
        owned = sum(1 for c in components if c["pieces"][slot] in seen)
        seen_any |= owned > 0
        left += len(components) - owned
    if unlock := data.get("unlock"):
        bought = player.vars.get(data["unlock_var"]) if data.get("unlock_var") else None
        # Ironmen can't get pieces from Treasure Hunter, so any piece they own was crafted: blueprint bought.
        crafted = player.ironman and seen_any
        if not bought and not crafted and skill not in player.outfit_blueprints:
            have = player.vars.get(data.get("unlock_currency_var") or "")
            state.blocked = unlock + (f" (you have {have})" if have is not None else "")
            state.craftable = 0
            if bought is None:
                state.notes.append("blueprint status not exported: outfit_blueprints = [\"" + skill + "\"] in "
                                   "overrides.toml marks it bought")
    override = next((n for k, n in (pieces_left or {}).items() if k in (skill, SKILL_ALIASES.get(skill, skill))), None)
    if override is not None:
        state.pieces_left = int(override)
        state.notes.append("pieces left from overrides.toml")
    elif seen_any:
        state.pieces_left = left
        state.notes.append("pieces left from pieces seen in exports (stored ones may be missing)")
    elif state.blocked and data.get("unlock_var") in player.vars:
        # The blueprint var says it was never bought and no piece was ever seen: nothing crafted yet.
        state.pieces_left = pieces_max
    if level < UNLOCK_LEVEL:
        state.status = "not started"
    elif fragments == 0 and not seen_any and state.pieces_left is None:
        state.status = "likely complete"
        state.notes.append("no fragments: the game destroys them once the combined outfit is made")
    elif state.pieces_left is None:
        state.notes.append(f"pieces owned unknown (none seen in exports): up to {pieces_max} left; "
                           f"set outfit_pieces_left in overrides.toml")
    return state
