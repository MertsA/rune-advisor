"""Anachronia base camp work still open (basecamp.py): every building tier and structure not built yet, and every Dream
of Iaia station not built yet, each with the spend-or-wait choice for its resources (basecamp.decisions). Listed, not
ranked by hours (the user's rule for unlocks; the choice itself uses unlock_value_hours): the value is 1 an unlock and
the cost is what the player must do themselves: the skill levels the tier asks, its item (a Remototem, a dinosaur
tooth...) and, for a station, the quest. The base camp resources are not play time: workers gather them (60 an hour
each) while the player is away, so the entry says how many days the shortfall takes at the current allocation.
"""

from __future__ import annotations

from ... import basecamp
from ..base import Candidate, Context
from ..plans import Plan, item_plan, quest_plan, skill_plan
from ...models import QuestStatus


def _resource_days(player, need: dict[str, int]) -> tuple[float | None, list[str]]:
    """Days of gathering the shortfall takes at the current workers (None: a resource with no workers), and why."""
    have, crew = basecamp.stock(player), basecamp.workers(player)
    per_hour = basecamp.data()["workers"]["per_hour"]
    days, lines = 0.0, []
    for r, n in need.items():
        short = n - have[r]
        if short <= 0:
            continue
        if crew[r] == 0:
            lines.append(f"{short:,} more {basecamp.NAMES[r]}: no workers on it")
            return None, lines
        d = short / (crew[r] * per_hour * 24)
        days = max(days, d)
        lines.append(f"{short:,} more {basecamp.NAMES[r]} (~{d:.1f} days at {crew[r]} workers)")
    return days, lines


def _needs_plan(ctx: Context, row: dict) -> Plan:
    plan = Plan()
    for skill, level in (row.get("levels") or {}).items():
        plan.add(skill_plan(ctx, skill, level))
    for item, qty in (row.get("items") or {}).items():
        if ctx.player.holdings.get(item, 0) >= qty:
            continue
        got = item_plan(ctx, item, qty)
        plan.add(got if got is not None else Plan(uncertain=True, needs=[f"{item} (no source known)"],
                                                  unknowns=[f"no source known for {item}"]))
    return plan


class _BaseCamp:
    name = "base_camp"
    description = "Anachronia base camp upgrades and Dream of Iaia stations still to build (listed, not valued)"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        if not p.members or not basecamp.known(p):
            return []
        out = []
        for name, row, choice, reason in basecamp.decisions(p, ctx):
            unmet = [f"{b} tier {t}" for b, t in (row.get("needs") or {}).items() if basecamp.tier(p, b) < t]
            plan = _needs_plan(ctx, row)
            days, lines = _resource_days(p, row["resources"])
            details = [*row.get("benefits", []), f"resources: {reason}", *lines, *plan.steps]
            if unmet:
                details.insert(0, "first: " + ", ".join(unmet))
            why = "; ".join(row.get("benefits", [])[:1] + lines[:2]) or "base camp"
            out.append(Candidate(track=self.name, title=f"Build the {name}", value=1, unit="unlock",
                                 cost_hours=plan.net_hours, details=details, why=why,
                                 uncertain=plan.uncertain or days is None, play_hours=plan.hours,
                                 **plan.guide_fields()))
        if advice := basecamp.allocation_advice(ctx):
            resource, now, then = advice
            skill = next(s for s, r in basecamp.data()["dream_of_iaia"]["stations"].items() if r == resource)
            total = sum(basecamp.workers(p).values())
            out.append(Candidate(
                track=self.name, title=f"Move all {total} base camp workers to {basecamp.NAMES[resource]} ({skill})",
                value=round(then - now, 2), unit="h saved a week", cost_hours=1 / 60, play_hours=1 / 60,
                why=f"~{then:.2f}h a week saved at the {skill} station vs ~{now:.2f}h with the current split",
                details=["Giles at the Town Hall: Base Camp Management, worker allocation",
                         f"{total * 60 * 24:,} {basecamp.NAMES[resource]} a day: the 150,000 store fills in about "
                         f"{150_000 / (total * 60 * 24):.1f} days, so spend it at the station that often"]))
        iaia = basecamp.data()["dream_of_iaia"]
        quest = p.quests.get(iaia["quest"])
        for skill, resource in iaia["stations"].items():
            key = skill.upper()
            if basecamp._var(p, basecamp.STATION_STATE_VAR.format(key)):
                continue
            plan = Plan()
            if quest is None or quest.status != QuestStatus.COMPLETED:
                plan.add(quest_plan(ctx, iaia["quest"]))
            if p.level(skill) < iaia["build_level"]:
                plan.add(skill_plan(ctx, skill, iaia["build_level"]))  # unboosted: the level must be trained
            need = {r: iaia["build_resources"] for r in basecamp.RESOURCES}
            days, lines = _resource_days(p, need)
            out.append(Candidate(
                track=self.name, title=f"Build the Dream of Iaia {skill} station", value=1, unit="unlock",
                cost_hours=plan.net_hours, uncertain=plan.uncertain or days is None, play_hours=plan.hours,
                why=f"{iaia['build_pays']:,} {skill} xp at once and {iaia['build_stores']:,} stored "
                    f"(350,000 xp an hour; restock with {basecamp.NAMES[resource]})",
                details=[f"{iaia['build_resources']:,} of every base camp resource", *lines, *plan.steps],
                **plan.guide_fields()))
        return out


BaseCampTrack = _BaseCamp()
