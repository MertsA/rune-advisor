"""Bottom-up gathering rates: the RuneScape Wiki's Woodcutting and Mining mechanics, per player.

Instead of a guide's xp/h for "maple trees", compute what this player gets from their levels and
tools, using the formulas and data behind the wiki's calculators (data/skilling_sim.json,
scripts/build_skilling_sim.py):

Woodcutting (Woodcutting#Mechanics, Module:SkillUtils): the hatchet's power and the tree's base
chop chance give a low (level 1) and high (level 99) chance, the level interpolates between them
(Jagex's integer interpolation), outfit/ring/skillchompa boosts multiply the high chance, and an
attempt happens every 4 ticks. Trees fall after a log with the tree's fell chance (x/256); walking
to the next tree costs FELL_TICKS, which the wiki calculator leaves out.

Mining (Mining#Damage, Module:Mining Rate Calculator Module): each swing (4 ticks) deals
Mining level + floor(Strength/10) + the pickaxe's damage roll + crits, less any hardness the
pickaxe's penetration doesn't cover; the rock's durability is the damage per ore; xp is damage x
the rock's multiplier x 0.4. The play style (method) scales it: Semi-AFK keeps 90%, rockertunities
add extra damage on core rocks.

Tools come from the player's items (a hatchet/pickaxe in the bank or worn), else overrides.toml
(`hatchet = "Crystal"`, `pickaxe = "Crystal"`), else the tool belt's (the export's TOOLBELT_*_LEVEL), else the best tool
for the level is assumed and the result says so.

Boosts (mechanics/profile.py): Mining applies the wiki calculator's own boost terms from Module:MS Data
(Varrock armour, golden mining suit/magic golem/Starfury outfits, Ring of Whispers, familiars, the
pickaxe's perks, potions, skillchompas, pocket items, the Mining cape, stone spirits/juju);
Woodcutting the hatchet's Honed/Wise perks, the Ring of Whispers, sentinel outfits and the Lumberjack
xp outfit. Items gathered for a plan count bank trips (mechanics/trips.py: ore box, porters); the xp
rate assumes the player drops or banks for free, which is how training is usually done.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from functools import cache
from . import datapkg

from .mechanics import data as mechanics_data
from .mechanics import profile as boost_profile
from .mechanics import trips
from .models import PlayerState

TICKS_PER_HOUR = 6000
WC_ATTEMPT_TICKS = 4
WC_MULT = 10_000  # woodcutting thresholds are kept in ten-thousandths
FELL_TICKS = 4  # walking to another tree after one falls (our assumption)
DEFAULT_MINING_METHOD = "Semi-AFK + Rockertunities"
# Trees that regrow too slowly to train on (wiki tree pages): name -> (trees available, regrow hours,
# logs per tree). They're still a source of their logs, at most this many per real-time hour.
LIMITED_TREES = {"Bloodwood tree": (8, 6, 4)}  # 25% fell chance per log: ~4 logs per tree
# Uncharted Isles nodes (wiki: Uncharted Isles, Golden Bamboo (tree), Alaea crablet): a claimed island holds at
# most 3 of crablet/bamboo/tortle/wobbegong, each giving so much a day (a bamboo plant 100 logs, a crablet
# colony 5 x 40 salt) before "exhausted for today" (reset 00:00 UTC). name -> (nodes, units a day each). The
# sim's per-hour rate is the pace while a node lasts; the gatherer adds the days, and they're no training row.
DAILY_NODES = {"Golden Bamboo (tree)": (3, 100), "Alaea crablet": (3, 200)}
# Trees the wiki calculator lacks, fitted to recorded sessions. Chimera trees (Fenmoor):
# chop chance 7.3 fits 34/99 swings at 72 and 115/250 at 99 with a crystal hatchet, and predicts 60% at 96 with
# Ember and glade + the sentinel outfit (measured 61.6%). Every log gives 195 base xp; the log type is random
# (pooled mix of 194 logs below). The fell chance (logs per tree 5, 6, 28) is a rough guess.
MEASURED_TREES = [
    {"name": "Chimera tree", "logs": "Maple logs", "level": 72, "xp": 195, "chance": 7.3, "ratio": 31250, "fell": 20,
     "shares": {"Maple logs": 39 / 194, "Yew logs": 39 / 194, "Oak logs": 35 / 194, "Logs": 30 / 194,
                "Willow logs": 29 / 194, "Magic logs": 20 / 194, "Elder logs": 2 / 194}},
]
WC_HIGH_BOOSTS = {"Ring of Whispers": 1.03, "Nature's sentinel": 1.07, "Sentinel": 1.05}
# Transcribed from Module:Woodcutting chance calculator (multipliers_list).
WC_MENAPHOS = 1.1
WC_OFFHAND = (1.06, 0.20)  # off-hand hatchet: high-chance multiplier, extra logs
WC_JUJU_LOGS = 0.05
WC_CADANTINE_LOGS = 0.025  # per incense tier
WC_TORSTOL_XP = 0.005  # per incense tier
WC_SKILLCHOMPA = {"Cobalt": 1.01, "Viridian": 1.02, "Azure": 1.03, "Crimson": 1.04, "Crystal": 1.05}
WC_FAMILIAR = {"Beaver": 2}
WC_CRYSTALLISE = {"Crystallise": 1.2, "Crystallise and Light Form": 1.4}


@cache
def data() -> dict:
    path = datapkg.path("skilling_sim.json")
    return json.loads(path.read_text()) if path.is_file() else {"trees": [], "hatchets": [], "rocks": [], "picks": []}


def threshold_at_level(level_1: int, level_99: int, level: int) -> int:
    """A skilling roll's success threshold at a level, between its level-1 and level-99 values. The game
    floors the two parts separately, so this is not one linear interpolation rounded once."""
    return (level_1 * (99 - level)) // 98 + (level_99 * (level - 1)) // 98


@dataclass
class Rate:
    activity: str  # tree / rock
    item: str  # log / ore
    level: int
    chance: float | None  # woodcutting success chance per attempt
    items_per_hour: float
    xp_per_hour: float
    xp_per_item: float
    tool: str
    assumed_tool: bool
    notes: str = ""
    # Rocks that roll one of several items per ore (granite sizes, gem rocks): item -> share of the ores.
    shares: dict[str, float] = field(default_factory=dict)
    # Items per hour before bank trips (items_per_hour counts them), and how the trips are made.
    raw_items_per_hour: float | None = None
    trip: str = ""

    def yields(self) -> dict[str, float]:
        return {i: self.items_per_hour * s for i, s in self.shares.items()} if self.shares else {self.item: self.items_per_hour}


# --- Woodcutting -----------------------------------------------------------------

def _chop_thresholds(tree: dict, power: int) -> tuple[int, int]:
    """A tree's success thresholds (in ten-thousandths of the 256 roll) at level 1 and level 99 for a hatchet
    power: half the tree's base chance again per 100 power above a bronze hatchet's, and the level-99 value a
    fixed ratio of the level-1 one."""
    base = int(tree["chance"] * WC_MULT)
    level_1 = base + (base // 2) * (power - 100) // 100
    return level_1, level_1 * (tree.get("ratio") or 31_250) // WC_MULT


def woodcutting_chance(tree: dict, power: int, level: int, high_boosts: list[float] = (),
                       honed_rank: int = 0, familiar: int = 0, menaphos: bool = False) -> float:
    """Chance of a log per attempt. The order and the rounding of the steps are the game's: each multiplier on
    the level-99 threshold is floored as it is applied (Menaphos trees' x1.1 among them, when asked for), Honed
    raises both thresholds and rounds up, and a familiar (beaver) adds `familiar` tenths of the unboosted
    spread to both."""
    level_1, level_99 = _chop_thresholds(tree, power)
    spread_tenths = math.floor(10 * (level_99 - level_1) / 98)  # taken before any boost
    multipliers = [*high_boosts, *([WC_MENAPHOS] if menaphos and tree.get("is_menaphos") else [])]
    for multiplier in multipliers:
        level_99 = math.floor(level_99 * multiplier)
    if honed_rank:
        honed = 1 + 0.02 * honed_rank
        level_1, level_99 = math.ceil(level_1 * honed), math.ceil(level_99 * honed)
    if familiar:
        lift = math.floor(familiar * spread_tenths / 10)
        level_1, level_99 = level_1 + lift, level_99 + lift
    return min(1.0, (threshold_at_level(level_1, level_99, level) + 1) / (256 * WC_MULT))


def _members_only(name: str) -> bool:
    from .recommend.catalogue import members_items
    return name in members_items()


def tool_names(label: str, kind: str) -> set[str]:
    """Item names a calculator tool label goes by: "Crystal pickaxe", "Pickaxe of earth and song"."""
    return {f"{label} {kind}".lower(), f"{kind} of {label}".lower()}


def owns_tool(player: PlayerState, label: str, kind: str) -> bool:
    names = tool_names(label, kind)
    return any(h.lower().split(" + ")[0].removeprefix("augmented ") in names for h in player.holdings)


def best_hatchet(player: PlayerState) -> tuple[dict, bool]:
    """(hatchet, assumed?) the player cuts with."""
    usable = [h for h in data()["hatchets"] if player.level("Woodcutting") >= h["level"]
              and (player.members or not _members_only(f"{h['label']} hatchet"))]
    named = player.tools.get("hatchet")
    for h in sorted(usable, key=lambda h: -h["power"]):
        if (named and named.lower() in {h["label"].lower(), *tool_names(h["label"], "hatchet")}) or \
                owns_tool(player, h["label"], "hatchet"):
            return h, False
    return max(usable, key=lambda h: h["power"]), True


def woodcutting_boosts(player: PlayerState) -> tuple[list[float], list[str]]:
    held, boosts, names = player.holdings, [], []
    if "Ring of Whispers" in held:
        boosts.append(WC_HIGH_BOOSTS["Ring of Whispers"]); names.append("Ring of Whispers")
    if any(n.startswith("Nature's sentinel") for n in held):
        boosts.append(WC_HIGH_BOOSTS["Nature's sentinel"]); names.append("Nature's sentinel outfit")
    elif any(re.match(r"(Oaken|Willow|Maple) sentinel", n) for n in held):
        boosts.append(WC_HIGH_BOOSTS["Sentinel"]); names.append("sentinel outfit")
    return boosts, names


def woodcutting_rates(player: PlayerState, level: int | None = None, hatchet: dict | None = None,
                      fell_walk: bool = True) -> list[Rate]:
    level = level or player.level("Woodcutting")
    hatchet, assumed = (hatchet, False) if hatchet else best_hatchet(player)
    power = hatchet["power"] + ((hatchet.get("proficiency") or {}).get("power", 0)
                                if level >= (hatchet.get("proficiency") or {}).get("level", 999) else 0)
    boosts, names = woodcutting_boosts(player)
    hatchet_perks = boost_profile.perks(player, "hatchet")
    honed = hatchet_perks.get("Honed", 0) if hatchet.get("augmentable") else 0
    xp_profile = boost_profile.profile(player, "Woodcutting", "hatchet")
    if honed:
        names.append(f"Honed {honed}")
    cfg = boost_profile.boosts(player)
    logs_mult = 1.0
    if chompa := (cfg.get("skillchompa") or {}).get("Woodcutting"):
        boosts.append(WC_SKILLCHOMPA.get(chompa, 1.0)); names.append(f"{chompa} skillchompas")
    if boost_profile.has(player, "Off-hand*hatchet*"):
        boosts.append(WC_OFFHAND[0]); logs_mult += WC_OFFHAND[1]; names.append("off-hand hatchet")
    if boost_profile.juju(player, "Woodcutting"):
        logs_mult += WC_JUJU_LOGS; names.append("juju woodcutting")
    if tier := int(cfg.get("cadantine_incense") or 0):
        logs_mult += WC_CADANTINE_LOGS * tier; names.append(f"cadantine incense {tier}")
    xp_mult = 1 + xp_profile.xp_bonus
    if tier := int(cfg.get("torstol_incense") or 0):
        xp_mult *= 1 + WC_TORSTOL_XP * tier; names.append(f"torstol incense {tier}")
    familiar = WC_FAMILIAR.get(boost_profile.familiar(player, "Woodcutting") or "", 0)
    if familiar:
        names.append("beaver")
    crystallise = WC_CRYSTALLISE.get(cfg.get("crystallise") or "", 1.0)
    names += xp_profile.notes
    trip = trips.gathering(player, "Woodcutting")
    out = []
    for tree in data()["trees"] + MEASURED_TREES:
        if level < tree["level"] or not tree.get("logs") or not tree.get("chance"):
            continue
        if not player.members and _members_only(tree["logs"]):  # free-to-play: no members' trees
            continue
        chance = woodcutting_chance(tree, power, level, boosts, honed, familiar, bool(cfg.get("menaphos")))
        fell = max(0, tree.get("fell") or 0) / 256 if fell_walk else 0
        ticks_per_log = WC_ATTEMPT_TICKS / chance + fell * FELL_TICKS
        per_hour = TICKS_PER_HOUR / ticks_per_log * logs_mult
        note = ", ".join(names)
        if tree["name"] in LIMITED_TREES:
            trees, regrow, logs = LIMITED_TREES[tree["name"]]
            per_hour = min(per_hour, trees * logs / regrow)
            note = f"{trees} trees, {regrow}h regrow: limited" + (f"; {note}" if note else "")
        # As the calculator: every log (extra ones from the off-hand, juju, incense too) gives the tree's xp.
        xp = (level if tree.get("is_bgh_tree") else tree["xp"]) * xp_mult
        if tree.get("can_crystallise"):
            xp *= crystallise
        out.append(Rate(tree["name"], tree["logs"], level, chance, trip.per_hour(per_hour), per_hour * xp, xp,
                        f"{hatchet['label']} hatchet", assumed, note, shares=dict(tree.get("shares") or {}),
                        raw_items_per_hour=per_hour, trip=trip.how))
    return out


# --- Mining ------------------------------------------------------------------------

def _tiered(level: int, thresholds: list[int] | None, values: list[float], base: float) -> float:
    value = base
    for t, v in zip(thresholds or [], values):
        if level >= t:
            value = v
    return value


def best_pickaxe(player: PlayerState) -> tuple[dict, bool]:
    usable = [p for p in data()["picks"] if player.level("Mining") >= p["level"]
              and (player.members or not _members_only(f"{p['label']} pickaxe"))]
    named = player.tools.get("pickaxe")
    for p in sorted(usable, key=lambda p: -p["avg"]):
        if (named and named.lower() in {p["label"].lower(), *tool_names(p["label"], "pickaxe")}) or \
                owns_tool(player, p["label"], "pickaxe"):
            return p, False
    return max(usable, key=lambda p: (p["avg"], p["pen"])), True


@dataclass
class MiningBoost:
    """What a player's gear, perks and consumables add to mining, as `mining_rate` takes it (the numbers per
    item are in mechanics.json, from the wiki)."""
    level_per: float = 1.0
    level_flat: float = 0.0
    cape: bool = False  # Mining cape: +1 level (unless potions give more), +5% double ore on core rocks
    damage: float = 0.0  # familiar, Ring of Whispers
    crit_chance: float = 0.0  # outfit, pocket, POF
    crit_bonus: float = 0.0  # skillchompa
    double_ore: float = 0.0  # Honed, Fortune, waterfiend
    varrock: int = 0  # Varrock armour tier worn (1-4)
    furnace: float = 0.0  # share of ores smelted (and so lost as ore)
    rck: float = 0.0  # extra rockertunity multiplier (magic golem outfit, Brooch of the Gods)
    careless: int = 0
    explosive: bool = False
    spirits: bool = False  # stone spirits (double ore on rocks that take them)
    juju: str | None = None
    clay_bracelet: bool = False  # two soft clay per ore at Prifddinas' soft clay rocks (Bracelet of clay page)
    xp_bonus: float = 0.0  # additive xp share
    notes: list[str] = field(default_factory=list)


def mining_boost(player: PlayerState) -> MiningBoost:
    """Boosts for Mining: items owned, pickaxe perks and consumables from overrides.toml."""
    t = (mechanics_data().get("mining") or {}).get("boosts") or {}
    b = MiningBoost()
    if pot := boost_profile.potion(player, "Mining"):
        if pot in (t.get("mining_potion") or {}):
            b.level_per, b.level_flat = t["mining_potion"][pot]["per"], t["mining_potion"][pot]["flat"]
            if boost_profile.boosts(player).get("potion_average"):
                # Drunk as each dose wears off (consumables.py): the decaying boost averages half its peak.
                b.level_per, b.level_flat = 1 + (b.level_per - 1) / 2, b.level_flat / 2
            b.notes.append(pot)
    if player.level("Mining") >= 99 and boost_profile.has(player, "Mining cape*"):
        b.cape = True
        b.notes.append("Mining cape")
    if fam := boost_profile.familiar(player, "Mining"):
        b.damage += (t.get("familiar_m") or {}).get(fam, 0)
        if "waterfiend" in fam.lower():
            b.double_ore += 0.05
        b.notes.append(fam)
    if boost_profile.has(player, "Ring of Whispers"):
        b.damage += t["rings"]["Ring of Whispers"]["damage"]
        b.notes.append("Ring of Whispers")
    outfits = t.get("outfit_m") or {}
    for name, items in (("Magic Golem Outfit", ("Magic golem*",)), ("Starfury Outfit", ("Starfury*",))):
        if boost_profile.has(player, *items) and name in outfits:
            b.crit_chance += outfits[name]["crit"]
            b.rck += outfits[name]["rck"]
            b.notes.append(name)
            break
    pieces = sum(1 for n in ("Golden mining helmet", "Golden mining top", "Golden mining trousers",
                             "Golden mining gloves", "Golden mining boots") if n in player.holdings)
    if pieces:
        key = "1 Piece" if pieces == 1 else f"{pieces} Pieces"
        b.xp_bonus += (t.get("outfit_s") or {}).get(key, 0)
        b.notes.append(f"golden mining suit ({pieces}/5)")
    for name, v in (t.get("pocket") or {}).items():
        if name != "None" and name in player.holdings:
            b.crit_chance += v["crit"]
            b.rck += v["rck"]
            b.notes.append(name)
            break
    for tier in (4, 3, 2, 1):
        if f"Varrock armour {tier}" in player.holdings:
            b.varrock = tier
            b.notes.append(f"Varrock armour {tier}")
            break
    perk = boost_profile.perks(player, "pickaxe")
    for name, key in (("Honed", "honed"), ("Fortune", "fortune")):
        if perk.get(name):
            b.double_ore += (t.get(key) or {}).get(f"{name} {perk[name]}", 0)
    if perk.get("Furnace"):
        b.furnace = (t.get("furnace") or {}).get(f"Furnace {perk['Furnace']}", 0)
        b.xp_bonus += b.furnace
    if perk.get("Wise"):
        b.xp_bonus += (t.get("wise") or {}).get(f"Wise {perk['Wise']}", 0)
    b.careless = perk.get("Careless", 0)
    b.explosive = bool(perk.get("Explosive"))
    if perk:
        b.notes.append(", ".join(f"{k} {v}" for k, v in perk.items()))
    if chompa := (boost_profile.boosts(player).get("skillchompa") or {}).get("Mining"):
        b.crit_bonus += (t.get("skillchompa") or {}).get(chompa, {}).get("bonus", 0)
        b.notes.append(f"{chompa} skillchompas")
    if boost_profile.juju(player, "Mining"):
        b.juju = "Perfect juju mining potion"
        b.notes.append("perfect juju mining")
    b.spirits = bool(boost_profile.boosts(player).get("stone_spirits"))
    b.clay_bracelet = bool(boost_profile.has(player, "Bracelet of clay"))
    if b.clay_bracelet:
        b.notes.append("bracelet of clay")
    if avatar := float(boost_profile.boosts(player).get("avatar") or 0):
        b.xp_bonus += avatar
    return b


def _swing_damage(rock: dict, pick: dict, level: int, strength: int, b: MiningBoost, consts: dict) -> float:
    """Average damage of one swing on a rock: the (boosted) Mining level, a tenth of Strength, the pickaxe's
    average damage, and critical hits, less what the rock's hardness takes off a pickaxe that can't penetrate it."""
    boosted = level * b.level_per + b.level_flat
    if b.cape:
        boosted = max(boosted, level + 1)  # the cape's +1 only counts when a potion gives less
    hardness_loss = min(0, pick["pen"] - rock["hardness"])
    # Critical hits: the chance grows with the real level (per rock), the bonus with the boosted one.
    chance = _tiered(level, rock.get("crit_chance_level"), consts["crit_chances"], consts["crit_chance_base"]) + b.crit_chance
    bonus = _tiered(boosted, consts["crit_bonus_level"], consts["crit_bonus"], 0) + b.crit_bonus
    from_skill = boosted + strength // 10 + hardness_loss + max(1, bonus + hardness_loss) * chance + b.damage
    if b.careless:
        from_skill *= 1 + 0.02 * b.careless
    if b.explosive and rock.get("core"):
        from_skill *= 1.2
    return max(1.0, from_skill + pick["avg"])


def _ore_per_success(rock: dict, level: int, b: MiningBoost, consts: dict, traits: dict) -> float:
    """Ores for each ore mined: the rock's own quantity, the chances of a second one, and stone spirits."""
    extra = _tiered(level, rock.get("double_ore_chance"), consts["double_ore_chances"], consts["double_ore_chance_base"])
    extra += b.double_ore
    if rock.get("core"):
        if b.cape:
            extra += 0.05
        tiers = traits.get("varrock") or [0] * 4  # which Varrock armour tiers cover this rock
        if b.varrock and tiers[b.varrock - 1] == 1:
            armour = (mechanics_data().get("mining") or {}).get("boosts", {}).get("varrock") or {}
            extra += armour.get(traits.get("varrock_no") or "", {}).get("effect", 0)
    return (rock.get("ore_qty") or 1) + extra


def mining_rate(rock: dict, pick: dict, level: int, strength: int, method: str = DEFAULT_MINING_METHOD,
                boost: MiningBoost | None = None) -> tuple[float, float]:
    """(ore per hour, xp per hour) on a rock with the player's boosts. Xp follows the damage dealt; ore comes
    each time the damage adds up to the rock's durability."""
    consts = data()["mining"]
    b = boost or MiningBoost()
    traits = ((mechanics_data().get("mining") or {}).get("rocks") or {}).get(rock["name"], {})
    style = consts["method_m"].get(method) or consts["method_m"]["Semi-AFK"]
    swings = consts["swing_hr"]
    takes_spirits = bool(traits.get("spirits"))

    # How much of the hour's swings the method lands at full strength (stamina; 1 on rocks without it).
    attention = 1.0 if rock.get("drain") else style["xp"]
    if b.juju and takes_spirits:
        attention = max(attention, 0.9)
    xp_share = attention
    if b.careless and not rock.get("drain"):
        xp_share *= 1 - 0.10 * b.careless

    damage = _swing_damage(rock, pick, level, strength, b, consts)
    xp_per_swing = math.floor(10 * damage * rock["xp"] * consts["base_xp"]) / 10
    xp = xp_per_swing * swings * xp_share * (1 + b.xp_bonus)

    # Swings for one ore. A node that cracks once and then gives a resource per swing averages the crack over
    # its resources. Damage beyond the durability is not lost: it makes up for swings the method wastes.
    needed = DURABILITY.get(rock["name"], rock["durability"]) / damage
    if (resources := CRACKED_NODES.get(rock["name"])):
        needed = (needed + resources - 1) / resources
    ore_per_swing = min(1.0, 1 / needed)
    overkill = max(1.0, 1 / needed)
    mined = ore_per_swing * swings * (1 - b.furnace) * min(attention * overkill, 1)

    each = _ore_per_success(rock, level, b, consts, traits)
    doubled = 1.0
    if takes_spirits:
        if b.spirits:
            doubled = 2.0
        elif b.juju:
            each += 0.1
    if b.clay_bracelet and rock["name"] == "Soft clay":
        doubled *= 2
    ore = mined * each * doubled

    if style.get("rck") and rock.get("core"):
        # Rockertunities: so many an hour, each worth a multiple of a swing (less the swing spent reaching it).
        worth = _tiered(level, rock.get("rockertunity_level"), consts["rockertunity_mult"], consts["rockertunity_mult_base"])
        worth += b.rck - consts["rck_loss"]
        xp += consts["rck_hr"] * worth * xp_per_swing * (1 + b.xp_bonus)
        ore += consts["rck_hr"] * min(1.0, worth * ore_per_swing) * each
    return ore, xp


# Rocks that convert what the player carries instead of yielding ore: mining the Exalted colossus turns
# essence lumps/chunks/slabs into exalted ones (the catalogue's conversion recipes), so it's neither a
# source of exalted essence nor training.
CONVERTER_ROCKS = {"Exalted colossus"}
# Exalted Quarry nodes: mining first cracks the node (its infobox hp is the crack, not per ore), then it
# gives a resource per mine: 46 in all (the first mine doubles) before depleting for 7.5 minutes. The
# deposit page states this for chunks; veins and large deposits are assumed the same. Measured (session
# 2026-10-29, essence deposit, Mining 104, primal pickaxe +5): 127 swings to crack (the sim: 126), then
# one chunk per 4-tick swing, the first doubled, 46 in all (27 + 19 over two inventories).
# The Mining calculator's "ore" for a few rocks is the end product it prices (potion flasks); the rocks
# give the sandstone (the daily cap went in March 2026).
ORE_NAMES = {"Red sandstone (Oo'glog)": "Red sandstone", "Red sandstone (Sophanem)": "Red sandstone",
             "Crystal-flecked sandstone (Ithell)": "Crystal-flecked sandstone",
             "Crystal-flecked sandstone (Meilyr)": "Crystal-flecked sandstone",
             "Alaea crablet": "Alaea sea salt"}  # the calculator names the crablet, not the salt it gives
# Rocks whose calculator durability lags the rock's infobox (20 July 2026 Mid-Game Rebalance).
DURABILITY = {"Red sandstone (Oo'glog)": 650, "Red sandstone (Sophanem)": 600,
              "Crystal-flecked sandstone (Ithell)": 750, "Crystal-flecked sandstone (Meilyr)": 700}
# Share of each item when the wiki's drop table for the rock adds up to (nearly) every ore.
MIN_SHARE_TOTAL = 0.95


@cache
def ore_shares(rock: str, ore: str | None = None) -> dict[str, float]:
    """{item: share} for rocks that give one of several items (Granite rock, gem rocks), from the drop tables.
    The rock's own ore must be one of them: otherwise the table lists by-products (primal rocks' arrow
    tips and stone spirits), not a split of the ore."""
    from .drops import tables

    srcs = tables()["sources"]
    rows = srcs.get(rock) or srcs.get(f"{rock} rock") or []
    shares: dict[str, float] = {}
    for name, chance, qty, _ in rows:
        if isinstance(chance, (int, float)) and 0 < chance < 1:
            shares[name] = shares.get(name, 0) + chance * (qty or 1)
    if ore is not None and ore not in shares:
        return {}
    return shares if len(shares) > 1 and sum(shares.values()) >= MIN_SHARE_TOTAL else {}


CRACKED_NODES = {"Essence vein": 46, "Essence deposit": 46, "Large essence deposit": 46}


def mining_rates(player: PlayerState, level: int | None = None, method: str | None = None,
                 pick: dict | None = None) -> list[Rate]:
    level = level or player.level("Mining")
    # Play style (the Mining calculator's methods): `mining_method` in the boosts table, else Semi-AFK.
    method = method or boost_profile.boosts(player).get("mining_method") or DEFAULT_MINING_METHOD
    pick, assumed = (pick, False) if pick else best_pickaxe(player)
    boost = mining_boost(player)
    rock_extras = (mechanics_data().get("mining") or {}).get("rocks") or {}
    out = []
    for rock in data()["rocks"]:
        if level < rock["level"] or not rock.get("durability") or rock["durability"] <= 1 or rock["name"] in CONVERTER_ROCKS:
            continue
        if not player.members and _members_only(rock["ore"]):
            continue
        ore_hr, xp_hr = mining_rate(rock, pick, level, player.level("Strength"), method, boost)
        shares = ore_shares(rock["name"], rock["ore"])
        ore = max(shares, key=shares.get) if shares else ORE_NAMES.get(rock["name"], rock["ore"])
        box = trips.ore_box_capacity(player, ore, (rock_extras.get(rock["name"]) or {}).get("ore_box")) \
            if rock["name"] in rock_extras else None  # ore box storable ores (the Mining calculator's rocks)
        trip = trips.gathering(player, "Mining", box)
        notes = ", ".join([method, *boost.notes])
        out.append(Rate(rock["name"], ore, level, None, trip.per_hour(ore_hr), xp_hr, xp_hr / ore_hr if ore_hr else 0,
                        f"{pick['label']} pickaxe", assumed, notes, shares, raw_items_per_hour=ore_hr, trip=trip.how))
    return out


def gather_rate(player: PlayerState, item: str) -> tuple[str, Rate] | None:
    """(skill, best rate) for gathering `item` by Woodcutting or Mining, if the player can."""
    best = None
    # The rates only change with levels and items: cache them per player state.
    cache = player.__dict__.setdefault("_gather_cache", {})
    state = (player.holdings_state, player.level("Woodcutting"), player.level("Mining"))
    if state not in cache:
        cache.clear()
        cache[state] = [("Woodcutting", woodcutting_rates(player)), ("Mining", mining_rates(player))]
    for skill, rows in cache[state]:
        for r in rows:
            per_hour = r.yields().get(item, 0)
            if per_hour and (best is None or per_hour > best[1].items_per_hour):
                # One of a rock's items: all its xp is charged to this one (the rest are by-products).
                share = per_hour / r.items_per_hour
                best = (skill, replace(r, item=item, items_per_hour=per_hour, xp_per_item=r.xp_per_item / share))
    return best


SIM_BAND = 5  # levels per simulated method row


# Trees the wiki marks fell = -1 ("different mechanics": Jadinko Lair roots that spawn and vanish,
# branching crystals, achey trees): fine as a source of their items, not as continuous training.
SPECIAL_TREES = {t["name"] for t in data()["trees"] if t.get("fell") == -1}


def method_rows(player: PlayerState, max_level: int = 120,
                skills: tuple[str, ...] = ("Woodcutting", "Mining")) -> list[dict]:
    """Simulated trees and rocks as training-method rows for the cost model (one per SIM_BAND levels,
    rated at the band's first level with the player's tools), so they compete with the guides' methods."""
    rows = []
    for skill, rates in (("Woodcutting", woodcutting_rates), ("Mining", mining_rates)):
        if skill not in skills:
            continue
        start = player.level(skill)
        for band in range(start, max_level + 1, SIM_BAND):
            for r in rates(player, band):
                if r.xp_per_hour <= 0 or r.activity in LIMITED_TREES or r.activity in SPECIAL_TREES \
                        or r.activity in DAILY_NODES:
                    continue
                rows.append({"skill": skill, "method": f"{r.activity} (simulated, {r.tool}{', assumed' if r.assumed_tool else ''})",
                             "level_min": max(band, next((t["level"] for t in data()["trees"] + data()["rocks"]
                                                          if t.get("name") == r.activity), band)),
                             "level_max": band + SIM_BAND - 1, "xp_per_hour": r.xp_per_hour,
                             "yields": r.yields(), "activity": r.activity, "basis": "simulated", "source": "simulation",
                             "assumptions": [r.trip] if "assumed" in r.trip else []})
    return rows

