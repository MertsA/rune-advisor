"""Gear and tool upgrades from drops: the classic ironman grinds (Bandos for melee power armour,
Subjugation for magic, a dragon pickaxe from chaos dwarves).

For every equipment item a monster drops, check whether it beats what the player owns:
- combat gear: the best loadout for its style (gear.best_loadout) with the item added, scored by
  gear.damage_score; value is the % damage gained;
- pickaxes/hatchets: the simulated best Mining/Woodcutting xp/h at the player's level (sim.py) with it.
Upgrades are grouped by the monster that drops them (a boss's whole set is one goal, valued as the
loadout with every piece). Cost: expected kills to collect all of them (each piece its own drop
chance) at the boss's money-making-guide kill rate (drops.boss_kills_per_hour) or the generic kill
estimate for regular monsters, plus the unlock chain: the guide's required levels and quests and the
items' wear requirements, plus a supply tax (SUPPLY_TAX) for an ironman's potions and food.

Crafted upgrades (`_crafted`): gear made from drops (Deathdealer robes from Hermodic plates, Ek-ZekKil from
TzKal-Zuk's pieces). The recipe
chain is expanded through intermediate gear the player doesn't own (tier 60 -> 70 -> 80 -> 90); dropped
inputs, less what's held, cost kills at their fastest source; a craft with an unsourced input is skipped; other inputs go through the gatherer; each recipe's level
is a skill plan.
Value points = % gained. Non-ironmen would buy these, so the track is ironman-only.
"""

from __future__ import annotations

from itertools import combinations

from ... import sim
from ...drops import boss_kills_per_hour, bosses, estimated_kills_per_hour, sources_of, tables, boss_sim_kills_per_hour, BOSS_OVERHEAD_SECONDS
from ...gear import STYLES, _item, best_loadout, damage_score, equipment, owned_equipment
from .. import gathering
from ..base import HIGH_ALCH_SECONDS, Candidate, Context, drop_coins_per_kill
from ..plans import MIN_REWARD_SHARE, Plan, quest_plan, skill_plan

SUPPLY_TAX = 0.25  # ironman hours making potions/food per hour of bossing (assumption; collection logs too)
MIN_GAIN = 0.01  # ignore goals (a whole set) under 1%
MIN_CHANCE = 1 / 50_000  # rare-table rolls of uniques aren't a plan
# A source is only offered if it's within this factor of the item's fastest source (boss minions
# drop boss uniques at 1/49,152: not worth listing next to the boss itself).
MAX_SOURCE_SLOWDOWN = 3.0
MAX_PIECES = 10  # inclusion-exclusion over the set is 2^n terms
TOOLS = {"pickaxe": ("Mining", "picks"), "hatchet": ("Woodcutting", "hatchets")}


def expected_kills(chances: list[float]) -> float:
    """Expected kills until every item (independent per-kill chances) has dropped at least once."""
    chances = sorted(chances, reverse=True)[:MAX_PIECES]
    total = 0.0
    for k in range(1, len(chances) + 1):
        for subset in combinations(chances, k):
            total += (-1) ** (k + 1) / sum(subset)
    return total


def _combat_gains(ctx: Context) -> dict[str, tuple[str, float]]:
    """{item: (style, gain)} for dropped combat gear that improves the player's best loadout."""
    p = ctx.player
    owned = {i.name for i in owned_equipment(p)}
    base = {s: best_loadout(p, s) for s in STYLES}
    score = {s: damage_score(p, l) for s, l in base.items()}
    out = {}
    for name in equipment()["items"]:
        if name in owned or not sources_of(name):
            continue
        item = _item(name)
        if item is None or not (item.damage or item.bonus):
            continue
        for style in ([item.cls] if item.cls in STYLES else STYLES if item.cls in ("hybrid", "all") else []):
            gain = damage_score(p, best_loadout(p, style, [item])) / score[style] - 1
            # Any single-piece gain counts here: a set of small upgrades can add up (MIN_GAIN per goal).
            if gain > 0 and gain > out.get(name, ("", 0))[1]:
                out[name] = (style, gain)
    return out


def _tool_gains(ctx: Context) -> dict[str, tuple[str, float]]:
    """{item: (skill, gain)} for dropped pickaxes/hatchets that speed up the player's best method."""
    p, out = ctx.player, {}
    for kind, (skill, key) in TOOLS.items():
        rates = sim.mining_rates if skill == "Mining" else sim.woodcutting_rates
        best_now = max((r.xp_per_hour for r in rates(p)), default=0)
        if not best_now:
            continue
        for tool in sim.data()[key]:
            name = f"{tool['label']} {kind}"
            if name in p.holdings or not sources_of(name) or p.level(skill) < tool["level"]:
                continue
            kw = {"pick": tool} if skill == "Mining" else {"hatchet": tool}
            gain = max((r.xp_per_hour for r in rates(p, **kw)), default=0) / best_now - 1
            if gain >= MIN_GAIN:
                out[name] = (skill, gain)
    return out


def _kill_rate(ctx: Context, monster: str) -> tuple[float, str, bool] | None:
    """(kills/h, how, group content?) for a drop source."""
    boss = boss_kills_per_hour(ctx.player, monster)
    if boss:
        how = f"{boss.kills_per_hour:.0f} kills/h ({boss.guide.removeprefix('Money making guide/')}"
        if boss.gear_factor < 1:
            how += f", x{boss.gear_factor:.2f} for your {boss.style} weapon"
        return boss.kills_per_hour, how + ")", boss.group
    if monster in bosses():
        sim = boss_sim_kills_per_hour(ctx.player, monster)
        return (sim[0], f"~{sim[0]:.1f} kills/h (combat sim, no guide: {BOSS_OVERHEAD_SECONDS:.0f}s overhead a kill assumed)",
                False) if sim else None
    est = estimated_kills_per_hour(ctx.player, monster, ctx.combat_style)
    return (est[0], f"~{est[0]:.0f} kills/h (estimated from your {est[1]} rate; spawns and travel not modelled)",
            False) if est else None


def _unlocks(ctx: Context, monster: str, items: list[str]) -> Plan:
    plan = Plan()
    boss = boss_kills_per_hour(ctx.player, monster)
    needs: dict[str, int] = {}
    if boss:
        needs.update(boss.row.get("skills") or {})
        for q in boss.row.get("quests") or []:
            if q in ctx.player.quests:
                plan.add(quest_plan(ctx, q))
    for name in items:
        for s, lvl in (_item(name).reqs if _item(name) else {}).items():
            needs[s] = max(needs.get(s, 0), lvl)
    for s, lvl in needs.items():
        s = "Hitpoints" if s == "Constitution" else s
        plan.add(skill_plan(ctx, s, lvl))
    return plan


STYLE_SKILLS = {"melee": ("Attack", "Strength", "Defence"), "ranged": ("Ranged",), "magic": ("Magic",),
                "necromancy": ("Necromancy",)}


def upgrade_hours_saved(ctx: Context, style: str, gain: float) -> float:
    """Training hours a damage gain saves: kills come gain x faster, so the player's remaining training to
    99 in the style's skills takes 1/(1+gain) as long (the roadmap's upgrade score x planned combat hours)."""
    p = ctx.player
    planned = sum(ctx.level_plan(s, 99).hours for s in STYLE_SKILLS.get(style, ()) if s in p.skills and p.level(s) < 99)
    return planned * gain / (1 + gain)


MAX_CHAIN = 12  # recipe steps followed through intermediate gear (Deathdealer tier 90 back to tier 10)


def _crafted_gains(ctx: Context) -> dict[str, tuple[str, float]]:
    """{item: (style, gain)} for combat gear nobody drops but a recipe makes, that beats the owned loadout."""
    from ..catalogue import catalogue

    p = ctx.player
    owned = {i.name for i in owned_equipment(p)}
    made = {r["output"] for r in catalogue()}
    base = {s: damage_score(p, best_loadout(p, s)) for s in STYLES}
    out = {}
    for name in equipment()["items"]:
        if name in owned or name not in made or sources_of(name):
            continue
        item = _item(name)
        if item is None or not (item.damage or item.bonus):
            continue
        for style in ([item.cls] if item.cls in STYLES else STYLES if item.cls in ("hybrid", "all") else []):
            gain = damage_score(p, best_loadout(p, style, [item])) / base[style] - 1
            if gain >= MIN_GAIN and gain > out.get(name, ("", 0))[1]:
                out[name] = (style, gain)
    # One craft per style and slot: the bane crossbow isn't worth making next to the elder one.
    best: dict[tuple[str, str], str] = {}
    for name, (style, gain) in out.items():
        key = (style, _item(name).slot)
        if key not in best or gain > out[best[key]][1]:
            best[key] = name
    return {n: out[n] for n in best.values()}


def _expand(ctx: Context, item: str, n: float, drops: dict, other: dict, levels: dict, held: dict, cache: dict,
            depth: int = 0) -> bool:
    """Walk `item`'s recipe chain: dropped inputs (a monster with a kill rate drops them) into `drops`, gear
    made on the way recursed (gear no recipe makes goes to `other`), the rest into `other`, recipe levels into
    `levels`. False when the chain is longer than MAX_CHAIN."""
    from ..catalogue import catalogue

    take = min(n, held.get(item, 0))
    held[item] = held.get(item, 0) - take
    n -= take
    if n <= 0:
        return True
    recipes = [r for r in catalogue() if r["output"] == item and not r.get("conversion")]
    if depth >= MAX_CHAIN:
        return False
    if not recipes:  # the bottom of the chain comes from somewhere else (a shop, a quest): the gatherer's call
        other[item] = other.get(item, 0) + n
        return True
    # The recipe that uses a boss drop (the upgrade path), else the first.
    r = next((r for r in recipes if any(_drop_source(ctx, m, cache) for m, _ in r["materials"])), recipes[0])
    levels[r["skill"]] = max(levels.get(r["skill"], 0), r["level"])
    for m, q in r["materials"]:
        need = q * n / (r["output_qty"] or 1)
        if _drop_source(ctx, m, cache):
            take = min(need, held.get(m, 0))
            held[m] = held.get(m, 0) - take
            if need - take > 0:
                drops[m] = drops.get(m, 0) + need - take
        elif m in equipment()["items"]:
            if not _expand(ctx, m, need, drops, other, levels, held, cache, depth + 1):
                return False
        else:
            other[m] = other.get(m, 0) + need
    return True


def _drop_source(ctx: Context, item: str, cache: dict) -> tuple[str, float, tuple] | None:
    """(monster, chance per kill, kill rate) of the source that drops `item` fastest, if any can be killed."""
    if item not in cache:
        best = None
        for s, c, q in sources_of(item):
            if not isinstance(c, (int, float)) or c * (q or 1) < MIN_CHANCE or not (rate := _kill_rate(ctx, s)):
                continue
            if best is None or c * (q or 1) * rate[0] > best[1] * best[2][0]:
                best = (s, c * (q or 1), rate)
        cache[item] = best
    return cache[item]


def _crafted(ctx: Context) -> list[Candidate]:
    p, out, cache = ctx.player, [], {}
    for name, (style, gain) in _crafted_gains(ctx).items():
        drops, other, levels, held = {}, {}, {}, dict(p.holdings)
        if not _expand(ctx, name, 1, drops, other, levels, held, cache) or not drops:
            continue
        hours, lines, unknowns = 0.0, [], []
        for m, n in drops.items():
            src, chance, rate = _drop_source(ctx, m, cache)
            kills = n / chance
            supply = SUPPLY_TAX * kills / rate[0]
            hours += kills / rate[0] + supply
            lines.append(f"{n:,.0f} more {m}: ~{kills:,.0f} kills of {src} (~{kills / rate[0]:.1f}h + {supply:.1f}h supplies)")
            if "estimated" in rate[1]:
                unknowns.append("kill rate estimated from gear")
        plan = Plan()
        for s, lvl in levels.items():
            plan.add(skill_plan(ctx, s, lvl))
        g = ctx.gatherer.plan({m: int(-(-q // 1)) for m, q in other.items()}) if other else None
        gather_h = (g.hours + g.coins / gathering.COINS_PER_HOUR) if g else 0.0
        if g and g.short:
            continue  # an input with no known source: the cost would be a fiction
        hours += gather_h + plan.hours
        saved = upgrade_hours_saved(ctx, style, gain)
        gross, hours = hours, max(MIN_REWARD_SHARE * hours, hours - saved)
        out.append(Candidate(
            track="gear", title=f"craft {name} (+{gain:.0%} {style} damage)", value=round(100 * gain, 1), unit="% gain",
            cost_hours=round(hours, 2),
            details=[*lines, *(f"gather the other inputs: ~{gather_h:.1f}h" for _ in [0] if other), *plan.steps],
            uncertain=bool(unknowns) or plan.uncertain, why=f"+{gain:.0%} {style} damage, crafted from drops"
            + (f"; faster kills save ~{saved:.1f}h of your remaining {style} training (cost {gross:.1f}h -> {hours:.1f}h)"
               if saved >= 0.05 else ""),
            skills=set(plan.skills), needs=plan.needs + [l.split(" (~")[0] for l in lines],
            unknowns=unknowns + plan.unknowns + [f"supplies: {SUPPLY_TAX:.0%} of kill time assumed"],
        ))
    return out


def _bought_gains(ctx: Context) -> dict[str, tuple[str, float]]:
    """{item: (style, gain)} for combat gear a shop sells (coins or a reward currency: chaotics and gravite for
    Dungeoneering tokens) that beats the owned loadout, the best per style and slot."""
    p = ctx.player
    owned = {i.name for i in owned_equipment(p)}
    base = {s: damage_score(p, best_loadout(p, s)) for s in STYLES}
    out = {}
    for name in equipment()["items"]:
        if name in owned:
            continue
        src = ctx.gatherer.complete_source(name)
        if src is None or not (src.kind == "shop" or (src.recipe or {}).get("currency_shop")):
            continue
        item = _item(name)
        if item is None or not (item.damage or item.bonus):
            continue
        for style in ([item.cls] if item.cls in STYLES else STYLES if item.cls in ("hybrid", "all") else []):
            gain = damage_score(p, best_loadout(p, style, [item])) / base[style] - 1
            if gain >= MIN_GAIN and gain > out.get(name, ("", 0))[1]:
                out[name] = (style, gain)
    best: dict[tuple[str, str], str] = {}
    for name, (style, gain) in out.items():
        key = (style, _item(name).slot)
        if key not in best or gain > out[best[key]][1]:
            best[key] = name
    return {n: out[n] for n in best.values()}


def _bought(ctx: Context) -> list[Candidate]:
    """Shop gear that beats the owned loadout: its price from the balance the player holds (reward currencies are the
    gatherer's stock: player.sync_currency_inventory), the rest earned, plus the levels to wear it."""
    out = []
    for name, (style, gain) in _bought_gains(ctx).items():
        g = ctx.gatherer.plan({name: 1})
        if g.short:
            continue
        unlocks = _unlocks(ctx, "", [name])
        gross = g.hours + g.coins / gathering.COINS_PER_HOUR + unlocks.hours
        saved = upgrade_hours_saved(ctx, style, gain)
        hours = max(MIN_REWARD_SHARE * gross, gross - saved)
        out.append(Candidate(
            track="gear", title=f"buy {name} (+{gain:.0%} {style} damage)", value=round(100 * gain, 1), unit="% gain",
            cost_hours=round(hours, 2), details=[*g.steps, *unlocks.steps], uncertain=unlocks.uncertain,
            why=f"+{gain:.0%} {style} damage over your best owned, from a shop"
            + (f"; faster kills save ~{saved:.1f}h of your remaining {style} training (cost {gross:.1f}h -> {hours:.1f}h)"
               if saved >= 0.05 else ""),
            skills=set(unlocks.skills), needs=unlocks.needs, unknowns=unlocks.unknowns))
    return out


def _wanted(ctx: Context) -> list[Candidate]:
    """Items the user asked for (overrides.toml `wanted`), costed like crafted upgrades: dropped inputs as kills,
    the rest gathered, no damage-gain test (the user's reasons are their own)."""
    p, out, cache = ctx.player, [], {}
    for name in p.wanted_items:
        if name in p.holdings:
            continue
        drops, other, levels, held = {}, {}, {}, dict(p.holdings)
        if not _expand(ctx, name, 1, drops, other, levels, held, cache):
            continue
        hours, lines, unknowns = 0.0, [], []
        for m, n in drops.items():
            src, chance, rate = _drop_source(ctx, m, cache)
            kills = n / chance
            supply = SUPPLY_TAX * kills / rate[0]
            hours += kills / rate[0] + supply
            lines.append(f"{n:,.0f} more {m}: ~{kills:,.0f} kills of {src} (~{kills / rate[0]:.1f}h + {supply:.1f}h supplies)")
            if "estimated" in rate[1] or "sim" in rate[1]:
                unknowns.append(f"kill rate at {src}: {rate[1]}")
        plan = Plan()
        for s, lvl in levels.items():
            plan.add(skill_plan(ctx, s, lvl))
        g = ctx.gatherer.plan({m: int(-(-q // 1)) for m, q in other.items()}) if other else None
        gather_h = (g.hours + g.coins / gathering.COINS_PER_HOUR) if g else 0.0
        if g and g.short:
            unknowns.append("no known source for " + ", ".join(f"{n:,} {k}" for k, n in g.short.items()))
        hours += gather_h + plan.hours
        out.append(Candidate(
            track="gear", title=f"get {name} (wanted)", value=1.0, unit="wanted", cost_hours=round(hours, 2),
            details=[*lines, *(f"gather the other inputs: ~{gather_h:.1f}h" + (f" ({', '.join(g.steps[:4])})" if g and g.steps else "")
                               for _ in [0] if other), *plan.steps],
            uncertain=bool(unknowns) or plan.uncertain, why="on your wanted list (overrides.toml)",
            skills=set(plan.skills), needs=plan.needs + [l.split(" (~")[0] for l in lines],
            unknowns=unknowns + plan.unknowns + ([f"supplies: {SUPPLY_TAX:.0%} of kill time assumed"] if drops else []),
        ))
    return out


class _Gear:
    name = "gear"
    description = "Ironman gear/tool upgrades from drops (Bandos, Subjugation, dragon pickaxe...): % damage or speed vs hours"

    def candidates(self, ctx: Context) -> list[Candidate]:
        p = ctx.player
        if not p.ironman:
            return []
        gains = {**_combat_gains(ctx), **_tool_gains(ctx)}
        by_source: dict[str, dict[str, float]] = {}
        rates: dict[str, tuple[float, str, bool] | None] = {}
        for name in gains:
            per_hour = {}
            for src, chance, qty in sources_of(name):
                if not isinstance(chance, (int, float)) or chance * (qty or 1) < MIN_CHANCE:
                    continue
                if src not in rates:
                    rates[src] = _kill_rate(ctx, src)
                if rates[src]:
                    per_hour[src] = max(per_hour.get(src, 0), chance * (qty or 1) * rates[src][0])
            fastest = max(per_hour.values(), default=0)
            for src, ph in per_hour.items():
                if ph * MAX_SOURCE_SLOWDOWN >= fastest:
                    by_source.setdefault(src, {})[name] = ph / rates[src][0]
        out = []
        monsters = tables()["monsters"]
        for src, items in by_source.items():
            if not p.members and monsters.get(src, {}).get("members", True):
                continue  # a free-to-play player can't fight members' monsters, whatever the guide rate says
            kph, how, group = rates[src]
            # One goal per thing improved (melee armour, Mining...): pieces for other styles are separate.
            for target in sorted({gains[i][0] for i in items}):
                names = [i for i in items if gains[i][0] == target]
                if target in STYLES:
                    # Only pieces the improved loadout wears: one of the Drygore variants, not all six.
                    new = best_loadout(p, target, [_item(n) for n in names])
                    names = [n for n in names if any(i.name == n for i in new.slots.values())]
                    gain = damage_score(p, new) / damage_score(p, best_loadout(p, target)) - 1
                    what = f"{target} damage"
                else:
                    names = [max(names, key=lambda n: gains[n][1])]  # one tool is enough
                    gain, what = gains[names[0]][1], f"{target} xp/h"
                if not names or gain < MIN_GAIN:
                    continue
                kills = expected_kills([items[n] for n in names])
                unlocks = _unlocks(ctx, src, names)
                supply = SUPPLY_TAX * kills / kph if p.ironman else 0.0  # potions and food, made by hand
                gross = kills / kph + supply + unlocks.hours
                saved = upgrade_hours_saved(ctx, target, gain) if target in STYLES else 0.0
                # The rest of the loot: coins and alchables, as time at the money-making rate less the casting.
                gp, casts = drop_coins_per_kill(src, set(names))
                coin_hours = max(0.0, kills * (gp / gathering.COINS_PER_HOUR - casts * HIGH_ALCH_SECONDS / 3600))
                hours = max(MIN_REWARD_SHARE * gross, gross - saved - coin_hours)
                pieces = ", ".join(sorted(names))
                out.append(Candidate(
                    track=self.name, title=f"Farm {src} for {pieces} (+{gain:.0%} {what})", value=round(100 * gain, 1), unit="% gain",
                    cost_hours=hours,
                    details=[f"~{kills:,.0f} kills for all {len(names)} ({' / '.join(f'1/{1 / items[n]:,.0f}' for n in sorted(names))}), {how}",
                             *unlocks.steps,
                             *(["group content: the guide's rate is for a team"] if group else []),
                             f"+{supply:.1f}h making supplies (potions, food: {SUPPLY_TAX:.0%} of kill time assumed)",
                             *([f"loot: ~{gp * kills / 1e6:,.1f}m gp in coins and alchables (~{coin_hours:.1f}h of money making)"]
                               if coin_hours >= 0.05 else [])],
                    uncertain=group or unlocks.uncertain or "estimated" in how,
                    why=f"+{gain:.0%} {what} over your best owned"
                    + (f"; faster kills save ~{saved:.1f}h of your remaining {target} training (cost {gross:.1f}h -> {hours:.1f}h)"
                       if saved >= 0.05 else ""), skills=set(unlocks.skills),
                    needs=unlocks.needs + [f"~{kills:,.0f} kills of {src} (~{kills / kph:.1f}h)"],
                    unknowns=unlocks.unknowns + (["group content: the guide's rate is for a team"] if group else [])
                    + [f"supplies: {SUPPLY_TAX:.0%} of kill time assumed"]
                    + (["kill rate estimated from gear"] if "estimated" in how else []),
                ))
        out += _crafted(ctx)
        out += _bought(ctx)
        out += _wanted(ctx)
        # The same pieces from several monsters (Morrigan's from every revenant): keep the fastest.
        best: dict[str, Candidate] = {}
        for c in out:
            key = c.title.split(" for ", 1)[1] if c.title.startswith("Farm ") else c.title
            if key not in best or c.cost_hours < best[key].cost_hours:
                best[key] = c
        return list(best.values())


GearTrack = _Gear()
