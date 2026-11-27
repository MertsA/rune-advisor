"""Anachronia base camp and the Dream of Iaia (basecamp.py, resources.iaia_resources, tracks/base_camp.py)."""

from __future__ import annotations

from rune_advisor import basecamp
from rune_advisor.models import PlayerState
from rune_advisor.resources import iaia_resources


def _maxed(totem: bool = False, **stations) -> PlayerState:
    """A camp like the test account's on 2026-11-26: 150,000 of each resource, everything at tier 3, the totem as given."""
    v = {f"DINO_BASE_CAMP_RESOURCE_{r}": 150_000 for r in basecamp.RESOURCES}
    v |= {f"DINO_BASE_CAMP_WORKER_ASSIGNED_{r}": 12 for r in basecamp.RESOURCES}
    v |= {b["var"]: 3 for b in basecamp.data()["buildings"].values()}
    v |= {row["var"]: 1 for row in basecamp.data()["one_off"].values()}
    v["DINO_BASE_CAMP_BUILDING_TIER_TOTEM"] = 1 if totem else 0
    for skill, xp in stations.items():
        v[f"QUEST_EGW_FINALE_REWARD_STATION_STATE_{skill.upper()}"] = 1
        v[f"QUEST_EGW_FINALE_REWARD_STATION_XP_{skill.upper()}"] = xp
    return PlayerState(name="PlayerOne", vars=v)


def test_data_has_every_tier_and_station():
    d = basecamp.data()
    assert len(d["buildings"]) == 7 and all(len(b["tiers"]) == 3 for b in d["buildings"].values())
    assert d["buildings"]["Slayer Lodge"]["tiers"][2]["items"] == {"Dinosaur tooth": 1}
    assert d["buildings"]["Storehouse"]["tiers"][1]["needs"] == {"Town Hall": 2}
    assert set(d["dream_of_iaia"]["stations"]) == {"Construction", "Crafting", "Fishing", "Fletching", "Herblore", "Hunter"}


def test_spend_or_wait():
    """No fixed rule (the user, 2026-11-26): an upgrade that can't go ahead (the totem without its Remototem) holds
    nothing back; a ready one is weighed: its unlock value against the training its resources buy at the stations."""
    p = _maxed(Hunter=146_070, Herblore=0, Fletching=0)
    assert [(n, c) for n, _, c, _ in basecamp.decisions(p)] == [("Remote totem access point", "spend")]
    assert basecamp.surplus(p) == {r: 150_000 for r in basecamp.RESOURCES}
    assert basecamp.station_xp(p, "Hunter") == (146_070, 75_000)   # 2 vines an xp
    assert basecamp.station_xp(p, "Crafting") is None              # not built
    p.inventories["BANK"] = {"Remototem": 1}
    assert basecamp.decisions(p)[0][2] == "wait"                   # ready; without a Context it is waited for
    assert basecamp.surplus(p)["VINES"] == 0 and basecamp.surplus(p)["LEAVES"] == 150_000

    class Ctx:
        player, unlock_value_hours = p, 2.0
    for station_hours, choice in ((10.0, "spend"), (1.0, "wait")):
        old = basecamp.station_hours
        basecamp.station_hours = lambda ctx, need, h=station_hours: h
        try:
            assert basecamp.decisions(p, Ctx())[0][2] == choice
        finally:
            basecamp.station_hours = old


def test_base_camp_workforce():
    """The camp as a workforce (workforce.py): all its workers on any one resource, 1,440 of it a worker-day; the spare
    store as stock; Advance Time's base camp casts add days to a window."""
    p = _maxed(Hunter=0)
    camp = basecamp.ledger(p)
    assert basecamp.BASE_CAMP.workers(p) == 72  # every assigned worker here (12 x 6); a real camp has 70
    assert camp.capacity("VINES", "VINES", basecamp.GATHER, days=1) == 72 * 1_440
    camp.take("CLAY", 72 * 1_440 / 2, "CLAY", basecamp.GATHER, days=1)  # half the day's workers on clay
    assert camp.capacity("VINES", "VINES", basecamp.GATHER, days=1) == 72 * 1_440 / 2
    assert camp.stock["VINES"] == 150_000 and camp.take_stock("VINES", 200_000) == 150_000

    class Ctx:
        player, _advance_time = p, {"Anachronia base camp": 3}
    assert basecamp.advance_days(Ctx(), 7) == 3


def test_level_plan_sizes_the_station_to_the_plan():
    """A Fishing plan (the plain rate here, 300,000 an hour, is slower than a station's 350,000) takes the station's
    stored xp, the spare clay and what the workers (moved to clay) gather over the plan's own window, so it uses more
    than the stock alone."""
    from rune_advisor.cost import CostModel
    from rune_advisor.models import Skill
    from rune_advisor.recommend.base import Context
    from rune_advisor.resources import load_pool
    from rune_advisor.xp import xp_at

    p = _maxed(Fishing=100_000)
    p.members = True
    p.skills = {"Fishing": Skill("Fishing", 90, xp_at("Fishing", 90))}
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False), resources=load_pool(p), urns=False)
    ctx._advance_time = {}
    plan = ctx.level_plan("Fishing", 92)
    used = plan.resources_used.get("Dream of Iaia Fishing station", 0) * 175
    stock = 100_000 + 75_000  # stored + 150,000 clay at 2 an xp
    assert used > stock, (used, plan.steps)
    assert any(step.startswith("stock the station: 150,000 clay from the store; ") and "worker-days on clay" in step
               for step in plan.steps)


def test_nothing_without_the_export():
    p = PlayerState(name="PlayerOne")
    assert not basecamp.known(p) and iaia_resources(p) == [] and basecamp.note(p) is None


def test_bonus_xp_pays_out_on_station_xp():
    """Station actions are trained xp: banked bonus xp doubles them while it lasts (a lamp would get none)."""
    from rune_advisor.cost import CostModel
    from rune_advisor.models import Skill
    from rune_advisor.resources import ResourcePool, iaia_resources, plan_level
    from rune_advisor.xp import xp_at

    p = _maxed(Fishing=100_000)
    p.skills = {"Fishing": Skill("Fishing", 90, xp_at("Fishing", 90))}
    pool = ResourcePool(iaia_resources(p))
    cost = CostModel.default(use_wiki=False)
    start = xp_at("Fishing", 90)
    plain = plan_level(cost, pool, "Fishing", start, 90, start + 100_000)
    boosted = plan_level(cost, pool, "Fishing", start, 90, start + 100_000, banked_bonus=50_000)
    # 100,000 xp: 572 actions alone; with 50,000 bonus the first 50,000 base xp pay double, so ~286 actions.
    assert plain.resources_used["Dream of Iaia Fishing station"] == 572
    assert boosted.resources_used["Dream of Iaia Fishing station"] == 286
    assert "50,000 of it banked bonus xp" in boosted.steps[0]


def test_modelled_perks():
    """The Spa pays its bonus on the Anachronia agility courses; a Sleeping Quarters tier's new workers are spread over
    the resources whose station is built (what perk_hours re-plans with)."""
    p = _maxed(Fishing=0, Hunter=0)
    course = {"skill": "Agility", "method": "Anachronia Agility Course (island-wide)", "xp_per_hour_low": 210_000}
    assert basecamp.adjust_row(course, p)["xp_per_hour_low"] == 210_000 * 1.10  # tier 3 Spa
    p.vars["DINO_BASE_CAMP_BUILDING_TIER_SPA"] = 0
    assert basecamp.adjust_row(course, p)["xp_per_hour_low"] == 210_000
    assert basecamp.adjust_row({"skill": "Agility", "method": "Hefin Agility Course"}, p) == \
        {"skill": "Agility", "method": "Hefin Agility Course"}
    p.vars["DINO_BASE_CAMP_BUILDING_TIER_SLEEPING_QUARTERS"] = 2
    up = basecamp._upgraded(p, "Sleeping Quarters (tier 3)")
    assert up.vars["DINO_BASE_CAMP_BUILDING_TIER_SLEEPING_QUARTERS"] == 3
    assert up.vars["DINO_BASE_CAMP_WORKER_ASSIGNED_CLAY"] == 12 + 15 and up.vars["DINO_BASE_CAMP_WORKER_ASSIGNED_VINES"] == 12 + 15
    assert up.vars["DINO_BASE_CAMP_WORKER_ASSIGNED_WOOD"] == 12  # no station built for wood
    assert basecamp._upgraded(p, "Slayer Lodge (tier 3)") is None  # not modelled: unlock value only


def test_worker_allocation_advice(monkeypatch):
    """All the workers to the resource whose station saves the most, when that beats the current split."""
    from rune_advisor.cost import CostModel
    from rune_advisor.models import Skill
    from rune_advisor.recommend import plans
    from rune_advisor.recommend.base import Context
    from rune_advisor.xp import xp_at

    p = _maxed(Fishing=0, Hunter=0)
    p.skills = {s: Skill(s, 90, xp_at(s, 90)) for s in ("Fishing", "Hunter")}
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False))
    monkeypatch.setattr(plans, "goal_levels", lambda ctx: {"Fishing": 99, "Hunter": 99})
    values = basecamp.worker_values(ctx)
    # Fishing trains at 300,000 an hour here, slower than a station's 350,000: its clay saves time; Hunter's 450,000
    # beats the station, so vines save nothing.
    assert values["CLAY"] > 0 and values["VINES"] == 0 and values["WOOD"] == 0
    resource, now, then = basecamp.allocation_advice(ctx)
    assert resource == "CLAY" and then > now


def test_compacted_resources_feed_later_goals():
    """In a sequence (Context.base_camp shared, as joint plans do), a station's actions drop compacted resources into
    the store for the other stations, and its worker-days are booked so a later goal can't use the same workers."""
    from rune_advisor.cost import CostModel
    from rune_advisor.models import Skill
    from rune_advisor.recommend.base import Context
    from rune_advisor.resources import load_pool
    from rune_advisor.xp import xp_at

    p = _maxed(Fishing=0)
    p.members = True
    p.skills = {"Fishing": Skill("Fishing", 90, xp_at("Fishing", 90))}
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False), resources=load_pool(p), urns=False)
    ctx._advance_time = {}
    ctx.base_camp = basecamp.ledger(p, ctx)
    before = dict(ctx.base_camp.stock)
    plan = ctx.level_plan("Fishing", 92)
    actions = plan.resources_used["Dream of Iaia Fishing station"]
    after = ctx.base_camp.stock
    assert after["CLAY"] == 0  # the spare clay went into the station
    import pytest

    assert after["VINES"] - before["VINES"] == pytest.approx(actions / 50 / 5 * 400)  # 1/50, 400 each, over 5
    assert sum(ctx.base_camp.alloc.values()) > 0  # worker-days booked on clay
