"""Fort Forinthry (fort.py, tracks/fort.py): tiers from the export, building costs and the perks the planner models."""

from __future__ import annotations

import pytest

from rune_advisor import fort
from rune_advisor.models import PlayerState


def _player(**tiers) -> PlayerState:
    names = {"workshop": "Workshop", "chapel": "Chapel", "kitchen": "Kitchen", "rangers": "Ranger's Workroom",
             "command": "Command Centre"}
    return PlayerState(name="x", vars={fort.buildings()[names[k]]["var"]: v for k, v in tiers.items()})


def test_every_building_has_three_tiers_with_materials_and_a_rate():
    assert len(fort.buildings()) == 9
    for name, b in fort.buildings().items():
        assert [t["tier"] for t in b["tiers"]] == [1, 2, 3], name
        for t in b["tiers"]:
            assert t["xp"] > 0 and t["materials"].get("Stone wall segment") == 6 and t["xp_per_hour"], (name, t)
    assert fort.tier_row("Command Centre", 3)["level"] == 99
    assert fort.tier_row("Chapel", 3)["materials"]["Elder frame"] == 50


def test_tiers_come_from_the_export_or_are_unknown():
    assert fort.tier(PlayerState(name="x"), "Chapel") is None and not fort.known(PlayerState(name="x"))
    assert fort.tier(_player(chapel=2), "Chapel") == 2


def test_quests_and_build_xp():
    assert fort.quests_needed("Kitchen", 1) == (["New Foundations"], ["Murder on the Border"])
    assert fort.quests_needed("Kitchen", 2) == (["New Foundations", "Murder on the Border"], [])
    assert fort.quests_needed("Workshop", 1) == ([], ["New Foundations"])
    # The Workshop's bonus: 5% a tier, its own previous tier while it is the one being built.
    assert fort.build_xp(_player(workshop=3), "Chapel", 3) == pytest.approx(165_600 * 1.15)
    assert fort.build_xp(_player(workshop=1), "Workshop", 2) == pytest.approx(17_280 * 1.05)
    assert fort.build_hours("Chapel", 3) == pytest.approx(165_600 / 552_000)


def test_chapel_rows_follow_the_chapel_tier():
    row = {"skill": "Prayer", "method": "Offering Dragon bones at Fort Forinthry chapel (tier 3)",
           "xp_per_hour_low": 410_400, "requirements": "Construction 90"}
    assert fort.adjust_row(row, PlayerState(name="x")) == (row, None)  # not exported: as the guide says
    assert fort.adjust_row(row, _player(chapel=0))[0] is None
    two, _ = fort.adjust_row(row, _player(chapel=2))
    assert two["xp_per_hour_low"] == pytest.approx(410_400 * 2.5 / 3) and two["requirements"] is None
    assert "(tier 2)" in two["method"]


def test_kitchen_and_rangers_workroom_perks():
    cook = {"skill": "Cooking", "output": "Shark", "facility": "Range", "materials": [["Raw shark", 1]]}
    assert fort.recipe_perks(_player(kitchen=3), cook)[:4] == (0.05, 1.0, 1.0, 0.97)
    assert fort.recipe_perks(_player(kitchen=1), cook)[:4] == (0.0, 1.0, 1.0, 1.0)
    arrows = {"skill": "Fletching", "output": "Rune arrows", "materials": [["Rune arrowheads", 15]]}
    xp, out, mats, _, notes = fort.recipe_perks(_player(rangers=3), arrows)
    assert (xp, out, mats) == (0.05, 1.01, 0.98) and notes
    bow = {"skill": "Fletching", "output": "Magic shortbow (u)", "materials": [["Magic logs", 1]]}
    assert fort.recipe_perks(_player(rangers=2), bow)[:3] == (0.0, 1.0, 1.0)


def test_effective_recipe_takes_the_workroom_perks():
    from rune_advisor.mechanics.processing import effective

    arrows = {"skill": "Fletching", "output": "Rune arrows", "output_qty": 15, "xp": 187.5, "level": 75,
              "materials": [["Rune arrowheads", 15], ["Headless arrow", 15]]}
    plain = effective(PlayerState(name="x"), arrows)
    built = effective(_player(rangers=3), arrows)
    assert built["output_qty"] == pytest.approx(plain["output_qty"] * 1.01)
    assert built["xp"] == pytest.approx(plain["xp"] * (1 + 0.05) * 1.01)
    assert built["materials"][0][1] == pytest.approx(15 * 0.98)


def test_command_centre_research():
    from rune_advisor import archaeology_guild as ag

    base = ag.research_cost(100, 100)
    assert ag.research_cost(100, 100, _player(command=2)) == pytest.approx(base * 0.97)
    assert ag.research_xp(100, 100, _player(command=3)) == pytest.approx(ag.research_xp(100, 100) * 1.03)


def test_eternal_reinforcement_is_repeatable_training():
    from rune_advisor.recommend.catalogue import catalogue

    r = next(r for r in catalogue() if r["output"] == "Eternal reinforcement")
    # 499,200 xp at the guide's ~891k an hour: about 34 minutes at the hotspots, not the bucket's 1 tick.
    assert r["ticks"] == pytest.approx(499_200 / 891_429 * 6000)


def test_porter_charges_off_the_buff_icon_become_held_signs():
    """UI_PORTER_CHARGES (the porter buff's number: a worn sign plus a Grace of the Elves' store) counts as full
    signs of the tier the player's Divination makes; -1 (not shown) counts nothing."""
    from rune_advisor.models import Skill
    from rune_advisor.player import apply_plugin_snapshot

    p = PlayerState(name="PlayerOne", skills={"Divination": Skill("Divination", 99, 13_034_431)})
    apply_plugin_snapshot(p, {"vars": {"UI_PORTER_CHARGES": 186}})
    assert p.inventories["PORTER_STORE"] == {"Sign of the porter VII": 3}  # 186 // 50
    low = PlayerState(name="PlayerOne", skills={"Divination": Skill("Divination", 30, 14_000)})
    apply_plugin_snapshot(low, {"vars": {"UI_PORTER_CHARGES": 25}})
    assert low.inventories["PORTER_STORE"] == {"Sign of the porter II": 2}  # 10 a sign
    unknown = PlayerState(name="PlayerOne", skills={"Divination": Skill("Divination", 99, 13_034_431)})
    apply_plugin_snapshot(unknown, {"vars": {"UI_PORTER_CHARGES": -1}})
    assert "PORTER_STORE" not in unknown.inventories
