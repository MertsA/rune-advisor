"""Patches kept planted (Herblore Habitat, harmony pillars) and jadinko hunting: the juju potions' chain."""

from __future__ import annotations

import pytest

from rune_advisor.cost import CostModel
from rune_advisor.models import PlayerState, Quest, QuestStatus, Skill
from rune_advisor.recommend import gathering
from rune_advisor.recommend.base import Context
from rune_advisor.recommend.gathering import Gatherer
from rune_advisor.xp import xp_at


def _player(quests=(), **levels) -> PlayerState:
    p = PlayerState(name="PlayerOne", members=True, ironman=True)
    p.skills = {s: Skill(s, lvl, xp_at(s, lvl)) for s, lvl in levels.items()}
    p.quests = {q: Quest(q, QuestStatus.COMPLETED, 1) for q in quests}
    p.inventories = {"MONEY_POUCH": {"Coins": 10_000_000}}
    return p


def _gatherer(monkeypatch, player, seed_list, shops) -> Gatherer:
    monkeypatch.setattr(gathering, "seeds", lambda: seed_list)
    monkeypatch.setattr(gathering, "tables_shops", lambda: shops)
    return Gatherer(Context(player=player, cost=CostModel.default(use_wiki=False)))


MOSS = {"seed": "Harmony moss seed", "level": 75, "patch": ["Harmony pillar"], "crops": ["Harmony moss"],
        "plant_xp": 165.0, "harvest_xp": 1133.3, "grow_minutes": 200000, "harvest_life": None, "max_harvests": 1.0,
        "yield_per_harvest": -1.0, "check_xp": 0}
HERB = {"seed": "Samaden seed", "level": 80, "patch": ["Vine herb patch"], "crops": ["Grimy samaden"],
        "plant_xp": 170.0, "harvest_xp": 190.0, "grow_minutes": 80, "harvest_life": 3.0, "max_harvests": None,
        "yield_per_harvest": None, "check_xp": 0}


def test_harmony_moss_grows_with_training_on_pillars_kept_planted(monkeypatch):
    shops = {"Harmony moss seed": [["Meilyr Clan Store", 4, 50000.0, True]]}
    g = _gatherer(monkeypatch, _player(["Plague's End"], Farming=80, Mining=90), [MOSS], shops)
    moss = g.source("Harmony moss")
    assert moss is not None and moss.passive and moss.complete
    patches, crop, _ = g.patches("Harmony pillar", MOSS)
    assert (patches, crop) == (4, 3.0)  # four pillars, 1-5 moss a harvest
    # A harvest per 200,000 xp trained, at the player's pace and a few hours of play a day.
    days_per_harvest = 200_000 / g.training_rate() / gathering.ASSUMED_PLAY_HOURS_PER_DAY
    assert moss.calendar_per_unit == pytest.approx(days_per_harvest * 24 / (patches * crop))
    plan = g.plan({"Harmony moss": 24})
    assert any("kept planted" in s and "xp gained" in s for s in plan.steps)
    # The pillars are in Prifddinas: no moss before Plague's End.
    g = _gatherer(monkeypatch, _player([], Farming=80), [MOSS], shops)
    assert g.source("Harmony moss") is None


def test_vine_herbs_are_a_passive_supply(monkeypatch):
    g = _gatherer(monkeypatch, _player(Farming=85), [HERB], {"Samaden seed": [["Papa Mambo's Shop", 35, 10000.0, True]]})
    herb = g.source("Grimy samaden")
    assert herb.kind == "farm" and herb.passive  # growth isn't a cost: the patches are always planted
    assert herb.cost_per_unit == pytest.approx(herb.hours_per_unit + herb.coins_per_unit / gathering.COINS_PER_HOUR)


def test_jadinkos_give_vines_and_use_juju_hunter_potions():
    p = _player(Hunter=85, Farming=85, Herblore=85)
    cost = CostModel.default(player=p)
    plain = {r["method"]: r for r in cost.available_methods("Hunter", 85)}
    assert any(r.get("yields", {}).get("Oily vine") for r in plain.values())
    # Draconic jadinkos need a juju hunter potion on the blossom: a costed row, out of the rate bands.
    draconic = [r for r in cost.boosted_methods("Hunter", 85) if "Draconic vine" in (r.get("yields") or {})]
    assert draconic and draconic[0]["consumes"] == {"Juju hunter potion (3)": 2}
    assert draconic[0]["recipe"]["variants"][0]["materials"] == [["Juju hunter potion (3)", pytest.approx(0.2)]]
    assert not [r for r in plain.values() if r.get("consumes")]
