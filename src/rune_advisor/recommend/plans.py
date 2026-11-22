"""Turn goals into costed step lists, so tracks can share "what does it take" logic.

Nothing is treated as unavailable: a goal's plan includes every unlock step we can model
(prereq quests, skill levels via resources or training, prerequisite achievements).
Steps we can't model are costed with a default and mark the plan `uncertain`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from .. import datapkg

from ..models import QuestStatus
from ..sources.plugin_db import SKILLTYPE_TO_NAME
from ..cost import WIKI_SKILL_NAMES
from ..xp import SKILL_ALIASES, lamp_xp, level_cap, xp_at
from .base import Context
from .. import blessings, respect
from ..reputation import BEST_REP_PER_HOUR, FACTION_ACHIEVEMENTS
from . import gathering

STARTED_DISCOUNT = 0.75  # assume a started quest is a quarter done
DEFAULT_TEST_HOURS = 1.0  # unmodelled requirement (a flag, an intangible...)
# A tally ("excavate 2,500 artefacts") without a known rate: assume this long per unit, so cost
# scales with how many are left. A crude placeholder until per-activity rates exist.
DEFAULT_SECONDS_PER_TALLY_UNIT = 30
LIST_ITEM_HOURS = 0.1  # one unknown entry of a long list (a music track, a log slot)
MIN_PRIOR_SAMPLES = 5  # modelled achievements a category needs before its median stands in for a guess
ALL_CATEGORIES = "all achievements"  # the last fallback: every category pooled


def category_priors(ctx: Context) -> dict[str, tuple[float, int]]:
    """{wiki category: (median hours, samples)} of the activity and quest part (skill training left out) of the
    player's open achievements with no guessed hours: a typical cost for a flag in that category the planner
    can't model."""
    if ctx._category_priors is None:
        ctx._category_priors = {}  # while computing, fallbacks use the flat guess (no recursion)
        by_cat: dict[str, list[float]] = {}
        todo = [a for a in ctx.player.achievements.values() if not a.completed and a.category]
        for i, a in enumerate(todo):
            ctx.report(i, len(todo))  # the open ones only: they take the time
            plan = achievement_plan(ctx, a.name)
            # Only the activity/quest part: a flag is an activity, and level grinds would swamp the median.
            activity = plan.hours - plan.skill_hours
            if activity > 0 and plan.guessed_hours == 0 and not plan.needs_membership:
                by_cat.setdefault(a.category, []).append(activity)
                by_cat.setdefault(ALL_CATEGORIES, []).append(activity)
                if " / " in a.category:  # also pooled by main category ("Skills") for thin subcategories
                    by_cat.setdefault(a.category.split(" / ")[0], []).append(activity)
        ctx._category_priors = {c: (sorted(h)[len(h) // 2], len(h)) for c, h in by_cat.items()
                                if len(h) >= MIN_PRIOR_SAMPLES}
    return ctx._category_priors


def guess_hours(ctx: Context, name: str) -> tuple[float, str]:
    """(hours, why) for an unmodelled requirement of achievement `name`: its category's prior, else the flat guess."""
    a = ctx.player.achievements.get(name)
    priors = category_priors(ctx) if a and a.category else {}
    for cat in ((a.category, a.category.split(" / ")[0], ALL_CATEGORIES) if priors else ()):
        if prior := priors.get(cat):
            hours, n = prior
            return hours, f"typical for {cat}: median of {n} modelled"
    return DEFAULT_TEST_HOURS, "flat guess"




@dataclass
class Plan:
    hours: float = 0.0
    steps: list[str] = field(default_factory=list)
    quest_points: int = 0
    quests: list[str] = field(default_factory=list)  # incomplete quests the plan finishes
    runescore: int = 0  # RuneScore from every achievement the plan completes, prereqs included
    uncertain: bool = False
    # Materials the plan consumes beyond the player's holdings (gathering not modelled yet).
    materials_short: dict[str, int] = field(default_factory=dict)
    needs_membership: bool = False  # some step is impossible on a free-to-play account
    # Training hours the xp this plan earns saves: quest rewards (data/quest_xp.json) and xp gained doing
    # the activities it costs (kills for a kill count). Kept apart from `hours`: candidates net it off their
    # cost like a by-product (never below MIN_REWARD_SHARE).
    reward_hours: float = 0.0
    # For the guide: the first unmet requirement of each part ("93 Summoning (have 92, ~1.5h)"), why the
    # hours may be off (`unknowns`, set wherever `uncertain` is), and what the plan trains (for mood tags).
    needs: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    skills: set[str] = field(default_factory=set)
    guessed_hours: float = 0.0  # hours that are placeholders (DEFAULT_TEST_HOURS, per-unit tally guesses)
    skill_hours: float = 0.0  # hours of skill training in `hours` (the rest is quests and activities)

    @property
    def net_hours(self) -> float:
        """Hours less what the xp earned on the way is worth (never below MIN_REWARD_SHARE of them)."""
        return max(MIN_REWARD_SHARE * self.hours, self.hours - self.reward_hours)

    def guide_fields(self) -> dict:
        """Candidate keyword arguments for the guide's view of this plan."""
        # Candidates cost net_hours, so the guessed part shrinks with it.
        share = self.net_hours / self.hours if self.hours else 1.0
        return {"needs": list(self.needs), "unknowns": list(self.unknowns), "skills": set(self.skills),
                "guessed_hours": self.guessed_hours * share}

    def reward_note(self) -> str | None:
        if self.reward_hours < 0.05:
            return None
        return f"xp earned saves ~{self.reward_hours:.1f}h of training (cost {self.hours:.1f}h -> {self.net_hours:.1f}h)"

    def add(self, other: "Plan") -> "Plan":
        self.hours += other.hours
        self.steps += other.steps
        self.quest_points += other.quest_points
        self.runescore += other.runescore
        self.reward_hours += other.reward_hours
        for name, n in other.materials_short.items():
            self.materials_short[name] = self.materials_short.get(name, 0) + n
        self.quests += [q for q in other.quests if q not in self.quests]
        self.uncertain |= other.uncertain
        self.needs_membership |= other.needs_membership
        self.needs += [n for n in other.needs if n not in self.needs]
        self.unknowns += [u for u in other.unknowns if u not in self.unknowns]
        self.skills |= other.skills
        self.guessed_hours += other.guessed_hours
        self.skill_hours += other.skill_hours
        return self


MIN_REWARD_SHARE = 0.1  # earned xp can cut a plan's cost to this share of its hours, not to zero


@cache
def _reward_data(name: str) -> dict[str, dict]:
    return json.loads(datapkg.path(name).read_text())


def quest_xp() -> dict[str, dict]:
    """xp rewards per (mini)quest, from scripts/build_quest_xp.py (RuneScape Wiki)."""
    return _reward_data("quest_xp.json")


def achievement_xp() -> dict[str, dict]:
    """Lamp rewards per task set ("Ardougne Set Tasks - Elite"), from scripts/build_achievement_xp.py."""
    return _reward_data("achievement_xp.json")


# "Firemaking 110", "Herblore 113 (Halfway There)": a level as its own achievement, not an unlock.
LEVEL_MILESTONE = re.compile(r"^[A-Z][a-z]+ \d+( \(Halfway There\))?$")
GOAL_LEVEL_CAP = 120  # a 120 requirement is the skill's cap, a very long-term goal: never what a lamp is for


def goal_levels(ctx: Context) -> dict[str, int]:
    """Per skill, the highest level an open goal that *unlocks* something still needs, below 120: skill
    requirements of open achievements (the game's, and the wiki's trees) other than level milestones ("Firemaking
    110", "Halfway There", the bare "Skills" category: Master Skiller), incomplete quests' requirements, undiscovered
    Invention blueprints, wiki recipes the player's level doesn't make yet (masterwork gear at Crafting/Smithing 110,
    adrenaline renewals at Herblore 119, eternal gilded furniture to Construction 118), and 99 for any skill below
    it. Skills missing here have nothing left to unlock before 120 (2026-11-11: e.g. Firemaking at 100), so xp there
    is worth nothing to the free_xp track. Levels already reached are left out."""
    from ..invention import blueprints, discovered
    from .catalogue import catalogue

    p = ctx.player
    goals: dict[str, int] = {}

    def want(skill: str, level) -> None:
        skill = SKILL_ALIASES.get(skill, skill)
        level = int(level or 0)
        # Above the skill's cap is boost-only content, not a level to train to (Primal feast: Cooking 115 with the
        # cap at 99, the wiki's one such case; an extreme cooking potion gets there).
        if skill in p.skills and p.skills[skill].level < level < GOAL_LEVEL_CAP and level <= level_cap(skill, p.members):
            goals[skill] = max(goals.get(skill, 0), level)

    def leaves(node):
        if isinstance(node, dict):
            if node.get("type") == "skill":
                yield node
            for u in node.get("units") or []:
                yield from leaves(u)

    for name, a in p.achievements.items():
        if a.completed or LEVEL_MILESTONE.match(name) or (a.category or "") == "Skills":
            continue
        for r in a.requirements:
            if r.get("kind") == "skill" and len(r.get("skills") or []) == 1:
                want(SKILLTYPE_TO_NAME.get(r["skills"][0], r["skills"][0]), r.get("totalLevels"))
        for leaf in leaves((getattr(ctx, "achievement_reqs", None) or {}).get(name)):
            want(leaf.get("name", ""), leaf.get("level"))
    if quest_info := getattr(ctx, "quest_info", None):  # test doubles may lack these
        for title in incomplete_quests(ctx):
            if (info := quest_info(title)) is not None:
                for skill, level in info.skill_reqs.items():
                    want(skill, level)
    done = discovered(p) or {}
    for b in blueprints():
        if not done.get(b["name"]):
            want("Invention", b["level"])
    for r in catalogue():
        if r.get("skill") and not r.get("conversion"):
            want(r["skill"], r.get("level"))
    for skill, s in p.skills.items():
        if s.level < 99:
            want(skill, 99)
    return goals


def xp_hours_saved(ctx: Context, skill: str, xp: float) -> float:
    """Training hours `xp` in `skill` saves: only the part up to the skill's cap counts (xp past 99 Smithing
    saves nothing), at the planner's rate for the player's level."""
    p = ctx.player
    skill = SKILL_ALIASES.get(skill, skill)  # wiki "Runecrafting"/"Constitution" -> the player's names
    if skill not in p.skills:
        return 0.0
    to_cap = max(0, xp_at(skill, level_cap(skill, p.members)) - p.skills[skill].xp)
    return min(xp, to_cap) / max(1.0, ctx.cost.rate(skill, p.level(skill)))


def quest_reward_hours(ctx: Context, title: str) -> tuple[float, float]:
    """(training hours saved, xp) for a quest's xp rewards at the player's current levels and rates."""
    return reward_hours(ctx, quest_xp().get(title))


def reward_hours(ctx: Context, reward: dict | None) -> tuple[float, float]:
    """(training hours saved, xp) for xp rewards in data/quest_xp.json's shape: set xp per skill, and
    choices/lamps, which go to the allowed skill (at or above their minimum level) saving the most time."""
    p = ctx.player
    if not reward:
        return 0.0, 0.0

    def xp_in(skill: str, amount) -> float:
        level = p.level(SKILL_ALIASES.get(skill, skill))
        return lamp_xp(level, amount.split()[0]) if isinstance(amount, str) else float(amount)

    def hours(skill: str, amount) -> float:
        return xp_hours_saved(ctx, skill, xp_in(skill, amount))

    total_h = total_xp = 0.0
    for skill, amount in reward["set"].items():
        if SKILL_ALIASES.get(skill, skill) in p.skills:
            total_h, total_xp = total_h + hours(skill, amount), total_xp + xp_in(skill, amount)
    for choice in reward["choice"]:
        allowed = list(p.skills) if choice["skills"] == "any" else [SKILL_ALIASES.get(s, s) for s in choice["skills"]]
        usable = [s for s in allowed if s in p.skills and p.level(s) >= choice["min_level"]]
        if usable:
            best = max(usable, key=lambda s: hours(s, choice["xp"]))
            total_h, total_xp = total_h + hours(best, choice["xp"]), total_xp + xp_in(best, choice["xp"])
    return total_h, total_xp


def incomplete_quests(ctx: Context, include_unlisted: bool = False) -> dict:
    p = ctx.player
    return {t: q for t, q in p.quests.items()
            if q.status != QuestStatus.COMPLETED and (p.members or not q.members)
            and (q.listed or include_unlisted)}


def quest_bundle(ctx: Context, title: str) -> list[str]:
    """`title` plus its incomplete prerequisite quests (from wiki requirement links), prereqs first."""
    incomplete = incomplete_quests(ctx)
    order: list[str] = []

    def visit(t: str, seen: set[str]) -> None:
        if t in seen or t in order or t not in incomplete:
            return
        seen.add(t)
        info = ctx.quest_info(t) if ctx.quest_info else None
        for linked in (info.linked_pages if info else []):
            if linked != t:
                visit(linked, seen)
        order.append(t)

    visit(title, set())
    return order


def skill_plan(ctx: Context, skill: str, level: int) -> Plan:
    p = ctx.player
    if p.level(skill) >= level:
        return Plan()
    if level > level_cap(skill, p.members):
        # Beyond the free-to-play cap: only reachable with membership.
        return Plan(uncertain=True, needs_membership=True, steps=[f"{level} {skill} needs membership"],
                    needs=[f"{level} {skill}"], unknowns=[f"{level} {skill} needs membership"])
    lp = ctx.level_plan(skill, level)
    short = lp.materials_short or lp.materials_unmodelled
    return Plan(hours=lp.hours, steps=[f"get {level} {skill} (have {p.level(skill)}, ~{lp.hours:.1f}h)", *lp.steps],
                materials_short=dict(lp.materials_short), skills={skill}, skill_hours=lp.hours,
                needs=[f"{level} {skill} (have {p.level(skill)}, ~{lp.hours:.1f}h)"],
                unknowns=[f"{skill}: supplies not fully costed"] if short else [])


def quests_plan(ctx: Context, titles: list[str]) -> Plan:
    """Plan to finish a set of quests: their lengths plus the highest skill requirement among them."""
    incomplete = incomplete_quests(ctx, include_unlisted=True)
    plan, skill_reqs = Plan(), {}
    for t in titles:
        q = incomplete.get(t)
        if q is None:
            known = ctx.player.quests.get(t)
            if known is None:
                # Required but not in our quest data at all: cost it as a typical quest, never as free.
                plan.hours += ctx.cost.quest_hours(None)
                plan.uncertain = True
                plan.quests.append(t)
                plan.steps.append(f"do {t} (not in quest data)")
                plan.needs.append(f"quest {t}")
                plan.unknowns.append(f"{t} isn't in the quest data (costed as a typical quest)")
            elif not known.done and known.members and not ctx.player.members:
                plan.needs_membership = plan.uncertain = True
                plan.steps.append(f"{t} needs membership")
                plan.unknowns.append(f"{t} needs membership")
            continue
        info = ctx.quest_info(t) if ctx.quest_info else None
        hours = ctx.cost.quest_hours(info.length if info else None)
        plan.hours += hours * (STARTED_DISCOUNT if q.status == QuestStatus.STARTED else 1.0)
        plan.needs.append(f"quest {t} (~{hours:.1f}h)")
        plan.quest_points += q.quest_points
        plan.quests.append(t)
        plan.reward_hours += quest_reward_hours(ctx, t)[0]
        for skill, lvl in (info.skill_reqs if info else {}).items():
            skill_reqs[skill] = max(skill_reqs.get(skill, 0), lvl)
        # Items the quest needs that the player doesn't hold. A normal account buys them in minutes; an
        # ironman has to make, buy from a shop or loot them, which can dwarf the quest itself (the ring of
        # stone for Wandering Ga'al is a 1/5,000 drop).
        if ctx.player.ironman:
            holdings = ctx.player.holdings
            for item, qty in (info.items if info else {}).items():
                short = qty - holdings.get(item, 0)
                if short <= 0:
                    continue
                got = item_plan(ctx, item, short)
                if got is None:
                    plan.uncertain = True
                    plan.steps.append(f"get {item} for {t} (not in your export, no source known)")
                    plan.unknowns.append(f"{t} needs {item}: not in your export and no source known")
                    continue
                plan.add(got)
        if info is None and not q.eligible:
            plan.uncertain = True  # can't see why it's ineligible
            plan.unknowns.append(f"no wiki data on {t}'s requirements")
    for skill, lvl in skill_reqs.items():
        plan.add(skill_plan(ctx, skill, lvl))
    plan.steps.append("do " + ", ".join(plan.quests) if plan.quests else "")
    plan.steps = [s for s in plan.steps if s]
    return plan


def quest_plan(ctx: Context, title: str) -> Plan:
    return quests_plan(ctx, quest_bundle(ctx, title))


def total_level_plan(ctx: Context, skills: list[str], total: int) -> Plan:
    """Cheapest way to raise the summed level of `skills` to `total`, one level at a time."""
    p = ctx.player
    levels = {s: p.level(s) for s in skills}
    if len(skills) == 1:
        return skill_plan(ctx, skills[0], total)
    plan = Plan()
    targets = dict(levels)
    while sum(targets.values()) < total:
        options = [(ctx.level_plan(s, targets[s] + 1).hours - ctx.level_plan(s, targets[s]).hours, s)
                   for s in skills if targets[s] < level_cap(s, p.members)]
        if not options:
            plan.uncertain = True
            break
        _, best = min(options)
        targets[best] += 1
    for s in skills:
        if targets[s] > levels[s]:
            plan.add(skill_plan(ctx, s, targets[s]))
    return plan


def requirement_tree_plan(ctx: Context, node: dict, seen: set[str]) -> Plan:
    """Cost a wiki requirement tree: AND sums its parts, OR takes the cheapest branch."""
    kind = node.get("type")
    if kind in ("and", "or"):
        parts = [requirement_tree_plan(ctx, unit, seen) for unit in node.get("units", [])]
        if not parts:
            return Plan()
        if kind == "or":
            return min(parts, key=lambda p: p.hours)
        plan = Plan()
        for part in parts:
            plan.add(part)
        return plan
    if kind == "skill":
        skill = WIKI_SKILL_NAMES.get(node["name"], node["name"])
        return skill_plan(ctx, skill, int(node["level"]))
    if kind == "quest":
        title = node["name"]
        known = ctx.player.quests.get(title)
        if known is None or known.done:
            return Plan()
        plan = quest_plan(ctx, title)
        plan.uncertain |= bool(node.get("partial"))  # only part of the quest is needed
        return plan
    if kind == "achievement":
        return achievement_plan(ctx, node["name"], seen)
    # "intangible": an activity or unlock the wiki describes in prose.
    content = node.get("content", "unmodelled requirement")
    return Plan(hours=DEFAULT_TEST_HOURS, guessed_hours=DEFAULT_TEST_HOURS, uncertain=True, steps=[content], needs=[content],
                unknowns=[f"unmodelled: {content} (guessed {DEFAULT_TEST_HOURS:g}h)"])


def activity_xp_hours(ctx: Context, rate, hours: float) -> float:
    """Training hours saved by the xp earned doing an activity for `hours` (activities.Rate.xp_per_hour)."""
    return sum(xp_hours_saved(ctx, skill, xp_h * hours) for skill, xp_h in rate.xp_per_hour.items())


def item_plan(ctx: Context, item: str, qty: int = 1, gather: bool = True) -> Plan | None:
    """Hours to get `qty` of an item the player lacks, or None when no source is known. An ironman makes,
    shop-buys or loots it through the gatherer (recommend/gathering.py) unless its only source is a drop;
    otherwise it's expected kills at the best drop source the player can do. `gather=False` skips the
    gatherer (tallies of many items are costed per drop only)."""
    desc = item if qty == 1 else f"{qty:,} {item}"
    if gather and ctx.player.ironman and (src := ctx.gatherer.source(item)) and src.kind != "drop":
        g = ctx.gatherer.plan({item: qty})
        # Shop coins: the money pouch and group mates' purses first, the rest earned at the money-making rate.
        short, transfers = ctx.cover_coins(g.coins)
        hours = g.hours + short / gathering.COINS_PER_HOUR
        if short > 0:
            transfers.append(f"earn ~{short:,.0f} more gp for the shops (~{short / gathering.COINS_PER_HOUR:.1f}h at "
                             f"{gathering.COINS_PER_HOUR / 1e6:g}m gp/h)")
        return Plan(hours=hours, uncertain=bool(g.short), needs=[f"{desc} (~{hours:.1f}h)"],
                    unknowns=[f"no source known for {', '.join(g.short)}"] if g.short else [],
                    steps=[f"make/buy {desc}: ~{hours:.1f}h"] + ["  " + s for s in g.steps + transfers])
    if found := ctx.activities.item_hours(item):
        per_drop, r = found
        hours = per_drop * qty
        return Plan(hours=hours, reward_hours=activity_xp_hours(ctx, r, hours), skills=set(r.xp_per_hour),
                    needs=[f"{desc} from {r.source} (~{hours:.1f}h)"],
                    unknowns=["kill rate estimated from gear"] if r.how.startswith("estimated") else [],
                    steps=[f"get {desc} as a drop: ~{hours:.1f}h at {r.how} ({r.source})"])
    return None


def tally_plan(ctx: Context, name: str, t: dict, many: bool = False) -> Plan:
    """Cost of a tally/flag test: from the activity it counts when we can tell (recommend/activities.py),
    or the item it names (made, bought or looted: recommend/gathering.py), else a generous guess (at
    least DEFAULT_TEST_HOURS, so unknown activities don't look free; `many` = one of a long list of
    small things, like music tracks, guessed at LIST_ITEM_HOURS each)."""
    total, current = t.get("totalValue") or 0, t.get("currentValue") or 0
    remaining = max(0, total - current) or 1
    desc = t.get("description") or ""
    acts = ctx.activities
    faction = next((f for f, a in FACTION_ACHIEVEMENTS.items() if a == name), None)
    if faction and total > 1:
        hours = remaining / BEST_REP_PER_HOUR
        return Plan(hours=hours, uncertain=True, needs=[f"{remaining:,.0f} {faction} reputation (~{hours:.0f}h)"],
                    unknowns=["reputation is daily-capped: calendar time is longer"], steps=[
            f"{remaining:,.0f} more {faction} reputation: ~{hours:.0f}h at soul obelisks (~{BEST_REP_PER_HOUR:,}/h, "
            "daily-capped), less whatever skilling in the district earns on the way"])
    if name in respect.ACHIEVEMENTS and desc in respect.PRICES:
        return respect_plan(ctx, desc)
    if desc and (got := item_plan(ctx, desc, gather=total <= 1)):
        return got
    wiki_desc = ctx.achievement_descriptions.get(name, "")
    if total > 1 and wiki_desc and (r := acts.source_in(wiki_desc)):
        hours = remaining / r.per_hour
        return Plan(hours=hours, reward_hours=activity_xp_hours(ctx, r, hours), skills=set(r.xp_per_hour),
                    needs=[f"{remaining:,} more at {r.source} (~{hours:.1f}h)"],
                    unknowns=["kill rate estimated from gear"] if r.how.startswith("estimated") else [],
                    steps=[f"{remaining:,} more at {r.source} (~{r.per_hour:,.0f}/h via {r.how}, ~{hours:.1f}h)"])
    if many and total <= 1:
        hours, how = LIST_ITEM_HOURS, "list entry"
    elif total > 1:
        hours, how = max(DEFAULT_TEST_HOURS, remaining * DEFAULT_SECONDS_PER_TALLY_UNIT / 3600), "per-unit guess"
    else:
        hours, how = guess_hours(ctx, name)
    # The game's test often has no description; the wiki's achievement description says what it is.
    label = desc or (f"{wiki_desc} ({current:,}/{total:,})" if wiki_desc and total > 1 else wiki_desc) \
        or (f"tally {current:,}/{total:,}" if total > 1 else "requirement flag")
    return Plan(hours=hours, guessed_hours=hours, uncertain=True, steps=[label], needs=[label],
                unknowns=[f"no rate for \"{label}\" (guessed {hours:.1f}h, {how})"])


def nested(name: str, plan: Plan) -> Plan:
    """A prerequisite achievement's plan with its needs folded into one line ("Abyssal Valet: 93 Summoning")."""
    if plan.needs:
        plan.needs = [f"{name}: " + "; ".join(plan.needs)]
    elif plan.hours:
        plan.needs = [name]
    return plan


def respect_plan(ctx: Context, reward: str) -> Plan:
    """One Artisans' Workshop reward: its respect as Smithing xp earned at the workshop's anvils (respect.py). The
    xp is training the player keeps, so it comes off as earned xp."""
    p = ctx.player
    percent = respect.PRICES[reward]
    xp = respect.xp_for(p, percent)
    hours = xp / max(1.0, ctx.cost.rate("Smithing", p.level("Smithing")))
    shield = f" ({respect.SHIELD} worn: +5%)" if respect.bonus(p) > 1 else ""
    return Plan(hours=hours, reward_hours=xp_hours_saved(ctx, "Smithing", xp), skills={"Smithing"}, uncertain=True,
                needs=[f"{reward}: {percent}% Artisans' Workshop respect (~{hours:.1f}h of smithing there)"],
                unknowns=["respect held isn't exported: counted from 0; it caps at 100%, so spend it as it fills"],
                steps=[f"{reward}: {percent}% respect = {xp:,.0f} Smithing xp from equipment finished at the "
                       f"Artisans' Workshop anvils{shield}, then buy it from Egil"])


def blessings_plan(ctx: Context) -> Plan:
    """The Shrine of the Spirit Wolves' unlocks: Blessings of the Wolf still short of 5,000, one per pouch infused
    at the Shrine of Inanna, at the cheapest pouch to make there (blessings.py)."""
    have = blessings.known_blessings(ctx.player)
    left = blessings.TOTAL - have
    pouch = blessings.cheapest_pouch(ctx)
    nxt = blessings.next_unlock(have)
    unknowns = ["the Blessings balance isn't exported: counted from the last reward unlocked (up to 599 more held)"]
    if pouch is None:
        hours = left * DEFAULT_SECONDS_PER_TALLY_UNIT / 3600
        return Plan(hours=hours, guessed_hours=hours, uncertain=True, skills={"Summoning"},
                    needs=[f"{left:,} more pouches infused at the Shrine of Inanna"],
                    unknowns=unknowns + ["no pouch with a costed chain: per-unit guess"])
    name, each = pouch
    hours = left * each
    need = f"{left:,} more pouches infused at the Shrine of Inanna (~{hours:.1f}h as {name})"
    steps = [f"infuse {left:,} more pouches at the Shrine of Inanna's empowered obelisk for {left:,} Blessings of the "
             f"Wolf ({have:,} of {blessings.TOTAL:,} proven): cheapest is {name}, ~{each * 60:.2f} min each with its "
             "materials, after swapping the pouches back for 70% of their shards",
             "any pouch infused there counts: Summoning training and pouches made to disassemble too"]
    if nxt:
        steps.append(f"next unlock: {nxt[0]} in {nxt[1]:,} pouches")
    return Plan(hours=hours, uncertain=True, skills={"Summoning"}, needs=[need], unknowns=unknowns, steps=steps)


def achievement_plan(ctx: Context, name: str, _seen: set[str] | None = None) -> Plan:
    """Cost of completing an achievement from its exported requirement tests.

    Tests in the same group are all needed; the achievement needs `required_groups` of its
    groups, so we take the cheapest ones.
    """
    seen = _seen or set()
    a = ctx.player.achievements.get(name)
    if a is None or a.completed:
        return Plan()
    if name in seen:
        return Plan(uncertain=True, unknowns=[f"{name} requires itself (circular requirement data)"])
    seen = seen | {name}
    if name == blessings.ACHIEVEMENT:
        return blessings_plan(ctx)

    # Tests by group: the plans of the ones still open, and how many already pass. A group may need
    # only some of its tests (a.group_needs: "any 3 of these 10"), so the cheapest open ones fill the gap.
    open_tests: dict[int, list[Plan]] = {}
    passed: dict[int, int] = {}
    for t in a.requirements:
        g = t.get("group", 0)
        open_tests.setdefault(g, [])
        if t.get("complete"):
            passed[g] = passed.get(g, 0) + 1
            continue
        kind = t.get("kind")
        if kind == "skill":
            names = [SKILLTYPE_TO_NAME.get(s, s) for s in t.get("skills", [])]
            open_tests[g].append(total_level_plan(ctx, names, t.get("totalLevels", 0)))
        elif kind == "quest" and t.get("quest"):
            open_tests[g].append(quest_plan(ctx, t["quest"]))
        elif kind == "achievement" and t.get("achievement"):
            open_tests[g].append(nested(t["achievement"], achievement_plan(ctx, t["achievement"], seen)))
        else:
            open_tests[g].append(tally_plan(ctx, name, t))
    groups: dict[int, Plan] = {}
    for g, plans in open_tests.items():
        need = a.group_needs.get(g)
        if need is not None and need - passed.get(g, 0) < len(plans):
            plans = sorted(plans, key=lambda pl: pl.hours)[: max(0, need - passed.get(g, 0))]
        groups[g] = Plan()
        for pl in plans:
            groups[g].add(pl)

    plan = Plan()
    # The game often gates an achievement on a single flag ("re-enact an event at the museum")
    # and leaves the skill/quest requirements implicit; the wiki lists them.
    gated_by_flags_only = not any(t.get("kind") in ("skill", "quest", "achievement") for t in a.requirements)
    if gated_by_flags_only and ctx.achievement_reqs and name in ctx.achievement_reqs:
        plan.add(requirement_tree_plan(ctx, ctx.achievement_reqs[name], seen))
    chosen = sorted(groups.values(), key=lambda g: g.hours)[: a.required_groups or len(groups)]
    for g in chosen:
        plan.add(g)
    plan.reward_hours += reward_hours(ctx, achievement_xp().get(name))[0]  # task-set lamps
    if not a.requirements:
        # We know nothing about what it takes; assume a generic activity.
        guess, how = guess_hours(ctx, name)
        plan.hours += guess * (1.0 - (a.progress or 0.0))
        plan.guessed_hours += guess * (1.0 - (a.progress or 0.0))
        plan.uncertain = True
        desc = ctx.achievement_descriptions.get(name, "")
        plan.needs.append(desc or name)
        plan.unknowns.append(f"no requirement data for {name} (guessed {guess:.1f}h, {how})")
    plan.runescore += a.runescore
    plan.steps.append(f"complete {name}")
    return plan
