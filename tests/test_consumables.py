"""Consumable boosts (consumables.py): "with <boost>" twins of simulated rows, costed by what they use up."""

from __future__ import annotations

import pytest

from rune_advisor import consumables, sim
from rune_advisor.cost import CostModel
from rune_advisor.models import PlayerState, Skill
from rune_advisor.recommend.base import Context
from rune_advisor.recommend.gathering import Gathering, Source
from rune_advisor.xp import xp_at


def _player(**levels) -> PlayerState:
    p = PlayerState(name="PlayerOne", members=True, ironman=True)
    p.skills = {s: Skill(s, lvl, xp_at(s, lvl)) for s, lvl in levels.items()}
    return p


def test_options_follow_summoning_level_and_overrides():
    labels = [o.label for o in consumables.options(_player(Mining=90, Summoning=90), "Mining")]
    assert "Lava Titan" in labels and "Obsidian Golem" in labels
    assert "Lava Titan + Super mining potion" in labels
    # +1 familiars can't pay for a pouch; a familiar above the Summoning level isn't offered.
    assert "Desert Wyrm" not in labels
    assert "Lava Titan" not in [o.label for o in consumables.options(_player(Mining=90, Summoning=80), "Mining")]
    # A familiar overrides.toml already lists is in the plain rates: no familiar twins then.
    p = _player(Mining=90, Summoning=90)
    p.boosts = {"familiar": {"Mining": "Lava Titan"}}
    assert not [o for o in consumables.options(p, "Mining") if "familiar" in o.boosts]
    titan = next(o for o in consumables.options(_player(Mining=90, Summoning=90), "Mining") if o.label == "Lava Titan")
    assert titan.per_hour == {"Lava titan pouch": pytest.approx(60 / 64)}  # 64 minutes a pouch (the wiki's infobox)


def test_boosted_rows_carry_their_consumables_as_a_recipe():
    p = _player(Mining=90, Summoning=90, Strength=90)
    plain = {(r["activity"], r["level_min"]): r for r in sim.method_rows(p)}
    twins = [r for r in sim.boosted_rows(p) if r["boost"] == "Lava Titan" and r["activity"] == "Runite"]
    assert twins
    t = twins[0]
    base = plain[(t["activity"], t["level_min"])]
    assert t["xp_per_hour"] > base["xp_per_hour"] and t["base_method"] == base["method"]
    v = t["recipe"]["variants"][0]
    per_action = 1 / consumables.ACTIONS_PER_HOUR
    assert v["materials"] == [["Lava titan pouch", pytest.approx(60 / 64 * per_action)]]
    assert v["xp"] == pytest.approx(t["xp_per_hour"] * per_action)
    # The cost model keeps twins apart: the plain methods and rates don't see them.
    cost = CostModel.default(player=p, extra_methods=sim.method_rows(p) + sim.boosted_rows(p))
    assert all("consumes" not in r for r in cost.available_methods("Mining", 90))
    assert cost.boosted_methods("Mining", 90)
    plain_cost = CostModel.default(player=p, extra_methods=sim.method_rows(p))
    assert cost.rate("Mining", 90) == plain_cost.rate("Mining", 90)


def _twin(rate: float, pouches_per_hour: float) -> dict:
    per_action = 1 / consumables.ACTIONS_PER_HOUR
    return {"skill": "Mining", "method": "Rock with Familiar", "level_min": 1, "level_max": None,
            "xp_per_hour_low": rate, "xp_per_hour_high": None, "consumes": {"Test pouch": pouches_per_hour},
            "recipe": {"output": "Ore", "variants": [{"output_qty": 100 * per_action, "xp": rate * per_action,
                                                      "ticks": None,
                                                      "materials": [["Test pouch", pouches_per_hour * per_action]]}]}}


def _ctx_with_pouch(monkeypatch, pouch_hours: float, summoning_xp: float) -> Context:
    ctx = Context(player=_player(Mining=90, Summoning=90), cost=CostModel.default(use_wiki=False))
    src = Source("craft", "make with Summoning", pouch_hours, xp_per_unit={"Summoning": summoning_xp})
    monkeypatch.setattr(ctx.gatherer, "complete_source", lambda item: src if item == "Test pouch" else None)
    return ctx


def test_boosted_run_pays_for_its_consumables(monkeypatch):
    xp, plain_rate = 1_000_000, 100_000
    plain_hours = xp / plain_rate  # 10 h
    twin = _twin(105_000, 1.0)  # 5% faster, a pouch an hour: ~9.5 h of mining, ~10 pouches
    # A pouch that takes 6 minutes to make: 9.5 h + 1 h > 10 h, so no.
    assert _ctx_with_pouch(monkeypatch, 0.1, 500)._boosted_run([twin], xp, {}, None, plain_hours) is None
    # A cheap pouch (30 s): worth it.
    run = _ctx_with_pouch(monkeypatch, 30 / 3600, 500)._boosted_run([twin], xp, {}, None, plain_hours)
    assert run is not None and run.shortfall == {"Test pouch": 10}
    # Held pouches are used first and cost nothing.
    run = _ctx_with_pouch(monkeypatch, 0.1, 500)._boosted_run([twin], xp, {"Test pouch": 20}, None, plain_hours)
    assert run is not None and run.covered


def test_boosted_run_counts_summoning_another_goal_wants(monkeypatch):
    """The user's case: a pouch that isn't worth making for the speed alone is, once its Summoning xp is
    worth something to another goal in the same joint plan."""
    xp, plain_hours, twin = 1_000_000, 10.0, _twin(105_000, 1.0)
    ctx = _ctx_with_pouch(monkeypatch, 0.1, 500)
    assert ctx._boosted_run([twin], xp, {}, None, plain_hours) is None
    ctx.wanted = {"Summoning": 0.1 / 600}  # that goal's own plan spends 6 min per 600 Summoning xp
    run = ctx._boosted_run([twin], xp, {}, None, plain_hours)
    assert run is not None and run.method == "Rock with Familiar"


def test_level_plan_takes_the_boost_off_the_grind():
    p = _player(Mining=90, Summoning=90, Strength=90)
    plain = Context(player=p, cost=CostModel.default(player=p, extra_methods=sim.method_rows(p)), urns=False)
    cost = CostModel.default(player=p, extra_methods=sim.method_rows(p) + sim.boosted_rows(p))
    boosted = Context(player=p, cost=cost, urns=False)
    without, plan = plain.level_plan("Mining", 95), boosted.level_plan("Mining", 95)
    # Pouches made from scratch (shards bought, charms from drops) are cheap next to an hour of mining.
    assert plan.hours < without.hours
    assert any(" with Lava Titan" in s for s in plan.steps)
    assert plan.byproduct_xp.get("Summoning", 0) > 0  # making the pouches


def test_short_boosted_visit_falls_back_to_the_plain_spot():
    ctx = Context(player=_player(Mining=90), cost=CostModel.default(use_wiki=False))
    g = ctx.gatherer
    g._drop_index = {"Ore": [("Rock", "Mining", 100.0, 1000.0), ("Rock with Familiar", "Mining", 110.0, 1100.0)]}
    g._consumes = {"Rock with Familiar": {"Test pouch": 1.0}}
    g._plain_spot = {"Rock with Familiar": "Rock"}
    g._consumable_cost = {"Test pouch": 0.05}  # 3 minutes a pouch: worth it per hour, not for 6 minutes' work
    short = Gathering()
    g._visit_spots(short, {"Ore": 11}, {}, {})
    assert any("at Rock:" in s for s in short.steps) and not any("with Familiar" in s for s in short.steps)
    long = Gathering()
    g._visit_spots(long, {"Ore": 1100}, {"Test pouch": 0}, {})
    assert any("with Familiar" in s for s in long.steps)
    assert any("using 10 Test pouch" in s for s in long.steps)


def test_capped_activities_are_not_training_methods():
    """A daily node or a D&D (Alaea crablets: 3 x 200 salt a day) is no way to train for hours: kept aside."""
    from rune_advisor.cost import limited_activity

    assert limited_activity({"method": "Alaea crablet"}).startswith("Uncharted Isles")
    assert limited_activity({"method": "Golden bamboo"}).startswith("Uncharted Isles")
    assert limited_activity({"method": "Elder evil trees (Evil Tree distraction)"})
    assert limited_activity({"method": "Elder trees"}) is None
    cost = CostModel.default()
    assert {r["method"] for r, _ in cost.limited} >= {"Alaea crablet", "Golden bamboo"}
    assert not [r for r in cost.available_methods("Mining", 110) if r["method"] == "Alaea crablet"]
