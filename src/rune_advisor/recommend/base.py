"""Shared types for recommendation tracks.

Every track turns a PlayerState into Candidates with a track-specific `value` (QP, RuneScore,
milestone points...) and a common `cost_hours`. Within a track we rank by value per hour;
across tracks we normalise so each track's best candidate scores 1.0 before weighting.
"""

from __future__ import annotations

import dataclasses
import json
import math
from functools import cache
from .. import datapkg
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..cost import COMBAT_SKILLS, CONSTITUTION_SHARE, CostModel, row_skills, typical_rate
from ..drops import DropNote, kills_per_hour, notable_drops, player_needs
from ..drops import tables as drop_tables
from ..gear import best_style
from ..models import PlayerState
from ..boosts import STAFF_RUNES, best_urn, prayer_urn, urns
from ..mechanics.processing import capped, effective

# Workbench recipes whose own speed holds (not capped at the skill's rate): restoring is fast and fed by gathering,
# so the gathering, costed separately, is what limits it (Context._recipe_band_run).
UNCAPPED_FACILITIES = {"Archaeologist's workbench"}
from ..resources import LevelPlan, ResourcePool, plan_level
from ..sources.wiki import QuestInfo
from ..xp import level_at_xp, level_cap, xp_at
from .catalogue import MIN_RATE_SHARE, _recipe_seconds, _usable, bank_mix, catalogue, recipe_level
from .activities import Activities
from . import gathering
from .gathering import Gatherer
from .materials import MaterialRun, choose_run, material_run

@dataclass
class Banded:
    """Material runs over level bands (Context._banded_runs)."""
    hours: float = 0.0
    shortfall: dict[str, int] = field(default_factory=dict)
    used: dict[str, int] = field(default_factory=dict)
    covered: bool = True  # every band's materials are in stock
    any: bool = False  # at least one band has recipe data
    runs: list = field(default_factory=list)
    # Hours a consumable boost (a familiar, juju) took off bands that had no recipe method, against their plain
    # rate (Context._boosted_run): plans that keep the plain grind hours subtract it.
    boost_saving: float = 0.0
    # Hours a workbench recipe took off gathering bands (Context._recipe_band_run: restoring artefacts instead of
    # only excavating), against their plain rate: plans subtract it from their hours and grind.
    recipe_saving: float = 0.0

    @property
    def steps(self) -> list[str]:
        """Each band's run, with consecutive bands using the same method merged into one step."""
        merged: list = []
        for run in self.runs:
            last = merged[-1] if merged else None
            if last and last.method == run.method and last.output == run.output:
                last.actions += run.actions
                last.produced += run.produced
                totals = {n: (need, have) for n, need, have in last.needs}
                for n, need, have in run.needs:
                    pn, ph = totals.get(n, (0, 0))
                    totals[n] = (pn + need, ph + min(need, have))
                last.needs = [(n, need, have) for n, (need, have) in totals.items()]
                moved: dict[tuple[str, str], int] = {}
                for m, item, k in last.transfers + run.transfers:
                    moved[(m, item)] = moved.get((m, item), 0) + k
                last.transfers = [(m, item, k) for (m, item), k in moved.items()]
            else:
                merged.append(dataclasses.replace(run, needs=list(run.needs), transfers=list(run.transfers)))
        return [line for run in merged for line in run.describe()]


def p_ok(recipe: dict, player: PlayerState) -> bool:
    """A recipe the player can do now: its level and the catalogue's other requirements."""
    return player.level(recipe["skill"]) >= recipe["level"] and _usable(recipe, player)


# High Level Alchemy on drops (ironman coins): worth a cast from this value; 3 s per cast, 65 Magic xp.
MIN_ALCH_GP = 1_000
HIGH_ALCH_SECONDS = 3.0
HIGH_ALCH_XP = 65


@cache
def alch_values() -> dict[str, int]:
    path = datapkg.path("alch_values.json")
    return json.loads(path.read_text())["values"] if path.is_file() else {}


def drop_coins_per_kill(source: str, exclude: set[str] = frozenset()) -> tuple[float, float]:
    """(gp, alch casts) per kill of `source`: coin drops plus the high-alch value of drops worth MIN_ALCH_GP or
    more, leaving out `exclude` (items wanted for something else: other plans' needs, log uniques)."""
    values = alch_values()
    gp = casts = 0.0
    for item, chance, qty, _ in drop_tables()["sources"].get(source, []):
        if not isinstance(chance, (int, float)) or item in exclude:
            continue
        n = chance * (qty or 1)
        if item == "Coins":
            gp += n
        elif values.get(item, 0) >= MIN_ALCH_GP:
            gp, casts = gp + n * values[item], casts + chance
    return gp, casts


# A side option (a method whose drops feed another goal) is suggested if it's at least this fast.
SIDE_OPTION_SHARE = 0.75
# Added to every cost so near-free goals don't get infinite scores.
OVERHEAD_HOURS = 0.25


@dataclass
class Candidate:
    track: str
    title: str
    value: float
    cost_hours: float
    unit: str = ""
    details: list[str] = field(default_factory=list)
    # Nothing is truly unavailable: cost always includes the unlock chain. This flags that the
    # chain has requirements we don't model yet, so the hours are a lower bound.
    uncertain: bool = False
    # For the guide (recommend/guide.py): a one-line reason, the first unmet requirement of each part,
    # why the hours may be off, the skills it trains (mood tags) and how many of its hours are guesses.
    why: str = ""
    needs: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    skills: set[str] = field(default_factory=set)
    guessed_hours: float = 0.0
    # Hours actually spent playing. `cost_hours` is net of the xp the goal earns (lamps, quest rewards:
    # Plan.net_hours), which is right for ranking but not for filling a session: the lamp comes after.
    play_hours: float | None = None

    @property
    def efficiency(self) -> float:
        return self.value / (self.cost_hours + OVERHEAD_HOURS)


@dataclass
class Context:
    player: PlayerState
    cost: CostModel
    # Looks up wiki quest data by title; None disables requirement-aware quest costing.
    quest_info: Callable[[str], QuestInfo | None] | None = None
    # Lamps, penguin points, D&Ds... spent before grinding when they're cheaper.
    resources: ResourcePool | None = None
    # Wiki requirement tree by achievement name (see wiki.fetch_achievement_requirements), used
    # when the game only exposes a done/not-done flag for an achievement.
    achievement_reqs: dict[str, dict] | None = None
    # Names of members-only achievements (wiki), excluded when planning for free-to-play.
    members_only_achievements: set[str] = field(default_factory=set)
    # Farming patch assumptions from rune_advisor.toml ([farming."Herb patch"] ...), see gathering.PATCHES.
    farming: dict[str, dict] = field(default_factory=dict)
    # Usefulness weights per skill from rune_advisor.toml ([skill_weights]; 1.0 when unset): the free_xp track
    # multiplies the hours a lamp saves in a skill by them when choosing where it goes.
    skill_weights: dict[str, float] = field(default_factory=dict)
    # rune_advisor.toml's bonus_xp_skill: the skill bonus xp that must be placed now goes to (free_xp).
    bonus_xp_skill: str | None = None
    # Hours per xp that other goals' own plans take (set by joint plans/roadmaps): a method's side xp
    # in these skills counts against its cost at that rate (side_value_per_xp).
    wanted: dict[str, float] = field(default_factory=dict)
    side_options: bool = True
    # GE prices (prices.Prices) for a normal account's bought supplies: their gp counts as time at
    # gathering.COINS_PER_HOUR. None (tests, offline, ironmen) leaves bought supplies uncosted.
    prices: object | None = None
    unique_value_hours: float = 0.0  # rune_advisor.toml unique_value_hours: what a missing log unique is worth
    prefer_exquisite_urns: bool = False  # rune_advisor.toml: exquisite urns whenever they can be made, cost aside
    _urn_options: dict = field(default_factory=dict, repr=False)
    _urn_xp: dict = field(default_factory=dict, repr=False)
    urns: bool = True  # carry urns on the skills that have them (boosts.best_urn)
    _needs: dict[str, str] | None = field(default=None, repr=False)
    _finding_needs: bool = field(default=False, repr=False)
    _gatherer: Gatherer | None = field(default=None, repr=False)

    # Wiki achievement descriptions by name ("Cleanse the Gate of Elidinis. (X/5)"), for tally costs.
    achievement_descriptions: dict[str, str] = field(default_factory=dict)
    _combat_style: tuple[str, float] | None = field(default=None, repr=False)
    _category_priors: dict[str, tuple[float, int]] | None = field(default=None, repr=False)
    _activities: Activities | None = field(default=None, repr=False)

    @property
    def activities(self) -> Activities:
        if self._activities is None:
            self._activities = Activities(self)
        return self._activities

    @property
    def combat_style(self) -> tuple[str, float]:
        """(skill, xp/h) of the player's best combat style with their own gear, for monsters the
        training guides don't cover."""
        if self._combat_style is None:
            _style, skill, rate = best_style(self.player, self.cost.baseline_rate)
            self._combat_style = (skill, rate)
        return self._combat_style

    def side_value_per_xp(self, skill: str) -> float:
        """Hours one xp in `skill` saves: what that goal's own plan spends per xp (0 if no goal wants it)."""
        return self.wanted.get(skill, 0.0)

    @property
    def gatherer(self) -> Gatherer:
        if self._gatherer is None:
            self._gatherer = Gatherer(self)
        return self._gatherer

    def needs(self) -> dict[str, str]:
        """Items worth getting as drops (other goals' shortfalls, Summoning charms), from plans to 99."""
        if self._needs is None:
            self._finding_needs = True  # plans made while finding needs don't look for side options
            p = self.player
            try:
                plans = {s: self.level_plan(s, min(99, level_cap(s, p.members))) for s in p.skills
                         if s != "Hitpoints" and p.level(s) < min(99, level_cap(s, p.members))}
            finally:
                self._finding_needs = False
            self._needs = player_needs(plans, p)
        return self._needs

    def method_drops(self, skill: str, level: int) -> list[tuple[dict, float, list[DropNote]]]:
        """Available wiki methods at a level, fastest first, with the drops that matter to this player."""
        needs = self.needs()
        return [(m, typical_rate(m), notable_drops(m, skill, typical_rate(m), self.player, needs))
                for m in self.cost.available_methods(skill, level)]

    def side_option(self, skill: str, level: int) -> str | None:
        """For an ironman: a method nearly as fast as the best one whose drops feed another goal."""
        if not self.player.ironman or self._finding_needs or not self.side_options:
            return None
        options = self.method_drops(skill, level)
        if not options:
            return None
        best_rate = options[0][1]
        for m, rate, drops in options:
            useful = [d for d in drops if d.kind in ("need", "charm")] or [d for d in drops if d.worth_chasing]
            if useful and rate >= SIDE_OPTION_SHARE * best_rate:
                pct = "" if rate >= best_rate else f", {1 - rate / best_rate:.0%} slower"
                return (f"side option: {m['method']} (~{rate:,.0f} xp/h{pct}) also drops "
                        + "; ".join(d.describe() for d in useful[:3]))
        return None

    def level_plan(self, skill: str, target_level: int) -> LevelPlan:
        p = self.player
        target_xp = xp_at(skill, target_level)
        if skill == "Hitpoints" and skill not in self.cost.overridden:
            return self._constitution_plan(target_xp)
        urn = self._urn(skill, target_xp - p.xp(skill)) if self.urns else None
        plan = plan_level(self.cost, self.resources, skill, p.xp(skill), p.level(skill), target_xp,
                          p.bonus_xp.get(skill, 0), use_bonus=p.members, urn=urn)
        urn_items = dict(plan.items_used)  # urn runes; _apply_materials replaces items_used
        if plan.urn_xp and urn and urn.prereq:
            plan.steps.append(urn.prereq)
        if self.urns and plan.grind_hours and not (urn and urn.prereq) and (note := self._exquisite_note(skill)):
            plan.steps.append(note)
        self._apply_materials(plan, skill, target_xp)
        for k, v in urn_items.items():
            plan.items_used[k] = max(plan.items_used.get(k, 0), v) if k in plan.items_used else v
        # Methods that also train other skills (a [[methods]] entry's side_xp) give that xp along the way.
        methods = self.cost.available_methods(skill, p.level(skill))
        if methods and methods[0].get("side_xp") and plan.grind_hours:
            for s, v in methods[0]["side_xp"].items():
                plan.byproduct_xp[s] = plan.byproduct_xp.get(s, 0) + v * plan.grind_hours
            plan.steps.append(f"{methods[0]['method']} also gives " + ", ".join(
                f"~{v * plan.grind_hours:,.0f} {s} xp" for s, v in methods[0]["side_xp"].items()))
        # Prayer urns fill with the ashes of the method's kills (demons): Prayer xp on the side.
        top = self._top_method(skill)
        if top and plan.grind_hours and self.urns and (pu := prayer_urn(p, top, skill, typical_rate(top))):
            n = pu.urns_per_hour * plan.grind_hours
            xp = pu.prayer_xp_per_hour * plan.grind_hours
            plan.byproduct_xp["Prayer"] = plan.byproduct_xp.get("Prayer", 0) + xp
            plan.hours += n * pu.hours_each
            if pu.rune:
                plan.items_used[pu.rune] = plan.items_used.get(pu.rune, 0) + math.ceil(n)
            plan.steps.append(f"carry {math.ceil(n):,} {pu.name}s: +{xp:,.0f} Prayer xp from the kills' ashes")
        if plan.hours and (side := self.side_option(skill, p.level(skill))):
            plan.steps.append(side)
        return plan

    def _top_method(self, skill: str) -> dict | None:
        """The fastest method at the player's level, with its monsters named: guide rows don't always link
        them, so the method's name does ("Abyssal demons (lords)" -> Abyssal demon, not its variants)."""
        methods = self.cost.available_methods(skill, self.player.level(skill))
        top = methods[0] if methods else None
        if top and not top.get("drop_sources"):
            named = sorted(self.activities.named_sources(top["method"], need_verb=False))
            top = {**top, "drop_sources": [n for n in named if " (" not in n] or named}
        return top

    def alch_income(self, skill: str, hours: float) -> tuple[float, int]:
        """(gp, casts) from High Level Alchemy on what `hours` of the skill's top method drops (kills only):
        items worth at least MIN_ALCH_GP each that no plan needs, plus coin drops (no cast)."""
        top = self._top_method(skill)
        kph = kills_per_hour(top, skill, typical_rate(top)) if top and hours > 0 else None
        if not kph:
            return 0.0, 0
        needs = self.needs() if not self._finding_needs else {}
        best = max((drop_coins_per_kill(src, set(needs)) for src in top.get("drop_sources") or []), default=(0.0, 0.0))
        return best[0] * kph * hours, int(best[1] * kph * hours)

    def _exquisite_note(self, skill: str) -> str | None:
        """When the player's Crafting is short of the skill's exquisite urn: what reaching it would pay."""
        p = self.player
        rows = [u for u in urns() if u["exquisite"] and (u["skill"] == skill or u["skill"] == "Runecrafting"
                                                          and skill == "Runecraft") and u["crafting"] > p.level("Crafting")]
        if not rows or not p.members:
            return None
        u = min(rows, key=lambda u: u["crafting"])
        regular = best_urn(p, skill, p.level(skill))
        now = f"{regular.share:.0%} from {regular.name}s" if regular else "no urn"
        return (f"at {u['crafting']} Crafting (have {p.level('Crafting')}) {u['name']}s would pay "
                f"{u['payout_xp'] / u['fill_xp']:.0%} of trained xp (now {now})")

    def _urn(self, skill: str, trained_xp: float = 0.0):
        """The urn worth carrying: the best regular one, or an exquisite one when its extra payout beats the
        time gathering its porcelain clay and gem takes (both per trained xp, at the skill's rate). Below the
        exquisite urn's Crafting level, the Crafting training it needs is spread over the urns this grind
        uses (`trained_xp`) and the urn says so (`prereq`)."""
        p = self.player
        regular = best_urn(p, skill, p.level(skill))
        if not p.ironman or not p.members:
            return regular or best_urn(p, skill, p.level(skill), exquisite=True)
        if regular is not None:
            regular.material_xp = self._urn_material_xp(regular.name)
        option = self._exquisite_option(skill)
        if option is None:
            return regular
        fancy, each, prereq_hours, prereq, boost_each, boost_text = option
        fancy = dataclasses.replace(fancy, hours_each=each, material_xp=self._urn_material_xp(fancy.name))
        if prereq_hours:
            n = fancy.count(trained_xp / (1 + fancy.share))
            if not n:
                return regular
            if boost_each is not None and boost_each * n < prereq_hours:
                # Boosting Crafting while making the urns beats training to their level.
                fancy.hours_each += boost_each
                fancy.prereq = f"{boost_text} for the {n:,} {fancy.name}s, instead of training (~{prereq_hours:.1f}h)"
            else:
                fancy.hours_each += prereq_hours / n
                fancy.prereq = prereq
        if self.prefer_exquisite_urns:
            return fancy  # the player's choice: exquisite urns, getting their Crafting level first if needed
        rate = max(1.0, self.cost.rate(skill, p.level(skill)))

        def worth(u):
            """Extra xp per trained xp, less the making time (as xp at the skill's rate), plus what the inputs'
            xp is worth: this skill's counts as trained xp, other skills' at what the goals wanting them spend."""
            own = u.material_xp.get(skill, 0.0) * (1 + u.share)
            side = sum(v * self.side_value_per_xp(s) * rate for s, v in u.material_xp.items() if s != skill)
            return u.share - (u.hours_each * rate - own - side) / u.fill_xp

        return max((u for u in (regular, fancy) if u), key=worth)

    def _exquisite_option(self, skill: str):
        """(urn, hours to make one, Crafting hours it needs first, step text) for the skill's exquisite urn, cached
        per skill and levels (level plans run thousands of times)."""
        p = self.player
        key = (skill, p.level(skill), p.level("Crafting"))
        if key in self._urn_options:
            return self._urn_options[key]
        option = None
        ctx, prereq_hours, prereq, boost_each, boost_text = self, 0.0, None, None, None
        fancy = best_urn(p, skill, p.level(skill), exquisite=True)
        if fancy is None and "Crafting" in p.skills:
            rows = [u for u in urns() if u["exquisite"] and u["crafting"] > p.level("Crafting") and
                    (u["skill"] == skill or u["skill"] == "Runecrafting" and skill == "Runecraft")]
            if rows:
                need = min(u["crafting"] for u in rows)
                self._urn_options[key] = None  # guards the Crafting plan below against re-entry
                prereq_hours = self.level_plan("Crafting", need).hours
                raised = dataclasses.replace(p, skills={**p.skills, "Crafting": dataclasses.replace(
                    p.skills["Crafting"], level=need, xp=max(p.xp("Crafting"), xp_at("Crafting", need)))})
                ctx = dataclasses.replace(self, player=raised, _gatherer=None, _needs={}, _activities=None,
                                          _urn_options={}, _urn_xp={})
                fancy = best_urn(raised, skill, p.level(skill), exquisite=True)
                prereq = f"get {need} Crafting first for {fancy.name}s (~{prereq_hours:.1f}h, counted in the urns)" \
                    if fancy else None
                if fancy is not None:
                    boost_each, boost_text = self._urn_boost(fancy.name, need)
        if fancy is not None:
            each = next((h for n in (f"{fancy.name} (empty)", f"{fancy.name} (no rune)")
                         if (h := ctx._urn_chain_hours(n, fancy.name)) is not None), None)
            if each is not None:
                option = (fancy, each, prereq_hours, prereq, boost_each, boost_text)
        self._urn_options[key] = option
        return option

    def _urn_boost(self, name: str, need: int, batch: int = 100) -> tuple[float | None, str | None]:
        """(hours per urn, step text) for boosting Crafting to `need` while making `name`s, when the wiki marks
        their Crafting steps boostable and a boost reaches it (level_boosts). Costed over `batch` urns, since a
        dose covers many."""
        from ..level_boosts import plan_boost

        steps = [r for stage in ("(unfired)", "(no gem)", "(no rune)") for r in catalogue()
                 if r["output"] == f"{name} {stage}" and r["skill"] == "Crafting" and r["level"] >= need]
        if not steps or not all("Crafting" in r.get("boostable", []) for r in steps):
            return None, None
        seconds = sum(min(_recipe_seconds(r, self.player) for r in steps if r["output"] == out)
                      for out in {r["output"] for r in steps})
        boost = plan_boost(self.gatherer, "Crafting", need, batch * seconds / 3600)
        if boost is None:
            return None, None
        if boost.doses:
            how = f"{boost.boost} (~1 dose per {batch / boost.doses:,.0f} urns)"
        else:
            how = f"the banner (~{batch * 24 / boost.calendar_hours:,.0f} urns per daily session)"
        return boost.hours / batch, f"boost Crafting to {boost.level} with {how}"

    def _urn_material_xp(self, name: str) -> dict[str, float]:
        """Xp per urn from getting its inputs: the gatherer's xp for each input that isn't an urn stage (porcelain
        clay mined, the gem mined and cut), followed down the chain like _urn_chain_hours (cached per urn)."""
        if name in self._urn_xp:
            return self._urn_xp[name]
        self._urn_xp[name] = {}
        xp: dict[str, float] = {}

        def walk(item: str, times: float, depth: int) -> None:
            recipes = [r for r in catalogue() if r["output"] == item]
            if not recipes or depth > 6:
                return
            r = min(recipes, key=lambda r: len(r["materials"]))
            per = times / (r["output_qty"] or 1)
            for m, q in r["materials"]:
                if m.startswith(name):
                    walk(m, per * q, depth + 1)
                elif m not in STAFF_RUNES and (src := self.gatherer.complete_source(m)):
                    for s, v in src.xp_per_unit.items():
                        xp[s] = xp.get(s, 0.0) + per * q * v
        top = next((n for n in (f"{name} (empty)", f"{name} (no rune)", name)
                    if any(r["output"] == n for r in catalogue())), None)
        if top:
            walk(top, 1.0, 0)
        self._urn_xp[name] = {s: v for s, v in xp.items() if v > 0}
        return self._urn_xp[name]

    def _urn_chain_hours(self, item: str, family: str, depth: int = 0) -> float | None:
        """Hours to make one `item` of an urn's chain (unfired -> no gem -> no rune -> empty): each step's
        recipe time, with the steps' own outputs followed down and other inputs (porcelain clay, the gem, runes
        a staff doesn't cover) from the gatherer. None if a step or input can't be made."""
        if depth > 6:
            return None
        recipes = [r for r in catalogue() if r["output"] == item and p_ok(r, self.player)]
        if not recipes:
            return None
        r = min(recipes, key=lambda r: len(r["materials"]))  # the plain step (no rune variants)
        hours = _recipe_seconds(r, self.player) / 3600
        for m, q in r["materials"]:
            if m.startswith(family):
                sub = self._urn_chain_hours(m, family, depth + 1)
            elif m in STAFF_RUNES:
                continue
            else:
                src = self.gatherer.complete_source(m)
                sub = src.cost_per_unit if src else None
            if sub is None:
                return None
            hours += q * sub
        return hours / (r["output_qty"] or 1)

    def _banded_runs(self, skill: str, from_xp: float, xp: float, stock: dict[str, int],
                     group: dict[str, dict[str, int]] | None) -> Banded:
        """Material runs for `xp` of training from `from_xp`, split where the available guide methods
        change (a method that starts at level 10 isn't used from level 1). Each band takes the fastest
        method the stock covers, else the fastest method (whose materials become a shortfall)."""
        rows = self.cost.wiki_rows.get(skill, [])
        lo, hi = level_at_xp(skill, from_xp), level_at_xp(skill, from_xp + xp)
        cuts = sorted({r["level_min"] for r in rows if lo < r["level_min"] <= hi}
                      | {r["level_max"] + 1 for r in rows if r.get("level_max") and lo < r["level_max"] + 1 <= hi})
        bounds = [from_xp] + [xp_at(skill, c) for c in cuts] + [from_xp + xp]
        out, stock = Banded(), dict(stock)
        for a, b in zip(bounds, bounds[1:]):
            seg = b - a
            if seg <= 0:
                continue
            level = level_at_xp(skill, a)
            # Guide rows sometimes start below the recipe's real level (spinning flax needs 10 Crafting).
            methods = [m for m in self.cost.available_methods(skill, level)
                       if not m.get("recipe") or recipe_level(skill, m["recipe"]["output"]) <= level]
            run = choose_run(methods, seg, stock, group, self.player.name, self.player.members)
            if self.player.ironman and run is not None and not run.covered:
                run = self._cheapest_to_gather(methods, seg, stock, group) or run
            plain_hours = None
            if run is None and self.player.ironman and \
                    (made := self._recipe_band_run(methods, seg, stock, group,
                                                   plain_hours := self.cost.hours_for_xp(skill, int(a), int(b)))):
                # A gathering band where a workbench recipe fed by gathering (restoring artefacts dug up and
                # materials from caches) beats gathering alone: the band is that recipe; its inputs are gathered
                # (Context._gather, which also counts the gathering's xp in this skill toward the goal).
                run = made
                out.recipe_saving += plain_hours - seg / run.rate
            if run is None and (twins := self.cost.boosted_methods(skill, level)):
                # A gathering band (no recipe): a familiar or juju kept up, where it beats the plain rate with its
                # consumables costed.
                plain = self.cost.hours_for_xp(skill, int(a), int(b))
                if (boosted := self._boosted_run(twins, seg, stock, group, plain)) is not None:
                    run = boosted
                    out.boost_saving += plain - seg / run.rate
            if run is None or not (run.covered or run.method == methods[0]["method"] or self.player.ironman):
                out.hours += self.cost.hours_for_xp(skill, int(a), int(b))
                out.covered = False
                continue
            out.any = True
            out.covered &= run.covered
            out.hours += seg / run.rate if run.covered else self.cost.hours_for_xp(skill, int(a), int(b))
            run.from_level = level if a > from_xp else 0
            out.runs.append(run)
            for name, need, have in run.needs:
                take = min(need, stock.get(name, 0))
                stock[name] = stock.get(name, 0) - take
                out.used[name] = out.used.get(name, 0) + take
            for name, n in run.shortfall.items():
                out.shortfall[name] = out.shortfall.get(name, 0) + n
        return out

    def _cheapest_to_gather(self, methods: list[dict], xp: float, stock: dict[str, int],
                            group: dict[str, dict[str, int]] | None) -> MaterialRun | None:
        """For an ironman: the method whose run, counting the time to gather what the stock lacks, is
        cheapest (the fastest guide method can need a million dart tips)."""
        best = None
        skill = row_skills(methods[0])[0] if methods else ""
        for m in methods + self._recipe_methods(methods, xp):
            run = material_run(m, xp, stock, group, self.player.name, self.player.members)
            if run is None:
                continue
            hours, same_skill = xp / run.rate, 0.0
            for name, n in run.shortfall.items():
                src = self.gatherer.complete_source(name)
                # Making the inputs with this skill (unfinished potions, supers for extremes) trains it too.
                same_skill += n * src.xp_per_unit.get(skill, 0) if src else 0
            # Net of by-products (side xp other goals want, reputation still needed); a capped passive supply
            # only for what it brings in (Gatherer.bulk_cost). A spot's whole catch is costed as one visit.
            hours += self._shortfall_hours(m, run.shortfall)
            per_xp = hours / (xp + same_skill)
            if best is None or per_xp < best[0]:
                best = (per_xp, run)
        return best[1] if best and best[0] < float("inf") else None

    def _shortfall_hours(self, method: dict, shortfall: dict[str, int]) -> float:
        """Hours to gather a run's shortfall. For a catch mix (`spot`: everything one multi-yield spot brings in, each
        processed), the cheaper of the items' own sources and that spot's visit, whose hours cover every item at once
        (the longest need sets it); per item, each would be charged the whole visit."""
        each = sum(self.gatherer.bulk_cost(name, n) for name, n in shortfall.items())
        spot = method.get("spot")
        if not spot or not shortfall:
            return each
        per_hour = self.gatherer.multi_yield_spots().get(spot, {})
        if any(not per_hour.get(name) for name in shortfall):
            return each
        visit = max(n / per_hour[name] for name, n in shortfall.items()) * self.gatherer.spot_hours(spot)
        return min(each, visit)

    def _recipe_band_run(self, methods: list[dict], xp: float, stock: dict[str, int],
                         group: dict[str, dict[str, int]] | None, plain_hours: float) -> MaterialRun | None:
        """For a band with no recipe method (gathering: Archaeology's excavation): the workbench recipe whose run,
        with what the stock lacks gathered, beats `plain_hours`. The gathering's xp in this skill counts (excavating
        the artefacts is Archaeology too), as Context._gather will."""
        if not methods:
            return None
        skill = row_skills(methods[0])[0]
        best = None
        for m in self._recipe_methods(methods, xp):
            if not m.get("uncapped"):
                continue  # only recipes whose speed is their own (others are capped at the band's gathering rate)
            run = material_run(m, xp, stock, group, self.player.name, self.player.members)
            if run is None:
                continue
            hours, same = xp / run.rate, 0.0
            for name, n in run.shortfall.items():
                src = self.gatherer.complete_source(name)
                hours += self.gatherer.bulk_cost(name, n)
                same += n * src.xp_per_unit.get(skill, 0) if src else 0
            hours *= xp / (xp + same)
            if hours < plain_hours and (best is None or hours < best[0]):
                best = (hours, run)
        return best[1] if best else None

    def _boosted_run(self, twins: list[dict], xp: float, stock: dict[str, int],
                     group: dict[str, dict[str, int]] | None, plain_hours: float) -> MaterialRun | None:
        """The consumable-boost twin (consumables.boosted_rows) that gets `xp` cheapest, counting the time to make what
        the stock lacks net of its by-products (making pouches is Summoning xp other goals may want), or None
        when none beats `plain_hours`. Consumables already held are used first and cost nothing."""
        best = None
        for m in twins:
            run = material_run(m, xp, stock, group, self.player.name, self.player.members)
            if run is None:
                continue
            hours = xp / run.rate
            for name, n in run.shortfall.items():
                hours += self.gatherer.bulk_cost(name, n)
            if hours < plain_hours and (best is None or hours < best[0]):
                best = (hours, run)
        return best[1] if best else None

    def _recipe_methods(self, methods: list[dict], xp: float) -> list[dict]:
        """Every catalogue recipe for the skill at this level, as pseudo guide rows (speed from ticks,
        capped at the skill's rate), so an ironman's method can be any recipe that's cheap to gather for."""
        if not methods:
            return []
        skill = row_skills(methods[0])[0]
        level = max(m["level_min"] for m in methods)
        level = self.player.level(skill) if level < self.player.level(skill) else level
        cap = self.cost.rate(skill, level)
        rows = []
        extra = []
        single: dict[str, tuple[float, float]] = {}  # input -> (this skill's xp per unit, xp/h) of its best recipe
        if skill == "Archaeology":  # restoring what each hotspot digs up, mixed as it's found
            from ..mechanics.archaeology import restoration_mixes
            extra = restoration_mixes(level)
        for r in catalogue() + extra:
            if r["skill"] != skill or r.get("conversion") or r.get("daemonheim") or r["level"] > level \
                    or not _usable(r, self.player):  # Daemonheim recipes: inside a dungeon only, no training
                continue
            r = effective(self.player, r)
            free = r.get("facility") in UNCAPPED_FACILITIES
            rate = r["xp"] * 3600 / _recipe_seconds(r, self.player)
            rate = rate if free else capped(rate, r, cap)
            if rate < MIN_RATE_SHARE * self.cost.baseline_rate(skill, level):
                continue
            label = (f"restore what {r['mix']} digs up (workbench)" if r.get("mix")
                     else f"{r['output']} (wiki recipe{', ' + r['boost'] if r.get('boost') else ''})")
            rows.append({"skill": skill, "method": label, "level_min": r["level"],
                         "xp_per_hour_low": rate, "xp_per_hour_high": None, **({"uncapped": True} if free else {}),
                         "recipe": {"output": r["output"], "variants": [
                             {"output_qty": 1, "xp": r["xp"] / r["output_qty"], "ticks": r["ticks"],
                              "materials": [[n, q / r["output_qty"]] for n, q in r["materials"]]}]}})
            if len(r["materials"]) == 1:  # one input, one at a time (cooking a fish): a catch mix can use it
                (name, q), = r["materials"]
                per = (r["xp"] / q, rate)
                if per[0] > single.get(name, (0, 0))[0]:
                    single[name] = per
        if self.player.ironman:
            rows += self._catch_mixes(skill, single)
        return rows

    def _catch_mixes(self, skill: str, single: dict[str, tuple[float, float]]) -> list[dict]:
        """A recipe per multi-yield spot whose items this skill processes (the fish farm's four fish, all cooked):
        one action is one average item of the spot's catch, so the catch is costed as one visit
        (Context._shortfall_hours) instead of each item paying for the whole visit."""
        rows = []
        for spot, per_hour in self.gatherer.multi_yield_spots().items():
            used = {i: n for i, n in per_hour.items() if i in single and n > 0}
            if len(used) < 2:
                continue
            total = sum(used.values())
            xp = sum(n * single[i][0] for i, n in used.items()) / total
            hours = sum(n * single[i][0] / single[i][1] for i, n in used.items()) / total
            # Processed where the skill usually is (a range by a bank: the recipes' own bank trips), not at the spot;
            # the catch comes back noted or banked (the fish farm's Maya notes raw fish).
            rows.append({"skill": skill, "method": f"take {spot}'s catch to the bank and process it there"
                                                   f" ({', '.join(used)})",
                         "level_min": 1, "xp_per_hour_low": xp / hours, "xp_per_hour_high": None, "spot": spot,
                         "recipe": {"output": f"processed fish from {spot}" if all(i.startswith("Raw ") for i in used)
                                    else f"processed catch of {spot}", "variants": [
                             {"output_qty": 1, "xp": xp, "ticks": hours * 6000,
                              "materials": [[i, n / total] for i, n in used.items()]}]}})
        return rows

    def _apply_materials(self, plan: LevelPlan, skill: str, target_xp: int) -> None:
        """Make the grind concrete: which recipe, how many, from what's in the bank."""
        start_xp = self.player.xp(skill) + plan.resource_xp
        # Bonus xp and urns come on top of each action, so only the trained part needs materials.
        grind_xp = target_xp - start_xp - plan.bonus_xp - plan.urn_xp
        if grind_xp <= 0:
            return
        p = self.player
        methods = self.cost.available_methods(skill, level_at_xp(skill, start_xp))
        # An ironman gathers whatever a recipe-less method consumes: we can't say how long that takes.
        if p.ironman and methods and not methods[0].get("recipe") and (
                methods[0].get("bought_supplies") or any(r["skill"] == skill for r in catalogue())):
            plan.materials_unmodelled = True
        banded = self._banded_runs(skill, start_xp, grind_xp, p.holdings, p.group)
        steps = [s for s in plan.steps if not s.startswith(f"then train {skill}")]
        if banded.any and banded.covered:
            plan.hours += banded.hours - plan.grind_hours  # bank-covered methods may be slower
            plan.steps, plan.items_used = steps + banded.steps, banded.used
            return
        # Not all covered. An ironman can't buy the fastest methods' supplies, so use what the bank
        # can make first, then the guide's methods; anyone else buys them.
        if not p.ironman:
            if banded.any:
                plan.steps, plan.materials_short = steps + banded.steps, banded.shortfall
                plan.hours -= banded.boost_saving
                self._buy(plan)
            return
        mix = bank_mix(p, skill, start_xp, grind_xp, lambda lvl: self.cost.rate(skill, lvl), p.group,
                       min_rate=lambda lvl: MIN_RATE_SHARE * self.cost.baseline_rate(skill, lvl))
        rest = grind_xp - mix.xp
        plan.items_used = {k: math.ceil(v) for k, v in mix.used.items()}
        stock = mix.stock if mix.xp else p.holdings
        rest_runs = self._banded_runs(skill, start_xp + mix.xp, rest, stock, None if mix.xp else p.group) \
            if rest > 0 else Banded(covered=True)
        if mix.xp:
            plan.hours += mix.hours + rest_runs.hours - plan.grind_hours
            steps += mix.describe(skill)
        if rest_runs.any:
            steps += rest_runs.steps
            if not mix.xp:  # with a bank mix, the hours above already come from the runs
                plan.hours -= rest_runs.boost_saving
            plan.hours -= rest_runs.recipe_saving  # restoring at the workbench, not excavating, for those bands
            plan.grind_hours -= rest_runs.recipe_saving
            plan.materials_short = rest_runs.shortfall
            for name, n in rest_runs.used.items():
                plan.items_used[name] = plan.items_used.get(name, 0) + n
        elif rest > 0 and mix.xp:
            plan.materials_unmodelled = True
            steps.append(f"then ~{rest:,.0f} more {skill} xp beyond what the bank covers "
                         f"(~{self.cost.hours_for_xp(skill, int(target_xp - rest), target_xp):.1f}h; materials not modelled)")
        # Only skills with no guide method at all here (Summoning) fall back to the cheapest recipe; a guide
        # method without recipe data (Abyss Surge: runes) is faster than any recipe, just not costed.
        no_guide = not self.cost.available_methods(skill, level_at_xp(skill, start_xp + mix.xp))
        if rest > 0 and not rest_runs.any and no_guide and \
                self._recipe_rest(plan, skill, start_xp + mix.xp, rest, mix.stock, steps):
            steps = plan.steps
        if mix.xp or rest_runs.any:
            plan.steps = steps
        self._gather(plan, mix.stock, skill, grind_xp)

    def _buy(self, plan: LevelPlan) -> None:
        """A normal account gets what the bank lacks the cheaper way, per item: buy it (the GE price as time at
        the money-making rate) or gather it like an ironman would (gathering.Gatherer)."""
        if self.prices is None or not plan.materials_short:
            return
        gp, bought, gathered, unpriced = 0.0, 0, [], []
        for item, n in plan.materials_short.items():
            v = self.prices.value(item)
            src = self.gatherer.complete_source(item)
            buy_h = v.gp * n / gathering.COINS_PER_HOUR if v else None
            gather_h = n * self.gatherer.net_cost(src) if src else None
            if gather_h is not None and (buy_h is None or gather_h < buy_h):
                plan.hours += gather_h
                gathered.append(f"{n:,} {item}: {src.how} (~{gather_h:.1f}h, cheaper than buying)")
            elif buy_h is not None:
                gp += v.gp * n
                bought += 1
            else:
                unpriced.append(item)
        hours = gp / gathering.COINS_PER_HOUR
        plan.hours += hours
        plan.gather_coins += gp
        if bought or unpriced:
            plan.steps.append(f"buy the rest on the GE: ~{gp / 1e6:,.1f}m gp (~{hours:.1f}h at "
                              f"{gathering.COINS_PER_HOUR / 1e6:g}m gp/h)"
                              + (f"; no price or source for {', '.join(unpriced[:3])}" if unpriced else ""))
        plan.steps += [f"gather {g}" for g in gathered]

    def _recipe_rest(self, plan: LevelPlan, skill: str, from_xp: float, rest: float, stock: dict[str, int],
                     steps: list[str]) -> bool:
        """No guide method for the xp the bank can't cover (Summoning): pick the recipe that's cheapest to
        gather for, and plan its materials as a shortfall."""
        level = level_at_xp(skill, from_xp)
        g = self.gatherer
        best = None
        for r in catalogue():
            if r["skill"] != skill or r.get("conversion") or r.get("daemonheim") or r["level"] > level \
                    or not _usable(r, self.player):  # Daemonheim recipes: inside a dungeon only, no training
                continue
            r = effective(self.player, r)
            srcs = [g.complete_source(m) for m, _ in r["materials"]]
            if any(s is None for s in srcs):
                continue
            unit = _recipe_seconds(r, self.player) / 3600 + sum(q * s.cost_per_unit for (_, q), s in zip(r["materials"], srcs))
            if best is None or unit / r["xp"] < best[0]:
                best = (unit / r["xp"], r)
        if best is None:
            return False
        r = best[1]
        actions = math.ceil(rest / r["xp"])
        need = {m: math.ceil(q * actions) - stock.get(m, 0) for m, q in r["materials"]}
        for m, q in r["materials"]:
            plan.items_used[m] = plan.items_used.get(m, 0) + min(math.ceil(q * actions), stock.get(m, 0))
        plan.hours += actions * _recipe_seconds(r, self.player) / 3600 - self.cost.hours_for_xp(skill, int(from_xp), int(from_xp + rest))
        plan.steps = [s for s in steps if not s.startswith(f"then ~{rest:,.0f} more")] + [
            f"then make {actions:,} {r['output']} for the last ~{rest:,.0f} xp (cheapest to gather for)",
            *([r["boost"]] if r.get("boost") else [])]
        plan.materials_short = {m: n for m, n in need.items() if n > 0}
        plan.materials_unmodelled = False
        return True

    def _gather(self, plan: LevelPlan, stock: dict[str, int] | None = None, skill: str = "",
                grind_xp: float = 0.0) -> None:
        """For an ironman: cost the materials a plan is short of (shops, drops, crafting).

        Crafting the inputs can train the plan's own skill (supers on the way to extremes): that xp
        counts toward the goal, so the whole run (crafting and gathering) shrinks to fit."""
        if not self.player.ironman or not plan.materials_short:
            return
        g = self.gatherer.plan(plan.materials_short, stock, training=skill)
        same = g.xp.pop(skill, 0.0) if skill else 0.0
        if same > 0 and grind_xp > 0:
            keep = grind_xp / (grind_xp + same)
            plan.hours -= (1 - keep) * plan.grind_hours
            g.hours *= keep
            g.coins *= keep
            g.calendar_hours *= keep
            g.xp = {k: v * keep for k, v in g.xp.items()}
            g.steps.append(f"making the inputs also gives ~{same:,.0f} {skill} xp, so only ~{keep:.0%} of the above "
                           f"is needed")
        plan.hours += g.hours
        short, transfers = self.cover_coins(g.coins)
        g.steps += transfers
        if short > 0:
            # Combat training's drops, alched on the way, pay for part of it.
            alch_gp, casts = self.alch_income(skill, plan.grind_hours) if skill else (0.0, 0)
            used = min(short, alch_gp)
            if used > 0:
                casts = int(casts * used / alch_gp)
                plan.hours += casts * HIGH_ALCH_SECONDS / 3600
                plan.items_used["Nature rune"] = plan.items_used.get("Nature rune", 0) + casts
                plan.byproduct_xp["Magic"] = plan.byproduct_xp.get("Magic", 0) + casts * HIGH_ALCH_XP
                g.steps.append(f"high-alch the training's drops: ~{used:,.0f} gp ({casts:,} casts, "
                               f"~{casts * HIGH_ALCH_SECONDS / 3600:.1f}h, {casts:,} nature runes)")
                short -= used
                plan.alch_gp += used
            if short > 0:
                extra = short / gathering.COINS_PER_HOUR
                plan.hours += extra
                g.steps.append(f"earn ~{short:,.0f} more gp for the shops (~{extra:.1f}h at "
                               f"{gathering.COINS_PER_HOUR / 1e6:g}m gp/h)")
        plan.gather_coins += g.coins
        plan.calendar_hours += g.calendar_hours
        for f, v in g.reputation.items():
            plan.reputation[f] = plan.reputation.get(f, 0) + v
        for s, v in g.xp.items():
            plan.byproduct_xp[s] = plan.byproduct_xp.get(s, 0) + v
        plan.steps = [s for s in plan.steps if not s.startswith("still need")] + ["gather:"] + ["  " + s for s in g.steps]
        if g.short:
            plan.steps.append("still need " + ", ".join(f"{n:,} {k}" for k, n in g.short.items()) + " (no known source)")
        plan.materials_short = g.short

    def cover_coins(self, gp: float) -> tuple[float, list[str]]:
        """Coins a plan spends, less the money pouch and what group mates can send through group storage
        (coins move like any other item): (gp still to earn, transfer steps). Group mates' coins come from their
        snapshot's money pouch (`PlayerState.group`), biggest purse first."""
        short = gp - self.player.holdings.get("Coins", 0)
        steps: list[str] = []
        for member, stock in sorted(self.player.group.items(), key=lambda kv: kv[1].get("Coins", 0), reverse=True):
            if short <= 0:
                break
            if (take := min(short, stock.get("Coins", 0))) > 0:
                steps.append(f"{member}: put {take:,.0f} coins in group storage (or trade them) for {self.player.name}")
                short -= take
        return max(0.0, short), steps

    def _constitution_plan(self, target_xp: int) -> LevelPlan:
        """Constitution isn't trained on its own: it's 0.33x the xp of whatever combat skill is trained."""
        p = self.player
        source, source_rate = self.cost.best_combat_source({s: p.level(s) for s in COMBAT_SKILLS})
        cost = self.cost.with_flat_rate("Hitpoints", source_rate * CONSTITUTION_SHARE)
        plan = plan_level(cost, self.resources, "Hitpoints", p.xp("Hitpoints"), p.level("Hitpoints"), target_xp,
                          p.bonus_xp.get("Hitpoints", 0), use_bonus=p.members)
        combat_xp_needed = max(0, target_xp - p.xp("Hitpoints") - plan.resource_xp - plan.bonus_xp) / CONSTITUTION_SHARE
        if combat_xp_needed > 0:
            plan.steps = [s for s in plan.steps if not s.startswith("then train Hitpoints")]
            plan.steps.append(f"train {source} (~{source_rate:,.0f} xp/h): ~{combat_xp_needed / source_rate:.1f}h, "
                              f"+{combat_xp_needed:,.0f} {source} xp on the side")
        return plan


class Track(Protocol):
    name: str
    description: str

    def candidates(self, ctx: Context) -> list[Candidate]: ...


def rank(cands: list[Candidate]) -> list[Candidate]:
    return sorted(cands, key=lambda c: c.efficiency, reverse=True)


def combine(per_track: dict[str, list[Candidate]], weights: dict[str, float] | None = None) -> list[tuple[float, Candidate]]:
    weights = weights or {}
    merged: list[tuple[float, Candidate]] = []
    for track, cands in per_track.items():
        if not cands:
            continue
        best = max(c.efficiency for c in cands) or 1.0
        w = weights.get(track, 1.0)
        merged += [(w * c.efficiency / best, c) for c in cands]
    return sorted(merged, key=lambda t: t[0], reverse=True)
