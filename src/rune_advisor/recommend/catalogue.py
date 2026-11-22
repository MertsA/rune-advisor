"""Train from the bank: fill a grind with whatever recipes the player's items already cover.

The wiki training guides name the *fastest* methods, which an ironman often has no materials
for (99 Fletching via dragon darts with no dart tips). This module looks through every wiki
recipe for the skill (data/recipe_catalogue.json, scripts/build_recipe_catalogue.py) and spends
the bank greedily, best xp/hour first, until the xp is covered or nothing more can be made:
"infuse 106 Fire titan pouches, then 169 Spirit kalphite pouches".

Recipes are single steps, and what one step makes goes back into the stock, so chains form on
their own (logs -> unstrung bows -> bows). A recipe is only used once the player meets its level,
its other skill levels and its quests, and members recipes are skipped on free-to-play.

Speed comes from the recipe's ticks plus banking overhead, capped at the skill's normal rate
(a 1-tick recipe isn't really 3m xp/h). Recipes far slower than normal training (turning pouches
into scrolls, cutting gems into bolt tips) are skipped: they'd use up useful items for little xp.
Materials from Group Ironman groupmates are used after
the player's own, as explicit transfer steps.

Material-limited xp gets two free boosts here (time isn't the limit, so they cost nothing extra):
  * Spirit gems (https://runescape.wiki/w/Spirit_gems): worn while infusing, each charge saves one
    charm, so the gems' charges act as extra charms of whatever kind is running out.
  * Voice of Seren (https://runescape.wiki/w/Voice_of_Seren): +20% base xp for some recipes while
    the Voice is in that clan's district (one hour in four per clan; Prifddinas needs Plague's End).
    It only applies to skilling *in* that district, so a pouch infused at the Shrine of Inanna in
    Havenhythe (boosts.shrine_recipe, the `shrine` flag) doesn't get it: the two never combine.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from .. import datapkg

from ..models import PlayerState
from ..xp import level_at_xp
from ..mechanics.processing import capped, effective
from .materials import _borrow

MAX_STEPS = 200
# Charges per spirit gem (= charms it saves on average). An activated "(a)" gem has used some of
# them, so its full charges are an upper bound.
SPIRIT_GEM_CHARGES = {"Spirit sapphire": 10, "Spirit emerald": 20, "Spirit ruby": 30, "Spirit diamond": 40,
                      "Spirit dragonstone": 50, "Spirit onyx": 60}
# (skill, does this recipe output count, clan): +20% base xp while the Voice of Seren is there.
VOS_BOOSTS = [
    ("Summoning", lambda out: out.endswith(" pouch"), "Amlodd"),
    ("Crafting", lambda out: out == "Crystal flask", "Ithell"),
]
VOS_MULTIPLIER = 1.2
# Skip recipes slower than this share of the skill's normal (hand-written) rate.
MIN_RATE_SHARE = 0.10
# Bank runs worth less than this share of a mix's xp are folded into one "odds and ends" step.
FOLD_SHARE = 0.01
# Recipe-bucket conversions that are one-off quest trades, not a repeatable source (found by
# scripts/audit_conversions.py): (output, inputs).
ONE_OFF_CONVERSIONS = {("Shark", ("Fremennik blade",))}
# Recipe facilities that aren't a supply: Stealing Creation's kiln turns sacred clay (minigame-only, never
# banked) into potions, and its "(4)" outputs would otherwise count as the highest dose Herblore makes, so
# the real (3)s never decanted up and Grand potions came out INCOMPLETE on sacred clay.
SKIP_FACILITIES = {"Creation kiln", "Pedestal (Dungeoneering)"}  # Daemonheim ensouling: nothing leaves the dungeon
# Potion doses every family can be decanted to (the wiki names "(4)" for some potions only).
DECANT_DOSES = {1, 2, 3, 4}


# Recipe outputs named by a wiki page alias rather than the item ("Extreme prayer potion (3)" redirects to
# Extreme prayer, whose items are "Extreme prayer (1-4)"): renamed so other recipes' inputs match.
OUTPUT_ALIASES = {"Extreme prayer potion": "Extreme prayer"}
_DOSES = re.compile(r"^(.*) \((\d)\)$")
# Jewellery recipes list the enchant *spell* as a material ("Onyx amulet" + "Lvl-6 enchant"); the spells'
# runes, level and xp come from mechanics.json (Bucket:Infobox_spell).
_ENCHANT = re.compile(r"^(Lvl-\d) enchant(?: \(item\))?$", re.I)


def _alias(name: str) -> str:
    m = _DOSES.match(name)
    return f"{OUTPUT_ALIASES[m[1]]} ({m[2]})" if m and m[1] in OUTPUT_ALIASES else name


@cache
def _enchant_spells() -> dict[str, dict]:
    """"Lvl-6" -> the spell (name, level, xp, runes) from the mechanics data."""
    from ..mechanics import data as mechanics_data

    return {s["name"].split(" ")[0]: s for s in mechanics_data().get("spells", [])
            if s.get("name", "").startswith("Lvl-") and s.get("runes")}


def cast_enchant(r: dict) -> dict:
    """A recipe whose material is an enchant spell, as casting the spell: Magic at the spell's level and xp,
    the spell's runes in place of the "item" (an elemental staff isn't assumed: enchants need 20+ of them)."""
    spells = [_ENCHANT.match(m)[1] for m, _ in r["materials"] if _ENCHANT.match(m)]
    if not spells or (spell := _enchant_spells().get(spells[0].capitalize())) is None:
        return r
    runes = [[name, float(q)] for name, q in spell["runes"]]
    other = [m for m in r["materials"] if not _ENCHANT.match(m[0])]
    return {**r, "skill": "Magic", "level": int(spell["level"]), "xp": float(spell["xp"]),
            "ticks": r.get("ticks") or 1.0, "materials": other + runes, "conversion": False,
            "facility": r.get("facility") or "", "tools": [],
            "other_skills": [[s, lvl] for s, lvl in r.get("other_skills", []) if s != "Magic"]}


def decant_recipes(recipes: list[dict]) -> list[dict]:
    """No-xp decanting between a potion's dose variants: "Extreme prayer (4)" from the (3)s Herblore makes,
    dose for dose (2 x (3) -> (4) + (2)). Each family decants only out of its highest dose a Herblore
    recipe makes, into every other dose any recipe names (and DECANT_DOSES), so decanting never loops back."""
    made: dict[str, int] = {}  # family -> highest dose a Herblore recipe makes
    named: dict[str, set[int]] = {}  # family -> doses named anywhere (outputs and inputs)
    members: dict[str, bool] = {}  # family -> every Herblore recipe for it is members-only
    for r in recipes:
        for name in [r["output"], *(m for m, _ in r["materials"])]:
            if m := _DOSES.match(name):
                named.setdefault(m[1], set()).add(int(m[2]))
        if r["skill"] == "Herblore" and (m := _DOSES.match(r["output"])):
            made[m[1]] = max(made.get(m[1], 0), int(m[2]))
            members[m[1]] = members.get(m[1], True) and r["members"]
    out = []
    for family, base in made.items():
        for dose in sorted((named[family] | DECANT_DOSES) - {base, 0}):
            out.append({"skill": "", "output": f"{family} ({dose})", "output_qty": 1.0, "level": 1, "xp": 0.0,
                        "ticks": 1.0, "materials": [[f"{family} ({base})", dose / base]], "members": members[family],
                        "quests": [], "other_skills": [], "facility": "Backpack (decant)", "conversion": True,
                        "tools": []})
    return out


@cache
def catalogue() -> list[dict]:
    """Wiki recipes plus hand-written conversions the recipe bucket lacks (data/conversions.json:
    weaving remnant energy into tier 70-95 energies) and generated potion decanting (decant_recipes)."""
    out = []
    for name in ("recipe_catalogue.json", "conversions.json"):
        path = datapkg.path(name)
        if path.is_file():
            # Daemonheim recipes (tagged by scripts/build_recipe_catalogue.py) make floor-only items, lost when the
            # floor ends: nothing a plan can carry out.
            out += [r for r in json.loads(path.read_text())["recipes"]
                    if (r["output"], tuple(m for m, _ in r["materials"])) not in ONE_OFF_CONVERSIONS
                    and r.get("facility") not in SKIP_FACILITIES and not r.get("daemonheim")]
    out = [{**r, "output": _alias(r["output"])} if _alias(r["output"]) != r["output"] else r for r in out]
    from ..currencies import arc_recipes, shop_recipes

    out += shop_recipes() + arc_recipes()  # reward shops: N of a currency -> the item (currencies.py earns it)
    out += collection_recipes()  # Archaeology collections: artefacts -> chronotes and repeat rewards
    out = [cast_enchant(r) for r in out]
    return out + decant_recipes(out) + augment_recipes(out)


@cache
def archaeology_collections() -> dict:
    path = datapkg.path("archaeology_collections.json")
    return json.loads(path.read_text()) if path.is_file() else {"collections": {}, "artefact_chronotes": {}}


def collection_recipes() -> list[dict]:
    """Handing an Archaeology collection's artefacts to its collector (data/archaeology_collections.json,
    scripts/build_archaeology_collections.py): the chronotes it pays (the artefacts' own plus the set bonus) and each
    repeat reward (Dragonkin V's two large Dungeoneering token boxes), as separate recipes on the same artefacts."""
    out = []
    for name, c in archaeology_collections()["collections"].items():
        materials = [[a, 1.0] for a in c["artefacts"]]
        for output, qty in [("chronotes", c["chronotes"]), *c["rewards"].items()]:
            if qty > 0:
                out.append({"skill": "Archaeology", "output": output, "output_qty": float(qty), "level": c["level"],
                            "xp": 0.0, "ticks": 1.0, "materials": materials, "members": c["members"], "quests": [],
                            "other_skills": [], "facility": f"{name} collection", "conversion": True,
                            "collection": name})
    return out


AUGMENTED = "Augmented "


def augment_recipes(recipes: list[dict]) -> list[dict]:
    """Augmenting gear the recipe data names only as an input ("Augmented sirenic hauberk" in a perk recipe, no
    recipe making it): the item and an Augmentor at an Inventor's workbench, as every augmentation is."""
    made = {r["output"] for r in recipes}
    wanted = {m for r in recipes for m, _ in r["materials"] if m.startswith(AUGMENTED) and m not in made}
    return [{"skill": "Invention", "output": name, "output_qty": 1.0, "level": 1, "xp": 0.0, "ticks": 1.0,
             "materials": [[name.removeprefix(AUGMENTED).capitalize() if name.removeprefix(AUGMENTED)[:1].islower()
                            else name.removeprefix(AUGMENTED), 1.0], ["Augmentor", 1.0]],
             "members": True, "quests": [], "other_skills": [], "facility": "Inventor's workbench",
             "conversion": True}
            for name in sorted(wanted)]


@cache
def by_output() -> dict[str, list[dict]]:
    """catalogue() indexed by output item."""
    index: dict[str, list[dict]] = {}
    for r in catalogue():
        index.setdefault(r["output"], []).append(r)
    return index


@dataclass
class BankMix:
    xp: float = 0.0
    hours: float = 0.0
    # (output, actions, made, xp, materials used {name: qty}) per recipe, in order.
    runs: list[tuple[str, int, float, float, dict[str, float]]] = field(default_factory=list)
    transfers: list[tuple[str, str, int]] = field(default_factory=list)
    recipient: str = ""
    # Own + group items left after the mix (for planning whatever xp is still missing).
    stock: dict[str, int] = field(default_factory=dict)
    charms_saved: int = 0  # charms the spirit gems stood in for
    used: dict[str, float] = field(default_factory=dict)  # items used up (own + group)
    vos_clan: str | None = None  # Voice of Seren clan whose boost the plan assumes
    boosts: list[str] = field(default_factory=list)  # how-to notes of recipe boosts used (Shrine of Inanna)

    def describe(self, skill: str) -> list[str]:
        steps = [f"{member}: put {n:,} {name} in group storage (or trade them) for {self.recipient or 'this player'}"
                 for member, name, n in self.transfers]
        if self.vos_clan:
            steps.append(f"do this while the Voice of Seren is in {self.vos_clan} (+20% base xp, an hour in every four)")
        steps += self.boosts
        if self.charms_saved:
            steps.append(f"wear spirit gems while infusing: they save ~{self.charms_saved:,} charms")
        # Leftovers (one terrorbird pouch from the one raw bird meat in the bank) don't each deserve a line.
        tiny = [run for run in self.runs if len(self.runs) > 1 and run[3] < FOLD_SHARE * self.xp]
        for output, actions, made, xp, used in self.runs:
            if any(run is (output, actions, made, xp, used) or run == (output, actions, made, xp, used) for run in tiny):
                continue
            uses = ", ".join(f"{math.ceil(q):,} {name}" for name, q in used.items())
            steps.append(f"from the bank: make {made:,.0f} {output} (~{xp:,.0f} {skill} xp), using {uses}")
        if tiny:
            steps.append("from the bank: odds and ends with what's left: "
                         + ", ".join(f"{made:,.0f} {output}" for output, _, made, _, _ in tiny)
                         + f" (~{sum(run[3] for run in tiny):,.0f} {skill} xp)")
        return steps


@cache
def members_items() -> frozenset[str]:
    path = datapkg.path("recipe_catalogue.json")
    return frozenset(json.loads(path.read_text()).get("members_items", [])) if path.is_file() else frozenset()


@cache
def _recipe_levels() -> dict[tuple[str, str], int]:
    levels: dict[tuple[str, str], int] = {}
    for r in catalogue():
        key = (r["skill"], r["output"])
        levels[key] = min(levels.get(key, 999), r["level"])
    return levels


def recipe_level(skill: str, output: str) -> int:
    """Lowest level any wiki recipe for `output` needs in `skill` (0 if unknown)."""
    return _recipe_levels().get((skill, output), 0)


def _recipe_seconds(r: dict, player: PlayerState | None = None) -> float:
    """Seconds per action: the recipe's ticks plus its share of a bank trip (mechanics/processing.py)."""
    from ..mechanics.processing import action_seconds

    return action_seconds(r, player)


def spirit_gem_charges(holdings: dict[str, int]) -> int:
    total = 0
    for name, count in holdings.items():
        base = name.removesuffix(" (a)")
        total += SPIRIT_GEM_CHARGES.get(base, 0) * count
    return total


# The Summoning charms spirit gems can save (others called "charm", like talon beast or obsidian
# charms, are pouch ingredients).
SUMMONING_CHARMS = {"Gold charm", "Green charm", "Crimson charm", "Blue charm"}


def _is_charm(name: str) -> bool:
    return name in SUMMONING_CHARMS


TOOL_BELT = {"Pestle and mortar", "Hammer", "Tinderbox", "Knife", "Chisel", "Saw", "Needle", "Machete", "Sieve",
             "Shears", "Spade", "Rake", "Secateurs", "Seed dibber", "Trowel", "Glassblowing pipe", "Specimen brush",
             "Bucket", "Rope", "Feather"}


def _usable(recipe: dict, player: PlayerState, levels: bool = True) -> bool:
    """Can the player use this recipe: members, tools, other skills' levels (unless `levels` is False:
    the gatherer costs those as unlocks) and quests. The recipe's own level is the caller's check."""
    if recipe["members"] and not player.members:
        return False
    # Skill-less conversions done with a special item (a Desert amulet) need one of those items; tool-belt
    # tools aren't exported, so they count as owned.
    if recipe.get("conversion") and recipe.get("tools") and not any(
            t in player.holdings or t in TOOL_BELT for t in recipe["tools"]):
        return False
    if levels and any(player.level(s) < lvl for s, lvl in recipe["other_skills"]):
        return False
    # Unknown quest titles (renamed pages, miniquests) don't block a recipe.
    return all(q not in player.quests or player.quests[q].done for q in recipe["quests"])


def bank_mix(player: PlayerState, skill: str, start_xp: float, xp_needed: float,
             rate_cap: Callable[[int], float], group: dict[str, dict[str, int]] | None = None,
             min_rate: Callable[[int], float] | None = None) -> BankMix:
    """Spend the player's (then the group's) items on `skill` recipes, up to `xp_needed` xp.

    Recipe rates come from their ticks and bank trips (mechanics/processing.py); `rate_cap(level)` caps the
    ones without ticks. Recipes slower than `min_rate(level)`
    (default MIN_RATE_SHARE of the cap) aren't worth the items they use up."""
    min_rate = min_rate or (lambda lvl: MIN_RATE_SHARE * rate_cap(lvl))
    own = player.holdings
    stock: dict[str, float] = dict(own)
    for mate in (group or {}).values():
        for name, n in mate.items():
            stock[name] = stock.get(name, 0) + n
    recipes = [effective(player, r) for r in catalogue()
               if r["skill"] == skill and not r.get("conversion") and not r.get("daemonheim") and _usable(r, player)]
    mix, xp, remaining = BankMix(recipient=player.name), start_xp, xp_needed
    gems = spirit_gem_charges(own) if skill == "Summoning" else 0
    prif = (q := player.quests.get("Plague's End")) is not None and q.done
    vos = {clan: match for s, match, clan in VOS_BOOSTS if s == skill} if prif else {}

    def xp_per_action(r: dict) -> float:
        # The Voice's bonus only applies while skilling in that clan's district of Prifddinas, so a
        # recipe made at the Shrine of Inanna (Havenhythe) can't have it.
        at_home = not r.get("shrine") and any(m(r["output"]) for m in vos.values())
        return r["xp"] * (VOS_MULTIPLIER if at_home else 1.0)

    def available(name: str) -> float:
        return stock.get(name, 0) + (gems if _is_charm(name) else 0)

    used_total: dict[str, float] = {}
    for _ in range(MAX_STEPS):
        if remaining <= 0:
            break
        level = level_at_xp(skill, xp)
        best = None
        for r in recipes:
            if r["level"] > level:
                continue
            can = min(math.floor(available(name) / qty + 1e-9) for name, qty in r["materials"])  # 90 / 0.9 = 100
            if can < 1:
                continue
            seconds = _recipe_seconds(r, player)
            rate = capped(xp_per_action(r) * 3600 / seconds, r, rate_cap(level))
            if rate < min_rate(level):
                continue
            key = (rate, xp_per_action(r))
            if best is None or key > best[0]:
                best = (key, r, int(can), rate)
        if best is None:
            break
        _, r, can, rate = best
        per = xp_per_action(r)
        actions = min(can, math.ceil(remaining / per))
        gained = actions * per
        if r.get("boost") and r["boost"] not in mix.boosts:
            mix.boosts.append(r["boost"])
        if per > r["xp"]:
            mix.vos_clan = next(c for c, m in vos.items() if m(r["output"]))
        used = {name: qty * actions for name, qty in r["materials"]}
        for name, q in used.items():
            take = min(q, stock.get(name, 0))
            if q > take:  # the rest are charms the spirit gems saved
                gems -= q - take
                mix.charms_saved += int(q - take)
            stock[name] = stock.get(name, 0) - take
            used_total[name] = used_total.get(name, 0) + take
        stock[r["output"]] = stock.get(r["output"], 0) + actions * r["output_qty"]
        mix.runs.append((r["output"], actions, actions * r["output_qty"], gained, used))
        mix.xp += gained
        mix.hours += gained / rate
        xp, remaining = xp + gained, remaining - gained
    # Whatever the player's own stock (plus what earlier steps made) didn't cover came from the group.
    made: dict[str, float] = {}
    for output, _, n, _, _ in mix.runs:
        made[output] = made.get(output, 0) + n
    for name, q in used_total.items():
        short = math.ceil(q) - own.get(name, 0) - made.get(name, 0)
        if short > 0:
            mix.transfers += _borrow(name, short, group or {})
    mix.stock = {name: int(n) for name, n in stock.items() if n >= 1}
    mix.used = used_total
    return mix
