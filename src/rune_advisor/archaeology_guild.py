"""The Archaeology Guild: its shop's one-time unlocks and the research team (data/archaeology_guild.json,
scripts/build_archaeology_guild.py), the two things chronotes are spent on.

Chronotes come from handing restored artefacts to collectors (completing a collection pays the artefacts' chronotes
plus a set bonus, mostly 100%: catalogue.collection_recipes; the training loop in mechanics/archaeology.py hands its
artefacts in, COLLECTION_FACTOR). They go first to the shop's unlocks (`unlocks_left`); once those are bought the
research team is the sink (the user, 2026-11-21): expeditions run offline, one at a time, cost chronotes by the hour
and return Archaeology xp (the Research page's table), materials and, rarely, damaged artefacts from the dig site.
The amounts of materials and artefacts aren't published: RESEARCH_MATERIALS_PER_HOUR and RESEARCH_ARTEFACTS_PER_HOUR
are flagged estimates.

Everything the Research page quotes is per hour of research as chosen (1/2/4/8/12/24 h): xp, chronotes, and what is
brought back. The team (`RESEARCHERS`, the wiki's Research page; ids as the game numbers them, worked out on the
beta 2026-11-27: the assigned team is ARCH_RESEARCH_CONTRACT_1_RESEARCHER_1-5_ID, exported since 0.5.0, and stays
set between runs) changes two things, each the sum of the researchers' modifiers plus the Fort Forinthry Command
Centre's (+3% speed at tier 3, -3% cost at tier 2): the chronotes an hour (x (1 + cost)), and how long an hour of
research takes, which the game shortens to (1 - speed) of it (the test account: Vanescula, Dorian, Eduardo, Movario = 22% +
3% = "Speed +25%": a 1-hour run took 45 minutes; cost 24% - 3% = "+21%"; a 24-hour Everlight run with Vanescula,
Gee'ka, Zanik, Dorian cost exactly the predicted 15,246 chronotes). Research xp is not trained xp: banked bonus xp
doesn't pay out on it (unchanged after 110,880 research xp, beta 2026-11-27). Perks: Gee'ka +10% xp, Movario and
Asgarnia Smith more materials, Elissa Giovanni more artefacts (`research_yield_factor`; how much more is assumed,
flagged). Without an exported team only the Command Centre counts.

Who can be hired (`unlocked`, matching the game's list for the test account on the beta, 2026-11-27: 11 of 21) is read from
achievements the export already carries: "Research Director" tests each recruit ("Eduardo unlocked", "Alpheus and
Eliza unlocked"...), "Finding Tali" and the six "Mysteries - <site>" achievements (a site's consultant joins once
every mystery there is solved, at their Archaeology level); the "Qualification - <rank>" achievements give the
associates (Professor) and Simon Templeton (Guildmaster), and each qualification a team slot (`team_slots`: 4 at
Professor, the fifth with Guildmaster). Jimmy and Scarlet come with the first. A researcher already on the team
stays there when the rules change (the test account's Vanescula, assigned before her level 105 was added).

Ownership of unlocks: the export counts the tiers bought of the soil box (ARCH_SOIL_BOX_UPGRADE), material storage
(ARCH_MATERIAL_STORAGE_UPGRADE), workbench storage (ARCH_RESTORATION_WORKBENCH_STORAGE_UPGRADE) and mattock precision
(ARCH_PRECISION_BASE_UPGRADE, mechanics.archaeology.precision_upgrades), and flags the relic loadout
(ARCH_RELIC_LOADOUT_GUILD_REWARD_SHOP), the auto-screener blueprint (INVENT_BLUEPRINT_AUTO_SCREENER_ACQUIRED) and each
outfit piece (OUTFIT_VARS); all but the soil box since plugin 0.5.0 (the test account read 3/1/3/0 with material
storage and precision through Professor, workbench storage at Associate, beta 2026-11-26). Outfit pieces and the
energised meteorite shard (or a mattock of Time and Space) are items held. Without the export,
`archaeology_unlocks = ["Material storage upgrade", ...]` under [players.X.boosts]
names the ones bought (with a count suffix " x2" for tiers), else they count as not bought.
"""

from __future__ import annotations

import json
import re
from functools import cache

from . import datapkg
from .mechanics import profile
from .models import PlayerState

QUALIFICATIONS = {"Intern": 5, "Assistant": 12, "Associate": 42, "Professor": 70, "Guildmaster": 98}
COLLECTION_FACTOR = 2.0  # a completed collection pays its artefacts' chronotes twice (the 100% set bonus)
# Per hour of research as chosen, without perks, measured at Everlight on Archaeology 100 (beta 2026-11-27): a
# 24-hour run with no material or artefact perk (Vanescula, Gee'ka, Zanik, Dorian) brought back 777 materials and 2
# damaged artefacts. Whether the amounts scale with level or site isn't known.
RESEARCH_MATERIALS_PER_HOUR = 777 / 24
RESEARCH_ARTEFACTS_PER_HOUR = 2 / 24
# The team: game id -> (name, speed %, cost %, perk). The wiki's Research page; ids from the game (beta 2026-11-27:
# 4, 5, 8, 9, 14, 17, 18 seen; the rest follow the page's order, which those confirm).
RESEARCHERS = {
    1: ("Jimmy", 0, 0, None), 2: ("Scarlet", 1, 1, None), 3: ("Katarina", 1, 0, None), 4: ("Dorian", 1, 0, None),
    5: ("Eduardo", 1, 0, None), 6: ("Alpheus", 2, 3, None), 7: ("Eliza", 2, 3, None), 8: ("Tali", 2, 3, None),
    9: ("Drew", 4, 6, None), 10: ("Helen", 4, 6, None), 11: ("Easty", 4, 6, None),
    12: ("Simon Templeton", 6, 9, None), 13: ("Anna Jones", 6, 9, None), 14: ("Asgarnia Smith", 6, 9, "materials"),
    15: ("Elissa Giovanni", 6, 9, "artefacts"), 16: ("Dr Nabanik", 8, 12, "Third Age Expert"),
    17: ("Vanescula Drakan", 12, 12, None), 18: ("Movario", 8, 12, "Extra Resourceful"),
    19: ("Gee'ka", 6, 12, "Meticulous"), 20: ("Zanik", 8, 0, None), 21: ("Mr Mordaut", 8, 12, "Treasure Hoarder"),
}
TEAM_VARS = [f"ARCH_RESEARCH_CONTRACT_1_RESEARCHER_{i}_ID" for i in range(1, 6)]
# Perks' size. Meticulous +10% xp: measured (the 24-hour run above paid 110,880 = 4,200 x 24 x 1.1). Extra
# Resourceful: one 1-hour run with Movario brought 40 materials against 32.4 a plain hour (~x1.25, a small sample).
# Asgarnia Smith's "increases" (Extra Resourceful "greatly increases") and Elissa's artefacts: assumed x1.1.
PERK_FACTORS = {"materials": ("materials", 1.1), "Extra Resourceful": ("materials", 1.25),
                "artefacts": ("artefacts", 1.1), "Meticulous": ("xp", 1.1)}
RESEARCH_MIN_LEVEL = 40  # the Research page's experience table starts here


@cache
def guild() -> dict:
    path = datapkg.path("archaeology_guild.json")
    return json.loads(path.read_text()) if path.is_file() else {"unlocks": [], "research_xp_per_hour": {},
                                                                 "research_sites": {}}


# Unlock series the export counts (tiers bought, in shop order) or flags (0/1), since plugin 0.5.0.
EXPORTED_TIERS = {"Material storage upgrade": "ARCH_MATERIAL_STORAGE_UPGRADE",
                  "Workbench storage upgrade": "ARCH_RESTORATION_WORKBENCH_STORAGE_UPGRADE",
                  "Additional relic loadout": "ARCH_RELIC_LOADOUT_GUILD_REWARD_SHOP",
                  # set by buying it from the Guild (0 -> 1 with "You've acquired a new blueprint...", beta
                  # 2026-11-26); studying it at an inventor's workbench is a separate step
                  "Auto-screener v1.080 Blueprint": "INVENT_BLUEPRINT_AUTO_SCREENER_ACQUIRED"}
# Outfit pieces bought from the shop: one bit each in ARCH_REWARD_1 (the test account: the five archaeologist's pieces set, no
# master ones, beta 2026-11-26), exported since 0.5.0; older exports fall back to the items held.
OUTFIT_VARS = {f"{prefix} {piece}": f"ARCH_{tier}SKILLING_{slot}_UNLOCKED"
               for prefix, tier in (("Archaeologist's", ""), ("Master archaeologist's", "ELITE_"))
               for piece, slot in (("hat", "HAT"), ("jacket", "BODY"), ("trousers", "LEGS"), ("gloves", "GLOVES"),
                                   ("boots", "BOOTS"))}
# The shop data names the first workbench tier "Workbench upgrade" and the later ones "Workbench storage upgrade".
SERIES = {"Workbench upgrade": "Workbench storage upgrade"}


def _named(player: PlayerState) -> dict[str, int]:
    out: dict[str, int] = {}
    for entry in profile.boosts(player).get("archaeology_unlocks") or []:
        m = re.match(r"(.+?)(?:\s+x(\d+))?$", str(entry).strip())
        out[m.group(1)] = out.get(m.group(1), 0) + int(m.group(2) or 1)
    return out


def unlocks_left(player: PlayerState) -> list[tuple[dict, bool]]:
    """The shop's unlocks the player hasn't bought, in shop order, with whether that's known (True) or only
    unknown from the export (False). Unlocks above the player's qualification (by level) are included: buying them
    waits for the level."""
    from .mechanics.archaeology import precision_upgrades

    level = player.level("Archaeology")
    held = player.holdings
    named = _named(player)
    vars_ = getattr(player, "vars", None) or {}
    tiers = {"Soil box upgrade": int(vars_.get("ARCH_SOIL_BOX_UPGRADE") or 0),
             "Mattock precision upgrade": precision_upgrades(player, level)[0] // 2}
    for name, var in EXPORTED_TIERS.items():
        if isinstance(vars_.get(var), int):
            tiers[name] = vars_[var]
    seen: dict[str, int] = {}
    out = []
    for u in guild()["unlocks"]:
        name = SERIES.get(u["name"], u["name"])
        nth = seen[name] = seen.get(name, 0) + 1
        if name in tiers:
            if nth <= tiers[name]:
                continue
            out.append((u, True))
        elif name.startswith(("Archaeologist's", "Master archaeologist's")):
            bought = vars_.get(OUTFIT_VARS.get(name, ""))
            if isinstance(bought, int):  # the export's purchase bit (0.5.0): pieces kept anywhere count
                if not bought:
                    out.append((u, True))
            elif name not in held:
                out.append((u, True))
        elif name == "Energised meteorite shard":
            if not ({"Energised meteorite shard", "Mattock of Time and Space"} & set(held)):
                out.append((u, True))
        elif nth > named.get(name, named.get("Workbench upgrade", 0) if name == "Workbench storage upgrade" else 0):
            out.append((u, False))
    return out


def research_sites(level: int) -> dict[str, dict]:
    return {k: v for k, v in guild()["research_sites"].items() if v["level"] <= level and level >= RESEARCH_MIN_LEVEL}


QUALIFICATION_RANKS = ["Intern", "Assistant", "Associate", "Professor", "Guildmaster"]
DIRECTOR = "Research Director"
RECRUITED = {3: "Katarina unlocked", 4: "Dorian unlocked", 5: "Eduardo unlocked", 6: "Alpheus and Eliza unlocked",
             7: "Alpheus and Eliza unlocked", 13: "Anna Jones unlocked", 14: "Asgarnia Smith unlocked",
             15: "Elissa Giovanni unlocked"}  # Research Director's test descriptions
QUALIFIED = {1: 1, 2: 1, 9: 4, 10: 4, 11: 4, 12: 5}  # id -> qualifications needed (1 = Intern ... 5 = Guildmaster)
TALI_ACHIEVEMENT = "Finding Tali"
CONSULTANTS = {16: ("Kharid-et", 107), 17: ("Everlight", 105), 18: ("Infernal Source", 68),
               19: ("Stormguard Citadel", 95), 20: ("Warforge", 110), 21: ("Orthen", 120)}


def _done(a) -> list[tuple[str, bool]]:
    """An achievement's tests as (name, complete): a test's description, or the sub-achievement it names."""
    return [(r.get("description") or r.get("achievement") or "?", bool(r.get("complete"))) for r in a.requirements]


def qualification(player: PlayerState) -> int | None:
    """Qualifications held (1 = Intern ... 5 = Guildmaster), None when the export doesn't carry them."""
    found = [player.achievements.get(f"Qualification - {r}") for r in QUALIFICATION_RANKS]
    if all(a is None for a in found):
        return None
    return sum(1 for a in found if a is not None and a.completed)


def team_slots(player: PlayerState) -> int | None:
    """Research team slots: one per qualification (the user, 2026-11-27: 4 at Professor, 5 at Guildmaster)."""
    return qualification(player)


def mysteries_left(player: PlayerState, place: str) -> list[str] | None:
    """A dig site's mysteries not solved yet ("Mysteries - <site>"'s tests), None when the export doesn't say."""
    a = player.achievements.get(f"Mysteries - {place}")
    if a is None:
        return None
    if a.completed:
        return []
    return [name for name, ok in _done(a) if not ok]


def unlocked(player: PlayerState) -> set[int] | None:
    """The researchers the player can hire, or None when the export doesn't say."""
    qual = qualification(player)
    director = player.achievements.get(DIRECTOR)
    if qual is None or director is None:
        return None
    recruited = {name for name, ok in _done(director) if ok} if not director.completed else set(RECRUITED.values())
    out = {i for i, q in QUALIFIED.items() if qual >= q}
    out |= {i for i, test in RECRUITED.items() if test in recruited}
    tali = player.achievements.get(TALI_ACHIEVEMENT)
    if tali is not None and tali.completed:
        out.add(8)
    level = player.level("Archaeology")
    for i, (place, need) in CONSULTANTS.items():
        if mysteries_left(player, place) == [] and level >= need:
            out.add(i)
    return out


def team(player: PlayerState | None) -> list[int]:
    """The assigned researchers' ids (empty slots and an export without them: none)."""
    values = (getattr(player, "vars", None) or {}) if player is not None else {}
    return [v for n in TEAM_VARS if isinstance(v := values.get(n), int) and v in RESEARCHERS]


def team_modifiers(player: PlayerState | None) -> tuple[float, float]:
    """(speed, cost) as fractions: the team's modifiers summed, with the Command Centre's (the game adds them)."""
    from . import fort

    ids = team(player)
    speed = sum(RESEARCHERS[i][1] for i in ids) / 100
    cost = sum(RESEARCHERS[i][2] for i in ids) / 100
    if player is not None:
        speed += fort.research_speed_factor(player) - 1
        cost -= 1 - fort.research_cost_factor(player)
    return speed, cost


def research_yield_factor(player: PlayerState | None, what: str) -> float:
    """The team's perks on "xp", "materials" or "artefacts" an hour of research (the same perk twice counts once)."""
    out = 1.0
    for perk in {RESEARCHERS[i][3] for i in team(player)}:
        kind, f = PERK_FACTORS.get(perk, (None, 1.0))
        if kind == what:
            out *= f
    return out


def research_xp(level: int, factor: int, player: PlayerState | None = None) -> float:
    """Archaeology xp an hour of general research as chosen (the Research page's table: Kharid-et, x factor%
    elsewhere), with Gee'ka's +10%."""
    table = guild()["research_xp_per_hour"]
    lvl = min(int(k) for k in table) if level < min(int(k) for k in table) else min(level, max(int(k) for k in table))
    return float(table.get(str(lvl), 0.0)) * factor / 100 * research_yield_factor(player, "xp")


def research_cost(level: int, factor: int, player: PlayerState | None = None) -> float:
    """Chronotes an hour of general research as chosen: 5 x the level rounded down to even, x factor% (Research
    page), x (1 + the team's cost modifier, less the Command Centre's 3%)."""
    return 5 * (level - level % 2) * factor / 100 * (1 + team_modifiers(player)[1])


def research_speed(player: PlayerState | None) -> float:
    """Hours of research the team gets through in an hour of real time: an hour takes (1 - speed) of one."""
    return 1 / max(0.05, 1 - team_modifiers(player)[0])


def team_note(player: PlayerState | None) -> str | None:
    ids = team(player)
    if not ids:
        return None
    speed, cost = team_modifiers(player)
    names = ", ".join(RESEARCHERS[i][0] for i in ids)
    text = f"research team {names}: speed +{speed:.0%} (an hour takes {1 - speed:.0%}), cost {cost:+.0%}"
    hired = unlocked(player)
    if hired is not None:
        kept = [RESEARCHERS[i][0] for i in ids if i not in hired]
        text += f"; {len(hired)} of {len(RESEARCHERS)} researchers unlocked"
        if kept:
            text += f" ({', '.join(kept)} no longer unlocked: replacing them loses them)"
    return text

    level = player.level("Archaeology")
    for i, (place, need) in CONSULTANTS.items():
        if i in hired:
            continue
        left = mysteries_left(player, place) or []
        why = ([f"{len(left)} {place} mysteries ({', '.join(left)})"] if left else []) + \
              ([f"Archaeology {need}"] if level < need else [])
        perk = RESEARCHERS[i][3]
        out.append(f"{RESEARCHERS[i][0]} (+{RESEARCHERS[i][1]}% speed, {RESEARCHERS[i][2]:+}% cost"
                   f"{', ' + perk if perk else ''}): {' and '.join(why)}")
    return out


def best_research(level: int) -> tuple[str, dict] | None:
    """The dig site whose research pays the most xp an hour at this level (they all cost in proportion)."""
    sites = research_sites(level)
    return max(sites.items(), key=lambda kv: kv[1]["factor"], default=None)
