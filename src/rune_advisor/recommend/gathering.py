"""Gathering costs: turn "still need 56,803 Pure essence" into hours.

For each item a plan is short of, the cheapest way to get it (fewest hours per unit):
  * shop   buy it from a coin shop (data/shops.json; ironmen can use shops). Stock refills or a world
           hop every SHOP_REFILL_SECONDS, so big orders take a while; coins come from the money pouch.
  * drop   kill/loot at a training spot the player can use now (wiki methods with `drop_sources` and a
           known kill rate, see drops.py). These also give that skill's xp, kept as a by-product. A spot
           drops several things at once, so needed items are collected per visit, not one spot each.
  * craft  make it with a wiki recipe from the catalogue, its inputs taken from the bank first and
           otherwise gathered the same way, all the way down. A recipe still counts when some input has no
           known source (that input stays a shortfall: Gathering.short, Gatherer.gaps) or when the player is
           below its levels (the levels' training is added once per plan, as an unlock): such recipes are
           only picked when no complete one exists.
  * farm   grow it on the player's farming patches from seeds (bank first, else gathered): a run's
           minutes are play time, and growth is calendar time (crops of one patch type share the
           patches), reported separately. Patch counts, yields and run lengths are assumptions (PATCHES).
           The Herblore Habitat's patches and the harmony pillars are taken as kept planted (KEPT_PLANTED): a
           passive supply like the kingdom's, whose growth isn't a cost; harmony moss grows with xp trained.
  * method a [[methods]] entry in rune_advisor.toml that `yields` the item (new content the guides
           don't cover yet), collected per visit like drops, with its `side_xp`.
  * disassemble  Invention materials: disassemble whichever item gives the material cheapest (the item's own
           source, plus 1.2 s per batch, over its expected yield; rune_advisor.disassembly). Yields are random, so a
           plan disassembles enough for disassembly.CONFIDENCE (95%) of getting the amount, and the other
           materials it surely gives (the same quantile) cover later shortfalls. When planning, the cheapest few
           items are re-costed against the bank (smithing a weapon from banked bars costs only the smithing), and
           items with banked inputs join them. Gear can be made to disassemble, never taken from the bank.
  * daily  Managing Miscellania (kingdom.py): subjects, at most 10 a category, gather a category's items
           (fishing: tuna and swordfish together) for 5,000 gp of pay a subject-day and a short visit; one plan
           shares its subject-days (kingdom.Ledger), so items that come together are paid for once; the days are
           calendar time.
  * gather logs and ores: simulated per player from the wiki's Woodcutting/Mining mechanics (sim.py);
           items sim.py doesn't know fall back to: logs per hour = the tree's own guide xp/h (a Woodcutting row naming
           it, e.g. "Maple") over the log's xp. Trees the guide has no row for (yew, magic, elder,
           blisterwood) get UNKNOWN_TREE_SHARE of the general rate, and say so.
Items with none of these stay as shortfalls ("no known source"), and are tallied in Gatherer.gaps (units and
the goals they block) so the data gaps that matter most get filled first (`radv gaps`).

Gathering skills' own outputs (ore, logs, fish) aren't sources yet: the training guides don't say
what each method yields per hour.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from functools import cache
from .. import datapkg
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..cost import row_skills, typical_rate
from ..boosts import GATHERING_SKILLS, item_multiplier
from ..drops import (ASSUMED_RATES, CASKETS_PER_HOUR, DAILY_KINDS, EXCLUDED_SOURCES, assumed_rate, drop_chance,
                     estimated_kills_per_hour, kills_per_hour, source_kind, sources_of, tables)
from .. import claims, currencies, kingdom, respect, traits
from .. import disassembly as dis
from .. import sim
from ..gear import equipment
from ..level_boosts import plan_boost, reach
from ..models import PlayerState, Skill
from ..reputation import REP_PER_ITEM, remaining, value_per_rep
from ..xp import xp_at
from ..mechanics.farming import expected_yield
from ..mechanics import is_stackable
from ..mechanics.processing import effective
from .catalogue import _recipe_seconds, _usable, by_output, members_items

if TYPE_CHECKING:
    from .base import Context

# Facilities too ordinary to name in a "make" step.
EVERYDAY_FACILITIES = {"Anvil", "Furnace", "Range", "Obelisk", "Summoning obelisk", "Backpack", "Trough", "Tailoring",
                       "Inventor's workbench", "Potter's wheel", "Runecrafting altar"}
SHOP_REFILL_SECONDS = 30
ASSUMED_RESTOCK_SECONDS = 60  # per item, for non-stackable shop stock with no known cadence
# Since the DailyScape Overhaul (16 March 2026) the shops ironmen do shop runs at accumulate up to a week of
# stock (data/shop_cadence.json): the listed stock is a weekly cap, bought in one visit, and a big order
# waits weeks of calendar time (3,480 uncut diamonds at a shop holding 35 is ~100 weeks, not 1.7 hours).
WEEK_HOURS = 7 * 24
# Non-stackable shop items go a backpack (28) at a time, with a bank trip each (~50 s).
MAX_SHOP_ITEMS_PER_HOUR = 2_000
# Shops stocking this many of an item sell stackables (shards, feathers, runes): no backpack limit.
STACKABLE_SHOP_STOCK = 10_000
# Disassembly candidates re-costed against the bank when planning (the cheapest by per-unit cost, and as
# many again whose recipe inputs are banked).
DISASSEMBLY_SHORTLIST = 8
CLAIM_VISIT_HOURS = 1 / 60  # collecting one weekly claim (claims.py), usually on the way past
# Waiting counts for a little: a day of calendar time (patch growth, weekly shop stock) costs this many
# hours of play when choosing sources, so a 100-day wait isn't "free" next to an hour of gathering.
# Managing Miscellania is the exception (Source.passive): the kingdom works whether the player plays or not, so
# its coal, logs, flax and fish beat gathering them by hand (the user's rule, 2026-11-12: never mine coal once
# the kingdom is managed); the plan still reports the days it takes. Weekly claims (claims.py) are passive too.
# A plan takes what such a supply brings in while the plan is being played (its play hours at PLAY_HOURS_PER_DAY,
# plus earlier goals' in a joint plan: Gatherer.window_days) and gathers the rest another way.
CALENDAR_HOUR_COST = 0.25 / 24
# By-products can make a source look up to this much cheaper, never free.
MIN_NET_SHARE = 0.1
# Trees without a guide rate: assume this share of the skill's general (hand-written) rate.
UNKNOWN_TREE_SHARE = 0.5

# Farming runs per patch type: (patches a high-level player has, average crop per patch, minutes per
# run). Rough assumptions, not wiki data.
PATCHES = {
    "Herb patch": (9, 7, 8),
    "Herb patch (Wilderness)": (1, 7, 8),  # the only patch bloodweed grows in
    "Fruit Tree Patch": (7, 6, 10),
    "Allotment patch": (8, 10, 10),
    "Bush patch": (5, 4, 8),
    "Cactus patch": (2, 3, 4),
    "Mushroom patch": (2, 6, 4),
    "Hops Patch": (4, 10, 6),
    # Herblore Habitat: one vine herb patch in the middle area and one in the east, one vine bush and one vine
    # blossom patch (Herblore Habitat page); the run is a witchdoctor mask teleport and a few steps.
    "Vine herb patch": (2, 7, 4),
    "Vine bush patch": (1, 4, 2),
    "Vine flower patch": (1, 1, 2),
    "Harmony pillar": (4, 3, 4),  # four in the Meilyr district (Harmony moss page)
    # Flower patches sit beside the allotments on an allotment run, so a few minutes on top of it.
    "Flower Patch": (7, 1, 4),
    "Calquat patch": (1, 6, 3),
    "Nightshade patch": (1, 7, 3),
}
# What planting takes besides the seed, used up each time (Starbloom flower seed page: a battlestaff).
PLANT_WITH = {"Starbloom flower seed": {"Battlestaff": 1}}
# Patches whose plant keeps producing once grown (fruit trees, bushes, cacti: the primal-fruit run "can be
# farmed without needing new seeds every run"): the seed is planted once, then each harvest only waits for
# the regrowth. Values are regrow minutes (the wiki's primal fruit guide: cacti and fruit trees every 2 hours,
# bushes a full run every 6 hours); the seed's cost is spread over PERENNIAL_HARVESTS harvests.
PERENNIAL_PATCHES = {"Fruit Tree Patch": 120, "Bush patch": 360, "Cactus patch": 120}
PERENNIAL_HARVESTS = 50
# Patches the planner takes as kept planted, replanted whenever the player passes (the user's rule, 2026-11-21):
# the Herblore Habitat's and the Harmony pillars. Their crop comes in like the kingdom's supplies (Source.passive):
# growth isn't a cost, only the runs and seeds are, and a plan counts on what grows while it is played.
KEPT_PLANTED = {"Vine herb patch", "Vine bush patch", "Vine flower patch", "Harmony pillar"}
PATCH_QUESTS = {"Harmony pillar": "Plague's End"}
# Patches that grow with xp, not time: harmony moss advances a stage per 50,000 xp gained in the skill its pillar
# is attuned to, 200,000 xp for a harvest (Harmony moss seed page). Attunement can be rerolled (10 vis wax a pillar
# a day), so the pillars are taken as attuned to what the player trains; that xp becomes calendar time at the
# player's median training rate and PLAY_HOURS_PER_DAY of play.
XP_GROWN = {"Harmony pillar": 200_000}
# The player's average play a day (`play_hours_per_day` in rune_advisor.toml; the CLI sets it here): turns a plan's
# play hours into the days passive supplies (kingdom, weekly claims, kept-planted patches) and offline research
# have to bring things in (the user's idea, 2026-11-22, replacing a fixed four-week cap).
PLAY_HOURS_PER_DAY = 3.0
# Bisection passes plan() makes to settle a plan's length against what passive supplies bring in over it.
WINDOW_PASSES = 5
# Coins as time, for comparing a shop with other sources and for coins beyond the money pouch: a
# rough ironman money-making rate. `coins_per_hour` in rune_advisor.toml replaces it (read at call time, so
# the CLI sets it here before planning).
COINS_PER_HOUR = 2_000_000


@dataclass
class Source:
    kind: str  # shop | drop | craft
    how: str
    hours_per_unit: float
    coins_per_unit: float = 0.0
    xp_per_unit: dict[str, float] = field(default_factory=dict)  # by-product xp per unit
    recipe: dict | None = None  # for craft
    seed: dict | None = None  # for farm
    calendar_per_unit: float = 0.0  # real-time hours of patch growth per unit (farm)
    rep: dict[str, float] = field(default_factory=dict)  # Menaphos reputation per unit, by faction
    # Inputs somewhere down the chain with no known source (their cost is missing from this one).
    unsourced: frozenset[str] = frozenset()
    # Levels the chain needs that the player doesn't have yet: skill -> level (costed once per plan).
    unlock: dict[str, int] = field(default_factory=dict)
    disassemble: str | None = None  # the item disassembled (disassemble)
    passive: bool = False  # comes in without play (Managing Miscellania): its calendar time isn't a cost
    flag: str | None = None  # a flagged estimate stands in for missing data (drops.ASSUMED_RATES): why
    kingdom: tuple[str, str] | None = None  # Managing Miscellania's (category, setting) bringing it in

    @property
    def complete(self) -> bool:
        return not self.unsourced and not self.unlock

    @property
    def cost_per_unit(self) -> float:
        """Hours per unit, counting coins as the time to earn them and waiting a little."""
        waiting = 0.0 if self.passive else self.calendar_per_unit * CALENDAR_HOUR_COST
        return self.hours_per_unit + self.coins_per_unit / COINS_PER_HOUR + waiting


@dataclass
class Unlock:
    level: int
    why: str  # the item that needs it
    hours: float  # of the plan's work that needs the level (how long a boost must hold)
    boostable: bool


@dataclass
class Gathering:
    hours: float = 0.0
    coins: float = 0.0
    steps: list[str] = field(default_factory=list)
    short: dict[str, int] = field(default_factory=dict)  # still not covered
    xp: dict[str, float] = field(default_factory=dict)  # by-product xp per skill
    calendar_hours: float = 0.0  # growth time on shared farming patches (not play time)
    reputation: dict[str, float] = field(default_factory=dict)  # Menaphos reputation earned, by faction
    # Levels the plan's recipes and resources need that the player lacks, by skill.
    unlocks: dict[str, Unlock] = field(default_factory=dict)
    unlock_hours: float = 0.0  # training or boosting for them
    kingdom: kingdom.Ledger | None = None  # the kingdom's bookings once this plan is done (Context.kingdom)

    def add_unlock(self, skill: str, u: Unlock) -> None:
        if (old := self.unlocks.get(skill)) is None:
            self.unlocks[skill] = u
            return
        top = u if u.level > old.level else old
        self.unlocks[skill] = Unlock(top.level, top.why, old.hours + u.hours, old.boostable and u.boostable)

    def add_xp(self, xp: dict[str, float], times: float) -> None:
        for skill, per in xp.items():
            self.xp[skill] = self.xp.get(skill, 0) + per * times


@cache
def _recipe_inputs() -> frozenset[str]:
    """Every item some catalogue recipe (spells included) takes as a material."""
    from .catalogue import catalogue

    return frozenset(m for r in catalogue() for m, _ in r["materials"])


def _same_npc(page: str, npc: str) -> bool:
    """A drop page ("Razmire Keelgan") and a weekly claim's NPC text ("Razmire in Mort'ton") name the same NPC."""
    core = re.split(r" in | by | \(", npc.removeprefix("the "))[0]
    return page.lower().startswith(core.lower()) or (" " not in core and page.split()[0] == core)


class Gatherer:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._sources: dict[str, Source | None] = {}
        self._drop_index: dict[str, list[tuple[str, str, float, float]]] | None = None
        self._estimated: set[str] = set()
        self._worded: set[tuple[str, str]] = set()  # (item, spot) whose drop chance is a worded rarity's guess
        self._side_xp: dict[str, dict[str, float]] = {}  # method -> other skills' xp per hour
        self._spot_yields: dict[str, dict[str, float]] = {}
        self._spot_info_cache: dict[str, tuple[str, float]] | None = None
        self._junk_tier: int | None = None  # gathering spots yielding 2+ items: method -> per hour
        # Spots that are consumable-boost twins (consumables.boosted_rows): method -> what they use up per hour.
        self._consumes: dict[str, dict[str, float]] = {}
        self._consumable_cost: dict[str, float] = {}
        self._in_progress: set[str] = set()  # items source() is still working out (recipe cycles)
        self._cycle_hits: set[str] = set()  # open items the current lookup ran into (source())
        self._provisional: dict[str, tuple[Source | None, frozenset[str]]] = {}  # answers valid while those stay open
        self._training_rate: float | None = None
        self._activities: frozenset[str] | None = None
        self._sift: dict[str, list[dict]] | None = None
        self._sim_memo: dict[str, Source | None] = {}
        self._boost_memo: dict[str, Source | None] = {}
        self._spots_added: set[str] = set()
        # Spots costed at a flagged estimate (drops.assumed_rate) -> why, and what plans used them for: spot ->
        # [hours, goals]. `radv estimates` ranks them so the ones plans lean on get real data first.
        self.flagged: dict[str, str] = {}
        self.flag_use: dict[str, list] = {}
        self._goals: frozenset[str] = frozenset()
        self._kingdom: kingdom.Ledger = self._booked()
        self._window_bound = False  # this pass of a plan ran into a passive supply's window (_plan_window)
        self._plain_spot: dict[str, str | None] = {}  # boosted spot -> the same spot without the boost
        self._making: set[str] = set()  # consumables being planned: spots that use them up are out meanwhile
        # Items no source covers, over every plan this gatherer made: item -> (units, goals they were for).
        self.gaps: dict[str, tuple[int, set[str]]] = {}
        self._unlocking = False  # costing an unlock's levels (which may gather in turn)
        self._unlock_hours: dict[tuple[str, int], float] = {}
        self._dis_options: dict[str, list[Source]] = {}
        self._swap_shards: dict[str, int] = {}
        self._alternates: dict[str, list[Source]] = {}  # item -> its sources in order, when a passive one wins
        self._held_value: dict[str, float] = {}
        self._at_level: dict[tuple[str, int], PlayerState] = {}  # the player with one skill at another level

    def _drops(self) -> dict[str, list[tuple[str, str, float, float]]]:
        """item -> [(method, skill, units per hour, skill xp per hour)] at spots the player can use now."""
        if self._drop_index is None:
            index: dict[str, list] = {}
            p, seen = self.ctx.player, set()
            for skill in sorted({s for rows in self.ctx.cost.wiki_rows.values() for r in rows for s in row_skills(r)}):
                # Consumable-boost twins too: their spots pay for what they use up (consumable_hours).
                for row in self.ctx.cost.available_methods(skill, p.level(skill)) \
                        + self.ctx.cost.boosted_methods(skill, p.level(skill)):
                    if id(row) in seen or not (row.get("drop_sources") or row.get("yields")):
                        continue
                    seen.add(id(row))
                    rate = typical_rate(row)
                    if row.get("side_xp"):
                        self._side_xp[row["method"]] = row["side_xp"]
                    if row.get("consumes"):
                        self._consumes[row["method"]] = row["consumes"]
                        self._plain_spot[row["method"]] = row.get("base_method")
                    boon = item_multiplier(p) if skill in GATHERING_SKILLS else 1.0
                    for item, per_hour in (row.get("yields") or {}).items():
                        index.setdefault(item, []).append((row["method"], skill, float(per_hour) * boon, rate))
                    if len(row.get("yields") or {}) > 1:
                        self._spot_yields[row["method"]] = {i: float(n) * boon for i, n in row["yields"].items()}
                    kph = kills_per_hour(row, skill, rate)
                    if not kph:
                        continue
                    # Several pages can be versions of the same monster: keep the best rate per item.
                    best: dict[str, float] = {}
                    for src in row["drop_sources"]:
                        for item, chance, qty, _ in tables()["sources"].get(src, []):
                            c, guessed = drop_chance(chance)
                            if c and kph * c * qty > best.get(item, 0):
                                best[item] = kph * c * qty
                                if guessed:
                                    self._worded.add((item, row["method"]))
                    for item, per_hour in best.items():
                        index.setdefault(item, []).append((row["method"], skill, per_hour, rate))
            self._drop_index = index
        return self._drop_index

    def consumable_hours(self, item: str) -> float:
        """Hours to get one more of a consumable a boosted spot uses up, net of its by-products (pouches give
        Summoning xp other goals may want); inf when nothing complete makes it."""
        if item in self._in_progress:
            # Mid-cycle (mining calcified fungus for the very super mining potion that would boost it): not an
            # option here, and not remembered, since the potion's own source isn't settled yet.
            return float("inf")
        if item not in self._consumable_cost:
            src = self.complete_source(item)
            self._consumable_cost[item] = self.net_cost(src) if src is not None else float("inf")
        return self._consumable_cost[item]

    def _spot_info(self) -> dict[str, tuple[str, float]]:
        """Spot -> (its skill, xp/h), over the drop index (built once)."""
        if self._spot_info_cache is None:
            self._spot_info_cache = {m: (skill, rate) for rows in self._drops().values() for m, skill, _, rate in rows}
        return self._spot_info_cache

    def junk_tier(self) -> int:
        """disassembly.junk_tier for this plan's player, worked out once (it scans every achievement)."""
        if self._junk_tier is None:
            self._junk_tier = dis.junk_tier(self.ctx.player)
        return self._junk_tier

    def multi_yield_spots(self) -> dict[str, dict[str, float]]:
        """Gathering spots the player can use that yield several items at once (the fish farm's pools, mixed ore
        rocks): method -> items per hour."""
        self._drops()
        return self._spot_yields

    def spot_hours(self, method: str) -> float:
        """What an hour at a spot costs: the hour, plus making what a boosted spot uses up in it. A spot whose
        consumable is being planned right now is out (mining the fungus for the potion that boosts that mining)."""
        uses = self._consumes.get(method, {})
        if self._making.intersection(uses):
            return float("inf")
        return 1.0 + sum(n * self.consumable_hours(i) for i, n in uses.items())

    def _sift_recipes(self, item: str) -> list[dict]:
        """Screening soil for `item` (mechanics/archaeology.sift_yields at the player's level): a recipe per soil that
        can give it, a heap in, the expected amount out."""
        if self._sift is None:
            from ..mechanics import archaeology

            p = self.ctx.player
            level = p.level("Archaeology")
            self._sift = {}
            if p.members and level > 1:
                for soil in sorted(archaeology.soils()):
                    for out, per in archaeology.sift_yields(p, soil, level).items():
                        if per > 0:
                            self._sift.setdefault(out, []).append({
                                "skill": "Archaeology", "output": out, "output_qty": per, "level": 1,
                                "xp": archaeology.sift_xp(level), "ticks": float(archaeology.SIFT_TICKS),
                                "materials": [[soil, 1.0]], "members": True, "quests": [], "other_skills": [],
                                "facility": "Screening station", "conversion": True, "sift": soil})
        return self._sift.get(item, [])

    def _modelled_activities(self) -> frozenset[str]:
        """Activities a guide or simulated row rates (a hotspot, a tree): their drop pages need no guessed rate."""
        if self._activities is None:
            cost = self.ctx.cost
            self._activities = frozenset(r.get("activity") or r["method"] for rows in (*cost.wiki_rows.values(),
                                                                                        *cost.boosted.values())
                                         for r in rows)
        return self._activities

    def _research_options(self, item: str) -> list[Source]:
        """The Archaeology research team bringing back `item` (archaeology_guild): offline, one expedition at a time,
        so it comes in like the kingdom's supplies (passive, calendar time), paid in chronotes. How many materials and
        artefacts an hour returns is a flagged estimate (the wiki lists them without amounts)."""
        from .. import archaeology_guild as ag

        p = self.ctx.player
        level = p.level("Archaeology")
        out = []
        for site, info in ag.research_sites(level).items():
            if item in info["materials"]:
                per_hour, what = ag.RESEARCH_MATERIALS_PER_HOUR / len(info["materials"]), "materials"
            elif item in info["artefacts"]:
                per_hour, what = ag.RESEARCH_ARTEFACTS_PER_HOUR / len(info["artefacts"]), "artefacts"
            else:
                continue
            chronotes = self.complete_source("chronotes") if item != "chronotes" else None
            if chronotes is None:
                continue
            cost = ag.research_cost(level, info["factor"])
            why = (f"{ag.RESEARCH_MATERIALS_PER_HOUR:g} materials / {ag.RESEARCH_ARTEFACTS_PER_HOUR:g} artefacts a "
                   f"research hour assumed")
            # Play time: sending the team out (a claim visit a day's worth) and earning the chronotes it costs.
            hours = CLAIM_VISIT_HOURS / 24 / per_hour + cost / per_hour * chronotes.cost_per_unit
            out.append(Source("daily", f"send the research team to {site} ({cost:,.0f} chronotes an hour; "
                                       f"ASSUMED rate: {why})", hours,
                              xp_per_unit={"Archaeology": ag.research_xp(level, info["factor"]) / per_hour},
                              calendar_per_unit=1 / per_hour, passive=True, flag=f"research {what} {why}"))
        return out

    def _daily_options(self, item: str) -> list[Source]:
        """NPC hand-outs and repeatable activities (port voyages, Arc contracts) with no rate: a flagged once a day,
        a minute of play each (drops.ASSUMED_RATES["daily"])."""
        out = []
        for src, chance, qty in sources_of(item):
            c, guessed = drop_chance(chance)
            if not c or source_kind(src) not in DAILY_KINDS or EXCLUDED_SOURCES.search(src) \
                    or src in collection_names() or src == "Managing Miscellania" \
                    or any(c_["item"] == item and _same_npc(src, c_["npc"]) for c_ in claims.data()):
                continue  # modelled properly: the kingdom (kingdom.py) and the weekly claims (claims.py)
            per = c * (qty or 1)
            why = ASSUMED_RATES["daily"][1]
            out.append(Source("daily", f"from {src} (ASSUMED rate: {why})", CLAIM_VISIT_HOURS / per,
                              calendar_per_unit=24 / per, flag=why))
        return out

    def _note_flag(self, where: str, why: str, hours: float) -> None:
        """A plan leaned on a flagged estimate for `hours`: tallied for `radv estimates`."""
        self.flagged[where] = why
        use = self.flag_use.setdefault(where, [0.0, set()])
        use[0] += hours
        use[1] |= self._goals

    def _is_item(self, name: str) -> bool:
        """A drop source that is itself an item (a pack, a gift, a casket): it's opened, not killed."""
        if name in tables()["monsters"]:
            return False
        if (kind := source_kind(name)) != "other":
            return kind == "item"
        return name in members_items() or name in by_output() or name in tables_shops() or bool(sources_of(name))

    def _container_options(self, item: str) -> list[Source]:
        """Opening a pack, gift or casket that holds `item` (its drop table): the container's own cost over the
        expected amount inside."""
        out = []
        for src, chance, qty in sources_of(item):
            c, guessed = drop_chance(chance)
            if not c or EXCLUDED_SOURCES.search(src) or not self._is_item(src) or src == item:
                continue
            sub = self.source(src)
            if sub is None:
                continue
            per = c * (qty or 1)
            r = {"skill": "", "output": item, "output_qty": per, "level": 1, "xp": 0.0, "ticks": 1.0,
                 "materials": [[src, 1.0]], "members": True, "quests": [], "other_skills": [], "facility": "",
                 "conversion": True}
            flag = sub.flag or ("chance assumed" if guessed else None)
            out.append(Source("craft", f"open {src}" + (" (chance assumed)" if guessed else ""),
                              sub.cost_per_unit / per, xp_per_unit={s: v / per for s, v in sub.xp_per_unit.items()},
                              recipe=r, unsourced=sub.unsourced, unlock=sub.unlock, flag=flag))
        return out

    def _add_estimated_spots(self, item: str) -> None:
        if item in self._spots_added:
            return
        self._spots_added.add(item)
        self._add_estimated_spots_uncached(item)

    def _add_estimated_spots_uncached(self, item: str) -> None:
        """Monsters that drop `item` but no training guide covers: kill rates estimated from the
        player's best combat style and gear (drops.estimated_kills_per_hour). All of such a
        monster's drops are indexed, so one visit can collect several needed items."""
        index = self._drops()
        for src, chance, _ in sources_of(item):
            if drop_chance(chance)[0] is None or src in self._estimated:
                continue
            self._estimated.add(src)
            spot = f"{src} (estimated kill rate)"
            est = estimated_kills_per_hour(self.ctx.player, src, self.ctx.combat_style)
            if est is None:
                # No kill or catch rate: a flagged estimate rather than no source (drops.ASSUMED_RATES).
                if self._is_item(src) or src in self._modelled_activities() or src in collection_names():
                    # a pack, gift or casket (opened: _container_options); a source a simulated or guide row already
                    # rates (a hotspot: a guess never stands in for real data); an Archaeology collection (handed in)
                    continue
                if (assumed := assumed_rate(self.ctx.player, src, self.ctx.combat_style)) is None:
                    continue
                kph, skill, xph, why = assumed
                if source_kind(src) in DAILY_KINDS:
                    continue  # once a day: a calendar-limited source (_daily_options), not a spot to stand at
                spot = f"{src} (ASSUMED rate: {why})"
                self.flagged[spot] = why
                est = (kph, skill, xph)
            kph, skill, xph = est
            # A plant's or patch's crop (harmony moss off a pillar, limpwurt off its plant) is grown: the farm model
            # covers it, not a guessed rate at the scenery.
            skip = crops() if spot.endswith(")") and "ASSUMED" in spot and source_kind(src) == "scenery" else ()
            best: dict[str, float] = {}
            for name, chance, qty, _ in tables()["sources"].get(src, []):
                if name in skip:
                    continue
                c, guessed = drop_chance(chance)
                if c and kph * c * qty > best.get(name, 0):
                    best[name] = kph * c * qty
                    if guessed:
                        self._worded.add((name, spot))
            for name, per_hour in best.items():
                index.setdefault(name, []).append((spot, skill, per_hour, xph))
            if best:
                self._spot_info()[spot] = (skill, xph)

    def source(self, item: str) -> Source | None:
        """The fastest way to get one more `item` (cached). Complete chains win over ones with unsourced
        inputs or unmet levels; None when nothing makes, sells or drops it."""
        if item in self._sources:
            if item in self._in_progress:
                self._cycle_hits.add(item)  # a cycle: whoever asked gets a provisional answer
            return self._sources[item]
        if (held := self._provisional.get(item)) is not None:
            result, hits = held
            if hits <= self._in_progress:  # what it hung on is still open: the same answer for this walk
                self._cycle_hits |= hits
                return result
            del self._provisional[item]
        self._sources[item] = None  # guards recipe cycles
        self._in_progress.add(item)
        outer, self._cycle_hits = self._cycle_hits, set()
        try:
            result = self._source(item)
        finally:
            self._in_progress.discard(item)
            hits = self._cycle_hits - {item}
            self._cycle_hits = outer | hits
        if hits:
            # Worked out while an item further up was still open (Augmentor -> components -> disassembling gear that
            # takes an Augmentor): right for this walk, but not cached, or the item would stay "unsourced" for good.
            self._sources.pop(item, None)
            self._alternates.pop(item, None)
            self._provisional[item] = (result, frozenset(hits))
        return result

    def _source(self, item: str) -> Source | None:
        p = self.ctx.player
        options: list[Source] = []
        cadence = shop_cadence()
        for shop, stock, price, members, *flags in tables_shops().get(item, []):
            if members and not p.members:
                continue
            pack = flags[0] if flags else None  # {"pack": name, "each": contents}: bought as packs, opened into notes
            per_hour, calendar = stock * 3600 / SHOP_REFILL_SECONDS, 0.0
            if pack:
                how = (f"buy {pack['pack']}s at {shop} ({price * pack['each']:,.0f} gp a pack of {pack['each']:g}, "
                       f"opened into noted {item})")
            else:
                how = f"buy at {shop} ({price:,.0f} gp each)"
            if shop in cadence["weekly"]:  # a week's stock per visit: calendar time, not refills
                calendar, how = WEEK_HOURS / stock, f"{how}; stock is {stock:,} a week"
            elif (per_day := cadence["daily"].get(shop, {}).get(item)) is not None:
                calendar, how = 24 / per_day, f"{how}; {per_day:,} a day"
            elif (secs := cadence["per_seconds"].get(shop)) is not None:
                per_hour, how = 3600 / secs, f"{how}; one restocks every {secs:,.0f} s"
            elif not pack and not is_stackable(item) and stock < STACKABLE_SHOP_STOCK:
                # No known cadence: equipment-like stock comes back an item at a time, not a shelf at a time
                # (both shops checked so far, the White Knight armoury and Tamayu's stall, restock one a minute).
                per_hour = 3600 / ASSUMED_RESTOCK_SECONDS
                how = f"{how}; stock {stock:,}, assumed to restock one every {ASSUMED_RESTOCK_SECONDS} s"
            if stock < STACKABLE_SHOP_STOCK and not pack:  # a backpack at a time (packs open into notes)
                per_hour = min(MAX_SHOP_ITEMS_PER_HOUR, per_hour)
            options.append(Source("shop", how, 1 / per_hour, price, calendar_per_unit=calendar))
        self._add_estimated_spots(item)
        for method, skill, per_hour, rate in self._drops().get(item, []):
            worded = " (the wiki gives only a worded rarity: chance assumed)" if (item, method) in self._worded else ""
            # A boosted spot's hour also costs its consumables (made net of their own by-products).
            hours = self.spot_hours(method)
            if hours == float("inf"):
                continue
            options.append(Source("drop", f"drops at {method}{worded}", hours / per_hour,
                                  xp_per_unit={skill: rate / per_hour}))
        # Simulated from the wiki's Woodcutting/Mining mechanics for this player's levels and tools.
        if (simulated := self._simulated(item)) is not None:
            options.append(simulated)
        elif (boosted := self._boosted_gather(item)) is not None:
            options.append(boosted)
        for gath in gathered():
            if simulated is not None:
                break
            if gath["output"] == item and p.level(gath["skill"]) >= gath["level"] and (p.members or not gath["members"]):
                rate, how = self._tree_rate(gath)
                rep = {}
                if item in REP_PER_ITEM and remaining(p, REP_PER_ITEM[item][0]) > 0:
                    faction, per = REP_PER_ITEM[item]
                    rep, how = {faction: per}, f"{how} in Menaphos (+{per:g} {faction} reputation each)"
                boon = item_multiplier(p)  # Boon of Crondis: extra logs, same xp per hour
                options.append(Source("gather", how, gath["xp"] / rate / boon,
                                      xp_per_unit={gath["skill"]: gath["xp"] / boon}, rep=rep))
        for category, setting, per in kingdom.settings_for(p, item):
            # Costed per unit as 10 subjects on the category paid for this item alone (its partners, swordfish with
            # tuna, are free when a plan wants them too: kingdom.Ledger).
            per_day = kingdom.SUBJECTS_PER_CATEGORY * per
            options.append(Source("daily", f"Managing Miscellania ({category}, {setting})",
                                  kingdom.VISIT_HOURS_PER_DAY / per_day,
                                  kingdom.SUBJECTS_PER_CATEGORY * kingdom.COINS_PER_SUBJECT_DAY / per_day,
                                  calendar_per_unit=24 / per_day, passive=True, kingdom=(category, setting)))
        for claim in claims.available(p):
            if claim.item == item:  # a week's free supply from a task set reward: a minute to collect, a week to wait
                options.append(Source("daily", f"weekly claim from {claim.npc} ({claim.qty:,} a week: {claim.why})",
                                      CLAIM_VISIT_HOURS / claim.qty, calendar_per_unit=WEEK_HOURS / claim.qty,
                                      passive=True))
        if farm := self._farm_source(item):
            options.append(farm)
        if (earned := currencies.earning(self.ctx, item)) is not None:  # reward-shop currencies
            hours, how, xp, calendar = earned
            flag = "assumed earning rate" if "assum" in how.lower() else None
            options.append(Source("earn", how, hours, xp_per_unit=xp, calendar_per_unit=calendar, flag=flag))
        for r in by_output().get(item, []) + self._sift_recipes(item):
            if not _usable(r, p, levels=False) or r.get("daily"):
                continue  # daily-capped conversions (Desert amulets) aren't an hourly source
            unlock = {s: lvl for s, lvl, _ in gates(r, p)}
            r = effective(p, r, for_xp=False)  # the item is wanted, so a pouch keeps its full shard cost
            hours = _recipe_seconds(r, p) / 3600
            xp = {r["skill"]: r["xp"]}
            unsourced: set[str] = set()
            for name, qty in r["materials"]:
                sub = self.source(name)
                if sub is None:
                    unsourced.add(name)
                    continue
                hours += qty * sub.cost_per_unit
                for s, v in sub.xp_per_unit.items():
                    xp[s] = xp.get(s, 0) + qty * v
                unsourced |= sub.unsourced
                for s, lvl in sub.unlock.items():
                    unlock[s] = max(unlock.get(s, 0), lvl)
            out = r["output_qty"]
            how = (f"convert from {', '.join(m for m, _ in r['materials'])}" if r.get("conversion")
                   else f"make with {r['skill']}")
            options.append(Source("craft", how, hours / out, xp_per_unit={s: v / out for s, v in xp.items()},
                                  recipe=r, unsourced=frozenset(unsourced), unlock=unlock))
        options += self._daily_options(item)
        options += self._research_options(item)
        if not any(o.complete for o in options):
            # Packs, gifts and caskets holding the item: a fallback for coverage (following every container from
            # every item widens the search for little gain where a complete source exists).
            options += self._container_options(item)
        if (casket := re.match(r"Reward casket \((\w+)\)$", item)) and casket.group(1) in CASKETS_PER_HOUR:
            n = CASKETS_PER_HOUR[casket.group(1)]
            why = f"{n:g} {casket.group(1)} clues solved an hour"
            options.append(Source("earn", f"solve {casket.group(1)} clue scrolls (ASSUMED rate: {why})", 1 / n, flag=why))
        options += self._disassembly_sources(item)[:1]
        best = min(options, key=lambda o: (not o.complete, self.net_cost(o)), default=None)
        if best is not None and best.passive:
            # What a plan falls back on past what a passive supply brings in over its window: the options in
            # order, so a weekly claim runs into the kingdom's supply before anything is gathered by hand.
            self._alternates[item] = sorted(options, key=lambda o: (not o.complete, self.net_cost(o)))
        self._sources[item] = best
        return best

    def _disassembly_sources(self, material: str) -> list[Source]:
        """Items to disassemble for an Invention material, each costed by its own (complete) source, cheapest
        first (cached)."""
        if material in self._dis_options:
            return self._dis_options[material]
        self._dis_options[material] = []  # guards cycles (parts -> device -> parts)
        p = self.ctx.player
        if not p.members or p.level("Invention") < 1 or material not in dis.sources():
            return []
        tier, out = self.junk_tier(), []
        for item in dis.sources()[material]:
            if item == material or not (y := dis.per_item(item, material, tier)) or y.mean <= 0:
                continue
            if material == dis.JUNK and by_output().get(item):
                # Junk only from raw items (bought, gathered, dropped): costing a crafted one here would walk
                # recipes that need junk themselves (refiner -> Refined components -> devices) mid-cycle.
                continue
            src = self.complete_source(item)
            if src is None:
                continue
            secs = dis.seconds_per_item(item)
            xp = {"Invention": dis.data()[item]["xp"], **src.xp_per_unit}
            out.append(Source("disassemble", f"disassemble {item}", (src.hours_per_unit + secs / 3600) / y.mean,
                              src.coins_per_unit / y.mean, xp_per_unit={s: v / y.mean for s, v in xp.items()},
                              calendar_per_unit=src.calendar_per_unit / y.mean, disassemble=item))
        out.sort(key=self.net_cost)
        self._dis_options[material] = out
        return out

    def complete_source(self, item: str) -> Source | None:
        """source(), but only when its whole chain is costed (no unsourced input, no level to train first):
        for comparing per-unit costs, where a partial chain would look cheaper than it is."""
        src = self.source(item)
        return src if src is not None and src.complete else None

    def _simulated(self, item: str) -> Source | None:
        # The player's own rates, the same whatever else is being worked out: memoised (recipe cycles walk an item
        # again while an answer further up is still open).
        if item not in self._sim_memo:
            self._sim_memo[item] = self._simulated_uncached(item)
        return self._sim_memo[item]

    def _boosted_gather(self, item: str) -> Source | None:
        if item not in self._boost_memo:
            self._boost_memo[item] = self._boosted_gather_uncached(item)
        return self._boost_memo[item]

    def _simulated_uncached(self, item: str) -> Source | None:
        found = sim.gather_rate(self.ctx.player, item)
        if found is None or found[1].items_per_hour <= 0:
            return None
        skill, r = found
        how = f"{'cut' if skill == 'Woodcutting' else 'mine'} {r.activity.lower()} ({r.items_per_hour:,.0f}/h with " \
              f"{r.tool}{', assumed' if r.assumed_tool else ''}; simulated)"
        rep = {}
        if item in REP_PER_ITEM and remaining(self.ctx.player, REP_PER_ITEM[item][0]) > 0:
            faction, per = REP_PER_ITEM[item]
            rep, how = {faction: per}, f"{how} in Menaphos (+{per:g} {faction} reputation each)"
        boon = item_multiplier(self.ctx.player)  # Boon of Crondis: 2% extra items, same xp per hour
        if boon > 1:
            how = how.replace("; simulated)", f"; simulated, +{boon - 1:.0%} Boon of Crondis)")
        calendar = 0.0
        if r.activity in sim.DAILY_NODES:  # Uncharted Isles nodes: so many a day on a claimed island
            nodes, units = sim.DAILY_NODES[r.activity]
            calendar, how = 24 / (nodes * units), f"{how}; {nodes} nodes x {units} a day on a claimed island"
        return Source("gather", how, 1 / (r.items_per_hour * boon), xp_per_unit={skill: r.xp_per_item / boon}, rep=rep,
                      calendar_per_unit=calendar)

    def _boosted_gather_uncached(self, item: str) -> Source | None:
        """A tree or rock the player is a few levels short of but can boost to (level_boosts.reach): the
        simulated rate at the lowest level that gets the item, with that level as an unlock (trained or boosted
        when planning)."""
        p = self.ctx.player

        def at(skill: str, level: int) -> PlayerState:
            if (skill, level) not in self._at_level:
                self._at_level[skill, level] = replace(p, skills={**p.skills, skill: Skill(skill, level, xp_at(skill, level))})
            return self._at_level[skill, level]
        for skill in ("Woodcutting", "Mining"):
            base, top = p.level(skill), reach(p, skill)
            if top <= base or (found := sim.gather_rate(at(skill, top), item)) is None or found[0] != skill:
                continue
            for level in range(base + 1, top + 1):
                found = sim.gather_rate(at(skill, level), item)
                if found is None or found[0] != skill or found[1].items_per_hour <= 0:
                    continue
                r = found[1]
                how = f"{'cut' if skill == 'Woodcutting' else 'mine'} {r.activity.lower()} ({r.items_per_hour:,.0f}/h at " \
                      f"{level} {skill} with {r.tool}; simulated)"
                return Source("gather", how, 1 / r.items_per_hour, xp_per_unit={skill: r.xp_per_item},
                              unlock={skill: level})
        return None

    def side_value(self, src: Source) -> float:
        """Hours per unit the source's by-products save: xp other goals want, and reputation still needed."""
        value = sum(xp * self.ctx.side_value_per_xp(s) for s, xp in src.xp_per_unit.items())
        return value + sum(r * value_per_rep(self.ctx.player, f) for f, r in src.rep.items())

    def _booked(self) -> kingdom.Ledger:
        """What passive supplies other plans in this sequence already took (Context.kingdom: a joint plan's goals
        share one), else nothing taken."""
        return self.ctx.kingdom or kingdom.Ledger(self.ctx.player, 0.0)

    def window_days(self, play_hours: float) -> float:
        """Days passive supplies have to bring things in for a plan of `play_hours`: those hours, plus the earlier
        goals' in a sequence (Context.play_elapsed), at the player's play a day."""
        return (self.ctx.play_elapsed + play_hours) / PLAY_HOURS_PER_DAY

    def passive_room(self, item: str, src: Source, days: float, ledger: kingdom.Ledger | None = None) -> float:
        """Units of `item` a passive source can still bring in over `days`, less what earlier plans drew."""
        ledger = ledger or self._booked()
        if src.kingdom:
            return ledger.capacity(item, *src.kingdom, days=days)
        return max(0.0, days * 24 / src.calendar_per_unit - ledger.passive.get(src.how, 0.0))

    def bulk_cost(self, item: str, n: float, play_hours: float = 0.0) -> float:
        """Net hours for `n` of an item the way plan() fills it: a passive supply (the kingdom, a weekly claim) only
        for what it brings in while `play_hours` are played (window_days), the rest from the next option. Comparing
        methods per unit would take the kingdom's tuna as nearly free for a grind that needs ten times what it
        supplies."""
        src = self.complete_source(item)
        if src is None:
            return float("inf")
        if n <= 0:
            return 0.0
        chain = [o for o in (self._alternates.get(item) or [src]) if o.complete]
        chain = chain[chain.index(src):] if src in chain else [src]
        hours, left = 0.0, float(n)
        for o in chain:
            take = left
            if o.passive and o.calendar_per_unit and o is not chain[-1]:
                # What it brings in while this is played, once earlier goals' draws are out.
                take = min(left, self.passive_room(item, o, self.window_days(play_hours)))
            hours += take * self.net_cost(o)
            left -= take
            if left <= 0:
                break
        return hours

    def net_cost(self, src: Source) -> float:
        """Cost per unit less what its by-products are worth (never below MIN_NET_SHARE of the cost)."""
        cost = src.cost_per_unit
        return max(MIN_NET_SHARE * cost, cost - self.side_value(src))

    def _tree_rate(self, gath: dict) -> tuple[float, str]:
        """(xp/h, description) for cutting the tree a log comes from."""
        skill, level = gath["skill"], self.ctx.player.level(gath["skill"])
        tree = gath["output"].removesuffix(" logs").removesuffix(" Logs").lower()
        if tree == "logs" or gath["output"] == "Logs":
            tree = "regular"
        rows = [r for r in self.ctx.cost.wiki_rows.get(skill, [])
                if re.match(rf"{re.escape(tree)}( trees?)?$", r["method"].lower()) and r["level_min"] <= level]
        if rows:
            best = max(rows, key=typical_rate)
            return typical_rate(best), f"cut {best['method'].lower()} ({typical_rate(best):,.0f} xp/h, wiki)"
        rate = UNKNOWN_TREE_SHARE * self.ctx.cost.baseline_rate(skill, level)
        return rate, f"cut with {skill} (no guide rate for this tree; assumed {rate:,.0f} xp/h)"

    def patches(self, patch: str, seed: dict | None = None) -> tuple[int, float, float]:
        """(patches, crop per patch, minutes per run) for a patch type: the player's [farming] settings
        over the defaults in PATCHES; the crop per patch comes from the wiki's yield formula for the seed
        (mechanics/farming.py) unless [farming] sets it."""
        default = PATCHES.get(patch, (1, 1, 5))
        mine = self.ctx.farming.get(patch, {})
        crop = mine.get("yield")
        if crop is None and seed is not None:
            crop = expected_yield(self.ctx.player, seed)
        return (int(mine.get("patches", default[0])), float(crop if crop is not None else default[1]),
                float(mine.get("run_minutes", default[2])))

    def training_rate(self) -> float:
        """The player's median xp per hour over their skills at their levels: how fast xp-grown patches (harmony
        pillars) come round."""
        if self._training_rate is None:
            p = self.ctx.player
            rates = sorted(self.ctx.cost.rate(s, p.level(s)) for s in p.skills if s != "Hitpoints")
            self._training_rate = rates[len(rates) // 2] if rates else 100_000.0
        return self._training_rate

    def _farm_source(self, item: str) -> Source | None:
        """Grow `item` on herb patches, if a seed for it is plantable at the player's Farming level."""
        p = self.ctx.player
        best = None
        for seed in seeds():
            patch = next((pt for pt in seed["patch"] if pt in PATCHES or pt in self.ctx.farming), None)
            if item not in seed["crops"] or patch is None or p.level("Farming") < seed["level"]:
                continue
            if (quest := PATCH_QUESTS.get(patch)) and not (p.quests.get(quest) and p.quests[quest].done):
                continue
            sub = self.source(seed["seed"])
            if sub is None:
                continue
            patches, crop, minutes = self.patches(patch, seed)
            harvests = PERENNIAL_HARVESTS if patch in PERENNIAL_PATCHES else 1  # one seed, many harvests
            grow = PERENNIAL_PATCHES.get(patch, seed["grow_minutes"])
            if patch in XP_GROWN:  # xp to train, as calendar minutes at the player's pace
                grow = XP_GROWN[patch] / self.training_rate() * 60 * 24 / PLAY_HOURS_PER_DAY
            extras = PLANT_WITH.get(seed["seed"], {})
            extra_cost = 0.0
            for name, qty in extras.items():
                if (with_src := self.source(name)) is None:
                    break
                extra_cost += qty * with_src.cost_per_unit
            else:
                extras = None
            if extras:  # a planting material with no source
                continue
            seed_use = traits.seeds_per_planting(p, patch)  # the scroll of life hands some seeds back
            per_unit = minutes / 60 / (patches * crop) + (sub.cost_per_unit * seed_use + extra_cost) / crop / harvests
            kept = patch in KEPT_PLANTED
            src = Source("farm", f"grow from {seed['seed']}" + (" (patches kept planted)" if kept else ""), per_unit,
                         seed={**seed, "patch_type": patch},
                         xp_per_unit={"Farming": (seed["plant_xp"] / harvests + seed["harvest_xp"] * crop) / crop},
                         calendar_per_unit=grow / 60 / (patches * crop), passive=kept)
            if best is None or src.hours_per_unit < best.hours_per_unit:
                best = src
        return best

    def plan(self, short: dict[str, int], stock: dict[str, int] | None = None, training: str = "",
             play_before: float = 0.0, play_of: Callable[[Gathering], float] | None = None) -> Gathering:
        """Hours (and coins) to get every item in `short`; `stock` covers recipe inputs first. Levels a recipe
        on the way needs are trained first, once each (except `training`: the plan's own skill). `play_before`: the
        rest of the plan's play hours (a level plan's grind), during which passive supplies bring things in too;
        `play_of`: the whole plan's play hours given this gathering, when the caller reshapes it (Context._gather)."""
        start = stock if stock is not None else self.ctx.player.holdings
        outer_goals, self._goals = self._goals, self._goals or frozenset(short)
        try:
            if outer_goals:  # within a plan: its window and bookings carry on
                stock = dict(start)
                g = self._plan(short, stock, {})
            else:
                g, stock = self._plan_window(short, start, play_before,
                                             play_of or (lambda gg: play_before + gg.hours))
        finally:
            self._goals = outer_goals
        g.kingdom = self._kingdom
        if (any(st.lstrip().startswith("disassemble ") for st in g.steps)
                and "MATERIALS_STORAGE" not in self.ctx.player.inventories):
            # Exports since 2026-10-31 carry the storage as INVENT_MATERIAL_* vars (inventory MATERIALS_STORAGE).
            g.steps.append("note: this export has no Invention materials storage, so materials already stored "
                           "are counted as 0; re-export (or re-plan after disassembling) to cover any shortfall")
        unlocks = {s: u for s, u in g.unlocks.items() if s != training}
        if unlocks and not self._unlocking:
            self._unlocking = True
            try:
                for skill, u in sorted(unlocks.items(), reverse=True):
                    self._cost_unlock(g, skill, u, stock)
            finally:
                self._unlocking = False
            g.hours += g.unlock_hours
        elif unlocks:
            g.steps.insert(0, "also needs " + ", ".join(f"{u.level} {s}" for s, u in sorted(unlocks.items()))
                           + " (not costed here)")
        for item, n in g.short.items():
            units, goals = self.gaps.get(item, (0, set()))
            self.gaps[item] = (units + n, goals | set(short))
        return g

    def _cost_unlock(self, g: Gathering, skill: str, u: Unlock, stock: dict[str, int]) -> None:
        """Train to the level, or (boostable gates) boost for as long as the work needing it takes: cheaper wins.
        Potions still to make are gathered like any other need (their missing inputs end up in `g.short`)."""
        if (skill, u.level) not in self._unlock_hours:
            self._unlock_hours[skill, u.level] = self.ctx.level_plan(skill, u.level).hours
        train = self._unlock_hours[skill, u.level]
        boost = plan_boost(self, skill, u.level, u.hours) if u.boostable else None
        if boost is not None and boost.hours < train:
            g.unlock_hours += boost.hours - boost.make_hours
            g.calendar_hours += boost.calendar_hours
            head = [f"{boost.steps[0]} for {u.level} {skill} ({u.why}), instead of training (~{train:.1f}h)"]
            if boost.to_make:
                sub = self._plan(boost.to_make, stock, {})
                head += ["  " + st for st in sub.steps]
                sub.steps = []
                self._merge(g, sub)  # the potions' hours, coins, xp and missing inputs
            g.steps[:0] = head
            return
        g.unlock_hours += train
        why = "" if boost is None else f"; boosting would cost ~{boost.hours:.1f}h"
        g.steps.insert(0, f"first get {u.level} {skill} for {u.why} (~{train:.1f}h{why})")

    def _merge(self, g: Gathering, sub: Gathering) -> None:
        """Add a sub-plan (a recipe's or disassembly's inputs) to `g`, its steps indented."""
        g.hours += sub.hours
        g.coins += sub.coins
        g.calendar_hours += sub.calendar_hours
        g.steps += ["  " + s for s in sub.steps]
        for name, k in sub.short.items():
            g.short[name] = g.short.get(name, 0) + k
        for s, u in sub.unlocks.items():
            g.add_unlock(s, u)
        for s, v in sub.xp.items():
            g.xp[s] = g.xp.get(s, 0) + v
        for f, v in sub.reputation.items():
            g.reputation[f] = g.reputation.get(f, 0) + v

    def _plan(self, short: dict[str, int], stock: dict[str, int], extra: dict[str, int]) -> Gathering:
        """plan() without the unlocks. Takes what it uses out of `stock`, so later items (and sibling
        recipes' inputs) only see what's left. `extra`: materials earlier disassemblies surely gave (also in
        `stock`), which cover later shortfalls."""
        g = Gathering()
        drops: dict[str, int] = {}
        # Coins a recipe charges (Aggie's 5 gp per dye) are spent, not gathered: they join the plan's gp, which the
        # money pouch, group mates or money making cover (Context.cover_coins), never a coin drop.
        if (gp := short.get("Coins", 0)) > 0:
            g.coins += gp
            short = {k: v for k, v in short.items() if k != "Coins"}
        sources = {item: self.source(item) for item in short}
        tier = self.junk_tier()

        def disassemblies(item: str) -> int:
            src = sources[item]
            return dis.items_needed(short[item], dis.per_item(src.disassemble, item, tier)) \
                if src is not None and src.kind == "disassemble" else 0
        # The biggest disassembly first (rare components), so its other materials cover the smaller ones.
        for item in sorted(short, key=disassemblies, reverse=True):
            n, src = short[item], sources[item]
            if (got := min(n, extra.get(item, 0))) > 0:
                extra[item] -= got
                stock[item] = stock.get(item, 0) - got
                n -= got
                g.steps.append(f"{got:,} {item} from the disassembly above")
                if n <= 0:
                    continue
            if src is None or n <= 0:
                if n > 0:
                    g.short[item] = n
                continue
            if src.kind == "craft":
                r = src.recipe
                actions = math.ceil(n / r["output_qty"])
                for s, lvl, boostable in gates(r, self.ctx.player):
                    g.add_unlock(s, Unlock(lvl, item, actions * _recipe_seconds(r, self.ctx.player) / 3600, boostable))
                g.hours += actions * _recipe_seconds(r, self.ctx.player) / 3600
                g.add_xp({r["skill"]: r["xp"]}, actions)
                need = {}
                for name, qty in r["materials"]:
                    want = math.ceil(qty * actions)
                    take = min(want, stock.get(name, 0))
                    stock[name] = stock.get(name, 0) - take
                    if want > take:
                        need[name] = want - take
                # Name the facility when it isn't the skill's everyday one (a Barbarian anvil, not any anvil).
                where = r.get("facility") or ""
                at = f" at a {where}" if where and where not in EVERYDAY_FACILITIES and "," not in where else ""
                # Equipment smithed at a plain anvil earns Artisans' Workshop respect when done there (respect.py).
                earns = ""
                if r["skill"] == "Smithing" and where == "Anvil" and r["xp"] and respect.open_rewards(self.ctx.player):
                    pct = respect.percent_for(self.ctx.player, r["xp"] * actions)
                    earns = f"; at the Artisans' Workshop that is +{pct:.1f}% respect" if pct >= 0.05 else ""
                if r.get("sift"):  # screening soil (mechanics/archaeology.sift_yields)
                    g.steps.append(f"screen {actions:,} {r['sift']} for {n:,} {item} (~{actions * _recipe_seconds(r, self.ctx.player) / 3600:.1f}h)"
                                   + (f", gathering {', '.join(f'{v:,} {k}' for k, v in need.items())}" if need else ""))
                    self._merge(g, self._plan(need, stock, extra))
                    continue
                if r.get("collection"):  # an Archaeology collection handed in (catalogue.collection_recipes)
                    g.steps.append(f"hand in the {r['collection']} collection {actions:,}x for {n:,} {item}"
                                   + (f", gathering {', '.join(f'{v:,} {k}' for k, v in need.items())}" if need else ""))
                    self._merge(g, self._plan(need, stock, extra))
                    continue
                if r.get("currency_shop") or not r["skill"]:  # reward shops, merchants, NPC services
                    price = ", ".join(f"{math.ceil(q * actions):,} {m}" for m, q in r["materials"])
                    place = f" at {where}" if where else ""
                    g.steps.append(f"buy {n:,} {item}{place} for {price}" if r.get("currency_shop")
                                   else f"get {n:,} {item}{place} for {price}")
                    self._merge(g, self._plan(need, stock, extra))
                    continue
                g.steps.append(f"make {n:,} {item}{at} ({r['skill']}, ~{actions * _recipe_seconds(r, self.ctx.player) / 3600:.1f}h)"
                               + (f", gathering {', '.join(f'{v:,} {k}' for k, v in need.items())}" if need else "") + earns)
                self._merge(g, self._plan(need, stock, extra))
                continue
            if src.kind == "disassemble":
                self._disassemble(g, item, n, src.disassemble, stock, extra, tier)
                continue
            if src.kind == "drop":
                drops[item] = n
                continue
            chain = self._alternates.get(item) or []
            alt = next(iter(chain[chain.index(src) + 1:]), None) if src in chain else None
            if src.kingdom:
                n = self._from_kingdom(g, item, n, src)
                if n > 0 and alt is not None:
                    self._overflow(g, drops, item, n, src, alt, stock, extra)
                elif n > 0:
                    g.short[item] = g.short.get(item, 0) + n
                continue
            if src.passive and src.calendar_per_unit and alt is not None:
                # A passive supply only counts for what it brings in while the plan is played; the rest is gathered.
                if (cap := int(self.passive_room(item, src, self._kingdom.days, self._kingdom))) < n:
                    self._window_bound = True
                    self._overflow(g, drops, item, n - cap, src, alt, stock, extra)
                    n = cap
                self._kingdom.passive[src.how] = self._kingdom.passive.get(src.how, 0.0) + n
                if n <= 0:
                    continue
            if src.kind == "daily":
                days = n * src.calendar_per_unit / 24
                if src.flag:
                    self._note_flag(src.how, src.flag, n * src.hours_per_unit)
                g.hours += n * src.hours_per_unit
                g.coins += n * src.coins_per_unit
                g.calendar_hours += n * src.calendar_per_unit
                coffers = f" (~{n * src.coins_per_unit:,.0f} gp from the coffers)" if src.coins_per_unit else ""
                g.steps.append(f"get {n:,} {item}: {src.how}, ~{days:,.0f} days of collections{coffers}")
                continue
            if src.kind == "farm":
                seed = src.seed
                patches, crop, minutes = self.patches(seed["patch_type"], seed)
                runs = math.ceil(n / (patches * crop))
                # Perennials are planted once per patch and harvested every regrowth.
                need_seeds = min(patches, runs) if seed["patch_type"] in PERENNIAL_PATCHES \
                    else math.ceil(n / crop * traits.seeds_per_planting(self.ctx.player, seed["patch_type"]))
                take = min(need_seeds, stock.get(seed["seed"], 0))
                stock[seed["seed"]] = stock.get(seed["seed"], 0) - take
                plant = {}
                for m, q in PLANT_WITH.get(seed["seed"], {}).items():  # the battlestaffs starblooms use up
                    want = math.ceil(q * need_seeds)
                    held = min(want, max(0, stock.get(m, 0)))
                    stock[m] = stock.get(m, 0) - held
                    if want > held:
                        plant[m] = want - held
                active = runs * minutes / 60
                g.hours += active
                g.calendar_hours += n * src.calendar_per_unit
                g.add_xp(src.xp_per_unit, n)
                if seed["patch_type"] in XP_GROWN:
                    xp = runs * XP_GROWN[seed["patch_type"]]
                    grows = (f"a harvest per {XP_GROWN[seed['patch_type']]:,} xp gained in the skill each is attuned to: "
                             f"~{xp:,} xp of training, ~{n * src.calendar_per_unit / 24:,.0f} days at "
                             f"{PLAY_HOURS_PER_DAY:g} h a day")
                else:
                    grows = f"~{n * src.calendar_per_unit / 24:,.0f} days of growth"
                kept = ", kept planted" if src.passive else ""
                g.steps.append(f"grow {n:,} {item} from {need_seeds:,} {seed['seed']} ({runs:,} {seed['patch_type'].lower()} runs{kept}, "
                               f"~{active:.1f}h of play, {grows})")
                if need_seeds > take or plant:
                    sub = self._plan({**({seed["seed"]: need_seeds - take} if need_seeds > take else {}), **plant},
                                     stock, extra)
                    g.hours += sub.hours
                    g.coins += sub.coins
                    g.calendar_hours += sub.calendar_hours
                    g.steps += ["  " + s for s in sub.steps]
                    g.short.update(sub.short)
                    for s2, v in sub.xp.items():
                        g.xp[s2] = g.xp.get(s2, 0) + v
                continue
            hours = n * src.hours_per_unit
            for s, lvl in src.unlock.items():  # a resource a few levels up (boosted_gather)
                g.add_unlock(s, Unlock(lvl, item, hours, True))
            g.hours += hours
            g.coins += n * src.coins_per_unit
            g.calendar_hours += n * src.calendar_per_unit
            g.add_xp(src.xp_per_unit, n)
            for faction, per in src.rep.items():
                g.reputation[faction] = g.reputation.get(faction, 0) + per * n
            if src.flag:
                self._note_flag(src.how, src.flag, hours)
            cost = f", {n * src.coins_per_unit:,.0f} gp" if src.coins_per_unit else ""
            if (days := n * src.calendar_per_unit / 24) >= 1:  # weekly shop stock, daily nodes
                cost += f", ~{days / 7:,.1f} weeks of stock" if src.kind == "shop" else f", ~{days:,.0f} days"
            g.steps.append(f"get {n:,} {item}: {src.how} (~{hours:.1f}h{cost})")
        self._visit_spots(g, drops, stock, extra)
        return g

    def _plan_window(self, short: dict[str, int], start: dict[str, int], play_before: float,
                     play_of: Callable[[Gathering], float]) -> tuple[Gathering, dict[str, int]]:
        """A new plan: passive supplies bring in what they make over the plan's own length (window_days), which
        depends on what they bring in (more from them, less play, a shorter window). The first pass assumes only
        `play_before`; while a supply is cut short by the window, bisect the assumed length between that and the
        longest plan, keeping the last pass whose window doesn't outlast its own play. Returns the plan and the
        stock it leaves."""
        flags = {k: [v[0], set(v[1])] for k, v in self.flag_use.items()}

        def run(assumed: float) -> tuple[Gathering, dict[str, int], kingdom.Ledger, dict, bool]:
            self.flag_use = {k: [v[0], set(v[1])] for k, v in flags.items()}
            self._kingdom = self._booked().copy()
            self._kingdom.days = self.window_days(assumed)
            self._window_bound = False
            stock = dict(start)
            g = self._plan(short, stock, {})
            return g, stock, self._kingdom, self.flag_use, self._window_bound

        lo = play_before
        best = run(lo)
        if not best[4]:
            return best[0], best[1]
        if play_of(best[0]) >= lo:  # fits: the answer lies between this and the plan's length
            hi = play_of(best[0])
        else:  # the plan comes out shorter than `play_before` (its inputs train its own skill): search below
            lo, hi, best = 0.0, lo, run(0.0)
        for _ in range(WINDOW_PASSES):
            if hi - lo <= 0.05 * max(hi, 1.0):
                break
            mid = (lo + hi) / 2
            trial = run(mid)
            if play_of(trial[0]) >= mid:  # the window fits inside the plan's own play
                lo, best = mid, trial
            else:
                hi = mid
        g, stock, self._kingdom, self.flag_use, _ = best
        return g, stock

    def _overflow(self, g: Gathering, drops: dict[str, int], item: str, n: int, src: Source, alt: Source,
                  stock: dict[str, int], extra: dict[str, int]) -> None:
        """What a capped passive supply can't bring in, from the item's next source. Dropped at a spot: in this
        plan's visits, so a spot's other items share them (the fish farm's catch)."""
        if alt.kind == "drop":
            drops[item] = drops.get(item, 0) + n
            return
        self._sources[item] = alt
        try:
            sub = self._plan({item: n}, stock, extra)
        finally:
            self._sources[item] = src
        self._merge(g, sub)

    def _from_kingdom(self, g: Gathering, item: str, n: int, src: Source) -> int:
        """Take `item` from the plan's kingdom ledger: first what subject-days already bought for a partner item
        bring in, then new subject-days while the window and the subjects allow. Returns what is still short."""
        category, setting = src.kingdom
        free, new, days = self._kingdom.take(item, n, category, setting)
        if free + new < n - 0.5:
            self._window_bound = True
        got = math.floor(free + new + 1e-6)
        if got <= 0:
            return n
        coins = days * kingdom.COINS_PER_SUBJECT_DAY
        g.hours += days / kingdom.SUBJECTS_PER_CATEGORY * kingdom.VISIT_HOURS_PER_DAY
        g.coins += coins
        g.calendar_hours += days / kingdom.SUBJECTS_PER_CATEGORY * 24
        parts = []
        if free >= 1:
            parts.append(f"{free:,.0f} with the {category} subjects already paid")
        if new >= 1:
            parts.append(f"{new:,.0f} from ~{days:,.0f} subject-days (~{days / kingdom.SUBJECTS_PER_CATEGORY:,.0f} "
                         f"days at 10 subjects, ~{coins:,.0f} gp of pay from the coffers)")
        g.steps.append(f"get {got:,} {item}: Managing Miscellania ({category}, {setting}): {'; '.join(parts)}")
        return n - got

    def _held(self, item: str, stock: dict[str, int]) -> int:
        """How many of `item` a disassembly may take from the stock: none of the player's gear."""
        return 0 if item in equipment()["items"] else max(0, stock.get(item, 0))

    def swap_shards(self, item: str) -> int:
        """Spirit shards Bogrog / Lord Amlodd hand back for a Summoning pouch (70% of its recipe's): what
        disassembling a held one gives up. 0 for anything else."""
        if item not in self._swap_shards:
            from ..boosts import POUCH_SWAP_REFUND
            from .catalogue import catalogue

            shards = 0
            if item.endswith(" pouch") and self.ctx.player.members:
                shards = next((q for r in catalogue() if r["output"] == item and r["skill"] == "Summoning"
                               for m, q in r["materials"] if m == "Spirit shards"), 0)
            self._swap_shards[item] = math.floor(shards * POUCH_SWAP_REFUND)
        return self._swap_shards[item]

    def held_value(self, item: str) -> float:
        """Hours a held item is worth when it isn't disassembled: a pouch's swap shards at the shards' cost; for
        anything a recipe or spell uses (runes, bars, logs), what it takes to replace it (its complete source);
        0 for the rest (drops nothing else wants are free to break down)."""
        if item in self._held_value:
            return self._held_value[item]
        self._held_value[item] = 0.0  # guards cycles while the source is costed
        value = 0.0
        if shards := self.swap_shards(item):
            src = self.complete_source("Spirit shards")
            value = shards * src.cost_per_unit if src is not None else 0.0
        elif item in _recipe_inputs():
            src = self.complete_source(item)
            value = src.cost_per_unit if src is not None and src.kind != "disassemble" else 0.0
        self._held_value[item] = value
        return value

    def _pick_disassembly(self, material: str, n: int, stock: dict[str, int], extra: dict[str, int],
                          tier: int) -> tuple[str, float]:
        """The item to disassemble for `n` of `material`, costed against what the bank holds: the
        DISASSEMBLY_SHORTLIST cheapest by per-unit cost, plus items whose recipe inputs are banked, plus the
        held items cheapest per expected unit. A held item isn't free when it has another use: a Summoning
        pouch gives up its swap shards (`held_value`), so the pick is the cheapest per component, which the
        material's drop rate can make a dearer pouch (2026-11-12: 149 Spirit terrorbird pouches at 8 shards
        each beat 248 Unicorn stallion pouches at 98 for Powerful components, 143 vs 1,093 shards a component)."""
        options = self._disassembly_sources(material)
        shortlist = options[:DISASSEMBLY_SHORTLIST]
        shortlist += [o for o in options[DISASSEMBLY_SHORTLIST:] if (r := self.source(o.disassemble).recipe)
                      and all(stock.get(m, 0) > 0 for m, _ in r["materials"])][:DISASSEMBLY_SHORTLIST]

        def held_unit(o: Source) -> float:
            item = o.disassemble
            return (self.held_value(item) + dis.seconds_per_item(item) / 3600) / dis.per_item(item, material, tier).mean

        held = sorted((o for o in options if o not in shortlist and self._held(o.disassemble, stock) > 0), key=held_unit)
        shortlist += held[:DISASSEMBLY_SHORTLIST]
        best, best_hours = options[0].disassemble, float("inf")
        for o in shortlist:
            item = o.disassemble
            k = dis.items_needed(n, dis.per_item(item, material, tier))
            hours = k * dis.seconds_per_item(item) / 3600 + min(k, self._held(item, stock)) * self.held_value(item)
            if (rest := k - min(k, self._held(item, stock))) > 0:
                trial = self._plan({item: rest}, dict(stock), dict(extra))
                if trial.short:
                    continue
                hours += trial.hours + trial.coins / COINS_PER_HOUR
            if hours < best_hours:
                best, best_hours = item, hours
        return best, best_hours

    def _held_first(self, material: str, n: int, stock: dict[str, int], tier: int, unit_hours: float,
                    skip: str) -> tuple[str, int, int] | None:
        """A held item worth using up before the pick: (item, how many held, the `material` they surely give).
        The pick is one item for the whole need, so a small stack that is cheaper per component (a few cheap
        pouches) would lose to it; here the cheapest such stack goes first and the pick covers the rest."""
        best = None
        for o in self._disassembly_sources(material):
            item = o.disassemble
            held = self._held(item, stock)
            y = dis.per_item(item, material, tier)
            if item == skip or not held or held >= dis.items_needed(n, y):
                continue
            sure = int(dis.sure_amount(held, y))
            if sure < 1:
                continue
            unit = (self.held_value(item) + dis.seconds_per_item(item) / 3600) / y.mean
            if unit < unit_hours and (best is None or unit < best[0]):
                best = (unit, item, held, sure)
        return best[1:] if best else None

    def _disassemble(self, g: Gathering, material: str, n: int, item: str, stock: dict[str, int],
                     extra: dict[str, int], tier: int) -> None:
        """Disassemble enough of the best item (`item` is the per-unit cheapest; the bank can change that) for `n`
        of `material` with disassembly.CONFIDENCE; the other materials it surely gives go to `extra` (and the
        stock) for later needs. A held stack cheaper per component than that pick is used up first."""
        item, pick_hours = self._pick_disassembly(material, n, stock, extra, tier)
        while n > 0 and (first := self._held_first(material, n, stock, tier, pick_hours / n, item)):
            held_item, held, sure = first
            # Only when the two steps together beat the pick alone: counting on each step's low quantile asks
            # for more items than one step sized for the whole need.
            cost = held * (self.held_value(held_item) + dis.seconds_per_item(held_item) / 3600)
            rest = self._pick_disassembly(material, n - sure, stock, extra, tier) if n > sure else (item, 0.0)
            if cost + rest[1] >= pick_hours:
                break
            self._disassemble_items(g, material, held_item, held, sure, stock, extra, tier, all_held=True)
            n -= sure
            item, pick_hours = rest
        if n > 0:
            k = dis.items_needed(n, dis.per_item(item, material, tier))
            self._disassemble_items(g, material, item, k, n, stock, extra, tier)

    def _disassemble_items(self, g: Gathering, material: str, item: str, k: int, n: int, stock: dict[str, int],
                           extra: dict[str, int], tier: int, all_held: bool = False) -> None:
        """One step: disassemble `k` of `item`, counted on for `n` of `material`."""
        y = dis.per_item(item, material, tier)
        hours = k * dis.seconds_per_item(item) / 3600
        g.hours += hours
        g.add_xp({"Invention": dis.data()[item]["xp"]}, k)
        take = min(k, self._held(item, stock))
        stock[item] = stock.get(item, 0) - take
        forgone = take * self.swap_shards(item)
        worth = take * self.held_value(item)
        g.hours += worth
        others = []
        for m, ym in dis.all_yields(item, tier).items():
            if m != material and (sure := int(dis.sure_amount(k, ym))) > 0:
                extra[m] = extra.get(m, 0) + sure
                stock[m] = stock.get(m, 0) + sure
                others.append(f"{sure:,} {m}")
        g.steps.append(f"disassemble {'all ' if all_held else ''}{k:,} {item} for {n:,} {material} "
                       f"({dis.CONFIDENCE:.0%} likely, ~{k * y.mean:,.0f} expected; ~{hours:.1f}h)"
                       + (f", gathering {k - take:,} {item}" if k > take else "")
                       + (f"; also at least {', '.join(others)}" if others else "")
                       + (f"; gives up {forgone:,} Spirit shards the {take:,} held would swap for" if forgone
                          else f"; the {take:,} held would take ~{worth:.1f}h to replace" if worth >= 0.05 else ""))
        if k > take:
            self._merge(g, self._plan({item: k - take}, stock, extra))

    def _visit_spots(self, g: Gathering, needs: dict[str, int], stock: dict[str, int] | None = None,
                     extra: dict[str, int] | None = None) -> None:
        """Get dropped items with as few hours at spots as possible: a spot drops several items at once,
        so time spent there for one item also collects the others (the longest need sets the visit). A boosted
        spot (a familiar, juju kept up) counts what it uses up, and those consumables are planned after."""
        if not needs:
            return
        index = self._drops()
        visits: dict[str, float] = {}

        def collected(item: str) -> float:
            return sum(per_hour * visits.get(m, 0) for m, _, per_hour, _ in index.get(item, []))

        def missing() -> dict[str, float]:
            return {i: n - collected(i) for i, n in needs.items() if n - collected(i) > 0.5}

        while left := missing():
            # The item that takes longest at its best spot decides the next (longer) visit.
            def best(i):
                return max(index[i], key=lambda r: r[2] / self.spot_hours(r[0]))  # inf cost -> 0, never best
            item = max(left, key=lambda i: left[i] / best(i)[2])
            m, _, per_hour, _ = best(item)
            visits[m] = visits.get(m, 0) + left[item] / per_hour
        # Consumables come whole: a short boosted visit still uses a full pouch. Where that makes it dearer than the
        # same spot unboosted for as long as the boost saved, go without.
        for m, hours in list(visits.items()):
            plain = self._plain_spot.get(m)
            if m not in self._consumes or not plain:
                continue
            ratio = max((r[2] / p[2] for i in needs for r in index.get(i, []) if r[0] == m
                         for p in index.get(i, []) if p[0] == plain), default=None)
            if ratio is None:
                continue
            whole = hours + sum(math.ceil(n * hours) * self.consumable_hours(i) for i, n in self._consumes[m].items())
            if whole >= hours * ratio:
                del visits[m]
                visits[plain] = visits.get(plain, 0) + hours * ratio
        for m, hours in sorted(visits.items(), key=lambda kv: -kv[1]):
            skill, rate = self._spot_info()[m]
            got = [f"{min(n, per_hour * hours):,.0f} {i}" + (" (worded rarity: chance assumed)" if (i, m) in self._worded else "")
                   for i, n in needs.items() for mm, _, per_hour, _ in index.get(i, []) if mm == m and per_hour * hours >= 1]
            g.hours += hours
            xp = {skill: rate, **self._side_xp.get(m, {})}
            g.add_xp(xp, hours)
            side = ", ".join(f"+{v * hours:,.0f} {s} xp" for s, v in xp.items())
            g.steps.append(f"spend ~{hours:.1f}h at {m}: {', '.join(got)} ({side})")
            if m in self.flagged:
                self._note_flag(m, self.flagged[m], hours)
            if uses := {i: math.ceil(n * hours) for i, n in self._consumes.get(m, {}).items()}:
                g.steps.append("  using " + ", ".join(f"{n:,} {i}" for i, n in uses.items()))
                need = {}
                for i, n in uses.items():
                    take = min(n, (stock or {}).get(i, 0))
                    if stock is not None:
                        stock[i] = stock.get(i, 0) - take
                    if n > take:
                        need[i] = n - take
                if need:
                    making = set(need) - self._making
                    self._making |= making
                    try:
                        self._merge(g, self._plan(need, stock if stock is not None else dict(self.ctx.player.holdings),
                                                  extra if extra is not None else {}))
                    finally:
                        self._making -= making


def gates(recipe: dict, player) -> list[tuple[str, int, bool]]:
    """The recipe's levels (its own skill's and others) the player is still below, and whether the wiki marks
    each boostable for this recipe."""
    need = [(recipe["skill"], recipe["level"]), *((s, lvl) for s, lvl in recipe["other_skills"])]
    return [(s, lvl, s in recipe.get("boostable", [])) for s, lvl in need if s and player.level(s) < lvl]


@cache
def gathered() -> list[dict]:
    path = datapkg.path("recipe_catalogue.json")
    return json.loads(path.read_text()).get("gathered", []) if path.is_file() else []


@cache
def seeds() -> list[dict]:
    path = datapkg.path("recipe_catalogue.json")
    return json.loads(path.read_text()).get("seeds", []) if path.is_file() else []


@cache
def collection_names() -> frozenset[str]:
    from .catalogue import archaeology_collections

    return frozenset(archaeology_collections()["collections"])


@cache
def crops() -> frozenset[str]:
    """Everything a seed grows (recipe catalogue seeds)."""
    return frozenset(c for seed in seeds() for c in seed["crops"])


@cache
def tables_shops() -> dict[str, list]:
    """{item: [[shop, stock, price, members], ...]} cheapest first: the wiki's Storeline bucket (data/shops.json)
    plus hand-written dialogue sellers it lacks (data/shops_extra.json: Wyson's woad leaves). Packs the shops
    sell (Raw bird meat pack, Spirit shard pack, Feather pack: `pack_contents`) also count as offers of what
    they open into, stock x contents at the price per unit, with a fifth element {"pack", "each"} so the
    source says so and skips the backpack limit (the contents come out noted). User report 2026-11-09."""
    shops: dict[str, list] = {}
    for name in ("shops.json", "shops_extra.json"):
        path = datapkg.path(name)
        if path.is_file():
            for item, offers in json.loads(path.read_text())["items"].items():
                shops.setdefault(item, []).extend(offers)
    for pack, (content, each) in pack_contents().items():
        for shop, stock, price, members in shops.get(pack, []):
            shops.setdefault(content, []).append([shop, stock * each, price / each, members, {"pack": pack, "each": each}])
    for offers in shops.values():
        offers.sort(key=lambda o: (o[2], -o[1]))
    return shops


@cache
def pack_contents() -> dict[str, tuple[str, float]]:
    """{pack item: (what it opens into, how many)} from the wiki's drop tables, where a pack is a "source" with
    one always-dropped row (data/drop_tables.json: "Raw bird meat pack" -> 50 Raw bird meat)."""
    path = datapkg.path("drop_tables.json")
    if not path.is_file():
        return {}
    tables = json.loads(path.read_text())
    tables = tables.get("sources", tables)
    out = {}
    for name, rows in tables.items():
        if name.endswith(" pack") and isinstance(rows, list) and len(rows) == 1 and rows[0][1] == 1.0 and rows[0][2] > 1:
            out[name] = (rows[0][0], float(rows[0][2]))
    return out


@cache
def shop_cadence() -> dict:
    """How shops restock (data/shop_cadence.json): `weekly` shops (a set of names), `daily` {shop: {item: per
    day}} and `per_seconds` {shop: seconds per unit}; anything else refills every SHOP_REFILL_SECONDS."""
    path = datapkg.path("shop_cadence.json")
    d = json.loads(path.read_text()) if path.is_file() else {}
    return {"weekly": frozenset(d.get("weekly", [])), "daily": d.get("daily", {}), "per_seconds": d.get("per_seconds", {})}
