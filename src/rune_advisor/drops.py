"""What a training method drops, and which of it this player actually needs.

Drop tables come from the wiki (data/drop_tables.json, scripts/build_drop_tables.py). Training
methods are linked to the sources they kill/loot via `drop_sources` on their wiki rate rows
(scripts/link_drop_sources.py). For an ironman a slightly slower method can be the better one when
its drops feed another goal, so each method's drops are scored against the player's needs:

  * need     items other plans are short of (dragon dart tips, pure essence...), with how many per hour
  * charm    Summoning charms, while Summoning still needs xp the bank can't cover
  * unique   rare drops (1/250 or rarer, from at most a few sources, not from the shared rare drop
             table) the player doesn't own, with the expected hours to get one

Kills per hour come from the method's xp rate and the monster's xp per kill (combat xp, or Slayer xp
for Slayer methods); without that (pickpocketing, wisps, skilling bosses) chances are per kill/roll.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from . import datapkg

from .models import PlayerState

UNIQUE_CHANCE = 1 / 250
# An item many sources drop (uncut gems, runes) isn't a unique even when it's rare per kill.
UNIQUE_MAX_SOURCES = 15
# More kills an hour than this means the xp figure isn't per kill of this monster (a whole-task or
# AoE-cluster rate), so per-hour drops aren't estimated.
MAX_KILLS_PER_HOUR = 1500
# A unique only counts as a reason to pick a method if it's this likely per kill...
SIDE_UNIQUE_CHANCE = 1 / 5000
# ...or expected within this many hours.
SIDE_UNIQUE_HOURS = 100
SUMMONING_CHARMS = ("Gold charm", "Green charm", "Crimson charm", "Blue charm")
COMBAT_STYLES = {"Melee", "Attack", "Strength", "Defence", "Ranged", "Magic", "Necromancy", "Constitution"}
# Rare drops that aren't things you'd train for.
_NOT_UNIQUE = re.compile(r"clue scroll|^coins$|^(?:\w+ )?casket|effigy|^starved ancient|\(unchecked\)", re.I)


# Drop lines that name a charged/variant form of the item plans ask for: "Black mask (10)" (cave horrors,
# 1/512) is the Black mask; without the alias the only "Black mask" source was Sea horror at 1/10,000.
ITEM_ALIASES = {"Black mask (10)": "Black mask"}


@cache
def tables() -> dict:
    path = datapkg.path("drop_tables.json")
    d = json.loads(path.read_text()) if path.is_file() else {"sources": {}, "monsters": {}}
    for drops in d["sources"].values():
        for line in drops:
            line[0] = ITEM_ALIASES.get(line[0], line[0])
    return d


@cache
def unfarmable() -> frozenset[str]:
    """Monsters whose drops can't be farmed at a generic kill rate: minigame and random-event monsters,
    player-owned farm animals, Daemonheim (data/unfarmable_monsters.json, scripts/build_unfarmable_monsters.py).
    They stay named drop sources; the gatherer just doesn't estimate hours for them."""
    path = datapkg.path("unfarmable_monsters.json")
    return frozenset(json.loads(path.read_text())["monsters"]) if path.is_file() else frozenset()


# Worded rarities (drop lines with no fraction, ~18,000 of them): the wiki's own sort value for each word
# (Module:DropsLine), taken as a chance per kill. A guess; callers flag it.
WORDED_CHANCE = {"common": 1 / 16, "uncommon": 1 / 64, "rare": 1 / 256, "very rare": 1 / 1024}


def drop_chance(chance) -> tuple[float | None, bool]:
    """(chance per kill/roll, guessed): a fraction as is, a worded rarity at WORDED_CHANCE, else None."""
    if isinstance(chance, (int, float)):
        return (chance if chance > 0 else None), False
    worded = WORDED_CHANCE.get(str(chance or "").strip().lower())
    return (worded, True) if worded else (None, False)


@cache
def source_count() -> dict[str, int]:
    counts: dict[str, int] = {}
    for drops in tables()["sources"].values():
        for item in {d[0] for d in drops}:
            counts[item] = counts.get(item, 0) + 1
    return counts


@cache
def bosses() -> frozenset[str]:
    return frozenset(tables().get("bosses", []))


@cache
def _item_index() -> dict[str, list[tuple[str, float | str | None, float]]]:
    index: dict[str, list] = {}
    for src, drops in tables()["sources"].items():
        for name, chance, qty, _ in drops:
            index.setdefault(name.lower(), []).append((src, chance, qty))
    return index


def sources_of(item: str) -> list[tuple[str, float | str | None, float]]:
    """Every source that drops `item`: (source, chance per kill/roll or worded rarity, quantity)."""
    return _item_index().get(item.lower(), [])


@dataclass
class DropNote:
    item: str
    kind: str  # need | charm | unique
    chance: float | None  # per kill/roll
    per_hour: float | None  # expected count per hour, when kills/hour is known
    detail: str = ""

    @property
    def worth_chasing(self) -> bool:
        """For uniques: likely enough to be a reason to pick a method."""
        if self.kind != "unique":
            return True
        if self.per_hour:
            return 1 / self.per_hour <= SIDE_UNIQUE_HOURS
        return bool(self.chance) and self.chance >= SIDE_UNIQUE_CHANCE

    def describe(self) -> str:
        if self.kind == "unique":
            when = f", ~{1 / self.per_hour:.0f}h on average" if self.per_hour else ""
            return f"{self.item} (1/{1 / self.chance:,.0f}{when}; you don't own it)"
        rate = f"~{self.per_hour:,.0f}/h" if self.per_hour else (f"{self.chance:.1%} per kill" if self.chance else "")
        return f"{self.item} {rate}{f' ({self.detail})' if self.detail else ''}".strip()


def kills_per_hour(row: dict, skill: str, xp_per_hour: float) -> float | None:
    """Kills (or actions: cleanses, loot rolls) per hour: the row's own `actions_per_hour` when the
    guide states one, else xp/h over the monster's xp per kill."""
    if row.get("actions_per_hour"):
        return float(row["actions_per_hour"])
    monsters = tables()["monsters"]
    key = "slayer_xp" if skill == "Slayer" else "xp"
    xps = [monsters[s][key] for s in row.get("drop_sources") or [] if s in monsters and monsters[s].get(key)]
    if not xps or (skill != "Slayer" and skill not in COMBAT_STYLES):
        return None
    kph = xp_per_hour / (sum(xps) / len(xps))
    return kph if kph <= MAX_KILLS_PER_HOUR else None


# Kills per hour on a monster the guides don't cover: single targets are limited by respawns.
MAX_ESTIMATED_KILLS_PER_HOUR = 300
# Bosses (more life points than this) need a strategy, not a generic xp rate: no estimate.
MAX_ESTIMATED_LIFE_POINTS = 50_000


BOSS_OVERHEAD_SECONDS = 10.0  # per kill at a guideless boss (collection_logs.boss_overhead fits ~0, floor 10)


def boss_sim_kills_per_hour(player: PlayerState, monster: str,
                            overhead: float = BOSS_OVERHEAD_SECONDS) -> tuple[float, str, float] | None:
    """(kills/h, combat skill, xp/h) at a boss no money making guide rates, from the combat sim (the player's
    best style against its stats) plus a per-kill overhead; None without combat stats or when the player can't
    fight it. Used wherever a boss drop has to be costed (Hermodic plates for Deathdealer tier 80)."""
    from .gear import STYLES
    from .mechanics import combat

    m = tables()["monsters"].get(monster)
    if not m or not m.get("xp") or (m.get("members", True) and not player.members):
        return None
    sim = combat.kills_per_hour(player, monster, overhead)
    if sim is None or sim[0] <= 0:
        return None
    kph, how, _ = sim
    return kph, STYLES[how], kph * m["xp"]


def estimated_kills_per_hour(player: PlayerState, monster: str,
                             style: tuple[str, float]) -> tuple[float, str, float] | None:
    """(kills/h, combat skill, xp/h) for killing `monster`, or None if it can't be estimated or the
    player can't fight it (Slayer level, members-only on free-to-play). `style` is the player's best
    combat (skill, xp/h) for a generic spot, from gear.best_style. Bosses go through the combat sim
    (boss_sim_kills_per_hour) when no guide rates them."""
    m = tables()["monsters"].get(monster)
    if monster in bosses():
        return None if boss_kills_per_hour(player, monster) else boss_sim_kills_per_hour(player, monster)
    if not m or not m.get("xp") or (m.get("life_points") or 0) > MAX_ESTIMATED_LIFE_POINTS:
        return None
    if monster in unfarmable():
        return None
    if (m.get("slayer_level") or 0) > player.level("Slayer") or (m.get("members", True) and not player.members):
        return None
    from .gear import STYLES
    from .mechanics import combat

    sim = combat.kills_per_hour(player, monster)
    if sim is None:  # no combat stats for it: the old estimate from the player's best generic xp rate
        skill, rate = style
        kph = min(MAX_ESTIMATED_KILLS_PER_HOUR, rate / m["xp"])
        return kph, skill, kph * m["xp"]
    kph, how, _ = sim
    kph = min(MAX_ESTIMATED_KILLS_PER_HOUR, kph)
    return kph, STYLES[how], kph * m["xp"]


# Boss kill rates from the wiki's money making guides (data/boss_rates.json, scripts/build_boss_rates.py).
# The guides assume an experienced player in the gear they list ("Tier 80+ weapon"); a weaker weapon
# scales the rate down by its ability damage vs a typical weapon of that tier (at most the guide's rate).
GROUP_ACTIVITY = re.compile(r"\b(duo|trio|team|group|mass|\d[- ]?(?:man|player)s?)\b", re.I)
DEFAULT_GUIDE_TIER = 80
MIN_BOSS_GEAR_FACTOR = 0.3


@cache
def boss_rates() -> dict[str, list[dict]]:
    path = datapkg.path("boss_rates.json")
    out: dict[str, list[dict]] = {}
    for r in json.loads(path.read_text())["rows"] if path.is_file() else []:
        if r.get("monster"):
            out.setdefault(r["monster"], []).append(r)
    return out


@dataclass
class BossRate:
    kills_per_hour: float
    guide: str
    style: str
    gear_factor: float
    group: bool  # the guide is for a team; solo is slower or impossible
    row: dict


def boss_kills_per_hour(player: PlayerState, monster: str) -> BossRate | None:
    """Kills/h at a boss from its money making guide, scaled for the player's best weapon."""
    from .gear import STYLES, best_loadout, typical_damage
    rows = boss_rates().get(monster, [])
    solo = [r for r in rows if not GROUP_ACTIVITY.search(r["activity"] or "")]
    if not rows:
        return None
    # Several guides (an "Alt Guide" with a faster setup): take the median one.
    pool = sorted(solo or rows, key=lambda r: r["kph"])
    row = pool[(len(pool) - 1) // 2]
    tier = row.get("gear_tier") or DEFAULT_GUIDE_TIER
    from .mechanics import combat

    target = combat.monster(monster)
    if target and target.get("life_points"):
        # Expected damage against this boss (hit chance included) vs a typical setup of the guide's tier.
        style, factor = max(((s, combat.loadout_dps(player, best_loadout(player, s), target)
                              / max(1e-9, combat.reference_dps(99, tier, target, s))) for s in STYLES),
                            key=lambda t: t[1])
    else:
        style, factor = max(((s, best_loadout(player, s).ability_damage / typical_damage(s, tier)) for s in STYLES),
                            key=lambda t: t[1])
    factor = max(MIN_BOSS_GEAR_FACTOR, min(1.0, factor))
    return BossRate(row["kph"] * factor, row["guide"], style, factor, not solo, row)


@cache
def collection_logs() -> dict[str, dict]:
    """Boss collection logs: {collection: {items: {name: chance per kill or None}, pets, achievement}}
    (scripts/build_collection_logs.py)."""
    return json.loads(datapkg.path("collection_logs.json").read_text())["collections"]


# Collection log vars the plugin couldn't name (ITEMLOG_ITEM_<config>, where the config name isn't the item's):
# word stand-ins, then a match counts only if every word of the suffix appears in exactly one log item's name.
_LOG_WORDS = {"dks": "dagannoth", "kq": "kalphite queen", "qbd": "queen black dragon", "gwd": "", "egwd": "",
              "barrows": "", "axe": "hatchet", "shard3": "shard 3", "body": "cuirass", "legs": "greaves",
              "gloves": "gauntlets"}


def resolve_item_log(item_log: dict[str, int]) -> dict[str, int]:
    """The item log with ITEMLOG_ITEM_* keys renamed to the boss log item they unambiguously match."""
    names = {i for c in collection_logs().values() for i in c["items"]}
    out = {}
    for key, n in item_log.items():
        if key.startswith("ITEMLOG_ITEM_"):
            words = [w for w in (_LOG_WORDS.get(t, t) for t in key[len("ITEMLOG_ITEM_"):].lower().split("_")) if w]
            hits = [i for i in names if words and all(w in i.lower() for w in words)]
            key = hits[0] if len(hits) == 1 else key
        out[key] = out.get(key, 0) + n
    return out


def owned(player: PlayerState, item: str) -> bool:
    """Owned per the exported inventories, the collection log (obtained before, maybe sold or stored with
    Diango since), or ticked off in an achievement's item list (boss logs)."""
    if item in player.holdings or player.item_log.get(item, 0) > 0:
        return True
    return any(r.get("description") == item and r.get("complete")
               for a in player.achievements.values() for r in a.requirements)


def notable_drops(row: dict, skill: str, xp_per_hour: float, player: PlayerState,
                  needs: dict[str, str]) -> list[DropNote]:
    """The drops of a method that matter to this player, most useful first.

    `needs` maps item -> why it's needed (from player_needs)."""
    kph = kills_per_hour(row, skill, xp_per_hour)
    best: dict[str, DropNote] = {}
    for source in row.get("drop_sources") or []:
        for item, chance, qty, rare_table in tables()["sources"].get(source, []):
            chance = chance if isinstance(chance, (int, float)) else None
            per_hour = kph * chance * qty if kph and chance else None
            note = None
            if item in needs:
                note = DropNote(item, "charm" if item in SUMMONING_CHARMS else "need", chance, per_hour, needs[item])
            elif chance and chance <= UNIQUE_CHANCE and not rare_table and not _NOT_UNIQUE.search(item) \
                    and source_count().get(item, 0) <= UNIQUE_MAX_SOURCES and not owned(player, item):
                note = DropNote(item, "unique", chance, kph * chance if kph else None)
            if note and (item not in best or (note.chance or 0) > (best[item].chance or 0)):
                best[item] = note
    order = {"need": 0, "charm": 1, "unique": 2}
    return sorted(best.values(), key=lambda n: (order[n.kind], -(n.per_hour or n.chance or 0)))


# War's Retreat campfire upgrades (War's Wares): tier, Firemaking level, logs (1,000 each, plus 1,000
# Marks of War), from the wiki's Campfire page.
CAMPFIRE_TIERS = [("regular", 1, None), ("oak", 15, "Oak logs"), ("willow", 30, "Willow logs"),
                  ("maple", 45, "Maple logs"), ("yew", 60, "Yew logs"), ("magic", 75, "Magic logs"),
                  ("elder", 90, "Elder logs"), ("eternal magic", 100, "Eternal magic logs")]
CAMPFIRE_LOGS = 1_000


def campfire_needs(player: PlayerState) -> dict[str, int]:
    """Logs for the campfire upgrades above the player's tier that their Firemaking level allows."""
    if player.campfire_tier is None:
        return {}
    names = [t for t, _, _ in CAMPFIRE_TIERS]
    have = names.index(player.campfire_tier) if player.campfire_tier in names else 0
    return {logs: CAMPFIRE_LOGS for tier, level, logs in CAMPFIRE_TIERS[have + 1:]
            if logs and player.level("Firemaking") >= level}


def player_needs(plans: dict[str, "LevelPlan"], player: PlayerState | None = None) -> dict[str, str]:  # noqa: F821
    """Items worth getting as drops, from the player's level plans ({skill: plan to its next goal})."""
    needs: dict[str, str] = {}
    for item, n in (campfire_needs(player) if player else {}).items():
        if (player.holdings.get(item, 0)) < n:
            needs[item] = f"War's Retreat campfire upgrade ({n:,})"
    for skill, plan in plans.items():
        for item, n in plan.materials_short.items():
            needs.setdefault(item, f"{n:,} short for {skill}")
    summoning = plans.get("Summoning")
    if summoning is not None and (summoning.materials_unmodelled or summoning.materials_short):
        for charm in SUMMONING_CHARMS:
            needs.setdefault(charm, "Summoning")
    return needs
