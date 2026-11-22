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
    twins = [r for r in consumables.boosted_rows(p) if r["boost"] == "Lava Titan" and r["activity"] == "Runite"]
    assert twins
    t = twins[0]
    base = plain[(t["activity"], t["level_min"])]
    assert t["xp_per_hour"] > base["xp_per_hour"] and t["base_method"] == base["method"]
    v = t["recipe"]["variants"][0]
    per_action = 1 / consumables.ACTIONS_PER_HOUR
    assert v["materials"] == [["Lava titan pouch", pytest.approx(60 / 64 * per_action)]]
    assert v["xp"] == pytest.approx(t["xp_per_hour"] * per_action)
    # The cost model keeps twins apart: the plain methods and rates don't see them.
    cost = CostModel.default(player=p, extra_methods=sim.method_rows(p) + consumables.boosted_rows(p))
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
    cost = CostModel.default(player=p, extra_methods=sim.method_rows(p) + consumables.boosted_rows(p))
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


def test_other_skills_get_their_boosts():
    p = _player(Fishing=80, Thieving=50, Archaeology=80, Summoning=80, Divination=99, Mining=80, Woodcutting=80)
    labels = {s: [o.label for o in consumables.singles(p, s)] for s in consumables.SKILLS}
    assert "Granite lobster" in labels["Fishing"] and "perfect juju fishing" in labels["Fishing"]
    assert labels["Thieving"] == ["Magpie"]
    porter = "signs of the porter (VII)"
    assert all(porter in labels[s] for s in ("Mining", "Woodcutting", "Fishing", "Archaeology"))
    # The best sign the player's Divination makes.
    assert "signs of the porter (IV)" in [o.label for o in consumables.singles(_player(Divination=70), "Archaeology")]
    rows = consumables.boosted_rows(p)
    by = {(r["skill"], r["boost"]) for r in rows}
    assert ("Fishing", "Granite lobster") in by and ("Thieving", "Magpie") in by
    # Porters: a charge per material (not artefacts or soil), a sign per 50. They save a bank trip a backpack at a
    # cache out in the world, but only a few steps at a dig site, where deposit spots are close by.
    arch = [r for r in rows if r["skill"] == "Archaeology" and r["boost"] == porter]
    plain = {(r["method"], r["level_min"]): r for r in sim_rows(p, "Archaeology")}

    def gain(name):
        r = next(r for r in arch if r["base_method"].startswith(name))
        return r["xp_per_hour"] / plain[(r["base_method"], r["level_min"])]["xp_per_hour"] - 1, r

    far, r = gain("Material cache (white oak) (")
    assert r["consumes"]["Sign of the porter VII"] == pytest.approx(r["items_per_hour"] / 50)
    assert r["items_per_hour"] == pytest.approx(sum(r["yields"].values()))  # no soil out in the world
    near, r = gain("Acropolis debris (simulated")
    # Excavated materials only: the screened ones are found beside a deposit spot, no porter worn.
    assert r["items_per_hour"] < 0.9 * sum(v for k, v in r["yields"].items() if not k.endswith("(damaged)"))
    assert far > 0.15 and near < 0.05


def sim_rows(p, skill):
    return consumables._sim_rows(p, skill)


def test_owning_the_grace_of_the_elves_is_not_free_porters():
    from rune_advisor.mechanics import profile

    p = _player(Divination=99)
    p.inventories = {"BANK": {"Grace of the elves": 1}}
    assert profile.porter(p) is None  # it holds charges, makes none
    p.boosts = {"porters": True}
    assert profile.porter(p) == "Grace of the Elves"


def test_guided_bosses_are_drop_sources(monkeypatch):
    """A boss with a money making guide used to give no kill rate to the gatherer (only guideless bosses went
    through the combat sim), so Araxxi's spider legs and the like had no source."""
    from rune_advisor import drops

    rate = drops.BossRate(14.0, "Money making guide/Araxxor", "necromancy", 1.0, False, {"skills": {"Constitution": 80}})
    monkeypatch.setattr(drops, "bosses", lambda: {"Araxxi"})
    monkeypatch.setattr(drops, "boss_kills_per_hour", lambda p, m: rate)
    p = _player(Hitpoints=90)
    kph, skill, _ = drops.estimated_kills_per_hour(p, "Araxxi", ("Attack", 100_000))
    assert (kph, skill) == (14.0, "Necromancy")
    assert drops.estimated_kills_per_hour(_player(Hitpoints=70), "Araxxi", ("Attack", 100_000)) is None
    # Loot tabled per mode: the normal mode's table at the boss's rate.
    monkeypatch.setattr(drops, "bosses", lambda: {"Araxxi"})
    assert drops.estimated_kills_per_hour(p, "Araxxi loot (normal)", ("Attack", 1))[0] == 14.0
    assert drops.estimated_kills_per_hour(p, "Araxxi loot (hard)", ("Attack", 1)) is None
    group = drops.BossRate(7.0, "Money making guide/Vorago", "melee", 1.0, True, {})
    monkeypatch.setattr(drops, "boss_kills_per_hour", lambda p, m: group)
    assert drops.estimated_kills_per_hour(p, "Araxxi", ("Attack", 1)) is None  # team content
