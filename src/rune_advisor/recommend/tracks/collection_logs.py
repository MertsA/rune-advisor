"""Boss collection logs (data/collection_logs.json, scripts/build_collection_logs.py), ranked by expected hours to
finish each log: value 1 per log, so the track compares bosses with each other (the user's call, 2026-10-29:
"an unowned unique that polishes off a collection log for a boss can't be compared to an hour of training,
but we can compare it to finishing out the collection log for a different boss").

Items still missing = not in the exported inventories or the collection log (drops.owned). Chances per kill
come from the wiki's collection log calculator, else the drop tables at the boss. Hours = expected kills
until every missing item has dropped (`kills_to_collect`) / kills per hour: the money making guides
(drops.boss_kills_per_hour, scaled for the player's gear), else the combat sim with a per-kill overhead fitted
to the guided bosses (`boss_overhead`, flagged). Pets and other threshold drops are listed apart (their bad-luck thresholds aren't
modelled, and they'd dominate every log). Unlocks (levels, quests) come from the boss's money making guide row.

Around the kills: an ironman makes their own potions and food, SUPPLY_TAX hours per hour of bossing (an
assumption, flagged). Drops another plan is short of (Context.needs: charms, herbs, bars...) are worth the
gathering time they replace (Gatherer.net_cost per unit), netted off the cost up to NEED_CREDIT_SHARE of it.
Missing log items that beat the player's gear (the gear track's gains) are named.
"""

from __future__ import annotations

import math

from ...drops import boss_kills_per_hour, collection_logs, owned, sources_of, tables
from ...mechanics import combat
from .. import gathering
from ..base import HIGH_ALCH_SECONDS, Candidate, Context, drop_coins_per_kill
from ..plans import DEFAULT_TEST_HOURS
from ...gear import STYLES
from ..plans import MIN_REWARD_SHARE
from .gear import MIN_GAIN, SUPPLY_TAX, _combat_gains, _tool_gains, _unlocks, upgrade_hours_saved

BOSS_OVERHEAD_SECONDS = 60.0  # fallback when no guided boss can be fitted (banking, instance setup, phases)
MIN_OVERHEAD_SECONDS = 10.0


def boss_overhead(ctx: Context) -> tuple[float, int]:
    """(seconds per kill, bosses fitted): what the combat sim misses at bosses (phases, banking, instancing),
    fitted to the bosses with a money making guide: their guide time per kill (scaled for the player's gear)
    less the sim's kill time with the player's gear, median over the bosses."""
    from ...drops import boss_rates

    gaps = []
    for name in boss_rates():
        guide = boss_kills_per_hour(ctx.player, name)
        m = combat.monster(name)
        if not guide or guide.group or not m or not m.get("life_points"):
            continue
        _style, dps = combat.best_style(ctx.player, m)
        if dps > 0:
            gaps.append(3600 / guide.kills_per_hour - m["life_points"] / dps)
    if len(gaps) < 3:
        return BOSS_OVERHEAD_SECONDS, 0
    return max(MIN_OVERHEAD_SECONDS, sorted(gaps)[len(gaps) // 2]), len(gaps)
NEED_CREDIT_SHARE = 0.5  # needed drops can cut a log's cost by at most this share
GRID_POINTS = 4000


def kills_to_collect(chances: list[float]) -> float:
    """Expected kills until every item (independent per-kill chances) has dropped at least once:
    the integral of P(not all yet by kill t), with each item's wait treated as exponential."""
    chances = [p for p in chances if p > 0]
    if not chances:
        return 0.0
    if len(chances) == 1:
        return 1 / chances[0]
    lo, hi = 1e-3 / max(chances), 60 / min(chances)
    # Log-spaced trapezoid: fine steps where the frequent drops arrive, long ones for the rare tail.
    ts = [lo * (hi / lo) ** (i / (GRID_POINTS - 1)) for i in range(GRID_POINTS)]
    f = [1 - math.prod(1 - math.exp(-p * t) for p in chances) for t in ts]
    return lo + sum((f[i] + f[i + 1]) / 2 * (ts[i + 1] - ts[i]) for i in range(GRID_POINTS - 1))


def _source(items: list[str]) -> str | None:
    """The drop-table source dropping the most of a collection's items."""
    counts: dict[str, int] = {}
    for item in items:
        for src, _, _ in sources_of(item):
            counts[src] = counts.get(src, 0) + 1
    return max(counts, key=lambda s: (counts[s], -len(s)), default=None)


def _chance(item: str, listed: float | None, src: str | None) -> float | None:
    if listed:
        return float(listed)
    for s, chance, qty in sources_of(item):
        if s == src and isinstance(chance, (int, float)) and chance:
            return chance * (qty or 1)
    return None


class _CollectionLogs:
    name = "collection_logs"
    description = "Boss collection logs by expected hours to finish (pets listed apart)"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p, out = ctx.player, []
        if not p.achievements and not p.item_log:
            return []  # nothing exported: can't tell what's owned
        unresolved = sum(1 for k in p.item_log if k.startswith("ITEMLOG_ITEM_"))
        overhead, fitted = boss_overhead(ctx)
        gains = {**_combat_gains(ctx), **_tool_gains(ctx)} if p.ironman else {}
        wanted = ctx.needs()
        for name, c in collection_logs().items():
            ach = p.achievements.get(c["achievement"] or "")
            if ach and ach.completed:
                continue
            missing = [i for i in c["items"] if not owned(p, i)]
            if not missing:
                continue
            src = _source(list(c["items"]))
            if src is None:
                continue
            pets = [i for i in missing if i in c["pets"]]
            rest = [i for i in missing if i not in c["pets"]]
            chances = {i: _chance(i, c["items"][i], src) for i in missing}
            known = [chances[i] for i in rest if chances[i]]
            unknown = [i for i in rest if not chances[i]]
            boss = boss_kills_per_hour(p, src)
            unknowns = []
            if boss:
                kph, how = boss.kills_per_hour, f"{boss.kills_per_hour:,.0f} kills/h ({boss.guide})"
                if boss.group:
                    unknowns.append("the guide's rate is for a team")
            elif sim := combat.kills_per_hour(p, src, overhead):
                kph, how = sim[0], f"{sim[0]:,.0f} kills/h (combat sim, {sim[1]})"
                unknowns.append(f"no guide: kill rate simulated with {overhead:.0f}s overhead per kill"
                                + (f" (fitted to {fitted} guided bosses)" if fitted else " (assumed)"))
            else:
                continue
            # Pets only (or nothing else with a known chance): the pets' base chances are the cost.
            pets_only = not rest
            kills = kills_to_collect([chances[i] for i in pets if chances[i]] if pets_only else known)
            unlocks = _unlocks(ctx, src, [])  # the boss's own requirements: log items needn't be wieldable
            kill_hours = kills / kph
            supply = SUPPLY_TAX * kill_hours if p.ironman else 0.0
            if supply:
                unknowns.append(f"supplies: {SUPPLY_TAX:.0%} of boss time assumed for potions and food")
            # Drops other plans are short of, at the gathering time each replaces.
            need_lines, need_hours = [], 0.0
            for item, chance, qty, _ in tables()["sources"].get(src, []):
                if item in wanted and isinstance(chance, (int, float)) and (g := ctx.gatherer.complete_source(item)):
                    per_hour = kph * chance * (qty or 1)
                    need_hours += per_hour * kill_hours * ctx.gatherer.net_cost(g)
                    need_lines.append(f"{item} ~{per_hour:,.1f}/h ({wanted[item]})")
            hours = kill_hours + supply + unlocks.hours
            credit = min(need_hours, NEED_CREDIT_SHARE * hours)
            # The rest of the loot: coins and alchables, as time at the money-making rate less the casting.
            gp, casts = drop_coins_per_kill(src, set(c["items"]) | set(wanted))
            coin_hours = max(0.0, kills * (gp / gathering.COINS_PER_HOUR - casts * HIGH_ALCH_SECONDS / 3600))
            guessed = 0.0
            if unknown:
                unknowns.append("drop chance unknown for " + ", ".join(unknown) + " (not counted)")
                if len(unknown) >= len(known):  # most of the log isn't costed: a placeholder, sorted last
                    hours = max(hours, DEFAULT_TEST_HOURS)
                    guessed = hours
            if pets_only:
                unknowns.append("pet thresholds not modelled (base chance only)")
            if unresolved:
                unknowns.append(f"{unresolved} collection log entries have no item name yet: owned items may show as missing")
            counted = len([i for i in pets if chances[i]]) if pets_only else len(known)
            needs = unlocks.needs + ([f"~{kills:,.0f} kills of {src} for {counted} item{'s' * (counted != 1)}"
                                      f"{' (pets)' if pets_only else ''} (~{kills / kph:.1f}h)"] if counted else [])
            details = [f"missing: {', '.join(rest) or 'only pets'}", f"at {how}"]
            upgrades = [f"{i} (+{gains[i][1]:.0%} {gains[i][0]})" for i in missing if i in gains and gains[i][1] >= MIN_GAIN]
            # The best upgrade on the log speeds up the player's remaining training in its style.
            top = max((gains[i] for i in missing if i in gains and gains[i][0] in STYLES), key=lambda g: g[1], default=None)
            saved = upgrade_hours_saved(ctx, *top) if top else 0.0
            # Both credits together never take a log below MIN_REWARD_SHARE of its cost.
            uniques = ctx.unique_value_hours * len(rest)  # 0 unless rune_advisor.toml values them
            gross = hours
            hours = max(MIN_REWARD_SHARE * hours, hours - credit - saved - uniques - coin_hours)
            if coin_hours >= 0.05:
                details.append(f"loot: ~{gp * kills / 1e6:,.1f}m gp in coins and alchables (~{coin_hours:.1f}h of money making)")
            if uniques:
                details.append(f"{len(rest)} uniques at {ctx.unique_value_hours:g}h each (unique_value_hours): -{uniques:.1f}h")
            if saved:
                details.append(f"its {top[0]} upgrade saves ~{saved:.1f}h of your remaining {top[0]} training")
            if upgrades:
                details.append("upgrades your gear: " + ", ".join(upgrades))
            if need_lines:
                details.append(f"also drops what other plans lack (saves ~{credit:.1f}h): " + "; ".join(need_lines[:4]))
            if supply:
                details.append(f"+{supply:.1f}h making supplies")
            for pet in ([] if pets_only else pets):
                if ch := chances[pet]:
                    details.append(f"pet/threshold drop {pet}: 1/{1 / ch:,.0f} (~{1 / ch / kph:.0f}h, threshold not modelled)")
            extras = [a for a, _ in ctx.activities.achievements_at(src) if a != c["achievement"]]
            if extras:
                details.append("also advances: " + ", ".join(extras[:5]))
            total = len(c["items"])
            out.append(Candidate(
                # Some of the log already in: the guide's "keep fighting Hermod for the rare drops".
                track=self.name, title=(f"Keep fighting {src} for the rest of its log" if len(missing) < total
                                        else f"Fight {src} for its log") + f": {len(missing)} of {total} missing",
                value=1, unit="log",
                cost_hours=round(hours, 2), details=details + unlocks.steps,
                uncertain=bool(unknowns) or unlocks.uncertain,
                why=f"{total - len(missing)} of {total} collected; ~{gross:.1f}h of play"
                + (f", net ~{hours:.1f}h after what it pays back" if gross - hours >= 0.05 else "")
                + (f"; gear upgrade: {', '.join(u.split(' (')[0] for u in upgrades)}" if upgrades else "")
                + (f"; needed drops save ~{credit:.1f}h" if credit >= 0.05 else "")
                + (f"; also {len(extras)} achievement{'s' * (len(extras) != 1)}" if extras else ""),
                needs=needs, unknowns=unknowns + unlocks.unknowns, guessed_hours=guessed,
                skills=set(unlocks.skills),
            ))
        return out


CollectionLogsTrack = _CollectionLogs()
