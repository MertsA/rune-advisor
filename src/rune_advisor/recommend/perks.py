"""Suggested perks: which gizmos to make for the player's augmentable gear, from the materials they can get.

For each style's best owned loadout and each augmentable tool (mechanics/item_xp setups), gizmo slots are filled
one at a time with the recipe (materials in slot order) whose outcome is worth the most, net of its materials:

  * Odds are exact (mechanics/perks.gizmo_odds) at the player's Invention level, boosted by the best Invention
    potion they hold.
  * A perk is worth hours where the planner can measure it:
      - damage perks (Precise, Eruptive, Equilibrium, Biting, Ruthless, Aftershock; Blunted, Inaccurate and
        Mediocrity count against): the gain in average damage x the style's planned combat training
        (level plans to plans.goal_levels in the style's skills), as gear upgrades are valued;
      - Enlightened: +3% item xp a rank on the item it sits on, against the planned Invention training;
      - tool perks that raise xp/h (Honed, Furnace, Tinker): against the skill's planned training.
    Every other perk is listed with the game's description and never valued (the user's rule for unlocks);
    perks that get in the way (AVOID) make an outcome one to throw away.
  * Making gizmos is a stopping problem: each one made costs its materials and the outcome is random, so a recipe
    comes with a rule, "keep any of these outcomes, else try again", chosen to maximise the kept outcome's hours
    less the expected materials (gizmo shell included). Materials in the storage are free; what is missing costs
    the gatherer's hours per unit (disassembly chains), and a material with no source can only come from storage.
  * Perks don't stack across a loadout (the highest rank counts), so a later slot only gets credit for what it
    adds; Enlightened is per item. Equilibrium stops critical strikes: with it, Biting is worth nothing.

Candidate recipes: for each perk worth something in the gizmo type, every mix of its four strongest materials,
and for each pair of such perks every mix of their two strongest each, up to the gizmo's slots. Wider searches
(three perks' materials, filler slots, slot-order permutations) are left out: this finds the good, makeable
gizmos, not the rarest combination.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations, combinations_with_replacement

from ..invention import gate_open, potion_boost
from ..mechanics import item_xp
from ..mechanics import perks as mech
from .base import Context

RUTHLESS_UPTIME = 0.5  # share of the five stacks held while killing one monster after another (assumed)
AFTERSHOCK_PER_RANK = 0.007  # up to 40% ability damage a rank per 50,000 dealt: ~1,800 ability damage, half the max
BASE_CRIT_CHANCE = 0.10
MIN_HOURS = 10.0  # planned hours a style or skill counts for at least, so a finished one still ranks its perks
ATTEMPT_HOURS = 10 / 3600  # making a shell and filling it
TOP_SINGLE, TOP_PAIR = 4, 2  # a perk's strongest materials tried alone / paired with another perk's
MAX_KEEP_SHOWN = 4

# Perks that get in the way: an outcome holding one is thrown away.
AVOID = {"Antitheism": "no protection prayers", "Profane": "no prayer potions", "Cautious": "no auto-retaliate",
         "Committed": "always skulled", "Demon Bait": "-30% damage to demons", "Dragon Bait": "-30% damage to dragons",
         "Undead Bait": "-30% damage to undead", "Confused": "teleports you away", "Butterfingers": "drops resources",
         "Cheapskate": "downgrades resources", "Fatiguing": "less adrenaline", "Junk Food": "food heals less"}
# Tool perks that raise xp/h: gain per rank, and the skills whose tool it works on.
TOOL_XP = {
    "Honed": (0.02, ("Woodcutting", "Fishing"), "+2% success chance a rank"),
    "Furnace": (0.05, ("Woodcutting", "Fishing"), "5% a rank of the resources burnt for double xp"),
    "Tinker": (0.0125, ("Firemaking",), "5% a rank chance of +25% xp"),
}
ENLIGHTENED = 0.03  # item xp per rank, on the item it sits on
GIZMO_OF_SLOT = {"2h weapon": ("weapon", 2), "main hand weapon": ("weapon", 1), "off-hand weapon": ("weapon", 1),
                 "off-hand": ("armour", 1), "torso": ("armour", 2), "legs": ("armour", 2), "tool": ("tool", 2)}


def crit_damage(level: int) -> float:
    """Critical strike damage bonus by combat level (Critical strike page): 10% at 1, +5% per 10 levels from 20, 50% at 90."""
    return 0.10 if level < 20 else min(0.50, 0.15 + 0.05 * ((level - 20) // 10))


def damage_gain(ranks: dict[str, int], crit: float) -> float:
    """The change in average damage from a set of perks (fraction): gains multiply; with Equilibrium there are no
    critical strikes, so Biting adds nothing and the base 10% chance is forgone."""
    gain = 1.0
    gain *= 1 + 0.008125 * ranks.get("Precise", 0)  # Precise page: +4.875% at rank 6 on a 110-130% ability
    gain *= 1 + 0.005 * ranks.get("Eruptive", 0)
    gain *= 1 + 0.005 * 5 * RUTHLESS_UPTIME * ranks.get("Ruthless", 0)
    gain *= 1 + AFTERSHOCK_PER_RANK * ranks.get("Aftershock", 0)
    gain *= 1 - 0.01 * ranks.get("Blunted", 0)
    gain *= 1 - 0.01 * ranks.get("Inaccurate", 0)
    gain *= 1 - 0.016 * ranks.get("Mediocrity", 0)  # 3% of the maximum hit a rank
    if eq := ranks.get("Equilibrium", 0):
        gain *= (1.06 + 0.02 * eq) / (1 + BASE_CRIT_CHANCE * crit)
    else:
        gain *= 1 + 0.02 * ranks.get("Biting", 0) * crit / (1 + BASE_CRIT_CHANCE * crit)
    return gain - 1


DAMAGE_PERKS = ("Precise", "Eruptive", "Ruthless", "Aftershock", "Equilibrium", "Biting", "Blunted", "Inaccurate",
                "Mediocrity")
ASSUMED = {"Ruthless": f"{RUTHLESS_UPTIME:.0%} of the five stacks assumed held",
           "Aftershock": "~0.7% a rank assumed (ability damage ~1,800)",
           "Furnace": "burns 5% a rank of what you gather", "Tinker": "assumed to work with the pyro-matic"}


@dataclass
class Target:
    """An item that takes gizmos."""
    item: str
    gizmo: str  # weapon | armour | tool
    capacity: int
    share: float  # of the setup's item xp
    filled: int = 0
    enlightened: int = 0  # rank already suggested on this item


@dataclass
class Pick:
    """One gizmo to make."""
    item: str
    gizmo: str
    ancient: bool
    level: int
    materials: tuple[str, ...]
    keep: list[tuple[mech.Outcome, float]]  # outcomes to keep, with their chance per gizmo made, likeliest first
    chance: float  # of a keeper, per gizmo made
    hours: float  # the kept outcome's expected worth
    cost_hours: float  # expected materials to gather
    gain: str  # what the likeliest keeper gives, in words
    needs: dict[str, int] = field(default_factory=dict)  # materials for the expected gizmos, shells included
    extras: list[str] = field(default_factory=list)  # unvalued perks in the keepers, with the game's description

    @property
    def gizmos(self) -> int:
        return max(1, math.ceil(1 / self.chance - 1e-9)) if self.chance > 0 else 0

    @property
    def net(self) -> float:
        return self.hours - self.cost_hours


@dataclass
class Plan:
    owner: str  # style or skill
    kind: str  # combat | skilling
    hours: float  # planned training the perks are measured against
    floor: bool  # `hours` is the MIN_HOURS floor
    picks: list[Pick] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Valuer:
    """What perks are worth to one loadout (a combat style's gear, or a skill's tool)."""

    def __init__(self, ctx: Context, kind: str, owner: str, goals: dict[str, int], invention_hours: float):
        from .tracks.gear import STYLE_SKILLS

        p = ctx.player
        self.kind, self.owner = kind, owner
        skills = STYLE_SKILLS.get(owner, ()) if kind == "combat" else (owner,)
        planned = sum(ctx.level_plan(s, goals[s]).hours for s in skills if s in goals and p.level(s) < goals[s])
        self.floor = planned < MIN_HOURS
        self.hours = max(planned, MIN_HOURS)
        self.invention_hours = invention_hours
        level = max((p.level(s) for s in skills if s in p.skills), default=1)
        if kind == "combat" and owner == "melee" and "Strength" in p.skills:
            level = p.level("Strength")
        self.crit = crit_damage(level)

    def _gain(self, ranks: dict[str, int]) -> float:
        if self.kind == "combat":
            return damage_gain(ranks, self.crit)
        gain = 1.0
        for perk, (per_rank, skills, _) in TOOL_XP.items():
            if self.owner in skills:
                gain *= 1 + per_rank * ranks.get(perk, 0)
        return gain - 1

    def wanted(self) -> list[str]:
        """Perks worth hours on this loadout."""
        if self.kind == "combat":
            out = [p for p in DAMAGE_PERKS if damage_gain({p: 1}, self.crit) > 0]
        else:
            out = [p for p, (_, skills, _) in TOOL_XP.items() if self.owner in skills]
        return out + (["Enlightened"] if self.invention_hours > 0 else [])

    def hours_of(self, outcome: mech.Outcome, taken: dict[str, int], target: Target, setup_share: float) -> float:
        """Hours the outcome adds on top of the perks already suggested (`taken`); -inf with a perk to avoid."""
        if any(perk in AVOID for perk, _ in outcome):
            return -math.inf
        ranks = dict(taken)
        for perk, rank in outcome:
            if perk != "Enlightened":
                ranks[perk] = max(ranks.get(perk, 0), rank)
        before, after = self._gain(taken), self._gain(ranks)
        hours = self.hours * (after / (1 + after) - before / (1 + before))
        extra = dict(outcome).get("Enlightened", 0) - target.enlightened
        if extra > 0 and setup_share > 0 and self.invention_hours > 0:
            g = ENLIGHTENED * extra * target.share / setup_share
            hours += self.invention_hours * g / (1 + g)
        return hours

    def gain_text(self, outcome: mech.Outcome, taken: dict[str, int], target: Target, setup_share: float) -> str:
        ranks = dict(taken)
        for perk, rank in outcome:
            if perk != "Enlightened":
                ranks[perk] = max(ranks.get(perk, 0), rank)
        before, after = self._gain(taken), self._gain(ranks)
        parts = []
        if abs(after - before) > 1e-9:
            what = "damage" if self.kind == "combat" else f"{self.owner} xp/h"
            parts.append(f"{(1 + after) / (1 + before) - 1:+.1%} {what}")
        extra = dict(outcome).get("Enlightened", 0) - target.enlightened
        if extra > 0 and setup_share > 0:
            parts.append(f"+{ENLIGHTENED * extra * target.share / setup_share:.1%} Invention xp/h")
        return ", ".join(parts)


def _strength(material: str, gizmo: str, perk: str) -> float:
    return next((base + (roll - 1) / 2 for name, base, roll in mech.materials()[material][gizmo] if name == perk), 0.0)


def candidates(gizmo: str, ancient: bool, wanted: list[str], usable: set[str]) -> list[tuple[str, ...]]:
    """Recipes to try: mixes of the strongest usable materials for each wanted perk, and for each pair of them."""
    slots = mech.ANCIENT_SLOTS if ancient else mech.SLOTS
    best: dict[str, list[str]] = {}
    for perk in wanted:
        feeds = [(m, _strength(m, gizmo, perk)) for m in usable
                 if (ancient or not mech.is_ancient_material(m)) and _strength(m, gizmo, perk) > 0]
        if feeds:
            best[perk] = [m for m, _ in sorted(feeds, key=lambda t: (-t[1], t[0]))]
    out: dict[tuple[str, ...], None] = {}
    for mats in best.values():
        for n in range(1, slots + 1):
            for combo in combinations_with_replacement(mats[:TOP_SINGLE], n):
                out[combo] = None
    for a, b in combinations(best, 2):
        mats = list(dict.fromkeys(best[a][:TOP_PAIR] + best[b][:TOP_PAIR]))
        for n in range(2, slots + 1):
            for combo in combinations_with_replacement(mats, n):
                out[combo] = None
    return list(out)


def recipe_units(materials: tuple[str, ...], gizmo: str, ancient: bool) -> dict[str, int]:
    """Material units one gizmo takes: the slots (5 a slot for common materials) and the shell."""
    units = dict(mech.shell(gizmo, ancient))
    for m in materials:
        units[m] = units.get(m, 0) + mech.slot_units(m)
    return units


def _invention_owner(ctx: Context) -> tuple[str, str] | None:
    """(kind, owner) of the loadout the planner's fastest Invention method levels: Enlightened only counts there."""
    from ..cost import typical_rate
    from ..mechanics.combat import GUIDE_STYLE

    rows = sorted(ctx.cost.wiki_rows.get("Invention", []), key=typical_rate, reverse=True)
    for r in rows:
        skill = r.get("base_skill") or ""
        if skill in GUIDE_STYLE:
            return ("combat", GUIDE_STYLE[skill])
        if skill in item_xp.SKILL_K:
            return ("skilling", skill)
    return None


COSMETIC = {"Talking", "Hallucinogenic", "Brassican", "Glow Worm", "Mysterious", "Committed"}
OTHER_MIN_CHANCE = 0.25  # an unvalued perk is listed at the highest rank it reaches this often


@dataclass
class Other:
    """A perk the planner doesn't value, with the gizmo that makes it."""
    perk: str
    rank: int
    chance: float
    materials: tuple[str, ...]
    desc: str


class Suggester:
    def __init__(self, ctx: Context, level: int | None = None, ancient: bool | None = None):
        from .gathering import Gatherer
        from .plans import goal_levels

        self.ctx = ctx
        p = ctx.player
        self.potion, boost = potion_boost(p)
        self.base_level = p.level("Invention")
        self.level = level if level is not None else self.base_level + boost
        self.ancient = bool(gate_open(p, "ancient")) if ancient is None else ancient
        self.goals = goal_levels(ctx)
        goal = self.goals.get("Invention", 0)
        self.invention_hours = ctx.level_plan("Invention", goal).hours if goal > self.base_level else 0.0
        self.invention_owner = _invention_owner(ctx) if self.invention_hours > 0 else None
        self.stock = dict(p.inventories.get("MATERIALS_STORAGE", {}))
        self.gatherer = Gatherer(ctx)
        self._unit: dict[str, float | None] = {}
        self._candidates: dict[tuple, list[tuple[str, ...]]] = {}

    def unit_hours(self, material: str) -> float | None:
        """Hours to gather one unit (its cheapest complete source: a disassembly chain); None without one."""
        if material not in self._unit:
            src = self.gatherer.complete_source(material)
            self._unit[material] = src.cost_per_unit if src is not None else None
        return self._unit[material]

    def usable(self) -> set[str]:
        """Materials the player can put in a gizmo: in storage or with a source, and, for the ones that need a
        blueprint (rare components: Manufactured at 79, the god components, Historic and Classic with ancient
        gizmos), discovered. An ancient preview counts the ancient materials its level would discover."""
        from ..invention import blueprints, discovered

        found = discovered(self.ctx.player) or {}
        gates = {b["name"]: b for b in blueprints()}

        def locked(m: str) -> bool:
            if found.get(m) is not False:
                return False
            b = gates.get(m, {})
            return not (self.ancient and b.get("gate") == "ancient" and b.get("level", 999) <= self.level)

        return {m for m in mech.materials()
                if not locked(m) and (self.stock.get(m, 0) > 0 or self.unit_hours(m) is not None)}

    def _cost(self, units: dict[str, int], gizmos: float) -> float:
        """Hours to gather what `gizmos` gizmos need beyond the storage; inf when a short material has no source."""
        hours = gizmos * ATTEMPT_HOURS
        for m, per in units.items():
            short = gizmos * per - self.stock.get(m, 0)
            if short > 0:
                unit = self.unit_hours(m)
                if unit is None:
                    return math.inf
                hours += short * unit
        return hours

    def _score(self, materials: tuple[str, ...], target: Target, valuer: Valuer, taken: dict[str, int],
               setup_share: float) -> Pick | None:
        odds = mech.gizmo_odds(self.level, target.gizmo, materials, self.ancient)
        if odds is None:
            return None
        made = mech.per_gizmo(odds)
        valued = sorted(((valuer.hours_of(o, taken, target, setup_share), p, o) for o, p in made.items()),
                        key=lambda t: -t[0])
        units = recipe_units(materials, target.gizmo, self.ancient)
        best, chance, worth = None, 0.0, 0.0
        for i, (hours, p, _) in enumerate(valued):
            if hours <= 0:
                break
            chance += p
            worth += p * hours
            if i + 1 < len(valued) and valued[i + 1][0] == hours:
                continue  # equal outcomes are kept together
            cost = self._cost(units, 1 / chance)
            net = worth / chance - cost
            if best is None or net > best[0]:
                best = (net, i + 1, chance, worth / chance, cost)
        if best is None or best[0] <= 0:
            return None
        _, n, chance, hours, cost = best
        keep = sorted(((o, p) for _, p, o in valued[:n]), key=lambda t: -t[1])
        pick = Pick(target.item, target.gizmo, self.ancient, self.level, materials, keep, chance, hours, cost,
                    valuer.gain_text(keep[0][0], taken, target, setup_share))
        pick.needs = {m: per * pick.gizmos for m, per in units.items()}
        return pick

    def _candidates_for(self, gizmo: str, wanted: list[str]) -> list[tuple[str, ...]]:
        key = (gizmo, self.ancient, tuple(wanted))
        if key not in self._candidates:
            self._candidates[key] = candidates(gizmo, self.ancient, wanted, self.usable())
        return self._candidates[key]

    def plan(self, kind: str, owner: str, setup: item_xp.Setup) -> Plan:
        """Fill the setup's gizmo slots, best first."""
        trains_invention = self.invention_owner == (kind, owner)
        valuer = Valuer(self.ctx, kind, owner, self.goals, self.invention_hours if trains_invention else 0.0)
        plan = Plan(owner, kind, valuer.hours, valuer.floor)
        targets = [Target(item_xp.short_name(s.item), *GIZMO_OF_SLOT[s.slot], s.share)
                   for s in setup.active if s.slot in GIZMO_OF_SLOT]
        setup_share = sum(t.share for t in targets)
        taken: dict[str, int] = {}
        wanted = valuer.wanted()
        while True:
            best: tuple[Pick, Target] | None = None
            for gizmo in dict.fromkeys(t.gizmo for t in targets):
                free = [t for t in targets if t.gizmo == gizmo and t.filled < t.capacity]
                if not free:
                    continue
                # Like items differ only in Enlightened: the one with the least of it gets the next gizmo.
                target = min(free, key=lambda t: (t.enlightened, -t.share, t.filled))
                for materials in self._candidates_for(gizmo, wanted):
                    pick = self._score(materials, target, valuer, taken, setup_share)
                    if pick is not None and (best is None or pick.net > best[0].net):
                        best = (pick, target)
            if best is None:
                break
            pick, target = best
            target.filled += 1
            likely = dict(pick.keep[0][0])
            for perk, rank in likely.items():
                if perk == "Enlightened":
                    target.enlightened = max(target.enlightened, rank)
                else:
                    taken[perk] = max(taken.get(perk, 0), rank)
            for m, n in pick.needs.items():
                self.stock[m] = max(0, self.stock.get(m, 0) - n)
            seen = {perk for o, _ in pick.keep for perk, _ in o}
            pick.extras = [f"{perk}: {mech.perks()[perk].get('desc', '')}" for perk in sorted(seen)
                           if perk not in DAMAGE_PERKS and perk not in TOOL_XP and perk != "Enlightened"]
            plan.picks.append(pick)
        for perk in sorted({perk for pk in plan.picks for o, _ in pk.keep for perk, _ in o} & set(ASSUMED)):
            plan.notes.append(f"{perk}: {ASSUMED[perk]}")
        free = [f"{t.item} ({t.capacity - t.filled})" for t in targets if t.filled < t.capacity]
        if free:
            plan.notes.append("gizmo slots left empty, nothing the planner values fits: " + ", ".join(free)
                              + "; --others lists the perks it doesn't value (Scavenging, Crystal Shield, Efficient...) "
                              "with a gizmo for each")
        if valuer.floor:
            plan.notes.append(f"no planned training left here: perks are measured against a nominal {MIN_HOURS:.0f} h")
        return plan


def others(s: Suggester, gizmo: str, skip: set[str]) -> list[Other]:
    """Perks for this gizmo type the planner puts no hours on, each with the recipe (mixes of its two strongest
    usable materials) that most often gives its best reachable rank without a perk to avoid."""
    slots = mech.ANCIENT_SLOTS if s.ancient else mech.SLOTS
    usable = s.usable()
    feeders: dict[str, list[tuple[float, str]]] = {}
    for m in usable:
        if mech.is_ancient_material(m) and not s.ancient:
            continue
        for perk, base, roll in mech.materials()[m][gizmo]:
            feeders.setdefault(perk, []).append((base + (roll - 1) / 2, m))
    out = []
    for perk, feeds in sorted(feeders.items()):
        if perk in skip or perk in AVOID or perk in COSMETIC or perk in DAMAGE_PERKS or perk in TOOL_XP:
            continue
        mats = [m for _, m in sorted(feeds, key=lambda t: (-t[0], t[1]))[:2]]
        best: tuple[int, float, tuple[str, ...]] | None = None
        for n in range(1, slots + 1):
            for combo in combinations_with_replacement(mats, n):
                odds = mech.gizmo_odds(s.level, gizmo, combo, s.ancient)
                if odds is None or s._cost(recipe_units(combo, gizmo, s.ancient), 1) == math.inf:
                    continue
                by_rank: dict[int, float] = {}
                for o, p in mech.per_gizmo(odds).items():
                    rank = dict(o).get(perk, 0)
                    if rank and not any(k in AVOID for k, _ in o):
                        for r in range(1, rank + 1):
                            by_rank[r] = by_rank.get(r, 0.0) + p
                top = max((r for r, p in by_rank.items() if p >= OTHER_MIN_CHANCE), default=0)
                if top and (best is None or (top, by_rank[top]) > best[:2]):
                    best = (top, by_rank[top], combo)
        if best is not None:
            out.append(Other(perk, best[0], best[1], best[2], mech.perks()[perk].get("desc", "")))
    return out


def shortfalls(picks: list[Pick], storage: dict[str, int]) -> dict[str, int]:
    """Materials the picks need beyond the storage (the picks share it)."""
    need: dict[str, int] = {}
    for pick in picks:
        for m, n in pick.needs.items():
            need[m] = need.get(m, 0) + n
    return {m: n - storage.get(m, 0) for m, n in need.items() if n > storage.get(m, 0)}


def suggest(ctx: Context, styles: list[str] | None = None, skills: list[str] | None = None,
            level: int | None = None, ancient: bool | None = None) -> tuple[Suggester, list[Plan]]:
    """Gizmo plans for the styles' loadouts and the skills' tools (default: every style with augmentable gear and
    every skill whose tool the player holds)."""
    s = Suggester(ctx, level, ancient)
    plans = []
    for style in styles if styles is not None else ("melee", "ranged", "magic", "necromancy"):
        setup = item_xp.combat_setup(ctx.player, style)
        if setup.active:
            plans.append(s.plan("combat", style, setup))
    for skill in skills if skills is not None else list(item_xp.SKILL_K):
        setup = item_xp.skilling_setup(ctx.player, skill)
        if setup is not None and setup.active and (skills is not None or not setup.tool_assumed):
            plans.append(s.plan("skilling", skill, setup))
    return s, plans
