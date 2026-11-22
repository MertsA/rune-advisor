"""The Archaeology Guild: its shop's one-time unlocks and the research team (data/archaeology_guild.json,
scripts/build_archaeology_guild.py), the two things chronotes are spent on.

Chronotes come from handing restored artefacts to collectors (completing a collection pays the artefacts' chronotes
plus a set bonus, mostly 100%: catalogue.collection_recipes; the training loop in mechanics/archaeology.py hands its
artefacts in, COLLECTION_FACTOR). They go first to the shop's unlocks (`unlocks_left`); once those are bought the
research team is the sink (the user, 2026-11-21): expeditions run offline, one at a time, cost chronotes by the hour
and return Archaeology xp (the Research page's table), materials and, rarely, damaged artefacts from the dig site.
The amounts of materials and artefacts aren't published: RESEARCH_MATERIALS_PER_HOUR and RESEARCH_ARTEFACTS_PER_HOUR
are flagged estimates. Researchers' speed buffs and the Command Centre discounts aren't modelled.

Ownership of unlocks: soil box upgrades from the export (ARCH_SOIL_BOX_UPGRADE), outfit pieces and the energised
meteorite shard (or a mattock of Time and Space) from items held, mattock precision upgrades as
mechanics.archaeology.precision_upgrades assumes them; material storage, workbench storage, the auto-screener
blueprint and the relic loadout aren't exported: `archaeology_unlocks = ["Material storage upgrade", ...]` under
[players.X.boosts] names the ones bought (with a count suffix " x2" for tiers), else they count as not bought.
"""

from __future__ import annotations

import json
import re
from functools import cache

from . import datapkg
from .mechanics import profile
from .models import PlayerState

QUALIFICATIONS = {"Intern": 5, "Assistant": 12, "Associate": 42, "Professor": 70, "Guildmaster": 98}
COLLECTION_FACTOR = 2.0  # a completed collection pays its artefacts' chronotes twice (the 100% set bonus)
RESEARCH_MATERIALS_PER_HOUR = 30.0  # an estimate: the research pages list materials but no amounts
RESEARCH_ARTEFACTS_PER_HOUR = 0.1  # an estimate: artefacts are "Rare" there
RESEARCH_MIN_LEVEL = 40  # the Research page's experience table starts here


@cache
def guild() -> dict:
    path = datapkg.path("archaeology_guild.json")
    return json.loads(path.read_text()) if path.is_file() else {"unlocks": [], "research_xp_per_hour": {},
                                                                 "research_sites": {}}


def _named(player: PlayerState) -> dict[str, int]:
    out: dict[str, int] = {}
    for entry in profile.boosts(player).get("archaeology_unlocks") or []:
        m = re.match(r"(.+?)(?:\s+x(\d+))?$", str(entry).strip())
        out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2) or 1)
    return out


def unlocks_left(player: PlayerState) -> list[tuple[dict, bool]]:
    """The shop's unlocks the player hasn't bought, in shop order, with whether that's known (True) or only
    unknown from the export (False). Unlocks above the player's qualification (by level) are included: buying them
    waits for the level."""
    from .mechanics.archaeology import precision_upgrades

    level = player.level("Archaeology")
    held = player.holdings
    named = _named(player)
    tiers = {"Soil box upgrade": int((getattr(player, "vars", None) or {}).get("ARCH_SOIL_BOX_UPGRADE") or 0),
             "Mattock precision upgrade": precision_upgrades(player, level)[0] // 2}
    seen: dict[str, int] = {}
    out = []
    for u in guild()["unlocks"]:
        name = u["name"]
        nth = seen[name] = seen.get(name, 0) + 1
        if name in tiers:
            if nth <= tiers[name]:
                continue
            out.append((u, True))
        elif name.startswith(("Archaeologist's", "Master archaeologist's")):
            if name not in held:
                out.append((u, True))
        elif name == "Energised meteorite shard":
            if not ({"Energised meteorite shard", "Mattock of Time and Space"} & set(held)):
                out.append((u, True))
        elif nth > named.get(name, named.get(name.replace("Workbench upgrade", "Workbench storage upgrade"), 0)):
            out.append((u, False))
    return out


def research_sites(level: int) -> dict[str, dict]:
    return {k: v for k, v in guild()["research_sites"].items() if v["level"] <= level and level >= RESEARCH_MIN_LEVEL}


def research_xp(level: int, factor: int) -> float:
    """Archaeology xp an hour of general research (the Research page's table: Kharid-et, x factor% elsewhere)."""
    table = guild()["research_xp_per_hour"]
    lvl = min(int(k) for k in table) if level < min(int(k) for k in table) else min(level, max(int(k) for k in table))
    return float(table.get(str(lvl), 0.0)) * factor / 100


def research_cost(level: int, factor: int) -> float:
    """Chronotes an hour of general research: 5 x the level rounded down to even, x factor% (Research page)."""
    return 5 * (level - level % 2) * factor / 100


def best_research(level: int) -> tuple[str, dict] | None:
    """The dig site whose research pays the most xp an hour at this level (they all cost in proportion)."""
    sites = research_sites(level)
    return max(sites.items(), key=lambda kv: kv[1]["factor"], default=None)
