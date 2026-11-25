"""Daemonheim's daily boss pick (daemonheim.py, tracks/dg_boss.py): worked back from the export's achievement tests."""

from __future__ import annotations

from types import SimpleNamespace

from rune_advisor import daemonheim as dh
from rune_advisor.models import Achievement, PlayerState, Skill
from rune_advisor.recommend.tracks.dg_boss import DgBossTrack
from rune_advisor.xp import xp_at


def _ach(name: str, missing: list[str], done: list[str] = ()) -> Achievement:
    reqs = [{"description": d, "complete": False} for d in missing] + [{"description": d, "complete": True} for d in done]
    return Achievement(name, 0, completed=not missing, requirements=reqs)


def _player(level: int = 100, perk: bool = True) -> PlayerState:
    p = PlayerState(name="PlayerOne", members=True)
    p.skills = {"Dungeoneering": Skill("Dungeoneering", level, xp_at("Dungeoneering", level))}
    bosses = [b["name"] for b in dh.rules()["bosses"]]
    open_kills = ["Yk'Lagor the Thunderous", "Blink", "Icy Bones", "Hope devourer", "To'Kash the Bloodchiller", "Bal'lak the Pummeller"]
    achs = [
        _ach("Daemonheim Set Tasks - Easy", [] if perk else ["Kill a boss"]),
        _ach("The Home of Daemons", open_kills, [b for b in bosses if b not in open_kills]),
        _ach("Kal'gerion Notes", ["Kal'gerion notes (part 4)"], ["Kal'gerion notes (part 1)"]),
        _ach("Miscellaneous Notes", ["Blink's scribblings", "Gulega missive"], ["Troll scrawlings"]),
        _ach("Behemoth Notes", ["Behemoth notes (part 5)"]),
        _ach("Without Blinking An Eye", [""]),
        _ach("Kal'gerion Commander", ["Bloodchiller", "Riftsplitter", "Pummeller", "Thunderous", "Warmonger"]),
        _ach("History of Bilrach", ["Mysterious chronicle (part 25)", "Mysterious chronicle (part 26)"]),
        _ach("Marmaros and Thok Letters", ["M. and Thok letter (part 1)"]),
    ]
    p.achievements = {a.name: a for a in achs}
    return p


def test_floor_rivals_follow_the_wiki_table():
    assert dh.rivals(1) == 3 and dh.rivals(5) == 4 and dh.rivals(11) == 6  # Frozen
    assert dh.rivals(14) == 3 and dh.rivals(35) == 6  # Abandoned 2 keeps Abandoned 1's bosses
    assert dh.rivals(47) == 6 and dh.rivals(50) == 3 and dh.rivals(60) == 6


def test_picks_rank_by_floors_replaced():
    reachable, later = dh.picks(_player())
    by = {x.boss: x for x in reachable}
    # Yk'Lagor: kill + journal, 1 floor in 6 at 45-47.
    assert reachable[0].boss == "Yk'Lagor the Thunderous" and reachable[0].worth == 12 and reachable[0].floor in (45, 46, 47)
    # Blink: kill + scribblings + the solo feat, 1 in 3 at 48-50; floor 49/50 also rolls chronicle 25.
    assert by["Blink"].worth == 9 and by["Blink"].floor == 50
    assert any("Mysterious chronicle (part 25)" in s for s in by["Blink"].floor_lore)
    # Icy Bones: kill only; it runs on a floor 1-5, where the missing letter 1 drops.
    assert by["Icy Bones"].worth == 3 and by["Icy Bones"].floor <= 5 and by["Icy Bones"].floor_lore
    # Kal'gerion titles go in order: only To'Kash's kill opens the next one.
    assert by["To'Kash the Bloodchiller"].notes and not by["Bal'lak the Pummeller"].notes
    # Hope devourer needs floor 51 (Dungeoneering 101); bosses with nothing open are left out.
    assert [x.boss for x in later] == ["Hope devourer"] and later[0].min_level == 101
    assert "Dreadnaut" not in by and "Gravecreeper" not in by


def test_floor_only_lore_says_where():
    lines = dh.floor_only(_player())
    assert "Mysterious chronicle (part 25): floor 49 or 50 (25% a floor)" in lines
    assert any(s.startswith("Mysterious chronicle (part 26)") and "from Dungeoneering 101" in s for s in lines)


def test_track_needs_the_perk():
    assert DgBossTrack.candidates(SimpleNamespace(player=_player(perk=False))) == []
    [c] = DgBossTrack.candidates(SimpleNamespace(player=_player()))
    assert c.track == "dg_boss" and "Yk'Lagor the Thunderous" in c.title and c.value == 12 and c.cost_hours > 0
