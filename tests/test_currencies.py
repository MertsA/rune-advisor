"""Reward-shop currencies (currencies.py): shop lines as recipes, and earning the currency."""

from __future__ import annotations

import pytest

from rune_advisor import currencies, drops
from rune_advisor.cost import CostModel
from rune_advisor.models import PlayerState, Skill
from rune_advisor.recommend.base import Context
from rune_advisor.xp import xp_at


def _ctx(**levels) -> Context:
    p = PlayerState(name="PlayerOne", members=True, ironman=True)
    p.skills = {s: Skill(s, lvl, xp_at(s, lvl)) for s, lvl in levels.items()}
    return Context(player=p, cost=CostModel.default(use_wiki=False))


def test_shop_lines_become_recipes(monkeypatch):
    monkeypatch.setattr(currencies, "shops", lambda: {
        "Incomplete hydrix": [["Death's Store", None, {"Reaper points": 300.0}, True]],
        "Bonecrusher#Upgrade": [["Ports", None, {"coins": 25000.0, "chimes": 25.0}, True]],
        "Golden bamboo": [["Bamboo Market 101", None, {"Chimes": 30.0}, True],
                          ["Big Bada Bamboo", None, {"chimes": 30.0}, True], ["Other", None, {"chimes": 30.0}, True]],
        "Party hat token": [["Solomon", None, {"RuneCoins": 100.0}, True]]})
    recipes = {(r["output"], r["facility"]): r for r in currencies.shop_recipes()}
    assert recipes["Incomplete hydrix", "Death's Store"]["materials"] == [["Reaper points", 300.0]]
    assert not [k for k in recipes if k[0].startswith("Bonecrusher")]  # an upgrade, not an item
    assert not [k for k in recipes if k[0] == "Party hat token"]  # paid currency
    assert recipes["Golden bamboo", "Bamboo Market 101"]["materials"] == [["chimes", 30.0]]  # one spelling


def test_reaper_points_at_the_best_guided_boss_and_the_weekly_cap(monkeypatch):
    monkeypatch.setattr(currencies, "reaper_tasks", lambda: {"weekly_cap": 300, "bosses": [
        {"boss": "Slow boss", "combat": 100, "kills": 10, "slayer_xp": 15000, "points": 15},
        {"boss": "Fast boss", "combat": 100, "kills": 2, "slayer_xp": 20000, "points": 20},
        {"boss": "Har-Aken", "combat": 100, "kills": 1, "slayer_xp": 18750, "points": 15}]})
    guided = {"Slow boss", "Fast boss"}
    monkeypatch.setattr(drops, "boss_kills_per_hour", lambda p, m: object() if m in guided else None)
    monkeypatch.setattr(drops, "estimated_kills_per_hour",
                        lambda p, m, style: (20.0, "Necromancy", 0.0) if m in guided else (600.0, "Necromancy", 0.0))
    hours, how, xp, calendar = currencies.earning(_ctx(Slayer=99), "Reaper points")
    task_hours = 2 / 20 + currencies.TASK_OVERHEAD_MINUTES / 60
    assert "Fast boss" in how and hours == pytest.approx(task_hours / 20)
    assert xp == {"Slayer": 1000.0} and calendar == pytest.approx(7 * 24 / 300)  # a week per 300 points
    assert currencies.earning(_ctx(Slayer=40), "Reaper points") is None  # Death assigns from 45 Slayer


def test_chimes_from_arc_commodities(monkeypatch):
    monkeypatch.setattr(currencies, "arc_commodities", lambda: {"chimes": {"Golden idol": 50.0, "Bamboo": 1.0}})
    recipes = {r["materials"][0][0]: r for r in currencies.arc_recipes()}
    assert recipes["Golden idol"]["output"] == "chimes" and recipes["Golden idol"]["output_qty"] == 50.0


def test_augmenting_gear_the_recipes_only_name_as_an_input():
    from rune_advisor.recommend.catalogue import augment_recipes

    recipes = [{"output": "Some gizmo", "materials": [["Augmented sirenic hauberk", 1.0], ["Augmentor", 1.0]]},
               {"output": "Augmented Seren godbow", "materials": [["Seren godbow", 1.0], ["Augmentor", 1.0]]},
               {"output": "Other", "materials": [["Augmented Seren godbow", 1.0]]}]
    made = augment_recipes(recipes)
    assert [(r["output"], r["materials"]) for r in made] == [
        ("Augmented sirenic hauberk", [["Sirenic hauberk", 1.0], ["Augmentor", 1.0]])]  # the godbow has its own
