"""Long-horizon roadmaps: everything left for a big achievement (e.g. the Completionist Cape).

Costing each sub-achievement on its own double counts: Master Skiller's 120s also cover every other
achievement's skill requirements, quests are shared, and gathering for one skill feeds another.
So the requirement tree is walked once and its needs merged:
  * skills: the highest level any requirement asks for, planned together (joint.py: shared bank
    and lamps, side xp from one goal counted toward the others);
  * quests: each unfinished quest once (with its own requirement chain, plans.quests_plan);
  * activities: everything else (tallies, flags, items to make), each costed by plans.tally_plan.
"Skill N" achievements ("Archaeology 120") become skill targets. Achievements the game gates on a
flag use the wiki's requirement tree, as achievement_plan does.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field

from ..cost import WIKI_SKILL_NAMES
from ..sources.plugin_db import SKILLTYPE_TO_NAME
from ..xp import SKILL_ALIASES, _skills
from .base import Context
from .joint import JointPlan, joint_plan
from .plans import Plan, quests_plan, tally_plan

_SKILL_ACHIEVEMENT = re.compile(r"^([A-Z][a-z]+) (\d{2,3})$")


@dataclass
class Activity:
    achievement: str
    plan: Plan
    tests: list[dict] = field(default_factory=list)  # the open flags, costed once skills are done


@dataclass
class Roadmap:
    target: str
    achievements: list[str] = field(default_factory=list)  # unfinished, in the tree
    skills: dict[str, int] = field(default_factory=dict)
    quests: set[str] = field(default_factory=set)
    activities: list[Activity] = field(default_factory=list)
    joint: JointPlan | None = None
    quest_plan: Plan | None = None

    @property
    def skill_hours(self) -> float:
        return self.joint.hours if self.joint else 0.0

    @property
    def quest_hours(self) -> float:
        return self.quest_plan.hours if self.quest_plan else 0.0

    @property
    def activity_hours(self) -> float:
        return sum(a.plan.hours for a in self.activities)

    @property
    def calendar_hours(self) -> float:
        """Calendar time the skill plans wait on: farming growth and Managing Miscellania collections."""
        return sum(s.plan.calendar_hours for s in self.joint.steps) if self.joint else 0.0

    @property
    def hours(self) -> float:
        return self.skill_hours + self.quest_hours + self.activity_hours

    @property
    def uncertain_hours(self) -> float:
        """Hours from parts we can't model precisely (lower bounds / guesses)."""
        return sum(a.plan.hours for a in self.activities if a.plan.uncertain) + \
            sum(s.plan.hours for s in self.joint.steps if s.plan.materials_unmodelled or s.plan.materials_short) \
            if self.joint else sum(a.plan.hours for a in self.activities if a.plan.uncertain)


def _skill_name(name: str) -> str | None:
    name = WIKI_SKILL_NAMES.get(name, SKILL_ALIASES.get(name, name))
    return name if name in _skills()["skills"] else None


def _need_skill(rm: Roadmap, skill: str | None, level: int) -> None:
    if skill:
        rm.skills[skill] = max(level, rm.skills.get(skill, 0))


def _walk_tree(ctx: Context, rm: Roadmap, node: dict, owner: str, seen: set[str]) -> None:
    kind = node.get("type")
    if kind in ("and", "or"):
        units = node.get("units", [])
        # "or": the first branch stands in for the choice (the planner can't see which is cheaper here).
        for unit in units[:1] if kind == "or" else units:
            _walk_tree(ctx, rm, unit, owner, seen)
    elif kind == "skill":
        _need_skill(rm, _skill_name(node["name"]), int(node["level"]))
    elif kind == "quest":
        q = ctx.player.quests.get(node["name"])
        if q is not None and not q.done:
            rm.quests.add(node["name"])
    elif kind == "achievement":
        _walk(ctx, rm, node["name"], seen)
    else:
        rm.activities.append(Activity(owner, Plan(hours=1.0, uncertain=True,
                                                  steps=[node.get("content", "unmodelled requirement")])))


def _walk(ctx: Context, rm: Roadmap, name: str, seen: set[str]) -> None:
    if name in seen:
        return
    seen.add(name)
    a = ctx.player.achievements.get(name)
    if a is None or a.completed:
        return
    rm.achievements.append(name)
    if (m := _SKILL_ACHIEVEMENT.match(name)) and (skill := _skill_name(m[1])):
        _need_skill(rm, skill, int(m[2]))
        return
    gated_by_flags_only = not any(t.get("kind") in ("skill", "quest", "achievement") for t in a.requirements)
    if gated_by_flags_only and ctx.achievement_reqs and name in ctx.achievement_reqs:
        _walk_tree(ctx, rm, ctx.achievement_reqs[name], name, seen)
    groups: dict[int, list[dict]] = {}
    for t in a.requirements:
        groups.setdefault(t.get("group", 0), []).append(t)
    # Achievements that need only some groups: take the groups closest to done.
    chosen = sorted(groups.values(), key=lambda g: sum(not t.get("complete") for t in g))
    for group in chosen[: a.required_groups or len(chosen)]:
        flags = [t for t in group if not t.get("complete") and t.get("kind") not in ("skill", "quest", "achievement")]
        for t in group:
            if t.get("complete"):
                continue
            kind = t.get("kind")
            if kind == "achievement" and t.get("achievement"):
                _walk(ctx, rm, t["achievement"], seen)
            elif kind == "skill":
                names = [SKILLTYPE_TO_NAME.get(s, s) for s in t.get("skills", [])]
                if len(names) == 1:
                    _need_skill(rm, names[0], int(t.get("totalLevels", 0)))
            elif kind == "quest" and t.get("quest"):
                rm.quests.add(t["quest"])
        if flags:
            rm.activities.append(Activity(name, Plan(), flags))


# Achievements with this many open flags (music tracks, collection logs) are lists of small things:
# unknown ones cost less each than a one-off unmodelled requirement.
MANY_FLAGS = 20


def roadmap(ctx: Context, target: str) -> Roadmap:
    rm = Roadmap(target)
    _walk(ctx, rm, target, set())
    p = ctx.player
    goals = [(s, lvl) for s, lvl in sorted(rm.skills.items()) if p.level(s) < lvl]
    rm.skills = dict(goals)
    rm.joint = joint_plan(ctx, goals) if goals else None
    rm.quest_plan = quests_plan(ctx, sorted(rm.quests)) if rm.quests else None
    # Activities come after the skills: cost them on the player the skill plans end with (so items
    # that need 110 Fletching or 120 Smithing can be made, from what the skill plans left in the bank).
    after = dataclasses.replace(ctx, player=rm.joint.player, side_options=False, _needs=None, _gatherer=None,
                                _activities=None, _combat_style=None) if rm.joint and rm.joint.player else ctx
    for a in rm.activities:
        for t in a.tests:
            a.plan.add(tally_plan(after, a.achievement, t, many=len(a.tests) > MANY_FLAGS))
    return rm
