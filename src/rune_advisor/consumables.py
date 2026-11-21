"""Consumable boosts as costed options: a familiar, a potion or a juju kept up while gathering.

overrides.toml's [players.<name>.boosts] says what a player always uses (mechanics/profile.py); everything
listed there is already in their rates. This module offers the rest as options with a price: the same
simulation with the boost applied, and the items it uses up per hour. The planner builds a "with <boost>"
twin of each simulated rock and tree (sim.boosted_rows) and takes it only where the faster rate is worth more
than making its consumables (recommend/base.Context._boosted_run, recommend/gathering.Gatherer._visit_spots).
Consumables are costed like any other plan need, net of their by-products: making pouches is Summoning xp,
juju is Herblore xp, which count when another goal in a joint plan or roadmap wants that skill (Context.wanted).

How long each consumable lasts:
  * a familiar: its duration (data/familiars.json, the wiki's Bucket:Infobox_familiar), one pouch per summon;
    Summoning points aren't costed (an obelisk recharges them).
  * juju: the wiki's potion pages: a dose of a juju potion lasts 6 minutes, of a perfect juju potion an hour.
  * mining potions: the boost decays a level a minute (level_boosts), so a +B dose drunk when the last ran out
    lasts B + 1 minutes at an average of half the boost (sim.mining_boost's `potion_average`).
Skillchompas, porters and incense aren't offered yet: how fast they're used up isn't modelled.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from functools import cache

from . import datapkg
from .mechanics import profile
from .models import PlayerState

JUJU_MINUTES = 6  # a dose of a juju potion (Juju mining potion, Juju fishing potion pages)
PERFECT_JUJU_MINUTES = 60  # a dose of a perfect juju potion (Perfect juju mining potion page)
# Only offer a twin that is at least this much faster than the plain row: anything less can't pay for itself.
MIN_GAIN = 0.005
# Twin rows split an hour into this many actions (material_run rounds actions up).
ACTIONS_PER_HOUR = 10
# Mining familiars below this damage boost (Desert wyrm, Void ravager: +1) aren't offered.
MIN_FAMILIAR_DAMAGE = 5


@dataclass
class Option:
    label: str  # "Lava titan", "perfect juju mining", "Lava titan + perfect juju mining"
    boosts: dict  # overlay on the player's [players.X.boosts]
    per_hour: dict[str, float]  # consumables used up per hour of the activity
    assumptions: list[str] = field(default_factory=list)


@cache
def familiars() -> dict[str, dict]:
    path = datapkg.path("familiars.json")
    return json.loads(path.read_text())["familiars"] if path.is_file() else {}


def familiar(name: str) -> dict | None:
    """The familiar's row by name, whatever its capitalisation ("Lava Titan" in the calculator data)."""
    rows = familiars()
    return rows.get(name) or next((v for k, v in rows.items() if k.lower() == name.lower()), None)


def doses(item: str) -> int:
    """Doses in a potion item: "Super mining potion (3)" -> 3; 1 for anything without a count."""
    m = re.search(r"\((\d)\)$", item)
    return int(m.group(1)) if m else 1


def _familiar_option(player: PlayerState, name: str) -> Option | None:
    f = familiar(name)
    if f is None or player.level("Summoning") < f["level"]:
        return None
    return Option(name, {"familiar": {}}, {f["pouch"]: 60 / f["minutes"]},
                  [f"a {name} kept summoned ({f['minutes']} min a pouch; Summoning points not costed)"])


def _juju(item: str, minutes: int) -> dict[str, float]:
    return {item: 60 / minutes / doses(item)}


def options(player: PlayerState, skill: str) -> list[Option]:
    """Consumable boosts for `skill` the player doesn't already list in overrides.toml, singly and the
    strongest of each kind together."""
    from . import sim
    from .mechanics import data as mechanics_data

    out: list[Option] = []
    if skill == "Mining":
        t = (mechanics_data().get("mining") or {}).get("boosts") or {}
        if not profile.familiar(player, "Mining"):
            # Every familiar with a real Mining boost the player can summon (the +1s can't pay for a pouch),
            # and the Waterfiend (5% double ore, not damage). Which one wins is the planner's costing.
            for name, damage in sorted((t.get("familiar_m") or {}).items(), key=lambda kv: -kv[1]):
                if damage >= MIN_FAMILIAR_DAMAGE and (o := _familiar_option(player, name)) is not None:
                    o.boosts = {"familiar": {"Mining": name}}
                    out.append(o)
            if (o := _familiar_option(player, "Waterfiend")) is not None:
                o.boosts = {"familiar": {"Mining": "Waterfiend"}}
                out.append(o)
        pot = (t.get("mining_potion") or {}).get("Super mining potion")
        if not profile.potion(player, "Mining") and pot:
            minutes = pot["flat"] + 1  # +B decays a level a minute: B + 1 minutes a dose
            out.append(Option("Super mining potion", {"potions": {"Mining": "Super mining potion"}, "potion_average": True},
                              {"Super mining potion (3)": 60 / minutes / 3},
                              [f"a super mining potion drunk as each dose wears off (+{pot['flat'] / 2:g} levels on average)"]))
        if not profile.juju(player, "Mining"):
            out.append(Option("perfect juju mining", {"juju": ["Mining"]},
                              _juju("Perfect juju mining potion (3)", PERFECT_JUJU_MINUTES)))
            out.append(Option("juju mining", {"juju": ["Mining"]}, _juju("Juju mining potion (3)", JUJU_MINUTES)))
    elif skill == "Woodcutting":
        if not profile.familiar(player, "Woodcutting"):
            for name in sim.WC_FAMILIAR:
                if (o := _familiar_option(player, name)) is not None:
                    o.boosts = {"familiar": {"Woodcutting": name}}
                    out.append(o)
        if not profile.juju(player, "Woodcutting"):
            out.append(Option("perfect juju woodcutting", {"juju": ["Woodcutting"]},
                              _juju("Perfect juju woodcutting potion (3)", PERFECT_JUJU_MINUTES)))
    # Several at once: each familiar with the strongest potion, and with the potion and a juju (the first offered
    # of each). A combination is only as available as its parts: one whose juju has no source loses on cost
    # alone, so the familiar + potion pair is offered too.
    potion = next((o for o in out if "potions" in o.boosts), None)
    juju = next((o for o in out if "juju" in o.boosts), None)
    sets = []
    for fam in [o for o in out if "familiar" in o.boosts] or [None]:
        sets += [[fam, potion], [fam, potion, juju], [fam, juju]]
    seen = set()
    for parts in sets:
        parts = [o for o in parts if o is not None]
        key = tuple(o.label for o in parts)
        if len(parts) < 2 or key in seen:
            continue
        seen.add(key)
        combo = Option(" + ".join(key), {}, {}, [])
        for o in parts:
            for k, v in o.boosts.items():
                combo.boosts[k] = {**combo.boosts.get(k, {}), **v} if isinstance(v, dict) else v
            for item, n in o.per_hour.items():
                combo.per_hour[item] = combo.per_hour.get(item, 0) + n
            combo.assumptions += o.assumptions
        out.append(combo)
    return out


def with_boosts(player: PlayerState, option: Option) -> PlayerState:
    """The player with the option's boosts added to their own (overrides.toml's win where both set one)."""
    have = dict(profile.boosts(player))
    merged = dict(option.boosts)
    for k, v in have.items():
        merged[k] = {**merged.get(k, {}), **v} if isinstance(v, dict) and isinstance(merged.get(k), dict) else \
            sorted(set(merged.get(k, [])) | set(v)) if isinstance(v, list) and isinstance(merged.get(k), list) else v
    return replace(player, boosts=merged)
