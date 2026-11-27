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


def test_plans_in_sequence_share_the_kingdom(monkeypatch):
    """A joint plan's goals share one booking (Context.kingdom): what one goal's gathering took, the next can't."""
    import re
    from rune_advisor.cost import CostModel
    from rune_advisor.recommend import gathering
    from rune_advisor.recommend.base import Context

    monkeypatch.setattr(gathering, "PLAY_HOURS_PER_DAY", 3.0)
    p = _player()
    booked = kingdom.Ledger(p, 0.0)
    month = booked.capacity("Coal", "mining", "coal", days=28)
    assert month == pytest.approx(280 * 54.6)
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False), kingdom=booked)
    first = ctx.gatherer.plan({"Coal": 10_000}, play_before=84)  # 84 h at 3 h a day: 28 days of subjects
    assert booked.capacity("Coal", "mining", "coal", days=28) == pytest.approx(month)  # plan() alone books nothing
    booked.adopt(first.kingdom)  # what Context._gather does with a kept plan
    assert booked.capacity("Coal", "mining", "coal", days=28) == pytest.approx(month - 10_000)
    ctx2 = Context(player=p, cost=CostModel.default(use_wiki=False), kingdom=booked)
    second = ctx2.gatherer.plan({"Coal": 10_000}, play_before=84)
    got = next(int(m[1].replace(",", "")) for s in second.steps if (m := re.match(r"get ([\d,]+) Coal: Managing", s)))
    window = ctx2.gatherer.window_days(84 + second.hours)  # the plan's own gathering is play time too
    room = booked.capacity("Coal", "mining", "coal", days=window)
    assert 0.9 * room <= got <= room + 1  # the window never outlasts the plan's own play (bisection keeps that side)
    assert got < 10_000


def test_passive_supplies_scale_with_play_per_day(monkeypatch):
    """The window is the plan's play hours at the player's play a day: half the play a day, twice the days."""
    from rune_advisor.cost import CostModel
    from rune_advisor.recommend import gathering
    from rune_advisor.recommend.base import Context

    ctx = Context(player=_player(), cost=CostModel.default(use_wiki=False))
    monkeypatch.setattr(gathering, "PLAY_HOURS_PER_DAY", 2.0)
    assert ctx.gatherer.window_days(10) == pytest.approx(5)
    ctx.play_elapsed = 10  # earlier goals of a joint plan
    assert ctx.gatherer.window_days(10) == pytest.approx(10)


def test_exported_coffers_pay_first_and_the_profile_note():
    """MISC_COFFERS (0.5.0) is pay already deposited: its subject-days cost no new coins."""
    p = _player()
    p.vars = {"MISC_COFFERS": 197_668, "MISC_APPROVAL": 107, "MISC_POINTS_FISH": 10, "MISC_POINTS_HERB": 5,
              "MISC_HERBS_OR_FLAX": 0, "MISC_COOKED": 0}
    led = kingdom.Ledger(p, days=28)
    assert led.prepaid == pytest.approx(197_668 / 5_000)
    new, old = led.pay(30)
    assert new == 0 and old == pytest.approx(150_000)
    new, old = led.pay(20)
    assert old == pytest.approx(47_668) and new == pytest.approx(100_000 - 47_668)
    note = kingdom.note(p)
    assert note.startswith("Miscellania: approval 84%, coffers 197,668 gp") and "10 fishing (raw), 5 herbs (herbs)" in note
    assert "top it up" in note
    assert kingdom.note(_player()) is None  # nothing exported
