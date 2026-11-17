"""Audit the training methods the planner would use for a player: which ones don't model their
materials or prerequisites yet.

Usage: uv run python scripts/audit_methods.py <player> [target_level=99]

For every skill below the target it prints the plan's method and flags:
  guess-rate    no wiki method: the hours come from data/xp_rates.toml
  no-recipe     the wiki method consumes items (production skill or bought supplies) but has no recipe
  short         the plan needs items the player (and group) doesn't have
  unmodelled    part of the xp has no material data at all
  unchecked     prerequisites in the method's text we can't check (items, outfits, unlocks)
"""

from __future__ import annotations

import sys

from rune_advisor.cli import _ctx
from rune_advisor.recommend.catalogue import catalogue
from rune_advisor.requirements import items_named, missing_items, parse
from rune_advisor.xp import level_cap

PRODUCTION = {r["skill"] for r in catalogue()}


def main(name: str, target: int = 99) -> None:
    ctx = _ctx(name, True, False, False, None, False, None)
    p, cost = ctx.player, ctx.cost
    for skill in sorted(p.skills):
        level = p.level(skill)
        goal = min(target, level_cap(skill, p.members))
        if level >= goal or skill == "Hitpoints":
            continue
        plan = ctx.level_plan(skill, goal)
        flags = []
        methods = cost.available_methods(skill, level)
        method = methods[0] if methods else None
        if cost.rate_source.get(skill) == "guess":
            flags.append("guess-rate")
        elif method and not method.get("recipe") and (skill in PRODUCTION or method.get("bought_supplies")):
            flags.append("no-recipe")
        if plan.materials_short:
            flags.append("short: " + ", ".join(f"{n:,} {k}" for k, n in list(plan.materials_short.items())[:3]))
        if plan.materials_unmodelled:
            flags.append("unmodelled")
        if method and (absent := missing_items(method.get("requirements"), p)):
            flags.append("not in bank export: " + ", ".join(absent))
        if method and (unchecked := [u for u in parse(method.get("requirements"), set(p.quests)).unparsed
                                     if not items_named(u)]):
            flags.append("unchecked: " + "; ".join(unchecked)[:90])
        label = method["method"] if method else "(hand-written rate)"
        print(f"{skill:<13} {level:>3}->{goal:<3} {plan.hours:>6.1f}h  {label[:55]:<55} {' | '.join(flags)}")
    if cost.excluded:
        print(f"\n{len(cost.excluded)} wiki methods skipped for unmet prerequisites:")
        for skill, method, missing in cost.excluded:
            print(f"  {skill}: {method} (needs {', '.join(missing)})")


if __name__ == "__main__":
    main(sys.argv[1], *(int(a) for a in sys.argv[2:3]))
