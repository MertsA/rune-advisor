"""The Archaeology Guild: its shop's one-time unlocks and the research team (data/archaeology_guild.json,
scripts/build_archaeology_guild.py), the two things chronotes are spent on.

Chronotes come from handing restored artefacts to collectors (completing a collection pays the artefacts' chronotes
plus a set bonus, mostly 100%: catalogue.collection_recipes; the training loop in mechanics/archaeology.py hands its
artefacts in, COLLECTION_FACTOR). They go first to the shop's unlocks (`unlocks_left`); once those are bought the
research team is the sink (the user, 2026-11-21): expeditions run offline, one at a time, cost chronotes by the hour
and return Archaeology xp (the Research page's table), materials and, rarely, damaged artefacts from the dig site.
The amounts of materials and artefacts aren't published: RESEARCH_MATERIALS_PER_HOUR and RESEARCH_ARTEFACTS_PER_HOUR
are flagged estimates. The Fort Forinthry Command Centre's cut (3% fewer chronotes, tier 2) and speed (+3%, tier 3)
apply when the export carries its tier (`player=`); researchers' own speed buffs aren't modelled.

Ownership of unlocks: the export counts the tiers bought of the soil box (ARCH_SOIL_BOX_UPGRADE), material storage
(ARCH_MATERIAL_STORAGE_UPGRADE), workbench storage (ARCH_RESTORATION_WORKBENCH_STORAGE_UPGRADE) and mattock precision
(ARCH_PRECISION_BASE_UPGRADE, mechanics.archaeology.precision_upgrades), and flags the relic loadout
(ARCH_RELIC_LOADOUT_GUILD_REWARD_SHOP); all but the soil box since plugin 0.5.0 (the test account read 3/1/3/0 with material
storage and precision through Professor, workbench storage at Associate, beta 2026-11-26). Outfit pieces and the
energised meteorite shard (or a mattock of Time and Space) are items held. Without the export, and for the
auto-screener blueprint always, `archaeology_unlocks = ["Material storage upgrade", ...]` under [players.X.boosts]
names the ones bought (with a count suffix " x2" for tiers), else they count as not bought.
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


# Unlock series the export counts (tiers bought, in shop order) or flags (0/1), since plugin 0.5.0.
EXPORTED_TIERS = {"Material storage upgrade": "ARCH_MATERIAL_STORAGE_UPGRADE",
                  "Workbench storage upgrade": "ARCH_RESTORATION_WORKBENCH_STORAGE_UPGRADE",
                  "Additional relic loadout": "ARCH_RELIC_LOADOUT_GUILD_REWARD_SHOP"}
# The shop data names the first workbench tier "Workbench upgrade" and the later ones "Workbench storage upgrade".
SERIES = {"Workbench upgrade": "Workbench storage upgrade"}


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
    vars_ = getattr(player, "vars", None) or {}
    tiers = {"Soil box upgrade": int(vars_.get("ARCH_SOIL_BOX_UPGRADE") or 0),
             "Mattock precision upgrade": precision_upgrades(player, level)[0] // 2}
    for name, var in EXPORTED_TIERS.items():
        if isinstance(vars_.get(var), int):
            tiers[name] = vars_[var]
    seen: dict[str, int] = {}
    out = []
    for u in guild()["unlocks"]:
        name = SERIES.get(u["name"], u["name"])
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
        elif nth > named.get(name, named.get("Workbench upgrade", 0) if name == "Workbench storage upgrade" else 0):
            out.append((u, False))
    return out


def research_sites(level: int) -> dict[str, dict]:
    return {k: v for k, v in guild()["research_sites"].items() if v["level"] <= level and level >= RESEARCH_MIN_LEVEL}


def research_xp(level: int, factor: int, player: PlayerState | None = None) -> float:
    """Archaeology xp an hour of general research (the Research page's table: Kharid-et, x factor% elsewhere), 3%
    more with a tier 3 Command Centre."""
    table = guild()["research_xp_per_hour"]
    lvl = min(int(k) for k in table) if level < min(int(k) for k in table) else min(level, max(int(k) for k in table))
    return float(table.get(str(lvl), 0.0)) * factor / 100 * research_speed(player)


def research_cost(level: int, factor: int, player: PlayerState | None = None) -> float:
    """Chronotes an hour of general research: 5 x the level rounded down to even, x factor% (Research page), 3%
    less with a Command Centre at tier 2 or more."""
    from . import fort

    cut = fort.research_cost_factor(player) if player is not None else 1.0
    return 5 * (level - level % 2) * factor / 100 * cut


def research_speed(player: PlayerState | None) -> float:
    """What the research team brings back an hour, against the Research page's figures (Command Centre tier 3)."""
    from . import fort

    return fort.research_speed_factor(player) if player is not None else 1.0


def best_research(level: int) -> tuple[str, dict] | None:
    """The dig site whose research pays the most xp an hour at this level (they all cost in proportion)."""
    sites = research_sites(level)
    return max(sites.items(), key=lambda kv: kv[1]["factor"], default=None)
