"""Dungeoneering floors (mechanics/dungeoneering.py), token boxes, and reward currencies as the gatherer's stock."""

from __future__ import annotations

import math

import pytest

from rune_advisor.mechanics import dungeoneering as dg
from rune_advisor.models import PlayerState, Skill
from rune_advisor.player import sync_currency_inventory
from rune_advisor.xp import xp_at


def _player(level: int, **boosts) -> PlayerState:
    p = PlayerState(name="PlayerOne", members=True, ironman=True)
    p.skills = {"Dungeoneering": Skill("Dungeoneering", level, xp_at("Dungeoneering", level))}
    p.boosts = boosts
    return p


def test_floor_xp_follows_the_winterface():
    assert dg.highest_floor(100) == 50 and dg.highest_floor(119) == 60 and dg.highest_floor(120) == 60
    base = 50 * 100 + 60 * math.exp(0.05 * 100)
    assert dg.floor_xp(100) == pytest.approx(base * 10.2)  # a rushed solo large floor is past the 1,020% cap


def test_floor_time_and_traded_tokens():
    p = _player(100)
    assumed = dg.xp_per_hour(p)
    assert assumed == pytest.approx(dg.floor_xp(100) * 60 / dg.SOLO_LARGE_FLOOR_MINUTES)
    assert dg.xp_per_hour(_player(100, dg_floor_minutes=20)) == pytest.approx(assumed * 25 / 20)
    plain, traded = dg.method_rows(p)[:2]
    assert traded["xp_per_hour"] == pytest.approx(plain["xp_per_hour"] * 1.2)
    assert "ASSUMED" in plain["assumptions"][0]


def test_token_boxes_are_a_recipe_at_the_level():
    (box,) = dg.box_recipes(_player(100), 100)
    assert box["xp"] == 1.25 * 100 ** 2 + 50 and box["materials"] == [[dg.TOKEN_BOX, 1.0]]


def test_reward_currencies_are_stock():
    p = _player(100)
    p.currencies = {"Dungeoneering tokens": 191_645, "Goebie reputation": 5_000}
    sync_currency_inventory(p)
    assert p.holdings.get("Dungeoneering tokens") == 191_645 and "Goebie reputation" not in p.holdings
