"""Bottom-up rate simulations: xp/h and items/h from game mechanics instead of guide quotes.

Each skill module turns the wiki's mechanics data (data/mechanics.json, scripts/build_mechanics.py, plus
data/skilling_sim.json for Woodcutting/Mining) and the player's levels, tools and boosts
(`profile.py`) into method rows shaped like the training-guide rows the cost model already reads:
skill, method, level band, xp_per_hour, yields, side_xp. `all_rows(player)` gathers them; the cost
model then prefers them over guide rows for the same activity (see cost.CostModel.default), and the
guides become a calibration check (scripts/check_sim_vs_guides.py).

Every row carries `basis`: "simulated" (wiki mechanics), with `assumptions` listing the constants that
are ours rather than the wiki's (bank trip times, attention) so `radv rates` can show what is still
guessed. Rates measured in play, curated by hand into [[methods]] with basis = "measured", beat both.
`radv rates --player X` shows each band's basis.
"""

from __future__ import annotations

import json
import re
from functools import cache
from .. import datapkg

from ..models import PlayerState

SIM_BAND = 5  # levels per simulated method row


@cache
def data() -> dict:
    path = datapkg.path("mechanics.json")
    return json.loads(path.read_text()) if path.is_file() else {}


@cache
def _stackable() -> frozenset[str]:
    return frozenset(data().get("stackable", []))


def is_stackable(item: str) -> bool:
    return item in _stackable()


def interpolate_by_level(low: float, high: float, level: int) -> int:
    """Jagex's skilling-chance interpolation between a level-1 and a level-99 value."""
    return int(((99 - level) * low + (level - 1) * high) // 98)


def row(skill: str, method: str, level_min: int, level_max: int | None, xp_per_hour: float,
        yields: dict[str, float] | None = None, side_xp: dict[str, float] | None = None,
        assumptions: list[str] | None = None, **extra) -> dict:
    """A simulated method row for the cost model (cost.custom_row reads it like a [[methods]] entry)."""
    return {"skill": skill, "method": method, "level_min": int(level_min), "level_max": level_max,
            "xp_per_hour": float(xp_per_hour), "yields": yields or {}, "side_xp": side_xp or {},
            "basis": "simulated", "source": "simulation", "assumptions": assumptions or [],
            "activity": extra.pop("activity", method), **extra}


def bands(player: PlayerState, skill: str, max_level: int = 120):
    """The level of each SIM_BAND-wide band from the player's level up (rows are rated at its start)."""
    return range(max(1, player.level(skill)), max_level + 1, SIM_BAND)


_WORD = re.compile(r"[a-z']+")


def _words(name: str) -> set[str]:
    out = set()
    for w in _WORD.findall(name.lower()):
        w = w.rstrip("s") if len(w) > 3 else w
        if w not in {"the", "of", "at", "in", "and", "with", "from", "a", "tree", "rock", "simulated"}:
            out.add(w)
    return out


def covers(sim_activity: str, guide_method: str) -> bool:
    """Whether a guide row describes the same activity as a simulated one ("Maple trees" vs "Maple Tree")."""
    sim = _words(sim_activity)
    return bool(sim) and sim <= _words(guide_method)


def all_rows(player: PlayerState) -> list[dict]:
    """Every simulated method row for the player."""
    from . import archaeology, combat, farming, fishing, magic, rituals, smithing, thieving
    from .. import sim

    from . import item_xp
    from ..cost import _load_wiki_rates, usable_wiki_rows
    from .combat import GUIDE_STYLE

    rows = sim.method_rows(player)
    for module in (fishing, thieving, magic, smithing, rituals, farming, archaeology, combat):
        rows += module.method_rows(player)
    # Invention twins of every method the player can run in augmented gear: the sims' rows plus the guides' rows
    # for the skilling skills the sims don't cover (combat guide rows are already re-rated by combat.method_rows).
    guide = [r for r in usable_wiki_rows(_load_wiki_rates(player.members)) if r["skill"] not in GUIDE_STYLE]
    rows += item_xp.method_rows(player, rows + guide)
    return rows
