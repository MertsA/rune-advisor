"""Managing Miscellania (kingdom.py): subjects split across categories, items that come together paid once."""

from __future__ import annotations

import pytest

from rune_advisor import kingdom
from rune_advisor.models import PlayerState, Quest, QuestStatus


def _player(royal_trouble: bool = True) -> PlayerState:
    p = PlayerState(name="PlayerOne", members=True, ironman=True)
    p.quests = {"Throne of Miscellania": Quest("Throne of Miscellania", QuestStatus.COMPLETED, 1)}
    if royal_trouble:
        p.quests["Royal Trouble"] = Quest("Royal Trouble", QuestStatus.COMPLETED, 1)
    return p


def test_fishing_subjects_bring_tuna_and_swordfish_together():
    led = kingdom.Ledger(_player(), days=28)
    free, new, days = led.take("Raw swordfish", 1310, "fishing", "raw")
    assert (free, days) == (0, pytest.approx(100))  # 10 subjects for 10 days
    free, new, days = led.take("Raw tuna", 5000, "fishing", "raw")
    assert free == pytest.approx(4400) and new == pytest.approx(600) and days == pytest.approx(600 / 44)


def test_subjects_cap_per_category_and_in_all():
    led = kingdom.Ledger(_player(), days=28)
    _, _, days = led.take("Coal", 10**6, "mining", "coal")
    assert days == pytest.approx(280)  # at most 10 subjects on a category
    _, _, days = led.take("Maple logs", 10**6, "wood", "maple")
    assert days == pytest.approx(140)  # the other 5 of 15
    assert led.take("Flax", 100, "herbs", "flax")[2] == 0  # none left


def test_without_royal_trouble():
    p = _player(royal_trouble=False)
    assert kingdom.subjects(p) == 10
    assert kingdom.per_subject_day(p, "wood", "maple")["Maple logs"] == pytest.approx(892 * 2 / 3 / 10)
    assert not kingdom.settings_for(p, "Mahogany logs")  # Etceteria's hardwood needs Royal Trouble
    assert [s for _, s, _ in kingdom.settings_for(_player(), "Teak logs")] == ["teak", "both"]


def test_plans_in_sequence_share_the_kingdom():
    """A joint plan's goals share one booking (Context.kingdom): what one goal's gathering took, the next can't."""
    from rune_advisor.cost import CostModel
    from rune_advisor.recommend.base import Context
    from rune_advisor.recommend.gathering import PASSIVE_HORIZON_HOURS

    p = _player()
    booked = kingdom.Ledger(p, PASSIVE_HORIZON_HOURS / 24)
    full = booked.capacity("Coal", "mining", "coal")
    assert full == pytest.approx(280 * 54.6)
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False), kingdom=booked)
    first = ctx.gatherer.plan({"Coal": 10_000})
    assert booked.capacity("Coal", "mining", "coal") == pytest.approx(full)  # plan() alone books nothing
    booked.adopt(first.kingdom)  # what Context._gather does with a kept plan
    assert booked.capacity("Coal", "mining", "coal") == pytest.approx(full - 10_000)
    second = Context(player=p, cost=CostModel.default(use_wiki=False), kingdom=booked).gatherer.plan({"Coal": 10_000})
    assert any(f"get {full - 10_000:,.0f} Coal: Managing Miscellania" in s for s in second.steps)
