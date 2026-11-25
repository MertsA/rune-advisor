"""Daemonheim's daily boss pick: which boss to choose for a floor, worked back from the open achievements a boss
advances (data/daemonheim_bosses.json, hand-written from the wiki).

The Easy Daemonheim task set's reward lets a player choose the boss of a floor once a day, among the bosses that can
appear on that floor. A boss advances an open achievement in three ways, all read from the export's achievement tests:
- beating it for the first time: The Home of Daemons (one tested bit per boss);
- its own journal: Behemoth, Stalker, Kal'gerion and Miscellaneous Notes. A boss journal drops only from the boss it
  describes, by the exit ladder once the boss is down; the drop chance is unknown (the wiki has "?");
- a feat only that boss allows: Without Blinking An Eye (Blink), Hoof Rot (Bal'lak), Nice to Meet You, Wall
  (Rammernaut), Gulega-d to Rest (Warped Gulega).

Worth: a pick is worth the random floors it replaces. Without it, a boss turns up on a floor with chance 1/n, n the
bosses that can appear there (assumed equally likely; the wiki gives no weights), so the player's best floor for that
boss is the one with the fewest rivals. One pick does for each thing the boss advances what n random floors would:
kills are certain on either side, and a journal's unknown drop chance is the same on a picked or a random floor, so it
cancels. Worth = (things advanced) x n at the boss's best reachable floor, in floors. Bilrach's chronicles (page n on
floors 2n - 1 and 2n, 1/4 a floor) and the Marmaros and Thok letters (1/1,000 a monster) depend on the floor, not the
boss: the player chooses the floor anyway, so they only pick which of the boss's floors to run.

Floor n needs Dungeoneering 2n - 1 (mechanics/dungeoneering.highest_floor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from . import datapkg
from .mechanics.dungeoneering import highest_floor
from .models import PlayerState


@lru_cache(maxsize=1)
def rules() -> dict:
    return datapkg.load_json("daemonheim_bosses.json")


def _missing(player: PlayerState, achievement: str) -> set[str] | None:
    """Requirement descriptions of `achievement` not yet met; empty when it is done, None when the export lacks it."""
    a = player.achievements.get(achievement)
    if a is None:
        return None
    if a.completed:
        return set()
    return {str(r.get("description") or "") for r in a.requirements if not r.get("complete")}


def perk_unlocked(player: PlayerState) -> bool | None:
    """Whether the player can choose a floor's boss (None: the export doesn't say)."""
    a = player.achievements.get(rules()["perk"]["task_set"])
    return None if a is None else a.completed


def _in(floor: int, ranges: list[list[int]]) -> bool:
    return any(lo <= floor <= hi for lo, hi in ranges)


def rivals(floor: int) -> int:
    """How many bosses can appear on `floor`."""
    return sum(_in(floor, b["floors"]) for b in rules()["bosses"])


@dataclass
class Gain:
    achievement: str
    item: str  # the requirement it meets (a journal, the boss's kill bit) or the feat to attempt
    kind: str  # "kill", "journal", "feat"

    def describe(self) -> str:
        if self.kind == "kill":
            return f"first kill ({self.achievement})"
        if self.kind == "journal":
            return f"{self.item} ({self.achievement}; drop chance unknown)"
        return f"a try at {self.achievement}: {self.item}"


@dataclass
class Pick:
    boss: str
    floor: int  # the floor to run it on
    rivals: int  # bosses that can appear on the boss's best floor without the pick
    gains: list[Gain]
    notes: list[str] = field(default_factory=list)  # side effects that aren't progress by themselves (Kal'gerion titles)
    floor_lore: list[str] = field(default_factory=list)  # floor-only drops the chosen floor also rolls
    min_level: int = 1  # Dungeoneering level for the boss's lowest floor

    @property
    def worth(self) -> float:
        """Random floors this pick replaces."""
        return float(len(self.gains) * self.rivals)


def _floor_lore(player: PlayerState, floor: int) -> list[str]:
    lore = rules()["floor_lore"]
    out = []
    ch = lore["chronicles"]
    page = f"Mysterious chronicle (part {(floor + 1) // 2})"
    if page in (_missing(player, ch["achievement"]) or set()):
        out.append(f"{page} ({ch['achievement']}; {ch['chance']:.0%} a floor)")
    le = lore["letters"]
    for letter in sorted(_missing(player, le["achievement"]) or set()):
        lo, hi = le["floors"].get(letter, (0, -1))
        if lo <= floor <= hi:
            out.append(f"{letter} ({le['achievement']}; 1/{1 / le['chance_per_kill']:,.0f} a monster)")
    return out


def _next_title(player: PlayerState) -> str | None:
    """The Kal'gerion title a commendation unlocks next (they go in the bosses' order), None when all are read or unknown."""
    missing = _missing(player, rules()["titles_achievement"])
    return next((b["title"] for b in rules()["bosses"] if b.get("title") in (missing or set())), None)


def _gains(player: PlayerState, boss: dict) -> tuple[list[Gain], list[str]]:
    data = rules()
    gains: list[Gain] = []
    notes: list[str] = []
    kills = _missing(player, data["kills_achievement"])
    killed = kills is not None and boss["name"] not in kills
    if kills is not None and boss["name"] in kills:
        gains.append(Gain(data["kills_achievement"], boss["name"], "kill"))
    achievement, journal = boss["journal"]
    if journal in (_missing(player, achievement) or set()):
        gains.append(Gain(achievement, journal, "journal"))
    for feat, how in boss.get("feats", {}).items():
        if (m := _missing(player, feat)) is not None and m:
            gains.append(Gain(feat, how, "feat"))
    title = boss.get("title")
    if title and not killed and title == _next_title(player):
        notes.append(f"its kill lets the next Kal'gerion battle commendation be read: 'the {title}' ({data['titles_achievement']})")
    return gains, notes


def picks(player: PlayerState) -> tuple[list[Pick], list[Pick]]:
    """(picks the player's floors reach, best first; picks above their level, by level)."""
    top = highest_floor(player.level("Dungeoneering"))
    reachable: list[Pick] = []
    later: list[Pick] = []
    for boss in rules()["bosses"]:
        gains, notes = _gains(player, boss)
        if not gains:
            continue
        floors = [f for lo, hi in boss["floors"] for f in range(lo, hi + 1)]
        lowest = min(floors)
        if lowest > top:
            later.append(Pick(boss["name"], lowest, rivals(lowest), gains, notes, [], 2 * lowest - 1))
            continue
        open_floors = [f for f in floors if f <= top]
        fewest = min(rivals(f) for f in open_floors)
        # The floor to run: one that also rolls floor-only lore still missing, else the highest (the most xp).
        floor = max(open_floors, key=lambda f: (len(_floor_lore(player, f)), f))
        reachable.append(Pick(boss["name"], floor, fewest, gains, notes, _floor_lore(player, floor), 2 * lowest - 1))
    reachable.sort(key=lambda p: (-p.worth, -len(p.floor_lore), p.min_level))
    later.sort(key=lambda p: p.min_level)
    return reachable, later


def floor_only(player: PlayerState) -> list[str]:
    """Floor-only lore still missing and where it drops (any boss)."""
    lore = rules()["floor_lore"]
    top = highest_floor(player.level("Dungeoneering"))
    out = []
    ch = lore["chronicles"]
    for page in sorted(_missing(player, ch["achievement"]) or set(), key=lambda s: int(s.rsplit(" ", 1)[1].rstrip(")"))):
        n = int(page.rsplit(" ", 1)[1].rstrip(")"))
        reach = "" if 2 * n - 1 <= top else f", from Dungeoneering {4 * n - 3}"
        out.append(f"{page}: floor {2 * n - 1} or {2 * n} ({ch['chance']:.0%} a floor{reach})")
    le = lore["letters"]
    for letter in sorted(_missing(player, le["achievement"]) or set(), key=lambda s: int(s.rsplit(" ", 1)[1].rstrip(")"))):
        lo, hi = le["floors"][letter]
        reach = "" if lo <= top else f", from Dungeoneering {2 * lo - 1}"
        out.append(f"{letter}: floors {lo}-{hi} (1/{1 / le['chance_per_kill']:,.0f} a monster{reach})")
    return out
