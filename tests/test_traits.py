"""Dungeoneering scroll traits (traits.py) and smelting's double bars."""

from __future__ import annotations

import pytest

from rune_advisor import traits
from rune_advisor.mechanics import smithing
from rune_advisor.mechanics.processing import effective
from rune_advisor.models import Achievement, PlayerState, Skill
from rune_advisor.xp import xp_at


def _player(*read: str, **levels) -> PlayerState:
    p = PlayerState(name="PlayerOne", members=True, ironman=True)
    p.skills = {s: Skill(s, lvl, xp_at(s, lvl)) for s, lvl in levels.items()}
    tests = [{"description": s.name, "complete": s.trait in read, "kind": "varTotal"} for s in traits.SCROLLS.values()]
    p.achievements = {traits.ACHIEVEMENT: Achievement(name=traits.ACHIEVEMENT, completed=False, runescore=10, requirements=tests)}
    return p


def _recipe(skill, output, materials, ticks=4, facility=""):
    return {"skill": skill, "output": output, "output_qty": 1.0, "xp": 10.0, "ticks": ticks, "materials": materials,
            "facility": facility, "level": 1, "other_skills": [], "quests": [], "members": True}


def test_read_comes_from_the_achievement_tests_and_a_what_if():
    p = _player("efficiency", "life")
    assert traits.read(p) == {"efficiency", "life"}
    assert [s.trait for s in traits.unread(p)] == [t for t in traits.SCROLLS if t not in ("efficiency", "life")]
    p.assumed_traits = {"restoration"}
    assert traits.has(p, "restoration")  # the cache is keyed on the assumption


def test_material_savings():
    p = _player(*traits.SCROLLS)
    platebody = effective(p, _recipe("Smithing", "Rune platebody", [["Rune bar", 5.0]], facility="Anvil"))
    assert dict(platebody["materials"])["Rune bar"] == pytest.approx(5 * 0.98)
    table = effective(p, _recipe("Construction", "Mahogany table", [["Mahogany plank", 6.0], ["Gold leaf", 4.0]]))
    assert dict(table["materials"]) == pytest.approx({"Mahogany plank": 6 - 2 * 0.10, "Gold leaf": 4.0})
    body = effective(p, _recipe("Crafting", "Black d'hide body", [["Black dragon leather", 3.0], ["Thread", 1.0]]))
    assert dict(body["materials"])["Black dragon leather"] == pytest.approx(3 - 0.10)
    potion = effective(p, _recipe("Herblore", "Super attack (3)", [["Irit potion (unf)", 1.0], ["Eye of newt", 1.0]]))
    assert dict(potion["materials"]) == pytest.approx({"Irit potion (unf)": 1.0, "Eye of newt": 0.9})
    assert potion["ticks"] == pytest.approx(4 - 1 / 8)
    tar = effective(p, _recipe("Herblore", "Guam tar", [["Swamp tar", 15.0], ["Clean guam", 1.0]]))
    assert dict(tar["materials"])["Clean guam"] == 1.0  # tars aren't potions
    assert traits.seeds_per_planting(p, "Herb patch") == pytest.approx(0.9)
    assert traits.seeds_per_planting(p, "Tree patch") == pytest.approx(0.95)
    assert traits.seeds_per_planting(p, "Flower Patch") == 1.0
    assert traits.restoration_share(p) == pytest.approx(0.98)
    bare = _player()
    assert effective(bare, _recipe("Smithing", "Rune platebody", [["Rune bar", 5.0]]))["materials"] == [["Rune bar", 5.0]]


def test_double_bars_and_fortune():
    p = _player(Smithing=99)
    rune = smithing.double_bar_chance(p, "Rune bar")
    assert rune == pytest.approx(0.10)  # past rune's own double-bar level, no armour
    assert smithing._fortune_double(3) == pytest.approx(0.003)  # 0.1% a rank (it was read as 1% a rank)
    bar = effective(p, _recipe("Smithing", "Rune bar", [["Runite ore", 1.0], ["Luminite", 1.0]], facility="Furnace"))
    assert bar["output_qty"] == pytest.approx(1 + rune) and bar["xp"] == pytest.approx(10.0)  # no xp for the extra


def test_what_if_reads_the_scroll_and_rebuilds_simulated_rows():
    from rune_advisor.cost import CostModel
    from rune_advisor.recommend.base import Context
    from rune_advisor.recommend.tracks.scrolls import what_if

    built = []
    p = _player(Archaeology=90)
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False),
                  build_cost=lambda q: built.append(q) or CostModel.default(use_wiki=False))
    alt = what_if(ctx, "restoration")
    assert traits.has(alt.player, "restoration") and not traits.has(p, "restoration")
    assert built == [alt.player] and alt.cost is not ctx.cost and alt._gatherer is None
