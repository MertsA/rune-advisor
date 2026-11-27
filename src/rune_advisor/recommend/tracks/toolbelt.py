"""Tool belt unlocks still missing (data/toolbelt.json, scripts/build_toolbelt.py), ranked by hours to unlock.
Value 1 per item: the unlock is listed by name, not valued in hours (the user's call, 2026-10-29).

What's on the belt comes from the player's TOOLBELT_* vars (listed by name in the exporter): an item counts as
missing when the var whose words all appear in its name ("TOOLBELT_LOCKPICK" -> "Master thief's lockpick")
is 0. Items with no matching var aren't offered (we can't tell if they're owned).

Cost: level requirements and quests from the wiki note, plus reward-shop currency at CURRENCY_RATES
(the cheapest listed shop with a known rate). Exported currency balances (pilfer points, tokens) are netted off,
so purchases are costed from zero and flagged. Earning the currency trains a skill; that's the overlap
focus mode shows (Safecracking: pilfer points and Thieving outfit fragments at once).
"""

from __future__ import annotations

import json
import re
from functools import cache
from ... import datapkg

from ..base import Candidate, Context
from ..plans import Plan, quest_plan, skill_plan

# Currency -> (skill, activity, per hour); per hour may depend on the player's rate in the skill.
# Pilfer points: "over 900 pilfer points per hour" at the three-dial Misthalin safes (wiki Safecracking).
# Dungeoneering tokens: 20% of a floor's xp (wiki Dungeoneering token), at the planner's Dungeoneering rate.
CURRENCY_RATES = {
    "pilfer point": ("Thieving", "Safecracking (Misthalin three-dial safes)", lambda ctx: 900.0),
    "dungeoneering token": ("Dungeoneering", "Dungeoneering floors",
                            lambda ctx: 0.2 * ctx.cost.rate("Dungeoneering", ctx.player.level("Dungeoneering"))),
    "slayer point": ("Slayer", "Slayer tasks (Laniakea: 22 a task, 110 every 10th, 330 every 50th)",
                     lambda ctx: SLAYER_POINTS_PER_HOUR),
}
# Slayer points per hour of tasks: Laniakea's 22 a task plus the 10th/50th bonuses average ~35 a task over a
# 50-task cycle, at about 2 tasks an hour (an assumption; balances aren't exported).
SLAYER_POINTS_PER_HOUR = 70.0
# Tools that need a separate unlock before they work from the belt ("Requires 500 slayer points to purchase
# the ability for it to be added to the tool belt"): the item itself may already be in the bank.
BELT_UNLOCK = re.compile(r"requires ([\d,]+) slayer points", re.I)
IGNORED_VAR_WORDS = {"bought", "enabled", "level", "old"}
WORD_ALIASES = {"acc": "accumulator", "keyring": "key ring"}


@cache
def toolbelt() -> dict[str, dict]:
    return json.loads(datapkg.path("toolbelt.json").read_text())


def _currency_key(name: str) -> str:
    return name.lower().removesuffix("s")


def _balance(ctx: Context, currency: str) -> int | None:
    """The exported balance of a currency, whatever the spelling ("pilfer points" = "Pilfer points")."""
    key = _currency_key(currency)
    return next((n for name, n in ctx.player.currencies.items() if _currency_key(name) == key), None)


def belt_vars(vars: dict) -> dict[str, list[str]]:
    """{tool belt item: the TOOLBELT_* vars naming it} for items with exactly-matching vars."""
    out: dict[str, list[str]] = {}
    for var in vars:
        if not var.startswith("TOOLBELT_"):
            continue
        words = [WORD_ALIASES.get(w, w) for w in var[len("TOOLBELT_"):].lower().split("_")]
        if any(w in IGNORED_VAR_WORDS for w in words) or not words:
            continue
        hits = [item for item in toolbelt() if all(w in item.lower().replace("'", "") for w in words)]
        if len(hits) == 1:
            out.setdefault(hits[0], []).append(var)
    return out


def unlock_plan(ctx: Context, item: str) -> tuple[Plan, list[str]]:
    """(plan, notes) to put `item` on the tool belt."""
    row = toolbelt()[item]
    plan, notes = Plan(), []
    owned = item in ctx.player.holdings  # in the bank, just not on the belt yet
    if owned:
        notes.append("you already have one in the bank (the export shows it): only the belt unlock is left")
    else:
        for skill, level in row["levels"].items():
            plan.add(skill_plan(ctx, "Hitpoints" if skill == "Constitution" else skill, level))
        for q in row["quests"]:
            if q in ctx.player.quests and not ctx.player.quests[q].done:
                plan.add(quest_plan(ctx, q))
    purchases = [] if owned else list(row["purchases"])
    if m := BELT_UNLOCK.search(row["note"]):
        purchases.append({"cost": int(m[1].replace(",", "")), "currency": "Slayer points", "unlock": True})
    for p in _cheapest(purchases, ctx):
        if p is None:  # a purchase with no rate for its currency
            p = next(x for x in purchases if not CURRENCY_RATES.get(_currency_key(x["currency"])))
            plan.add(Plan(hours=1.0, guessed_hours=1.0, uncertain=True, needs=[f"{p['cost']:,} {p['currency']}"],
                          unknowns=[f"no rate for {p['currency']} (guessed 1h)"]))
            continue
        hours, p, skill, activity = p
        what = "the belt unlock" if p.get("unlock") else item
        balance = _balance(ctx, p["currency"])
        if balance is not None and balance >= p["cost"]:  # the export shows enough in hand
            plan.add(Plan(hours=0.0, needs=[f"spend {p['cost']:,} {p['currency']} (you have {balance:,})"],
                          steps=[f"spend {p['cost']:,} {p['currency']} on {what} (you have {balance:,})"]))
            continue
        if balance is not None:
            hours *= (p["cost"] - balance) / p["cost"]
        plan.add(Plan(hours=hours, skills={skill}, needs=[f"{p['cost']:,} {p['currency']} from {activity} (~{hours:.1f}h)"],
                      unknowns=([] if balance is not None else [f"{p['currency']} balance isn't exported: costed from 0"]),
                      steps=[f"earn {p['cost'] - (balance or 0):,} {p['currency']} for {what} ({activity}, ~{hours:.1f}h"
                             + (f"; you have {balance:,})" if balance is not None else ")")]))
    if not (owned or row["levels"] or row["quests"] or row["purchases"]):
        plan.add(Plan(hours=1.0, guessed_hours=1.0, uncertain=True, needs=[row["note"] or item],
                      unknowns=["unlock not modelled (guessed 1h)"]))
    return plan, notes


def _cheapest(purchases: list[dict], ctx: Context) -> list[tuple | None]:
    """The item's cheapest purchase option (by hours of earning its currency) plus the belt unlock, if any;
    None stands for an item purchase whose currencies have no rate."""
    out: list[tuple | None] = []
    for group in (p for p in purchases if not p.get("unlock")), (p for p in purchases if p.get("unlock")):
        group = list(group)
        options = []
        for p in group:
            if rate := CURRENCY_RATES.get(_currency_key(p["currency"])):
                skill, activity, per_hour = rate
                options.append((p["cost"] / max(1.0, per_hour(ctx)), p, skill, activity))
        if options:
            out.append(min(options, key=lambda o: o[0]))
        elif group:
            out.append(None)
    return out


def currency_unlocks(ctx: Context) -> list[tuple[str, str, str, float]]:
    """Missing tool belt items bought with a currency some activity earns: (item, skill, activity, hours)."""
    out = []
    for item, vars_ in sorted(belt_vars(ctx.player.vars).items()):
        if any(ctx.player.vars.get(v) for v in vars_):
            continue
        purchases = [] if item in ctx.player.holdings else list(toolbelt()[item]["purchases"])
        if m := BELT_UNLOCK.search(toolbelt()[item]["note"]):
            purchases.append({"cost": int(m[1].replace(",", "")), "currency": "Slayer points"})
        for p in purchases:
            if rate := CURRENCY_RATES.get(_currency_key(p["currency"])):
                skill, activity, per_hour = rate
                short = max(0, p["cost"] - (_balance(ctx, p["currency"]) or 0))
                if short:  # a balance covering it needs no earning
                    out.append((item, skill, f"{short:,} {p['currency']} from {activity}", short / max(1.0, per_hour(ctx))))
    return out


class _Toolbelt:
    name = "toolbelt"
    description = "Tool belt unlocks still missing, by hours to unlock (listed, not valued)"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p, out = ctx.player, []
        for item, vars_ in sorted(belt_vars(p.vars).items()):
            if any(p.vars.get(v) for v in vars_):
                continue
            plan, notes = unlock_plan(ctx, item)
            if plan.needs_membership:
                continue
            out.append(Candidate(
                track=self.name, title=item, value=1, unit="unlock", cost_hours=round(plan.hours, 2),
                details=[toolbelt()[item]["note"], *notes, *plan.steps], uncertain=plan.uncertain,
                why=toolbelt()[item]["note"].split(". ")[0].rstrip("."), **plan.guide_fields(),
            ))
        return out


ToolbeltTrack = _Toolbelt()
