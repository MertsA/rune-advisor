"""What one extra completion of a Distraction & Diversion gives this player: the rewards a D&D reset token buys
(data/dnd_resets.json carries the token rules and the lists; the per-D&D figures below come from the wiki's D&D
pages, read 2026-11-11, and are cited inline). A `Reward` is xp in named skills, "choice" xp the player puts in a
skill of their own pick (Troll Invasion's book, penguin points, effigies), items, and the minutes it takes; the
tracks value it (tracks/dnd_tokens.py). Nothing here is read from game vars: the TOKEN_DD_* vars all read 0 on an
account holding tokens, so the bank count is the stock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from . import datapkg
from typing import Callable

from .models import PlayerState
from .xp import lamp_xp

DATA = "dnd_resets.json"
CADENCES = ("daily", "weekly", "monthly")


@cache
def rules() -> dict:
    path = datapkg.path(DATA)
    return json.loads(path.read_text()) if path.is_file() else {}


@dataclass
class Choice:
    """Xp the player puts in a skill of their choice: `units` x `xp(level)` in any skill of `skills` (None = any)
    at `min_level` or above."""
    units: float
    xp: Callable[[int], float]
    skills: set[str] | None = None
    min_level: int = 1
    what: str = ""


@dataclass
class Reward:
    dnd: str
    cadence: str
    minutes: float  # one reset's worth of play, an assumption unless the wiki gives a duration
    xp: dict[str, float] = field(default_factory=dict)  # fixed-skill xp per reset
    choice: list[Choice] = field(default_factory=list)
    items: dict[str, float] = field(default_factory=dict)  # expected items per reset
    charm_buff_minutes: float = 0.0  # Familiarisation: triple charm drops for this long
    notes: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # unmet requirements (quests, levels, other players)
    players: int = 1  # other players needed (Sinkholes: 2-5)


def quest_done(p: PlayerState, title: str) -> bool:
    """True unless the player's quest list has the quest and it isn't done (unknown quests are taken as done)."""
    q = p.quests.get(title)
    return q is None or q.done


def _interp(table: list[tuple[int, float]], level: int) -> float:
    """Piecewise-linear lookup in a sorted (level, value) table, clamped at the ends."""
    if level <= table[0][0]:
        return table[0][1]
    for (l0, v0), (l1, v1) in zip(table, table[1:]):
        if level <= l1:
            return v0 + (v1 - v0) * (level - l0) / (l1 - l0)
    return table[-1][1]


def _step(table: list[tuple[int, float]], level: int) -> float:
    """The value of the highest table level at or below `level` (the wiki's tiered tables)."""
    value = table[0][1]
    for lvl, v in table:
        if lvl <= level:
            value = v
    return value


# --- daily ---------------------------------------------------------------------------------------------------------

# Big Chinchompa: (Hunter level, xp per competence point) for the best moth the level allows; a game pays at most
# 1,500 points (wiki: "up to around 33,000 experience each game" at 78+), and the Hunter xp lamp in Nyriki's shop
# gives 5 xp a point.
CHINCHOMPA_XP_PER_POINT = [(1, 15.0), (13, 25.0), (26, 30.0), (39, 31.25), (52, 25.0), (65, 320 / 15), (78, 400 / 18), (91, 450 / 21)]
CHINCHOMPA_POINTS = 1500
CHINCHOMPA_LAMP_XP_PER_POINT = 5.0
CHINCHOMPA_GAMES = 2  # two games a day; a reset is taken to give both again (assumption)

# The Pit: success xp at Het's Oasis, the best course, averaged over the six games (wiki table), times the
# gamble's expected 7/6. A 99 Agility player wearing the cape gets a few hundred xp instead.
PIT_XP = sum((5940, 6534, 7365, 8316, 9504, 10692)) / 6 * 7 / 6
PIT_CAPE_XP = 270.0

# Sinkholes: 3-objective lamp xp by Dungeoneering level (wiki Sinkholes/Lamp experience): (level, (small, medium,
# large, huge)). Rows above 99 are the wiki's own, with its gaps filled from the neighbouring rows.
SINKHOLE_LAMPS: list[tuple[int, tuple[float, float, float, float]]] = [
    (60, (11941.9, 13647.8, 17059.8, 21324.8)), (70, (16396.2, 18738.5, 23423.1, 29278.9)),
    (80, (21888.3, 25015.2, 31269.0, 39086.3)), (90, (28581.4, 32664.4, 40830.5, 51038.1)),
    (95, (33250.2, 38000.2, 47500.3, 59375.4)), (99, (36661.9, 41899.4, 52374.2, 65467.8)),
    (100, (52033.0, 59466.0, 70853.0, 92916.0)), (103, (54558.0, 62352.0, 85734.0, 97425.0)),
    (105, (62892.0, 65343.0, 89846.0, 102098.0)), (107, (59885.0, 68440.0, 85551.0, 106938.0)),
    (109, (62892.0, 71649.0, 98519.0, 123148.0)), (113, (68610.0, 78412.0, 98015.0, 112717.0)),
    (115, (71724.0, 81971.0, 98015.0, 128080.0)), (117, (74919.0, 85655.0, 107069.0, 133856.0)),
    (119, (74919.0, 88412.0, 117000.0, 153769.0)), (120, (74919.0, 89465.0, 117000.0, 153769.0)),
]
SINKHOLE_PLAYERS = 5  # the huge lamp goes to the top player, the other four boxes (large/medium/small/tokens) to the rest
SINKHOLE_GAMES = 2
SINKHOLE_TOKEN_SHARE = 0.05  # Dungeoneering tokens worth 5% of the lamp's xp (not xp: ignored)


def sinkhole_xp(level: int) -> float:
    """Expected Dungeoneering xp from one Sinkholes round at an average placing (five players, all three objectives)."""
    row = SINKHOLE_LAMPS[0][1]
    for lvl, r in SINKHOLE_LAMPS:
        if lvl <= level:
            row = r
    return sum(row) / SINKHOLE_PLAYERS


# --- weekly --------------------------------------------------------------------------------------------------------

TEARS_PER_SECOND = 1.0
TEARS_SECONDS_PER_QP = 0.6
TEARS_MAX_SECONDS = 286
TEARS_CAPE_SECONDS = 30  # the quest point cape's perk
TEARS_XP_PER_LEVEL = 6
TEARS_LEVEL_CAP = 30
TEARS_BOWL_LEVEL_CAP = 50  # with an ornate bowl (Crafting and Mining 80)
QUEST_CAPES = ("Quest point cape", "Quest point cape (t)", "Hooded Quest Cape", "Hooded quest point cape")


def tears_skill(p: PlayerState) -> str | None:
    """The skill Tears of Guthix pays: the lowest level, ties to the lowest xp (the wiki's rule; its 120-cap
    preference and tutorial-skill skips aren't modelled)."""
    if not p.skills:
        return None
    return min(p.skills, key=lambda s: (p.skills[s].level, p.skills[s].xp))


# Penguin Hide and Seek: this week's set (wiki, week of 2026-11-11): penguins 1-5 one point, 6-10 two, the Shadow
# Realm penguin three, the toadstool two, the polar bear one; a point buys a small lamp's xp (resources.py).
PENGUIN_POINTS_PER_WEEK = 21
PENGUIN_MINUTES = 22  # hunting the twelve plus the bear (the user: 20-25 min, 2026-11-11)

FAMILIARISATION_BUFF_MINUTES = 40  # triple charms for 40 s a shard, 60 shards

# Herby Werby: Herblore xp per point from taking ancient herbs (wiki table, every row the page fills in), 100 points
# a week; 100 potent herbs buy a small Herblore lamp.
HERBY_XP_PER_POINT = [
    (1, 14), (10, 24), (20, 27), (30, 36.8), (40, 36), (50, 58.4), (60, 104), (70, 146), (80, 218.5), (84, 236),
    (85, 480.8), (90, 524), (94, 550.4), (95, 835.2), (99, 889.2), (105, 1185.6), (115, 1482),
]
HERBY_POINTS = 100

# Skeletal Horror (wiki tables): Slayer 9,125 at 72 rising 125 a level; Prayer 3,100 at 30 rising 100 a level to 99.
SKELETAL_SLAYER = lambda lvl: 9125 + 125 * (max(lvl, 72) - 72)  # noqa: E731
SKELETAL_PRAYER = lambda lvl: 3100 + 100 * (min(max(lvl, 30), 99) - 30)  # noqa: E731

# The Circus (wiki tables): xp paid once per level row at or below the player's level; x1.5 after Back to the Freezer.
CIRCUS_ROWS: dict[str, list[tuple[int, float]]] = {
    "Magic": [(10, 970), (20, 2055), (30, 2059), (40, 2133), (50, 2206), (60, 2647), (70, 2794), (80, 3088), (90, 3525), (99, 3529)],
    "Agility": [(10, 350), (20, 578), (30, 805.5), (40, 1033.5), (50, 1261), (60, 1489), (70, 1716.5), (80, 1944.5), (90, 2172), (99, 2400)],
    "Ranged": [(10, 687.5), (20, 1455), (30, 1458.5), (40, 1510.5), (50, 1562.5), (60, 1875), (70, 1979), (80, 2187.5), (90, 2495), (99, 2500)],
    "Thieving": [(50, 2206), (60, 2647), (70, 2794), (80, 3088), (90, 3525)],
    # Both fire acts pay the same table.
    "Firemaking": [(74, 2 * 2580), (78, 2 * 2830), (82, 2 * 3080), (87, 2 * 3330), (91, 2 * 3580), (95, 2 * 3830)],
}
CIRCUS_QUESTS = {"Thieving": "Some Like It Cold", "Firemaking": "The Firemaker's Curse"}
CIRCUS_FREEZER_MULT = 1.5

# Memory of Nomad: expected drops per rewarded kill (wiki: 10-20 of one charm colour at 1/4 each, 350-700 incandescent
# energy, 8 rocktail, 6 Super restore flask (6), 4 Saradomin brew flask (6)).
NOMAD_ITEMS = {"Gold charm": 15 / 4, "Green charm": 15 / 4, "Crimson charm": 15 / 4, "Blue charm": 15 / 4,
               "Incandescent energy": 525.0, "Rocktail": 8.0, "Super restore flask (6)": 6.0, "Saradomin brew flask (6)": 4.0}

# --- monthly -------------------------------------------------------------------------------------------------------


def troll_invasion_xp(level: int) -> float:
    """The reward book at full completion: 8 x (L^2 - 2L + 100) in the chosen skill (wiki formula)."""
    return 8 * (level * level - 2 * level + 100)


GOD_STATUES = 5  # one statue a location; Construction xp = a large lamp each, Prayer (to 99) or Slayer = a medium lamp

# Giant Oyster: xp per harvest by level (wiki table), 30 harvests of each a month.
OYSTER_FISHING = [(40, 54), (60, 264), (80, 1435), (90, 3435), (98, 6966)]
OYSTER_FARMING = [(1, 3), (40, 18), (60, 88), (80, 478), (90, 1145), (98, 2322)]
OYSTER_HARVESTS = 30

# Effigy Incubator: a five-minute session makes ~10 effigies (one casing each); every effigy pays 2,500 xp in the skill
# that crafts it (Crafting/Smithing/Runecrafting/Invention at 85), and once charged with 60,000 xp of its skills it
# cracks into an effigy lamp or star (a medium lamp's xp, restricted skills: resources.py already counts held ones).
EFFIGIES_PER_SESSION = 10
EFFIGY_CRAFT_XP = 2500.0
EFFIGY_SKILLS = {"Crafting", "Smithing", "Runecraft", "Invention"}
EFFIGY_LEVEL = 85
EMPTY_EFFIGIES = ("Volatile effigy (empty)", "Explosive effigy (empty)", "Erratic effigy (empty)", "Unstable effigy (empty)")


def rewards(p: PlayerState) -> list[Reward]:
    """One reset's rewards of every daily, weekly and monthly D&D the data lists, for this player."""
    out: list[Reward] = []
    lvl = p.level

    # daily
    hunter = lvl("Hunter")
    per_point = _step(CHINCHOMPA_XP_PER_POINT, hunter)
    out.append(Reward("Big Chinchompa", "daily", minutes=20 * CHINCHOMPA_GAMES,
                      xp={"Hunter": CHINCHOMPA_GAMES * CHINCHOMPA_POINTS * (per_point + CHINCHOMPA_LAMP_XP_PER_POINT)},
                      notes=["moth xp plus the 5 xp a point of Nyriki's Hunter lamp"],
                      assumptions=[f"a full {CHINCHOMPA_POINTS:,}-point game catching the best moth your level allows",
                                   f"a reset gives both daily games ({CHINCHOMPA_GAMES})"]))
    cape = any(c in p.holdings for c in ("Agility cape", "Agility cape (t)", "Max cape", "Completionist cape"))
    pit_xp = PIT_CAPE_XP if cape and lvl("Agility") >= 99 else PIT_XP
    out.append(Reward("The Pit", "daily", minutes=4, xp={"Agility": pit_xp},
                      notes=["a daily token grants the event at the next talent scout (Het's Oasis pays most)"],
                      assumptions=["the best course's average game, gamble taken (expected 7/6)"]
                      + (["Agility cape: free attempts pay a few hundred xp"] if pit_xp == PIT_CAPE_XP else [])))
    out.append(Reward("Sinkholes", "daily", minutes=15 * SINKHOLE_GAMES, xp={"Dungeoneering": SINKHOLE_GAMES * sinkhole_xp(lvl("Dungeoneering"))},
                      players=2, notes=["rounds start at half past the hour and need 2-5 players; Group Ironman accounts can't take part"],
                      assumptions=["an average placing among five players, all three objectives met", f"a reset gives both daily rounds ({SINKHOLE_GAMES})"]))

    # weekly
    skill = tears_skill(p)
    if skill:
        seconds = min(TEARS_MAX_SECONDS, p.quest_points * TEARS_SECONDS_PER_QP) + (TEARS_CAPE_SECONDS if any(c in p.holdings for c in QUEST_CAPES) else 0)
        bowl = "Ornate bowl" in p.holdings
        tear = TEARS_XP_PER_LEVEL * min(lvl(skill), TEARS_BOWL_LEVEL_CAP if bowl else TEARS_LEVEL_CAP)
        out.append(Reward("Tears of Guthix", "weekly", minutes=6, xp={skill: seconds * TEARS_PER_SECOND * tear},
                          notes=[f"pays your lowest skill ({skill} {lvl(skill)}): {tear} xp a tear for ~{seconds:.0f} s"
                                 + ("" if bowl else "; an ornate bowl (Crafting and Mining 80) would raise the tear cap to level 50")],
                          assumptions=["one tear a second"]))
    out.append(Reward("Penguin Hide and Seek", "weekly", minutes=PENGUIN_MINUTES,
                      choice=[Choice(PENGUIN_POINTS_PER_WEEK, lambda l: lamp_xp(l, "small"), what="penguin points (a small lamp each)")],
                      notes=[f"another {PENGUIN_POINTS_PER_WEEK} points: this week's twelve penguins and the polar bear"],
                      assumptions=[f"~{PENGUIN_MINUTES} min to find them all"]))
    out.append(Reward("Familiarisation", "weekly", minutes=20, charm_buff_minutes=FAMILIARISATION_BUFF_MINUTES,
                      notes=["the charm drop enhancer triples charm drops for 40 s a shard, up to 40 min"],
                      assumptions=["all 60 shards collected; the buff is worth 80 min of charm farming only if your plans farm charms"]))
    herb = lvl("Herblore")
    out.append(Reward("Herby Werby", "weekly", minutes=20,
                      xp={"Herblore": HERBY_POINTS * _step(HERBY_XP_PER_POINT, herb) + lamp_xp(herb, "small")},
                      notes=[f"{HERBY_POINTS} points at {_step(HERBY_XP_PER_POINT, herb):,.1f} Herblore xp each, plus the 100-point small Herblore lamp;"
                             " Firemaking from burning corrupt herbs not counted"],
                      assumptions=["~20 min for the week's 100 points"]))
    out.append(Reward("Skeletal Horror", "weekly", minutes=6,
                      xp={"Slayer": SKELETAL_SLAYER(lvl("Slayer")), "Prayer": SKELETAL_PRAYER(lvl("Prayer"))},
                      notes=["also an elite (1/12 master) clue scroll", "needs both wish lists (Rag and Bone Man II, Fur 'n Seek)"]))
    circus_xp: dict[str, float] = {}
    blocked = []
    mult = CIRCUS_FREEZER_MULT if quest_done(p, "Back to the Freezer") else 1.0
    for s, rows in CIRCUS_ROWS.items():
        if (q := CIRCUS_QUESTS.get(s)) and not quest_done(p, q):
            blocked.append(f"{s} act: {q}")
            continue
        total = sum(v for l, v in rows if l <= lvl(s)) * mult
        if total:
            circus_xp[s] = total
    out.append(Reward("Balthazar Beauregard's Big Top Bonanza", "weekly", minutes=15, xp=circus_xp,
                      notes=["the circus: every act's xp is paid once per level row at or below yours"
                             + (" (x1.5 after Back to the Freezer)" if mult > 1 else "")] + ([f"not yet: {', '.join(blocked)}"] if blocked else []),
                      assumptions=["a clean performance"]))
    out.append(Reward("Wisps of the Grove", "weekly", minutes=10,
                      xp={"Hunter": lamp_xp(lvl("Hunter"), "medium"), "Farming": lamp_xp(lvl("Farming"), "medium")},
                      notes=["a medium lamp's xp in Hunter and in Farming"]))
    out.append(Reward("Memory of Nomad", "weekly", minutes=20, items=dict(NOMAD_ITEMS),
                      notes=["a rewarded kill's drops: charms, incandescent energy, flasks, rocktail, onyx bolts, rune salvage; a hard fight"],
                      blocked=[] if quest_done(p, "Nomad's Elegy") else ["Nomad's Elegy"]))

    # monthly
    out.append(Reward("Troll Invasion", "monthly", minutes=20,
                      choice=[Choice(1, troll_invasion_xp, what="the reward book (any skill)")],
                      notes=["8 x (L^2 - 2L + 100) xp in any one skill at full completion"], assumptions=["all 20 easy waves cleared"]))
    out.append(Reward("God Statues", "monthly", minutes=20,
                      xp={"Construction": GOD_STATUES * lamp_xp(lvl("Construction"), "large")},
                      choice=[Choice(GOD_STATUES, lambda l: lamp_xp(min(l, 99), "medium"), skills={"Prayer", "Slayer"},
                                     what="a medium lamp per statue in Prayer (to 99) or Slayer")],
                      notes=[f"{GOD_STATUES} statues: a large lamp of Construction each, plus a medium lamp of Prayer or Slayer each"],
                      blocked=[] if lvl("Construction") >= 75 else ["Prifddinas statue: Construction 75"]))
    out.append(Reward("Giant Oyster", "monthly", minutes=15,
                      xp={"Fishing": OYSTER_HARVESTS * _interp(OYSTER_FISHING, lvl("Fishing")),
                          "Farming": OYSTER_HARVESTS * _interp(OYSTER_FARMING, lvl("Farming"))},
                      notes=["30 sea cucumber and 30 seaweed harvests; the oyster's Treasure Trails loot opens at the next monthly reset (not valued)"],
                      blocked=[] if quest_done(p, "Beneath Cursed Tides") else ["Beneath Cursed Tides"]))
    empties = sum(p.holdings.get(e, 0) for e in EMPTY_EFFIGIES)
    skills = {s for s in EFFIGY_SKILLS if lvl(s) >= EFFIGY_LEVEL}
    out.append(Reward("Effigy Incubator", "monthly", minutes=10,
                      choice=[Choice(EFFIGIES_PER_SESSION, lambda l: EFFIGY_CRAFT_XP, skills=skills or None, min_level=EFFIGY_LEVEL,
                                     what="crafting the effigies")] if skills else [],
                      notes=["~10 effigies a session: 2,500 xp each in the crafting skill; each then needs 60,000 xp of its skills to crack into "
                             "a medium effigy lamp or star (not valued: that xp is training you'd do anyway)"]
                            + ([f"you already hold {empties} empty effigies waiting to be charged"] if empties else []),
                      blocked=([] if quest_done(p, "Sins of the Father") else ["Sins of the Father"]) + ([] if skills else ["85 in Crafting, Smithing, Runecrafting or Invention"]),
                      assumptions=[f"{EFFIGIES_PER_SESSION} effigies a session"]))
    return out
