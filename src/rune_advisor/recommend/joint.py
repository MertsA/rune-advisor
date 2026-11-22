"""Plan several goals together instead of each as if it were the only one.

Goals are planned one after another on a copy of the player that changes as the plan goes:
  * what a goal uses up (bank items, lamps and stars, rested xp, banked bonus xp) is gone for the
    goals after it, so two goals can't both count the same 49k maple logs;
  * xp a goal earns on the side (Slayer xp from killing abyssal demons for pure essence, Ranged xp
    from charm runs) is added to the player before the next goal, so a later Slayer goal is shorter.
Goals that earn the most side xp for the other goals go first, then the rest in the given order.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .. import kingdom, reputation
from ..models import PlayerState, Skill
from ..resources import LevelPlan, ResourcePool
from ..xp import MAX_XP, level_at_xp, xp_at
from .base import HIGH_ALCH_SECONDS, Context


@dataclass
class JointStep:
    skill: str
    target: int
    hours_alone: float
    plan: LevelPlan
    # Side xp from earlier goals already in this skill when the goal starts.
    credited_xp: float = 0.0


@dataclass
class JointPlan:
    steps: list[JointStep] = field(default_factory=list)
    # The player after every goal (levels reached, side xp landed, items and coins used up).
    player: PlayerState | None = None

    @property
    def hours(self) -> float:
        return sum(s.plan.hours for s in self.steps)

    @property
    def hours_alone(self) -> float:
        return sum(s.hours_alone for s in self.steps)


def _copy_player(p: PlayerState) -> PlayerState:
    return dataclasses.replace(
        p, skills={k: dataclasses.replace(v) for k, v in p.skills.items()},
        achievements={k: dataclasses.replace(v, requirements=[dict(t) for t in v.requirements])
                      for k, v in p.achievements.items()},
        inventories={k: dict(v) for k, v in p.inventories.items()},
        group={k: dict(v) for k, v in p.group.items()}, bonus_xp=dict(p.bonus_xp), notes=[])


def _copy_pool(pool: ResourcePool | None) -> ResourcePool | None:
    return ResourcePool([dataclasses.replace(r) for r in pool.resources]) if pool else None


def _use_items(p: PlayerState, used: dict[str, int]) -> None:
    """Take used items out of the player's inventories (bank first), then out of the group's."""
    for item, n in used.items():
        for inv in sorted(p.inventories, key=lambda k: k != "BANK"):
            take = min(n, p.inventories[inv].get(item, 0))
            p.inventories[inv][item] = p.inventories[inv].get(item, 0) - take
            n -= take
        for mate in p.group.values():
            take = min(n, mate.get(item, 0))
            mate[item] = mate.get(item, 0) - take
            n -= take


def _add_xp(p: PlayerState, skill: str, xp: float) -> float:
    """Add xp (capped at 200m); returns how much actually landed."""
    s = p.skills.get(skill) or Skill(skill, 1, 0)
    landed = max(0.0, min(xp, MAX_XP - s.xp))
    s.xp = int(s.xp + landed)
    s.level = max(s.level, level_at_xp(skill, s.xp))
    p.skills[skill] = s
    return landed


def joint_plan(ctx: Context, goals: list[tuple[str, int]]) -> JointPlan:
    """Plan goals in sequence. Tried with and without valuing side xp in method choice (a method that
    trains another goal's skill on the way); the cheaper total wins, so it's never worse than separate."""
    alone = {g: ctx.level_plan(*g) for g in goals}
    with_value = _sequence(ctx, goals, alone, value_side_xp=True)
    without = _sequence(ctx, goals, alone, value_side_xp=False)
    return with_value if with_value.hours <= without.hours else without


def _sequence(ctx: Context, goals: list[tuple[str, int]], alone: dict, value_side_xp: bool) -> JointPlan:
    skills = {s for s, _ in goals}

    def helps_others(g: tuple[str, int]) -> float:
        return sum(v for s, v in alone[g].byproduct_xp.items() if s in skills and s != g[0])

    # Goals that wait on the calendar (herb growth, dailies) start first so the waiting runs alongside the
    # rest; then goals whose side xp feeds the others.
    order = sorted(goals, key=lambda g: (-(alone[g].calendar_hours > 0), -helps_others(g)))
    player, pool = _copy_player(ctx.player), _copy_pool(ctx.resources)
    # One kingdom and one set of weekly claims for all goals; each goal's window starts after the earlier goals' play.
    booked = (ctx.kingdom or kingdom.Ledger(ctx.player, 0.0)).copy()
    elapsed = ctx.play_elapsed
    credited: dict[str, float] = {}
    joint = JointPlan()
    for skill, target in order:
        # Other goals still short of xp: their own plan's hours per xp is what side xp there saves.
        wanted = {}
        if value_side_xp:
            for s, t in goals:
                need = xp_at(s, t) - player.xp(s)
                if s != skill and need > 0 and alone[(s, t)].hours > 0:
                    wanted[s] = alone[(s, t)].hours / max(1.0, xp_at(s, t) - ctx.player.xp(s))
        step_ctx = dataclasses.replace(ctx, player=player, resources=pool, side_options=False, wanted=wanted,
                                       _needs=None, _gatherer=None, _finding_needs=False, _activities=None,
                                       _urn_options={}, _urn_xp={}, kingdom=booked,
                                       play_elapsed=elapsed)
        plan = step_ctx.level_plan(skill, target)
        elapsed += plan.hours
        joint.steps.append(JointStep(skill, target, alone[(skill, target)].hours, plan, credited.get(skill, 0.0)))
        # Apply the goal to the player: the skill reaches its target, side xp lands, things get used up.
        _add_xp(player, skill, max(0, xp_at(skill, target) - player.xp(skill)))
        for s, v in plan.byproduct_xp.items():
            if s != skill:
                credited[s] = credited.get(s, 0.0) + _add_xp(player, s, v)
        _use_items(player, plan.items_used)
        for faction, rep in plan.reputation.items():
            reputation.add(player, faction, rep)
        # Shop coins are spent (anything beyond the pouch was earned and spent within the goal).
        _use_items(player, {"Coins": int(plan.gather_coins)})
        # An ironman alchs the rest of the goal's drops too: coins for later goals' shops.
        if player.ironman and (skill, target) != order[-1]:
            gp, casts = step_ctx.alch_income(skill, plan.grind_hours)
            if gp > plan.alch_gp:
                share = (gp - plan.alch_gp) / gp
                spare, spare_casts = gp - plan.alch_gp, int(casts * share)
                player.inventories.setdefault("BANK", {})["Coins"] = player.bank.get("Coins", 0) + int(spare)
                plan.hours += spare_casts * HIGH_ALCH_SECONDS / 3600
                plan.items_used["Nature rune"] = plan.items_used.get("Nature rune", 0) + spare_casts
                _use_items(player, {"Nature rune": spare_casts})
                plan.steps.append(f"high-alch the rest of the drops for later goals: ~{spare:,.0f} gp "
                                  f"({spare_casts:,} casts, ~{spare_casts * HIGH_ALCH_SECONDS / 3600:.1f}h)")
        if pool:
            for r in pool.resources:
                r.units = max(0, r.units - plan.resources_used.get(r.name, 0))
        banked = player.bonus_xp.get(skill, 0)
        if banked:
            player.bonus_xp[skill] = max(0, int(banked - plan.bonus_xp))
    joint.player = player
    return joint
