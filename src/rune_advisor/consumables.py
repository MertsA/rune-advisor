"""Consumable boosts as costed options: a familiar, a potion, a juju or signs of the porter kept up while skilling.

overrides.toml's [players.<name>.boosts] says what a player always uses (mechanics/profile.py); everything
listed there is already in their rates. This module offers the rest as options with a price: the same
simulation with the boost applied, and the items it uses up. `boosted_rows` builds a "with <boost>" twin of
each simulated row (rocks, trees, fishing spots, pickpocket targets, hotspots) where the boost makes it better,
and the planner takes one only where that is worth more than making its consumables
(recommend/base.Context._boosted_run, recommend/gathering.Gatherer._visit_spots). Consumables are costed like
any other plan need, net of their by-products: pouches are Summoning xp, juju Herblore xp, signs of the porter
Divination xp, which count when another goal in a joint plan or roadmap wants that skill (Context.wanted).

What each sim models, and how long its consumables last:
  * familiars (Mining, Woodcutting, Fishing, Thieving): a pouch per its duration (data/familiars.json, the wiki's
    Bucket:Infobox_familiar); Summoning points aren't costed (an obelisk recharges them).
  * juju (Mining, Woodcutting, Fishing): a dose of a juju potion lasts 6 minutes, of a perfect juju potion an hour
    (the potions' wiki pages).
  * a mining potion: the boost decays a level a minute (level_boosts), so a +B dose drunk when the last ran out
    lasts B + 1 minutes at an average of half the boost (sim.mining_boost's `potion_average`).
  * signs of the porter (Mining, Woodcutting, Fishing, Archaeology: no bank trips, or Archaeology's full hit
    rate): a charge per item sent to the bank, PORTER_CHARGES a sign (the signs' wiki pages), the best sign the
    player's Divination makes (recipes in data/conversions.json).
Skillchompas and incense aren't offered: how fast they're used up isn't modelled.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from functools import cache
from itertools import combinations

from . import datapkg
from .mechanics import profile
from .models import PlayerState

JUJU_MINUTES = 6  # a dose of a juju potion (Juju mining potion, Juju fishing potion pages)
PERFECT_JUJU_MINUTES = 60  # a dose of a perfect juju potion (Perfect juju mining potion page)
# Charges per sign of the porter, and the Divination level that makes it (the signs' wiki pages).
PORTER_CHARGES = {"Sign of the porter VII": (99, 50), "Sign of the porter VI": (94, 30),
                  "Sign of the porter V": (88, 25), "Sign of the porter IV": (68, 20),
                  "Sign of the porter III": (48, 15), "Sign of the porter II": (28, 10),
                  "Sign of the porter I": (6, 5)}
PORTER_SKILLS = ("Mining", "Woodcutting", "Fishing", "Archaeology")
# Only offer a twin that is at least this much better than the plain row: anything less can't pay for itself.
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
    per_item: dict[str, float] = field(default_factory=dict)  # used up per item gathered (porter charges)
    kind: str = ""  # familiar, potion, juju, porter: one of each kind at a time


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


def _porter_option(player: PlayerState) -> Option | None:
    sign = next((n for n, (lvl, _) in PORTER_CHARGES.items() if player.level("Divination") >= lvl), None)
    if sign is None:
        return None
    charges = PORTER_CHARGES[sign][1]
    return Option(f"signs of the porter ({sign.removeprefix('Sign of the porter ')})", {"porters": True}, {},
                  [f"{sign}: a charge per item sent to the bank, {charges} a sign"], {sign: 1 / charges}, "porter")


def singles(player: PlayerState, skill: str) -> list[Option]:
    """Each consumable boost for `skill` the player doesn't already list in overrides.toml."""
    from . import sim
    from .mechanics import data as mechanics_data
    from .mechanics import fishing

    out: list[Option] = []

    def familiars_of(names):
        if profile.familiar(player, skill):
            return
        for name in names:
            if (o := _familiar_option(player, name)) is not None:
                o.boosts, o.kind = {"familiar": {skill: name}}, "familiar"
                out.append(o)

    def juju(label: str, item: str, minutes: int):
        if not profile.juju(player, skill):
            out.append(Option(label, {"juju": [skill]}, _juju(item, minutes), kind="juju"))

    if skill == "Mining":
        t = (mechanics_data().get("mining") or {}).get("boosts") or {}
        # Every familiar with a real Mining boost (the +1s can't pay for a pouch), and the Waterfiend (5% double
        # ore, not damage). Which one wins is the planner's costing.
        familiars_of([n for n, d in sorted((t.get("familiar_m") or {}).items(), key=lambda kv: -kv[1])
                      if d >= MIN_FAMILIAR_DAMAGE] + ["Waterfiend"])
        pot = (t.get("mining_potion") or {}).get("Super mining potion")
        if not profile.potion(player, "Mining") and pot:
            minutes = pot["flat"] + 1  # +B decays a level a minute: B + 1 minutes a dose
            out.append(Option("Super mining potion", {"potions": {"Mining": "Super mining potion"}, "potion_average": True},
                              {"Super mining potion (3)": 60 / minutes / 3},
                              [f"a super mining potion drunk as each dose wears off (+{pot['flat'] / 2:g} levels on average)"],
                              kind="potion"))
        juju("perfect juju mining", "Perfect juju mining potion (3)", PERFECT_JUJU_MINUTES)
        juju("juju mining", "Juju mining potion (3)", JUJU_MINUTES)
    elif skill == "Woodcutting":
        familiars_of(list(sim.WC_FAMILIAR))
        juju("perfect juju woodcutting", "Perfect juju woodcutting potion (3)", PERFECT_JUJU_MINUTES)
    elif skill == "Fishing":
        familiars_of([n for n, _ in sorted(fishing.FAMILIAR.items(), key=lambda kv: -kv[1][0])])
        # The sim's juju is the perfect potion's +10% catch chance (a plain juju fishing potion only turns sharks
        # into baron sharks).
        juju("perfect juju fishing", "Perfect juju fishing potion (3)", PERFECT_JUJU_MINUTES)
    elif skill == "Thieving":
        familiars_of(["Magpie"])
    if skill in PORTER_SKILLS and not profile.porter(player) and (o := _porter_option(player)) is not None:
        out.append(o)
    return out


def options(player: PlayerState, skill: str) -> list[Option]:
    """Consumable boosts for `skill`: each one alone, and every combination of one per kind (each familiar with
    the first potion, juju and porter offered). A combination is only as available as its parts, so one with a
    juju nothing makes loses on cost alone and the combinations without it are there too."""
    single = singles(player, skill)
    firsts = {}
    for o in single:
        if o.kind != "familiar":
            firsts.setdefault(o.kind, o)
    others = list(firsts.values())
    out = list(single)
    seen = {(o.label,) for o in single}
    for fam in [o for o in single if o.kind == "familiar"] + [None]:
        for k in range(1, len(others) + 1):
            for extra in combinations(others, k):
                parts = ([fam] if fam else []) + list(extra)
                key = tuple(o.label for o in parts)
                if len(parts) < 2 or key in seen:
                    continue
                seen.add(key)
                combo = Option(" + ".join(key), {}, {}, [], {}, "combo")
                for o in parts:
                    for name, v in o.boosts.items():
                        combo.boosts[name] = {**combo.boosts.get(name, {}), **v} if isinstance(v, dict) else v
                    for item, n in o.per_hour.items():
                        combo.per_hour[item] = combo.per_hour.get(item, 0) + n
                    for item, n in o.per_item.items():
                        combo.per_item[item] = combo.per_item.get(item, 0) + n
                    combo.assumptions += o.assumptions
                out.append(combo)
    return out


SKILLS = ("Mining", "Woodcutting", "Fishing", "Thieving", "Archaeology")


def _sim_rows(player: PlayerState, skill: str) -> list[dict]:
    from . import sim
    from .mechanics import archaeology, fishing, thieving

    if skill in ("Mining", "Woodcutting"):
        return sim.method_rows(player, skills=(skill,))
    return [r for r in {"Fishing": fishing, "Thieving": thieving, "Archaeology": archaeology}[skill].method_rows(player)
            if r["skill"] == skill]


def boosted_rows(player: PlayerState) -> list[dict]:
    """A "with <boost>" twin of each simulated row for every consumable boost (options) where the boost gives
    more xp or more items. Each twin names what it uses up per hour (`consumes`: the option's per-hour items
    plus its per-item ones at the row's item rate) and carries it as a recipe, one action a tenth of an hour, so
    the material-aware planner costs the consumables. The cost model keeps twins apart from the training rows
    (CostModel.boosted), so a skill's plain rates stay unboosted."""
    rows = []
    for skill in SKILLS:
        opts = options(player, skill)
        if not opts:
            continue
        plain = {(r["method"], r["level_min"]): r for r in _sim_rows(player, skill)}
        gain = 1 + MIN_GAIN
        for option in opts:
            for r in _sim_rows(with_boosts(player, option), skill):
                base = plain.get((r["method"], r["level_min"]))
                yields = r.get("yields") or {}
                if base is None or (r["xp_per_hour"] < base["xp_per_hour"] * gain
                                    and not any(v >= (base["yields"].get(i) or 0) * gain and v > 0
                                                for i, v in yields.items())):
                    continue  # faster xp, or more items for the same xp (juju's stone spirits, a Waterfiend)
                # Items gathered (porter charges): a row can say (a loop handing its finds in for chronotes).
                items = r.get("items_per_hour", sum(yields.values()))
                consumes = dict(option.per_hour)
                for item, n in option.per_item.items():
                    consumes[item] = consumes.get(item, 0) + n * items
                if not consumes:
                    continue
                per_action = 1 / ACTIONS_PER_HOUR
                main = max(yields, key=yields.get) if yields else r["method"]
                out_qty = yields.get(main, 0) * per_action if yields else 1
                rows.append({
                    **base, "method": f"{base['method']} with {option.label}", "xp_per_hour": r["xp_per_hour"],
                    "yields": yields, "consumes": consumes, "boost": option.label,
                    "assumptions": list(base.get("assumptions") or []) + option.assumptions,
                    "base_skill": skill, "base_method": base["method"],
                    "recipe": {"output": main, "variants": [{
                        "output_qty": out_qty, "xp": r["xp_per_hour"] * per_action, "ticks": None,
                        "materials": [[i, n * per_action] for i, n in consumes.items()]}]},
                })
    return rows


def with_boosts(player: PlayerState, option: Option) -> PlayerState:
    """The player with the option's boosts added to their own (overrides.toml's win where both set one)."""
    have = dict(profile.boosts(player))
    merged = dict(option.boosts)
    for k, v in have.items():
        merged[k] = {**merged.get(k, {}), **v} if isinstance(v, dict) and isinstance(merged.get(k), dict) else \
            sorted(set(merged.get(k, [])) | set(v)) if isinstance(v, list) and isinstance(merged.get(k), list) else v
    boosted = replace(player, boosts=merged)
    # The same inventories: share the item caches (models.PlayerState.holdings, profile.has). Rates differ, so the
    # gathering-rate cache (sim.gather_rate) isn't shared.
    for key in ("_holdings", "_glob_cache"):
        if key in player.__dict__:
            boosted.__dict__[key] = player.__dict__[key]
    return boosted
