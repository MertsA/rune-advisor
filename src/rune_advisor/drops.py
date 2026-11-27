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
from .xp import SKILL_ALIASES

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
WORDED_CHANCE = {"common": 1 / 16, "uncommon": 1 / 64, "rare": 1 / 256, "very rare": 1 / 1024,
                 # No figure at all: flagged guesses (the user's rule, 2026-11-21: model it, flag it, fix it later).
                 "varies": 1 / 64, "unknown": 1 / 128}


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


# Flagged estimates for drop sources with no kill or catch rate (the user's rule, 2026-11-21: an item and its whole
# downstream chain shouldn't be unavailable because a rate isn't known; model it roughly, flag it, and replace it
# once there's data. `radv estimates` ranks the flagged sources plans lean on, so the ones the planner would send the
# player to get fixed first). Per hour: kills, catches, opens or loot rolls.
ASSUMED_RATES = {
    "boss": (10.0, "a boss with no guide or combat stats: 10 kills an hour"),
    "big": (20.0, "a big monster no estimate covers: 20 kills an hour"),
    "minigame": (30.0, "a minigame or activity monster: 30 an hour"),
    "no xp": (60.0, "a creature with no combat xp: 60 an hour"),
    "scenery": (60.0, "scenery (a plant, chest or node) with no rate: 60 an hour"),
    "creature": (60.0, "a Hunter creature with no catch rate: 60 an hour"),
    # The Hunter training guide's giant chinchompas: 20 catches an hour (21,000 xp a catch at 420,000 xp/h).
    "big game": (20.0, "Big Game Hunter quarry with no rate: 20 catches an hour"),
    "daily": (1 / 24, "an NPC's hand-out or a repeatable activity: once a day"),
    "wave": (1.0, "a minigame's last wave: one run an hour"),
    "random": (1.0, "a random event while skilling: one an hour"),
    "chest": (4.0, "a chest (keys, resets): 4 opens an hour"),
}
# The Wendlewick fish farm's pools: fished as one farm (its content rows), never a spot of their own.
FISH_FARM_POOLS = {"Lobster pool", "Swordfish pool", "Shark pool", "Giant crayfish pool"}
# Chests opened with a key a recipe makes: costed per key (gathering._keyed_chest_options), never at an hourly guess.
# The Raptor's chest takes a Raptor key, combined from four parts that drop on Slayer tasks (the wiki, 2026-11-27).
KEY_CHESTS = {"Raptor's chest of slaying": "Raptor key"}
CHEST_OPEN_HOURS = 1 / 60  # a minute to get there and open it (assumed)
DAILY_KINDS = {"npc", "other"}  # drop_source_kinds.json kinds costed as once a day (calendar time, a minute of play)
ONE_OFF_KINDS = {"quest", "achievement"}  # rewards once: no repeatable source
# Clue scroll caskets per hour by tier, solving clues the player gets (an estimate; clue sources aren't modelled).
CASKETS_PER_HOUR = {"easy": 12.0, "medium": 8.0, "hard": 5.0, "elite": 3.0, "master": 2.0}
# Sources no hour of play gets: Treasure Hunter, paid promotions and past holiday events.
EXCLUDED_SOURCES = re.compile(r"Treasure Hunter|promotion|Christmas|Santa|Grotto|Halloween|Hween|Easter|Beach \(|"
                              r"The Beach|\(20\d\d\)|Archive|Squeal of Fortune|Solomon|Mystery box|Present|"
                              r"Holiday|Event|appearance token|Lottery|Golden Gnome|Summer of Adventure|Solstice|Genie's Wishes|Bundle|"
                              r"Choosedays|Frank's Free Chest", re.I)


def assumed_rate(player: PlayerState, source: str, style: tuple[str, float]) -> tuple[float, str, float, str] | None:
    """(per hour, skill, xp/h, why) for a drop source with no kill or catch rate: a flagged estimate (ASSUMED_RATES),
    or None where no play gets it (EXCLUDED_SOURCES, a Slayer level the player lacks, free-to-play)."""
    if EXCLUDED_SOURCES.search(source) or source in KEY_CHESTS:
        return None
    skill = style[0]
    if source.endswith(" (pickpocket)"):
        return None  # rated by the Thieving sim's rows, at the player's level
    if source in FISH_FARM_POOLS:
        return None  # rated as the whole farm (data/content_rates.json, "Wendlewick fish farm")
    if source in WAVE_FINALES:  # the Fight Cave / Fight Kiln: a whole run per kill
        n = ASSUMED_RATES["wave"][0]
        return n, skill, 0.0, ASSUMED_RATES["wave"][1]
    m = tables()["monsters"].get(source)
    kind = source_kind(source)
    if kind in ONE_OFF_KINDS or kind == "item":
        return None  # a quest reward, or a container (the gatherer opens those)
    if m is None and kind == "monster":  # a monster page with no stats in the tables (often a boss's page name)
        n, why = ASSUMED_RATES["boss"]
        return n, skill, 0.0, f"a monster with no stats: {n:g} kills an hour"
    if m is None or kind in ("scenery", "creature", "random", "big game") or kind in DAILY_KINDS:
        key = "daily" if kind in DAILY_KINDS else kind if kind in ("scenery", "creature", "random", "big game") else "scenery"
        if key == "scenery" and "chest" in source.lower():
            key = "chest"  # a chest wants a key or a reset: not a node to hit 60 times an hour
        n, why = ASSUMED_RATES[key]
        return n, skill, 0.0, why
    if (m.get("members", True) and not player.members) or (m.get("slayer_level") or 0) > player.level("Slayer"):
        return None
    xp = m.get("xp") or 0
    if source in bosses() and source in boss_rates():
        guide = boss_kills_per_hour(player, source)
        if guide is None or not guide.group or _guide_blocked(player, guide.row):
            return None  # levels or quests the guide needs: an unlock, not a missing rate
        n = ASSUMED_RATES["boss"][0]
        return n, skill, n * xp, f"a team boss done solo: {n:g} kills an hour"
    kind = ("boss" if source in bosses() else "minigame" if source in unfarmable()
            else "no xp" if not xp else "big")
    n, why = ASSUMED_RATES[kind]
    return n, skill, n * xp, why


@cache
def source_kinds() -> dict[str, str]:
    """What each drop source page is: monster, item, scenery, npc, creature, quest, achievement or other
    (data/drop_source_kinds.json, scripts/build_drop_source_kinds.py)."""
    path = datapkg.path("drop_source_kinds.json")
    return json.loads(path.read_text())["kinds"] if path.is_file() else {}


def source_kind(source: str) -> str:
    if source in bosses():
        return "monster"  # "The Twin Furies" page vs the boss: bosses get the boss rules whatever the page is
    kind = source_kinds().get(source)
    if kind:
        return kind
    return "monster" if source in tables()["monsters"] else "other"


def _guide_blocked(player: PlayerState, row: dict) -> bool:
    """A money making guide's levels or quests the player doesn't have yet."""
    return any(player.level(SKILL_ALIASES.get(sk, sk)) < lvl for sk, lvl in (row.get("skills") or {}).items()) \
        or any(not (q in player.quests and player.quests[q].done) for q in row.get("quests") or [])


LOOT_TABLE = re.compile(r"^(.+) loot \((story|normal|hard)\)$")
# Bosses that are a minigame's last wave (the Fight Cave, the Fight Kiln): reached after the waves before them, so
# the combat sim's rate on the boss alone is no kill rate.
WAVE_FINALES = {"TzTok-Jad", "Har-Aken"}


def estimated_kills_per_hour(player: PlayerState, monster: str,
                             style: tuple[str, float]) -> tuple[float, str, float] | None:
    """(kills/h, combat skill, xp/h) for killing `monster`, or None if it can't be estimated or the
    player can't fight it (Slayer level, members-only on free-to-play). `style` is the player's best
    combat (skill, xp/h) for a generic spot, from gear.best_style. Bosses go through the combat sim
    (boss_sim_kills_per_hour) when no guide rates them, else at the guide's rate when the player meets its
    levels and quests and it isn't team content."""
    if (loot := LOOT_TABLE.match(monster)) is not None:
        # A boss whose loot the wiki tables per mode ("Zemouregal & Vorkath loot (normal)"): the normal mode's
        # table at the boss's own rate; story and hard modes aren't separate rates here.
        return estimated_kills_per_hour(player, loot.group(1), style) if loot.group(2) == "normal" else None
    if monster in WAVE_FINALES:
        return None
    m = tables()["monsters"].get(monster)
    if monster in bosses():
        if (guide := boss_kills_per_hour(player, monster)) is None:
            return boss_sim_kills_per_hour(player, monster)
        if guide.group or _guide_blocked(player, guide.row):
            return None
        from .gear import STYLES
        return guide.kills_per_hour, STYLES[guide.style], guide.kills_per_hour * ((m or {}).get("xp") or 0)
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
    return any(r.get("complete") for r in _requirements_by_description(player).get(item, ()))


def _requirements_by_description(player: PlayerState) -> dict[str, list[dict]]:
    """Every achievement requirement test under its description. Planning asks `owned` tens of thousands of
    times, and walking all achievements each time was a fifth of a guide's run. The index holds the tests
    themselves, so a flag set later is seen; it is rebuilt when the player's achievements are another dict
    (kept alongside, so the comparison is by identity) or change in number."""
    cached = player.__dict__.get("_requirements_by_description")
    if cached is None or cached[0] is not player.achievements or cached[1] != len(player.achievements):
        index: dict[str, list[dict]] = {}
        for a in player.achievements.values():
            for r in a.requirements:
                if isinstance(d := r.get("description"), str):
                    index.setdefault(d, []).append(r)
        cached = player.__dict__["_requirements_by_description"] = (player.achievements, len(player.achievements), index)
    return cached[2]


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
