"""Flagged estimates for drop sources with no kill or catch rate (drops.assumed_rate, Gatherer): an item and its chain
stay available on a rough rate, flagged, and `radv estimates` ranks the ones plans lean on."""

from __future__ import annotations

import pytest

from rune_advisor import drops
from rune_advisor.cost import CostModel
from rune_advisor.models import PlayerState, Skill
from rune_advisor.recommend import gathering
from rune_advisor.recommend.base import Context
from rune_advisor.recommend.gathering import Gatherer
from rune_advisor.xp import xp_at

STYLE = ("Attack", 100_000.0)


def _player(**levels) -> PlayerState:
    p = PlayerState(name="PlayerOne", members=True, ironman=True)
    p.skills = {s: Skill(s, lvl, xp_at(s, lvl)) for s, lvl in levels.items()}
    return p


@pytest.fixture
def world(monkeypatch):
    """A small drop world: a scenery chest, an NPC hand-out, a gift item, a quest reward, an event."""
    sources = {"Old crate": [["Zq widget", 0.5, 2.0, 0]], "Nice NPC": [["Zq gadget", 1.0, 10.0, 0]],
               "Gift box": [["Zq sprocket", 0.25, 4.0, 0]], "Some Quest": [["Zq trinket", 1.0, 1.0, 0]],
               "Santa's Grotto": [["Zq bauble", 1.0, 1.0, 0]], "Box seller": [["Gift box", 1.0, 1.0, 0]]}
    kinds = {"Old crate": "scenery", "Nice NPC": "npc", "Gift box": "item", "Some Quest": "quest",
             "Santa's Grotto": "other", "Box seller": "npc"}
    monkeypatch.setattr(drops, "tables", lambda: {"sources": sources, "monsters": {}, "bosses": []})
    monkeypatch.setattr(drops, "source_kinds", lambda: kinds)
    monkeypatch.setattr(drops, "bosses", lambda: frozenset())
    by_item: dict[str, list] = {}
    for src, rows in sources.items():
        for item, chance, qty, _ in rows:
            by_item.setdefault(item, []).append((src, chance, qty))
    monkeypatch.setattr(gathering, "sources_of", lambda item: by_item.get(item, []))
    monkeypatch.setattr(gathering, "tables", drops.tables)
    return Gatherer(Context(player=_player(Attack=99), cost=CostModel.default(use_wiki=False)))


def test_rates_by_kind(world):
    p = world.ctx.player
    assert drops.assumed_rate(p, "Old crate", STYLE)[0] == drops.ASSUMED_RATES["scenery"][0]
    assert drops.assumed_rate(p, "Nice NPC", STYLE)[0] == pytest.approx(1 / 24)  # once a day
    assert drops.assumed_rate(p, "Some Quest", STYLE) is None  # rewards once
    assert drops.assumed_rate(p, "Gift box", STYLE) is None  # opened, not farmed
    assert drops.assumed_rate(p, "Santa's Grotto", STYLE) is None  # a past event
    assert drops.assumed_rate(p, "TzTok-Jad", STYLE)[0] == 1.0  # a Fight Cave run an hour


def test_flagged_sources_keep_items_available_and_are_tallied(world):
    g = world
    widget = g.source("Zq widget")
    assert widget.kind == "drop" and "ASSUMED" in widget.how  # 60 opens an hour x 0.5 x 2
    assert widget.hours_per_unit == pytest.approx(1 / (drops.ASSUMED_RATES["scenery"][0] * 0.5 * 2))
    gadget = g.source("Zq gadget")
    assert gadget.kind == "daily" and gadget.flag and gadget.calendar_per_unit == pytest.approx(24 / 10)
    sprocket = g.source("Zq sprocket")  # in a gift box the Box seller hands out once a day
    assert sprocket.kind == "craft" and sprocket.how == "open Gift box" and sprocket.flag
    assert g.source("Zq trinket") is None and g.source("Zq bauble") is None
    g.plan({"Zq widget": 10, "Zq gadget": 5})
    used = {where for where in g.flag_use}
    assert any(w.startswith("Old crate") for w in used) and any("Nice NPC" in w for w in used)
    hours, goals = next(v for k, v in g.flag_use.items() if k.startswith("Old crate"))
    assert hours == pytest.approx(10 * widget.hours_per_unit) and goals == {"Zq widget", "Zq gadget"}


def test_daemonheim_floor_items_are_not_sources():
    """Whatever is made or bought on a Daemonheim floor is lost when it ends (only bound items carry over): the
    tagged recipes stay in the data but out of the catalogue, and rusty coins buy nothing."""
    import json

    from rune_advisor import currencies, datapkg
    from rune_advisor.recommend.catalogue import catalogue

    raw = json.loads(datapkg.path("recipe_catalogue.json").read_text())["recipes"]
    assert [r for r in raw if r.get("daemonheim")]
    assert not [r for r in catalogue() if r.get("daemonheim")]
    assert "Rusty coins" in currencies.SKIPPED


def test_dungeoneering_tokens_cheapest_way(monkeypatch):
    from rune_advisor import currencies

    ctx = Context(player=_player(Dungeoneering=100, Archaeology=60), cost=CostModel.default(use_wiki=False))
    hours, how, xp, _ = currencies.earning(ctx, "Dungeoneering tokens")
    from rune_advisor.mechanics import dungeoneering
    assert "floors" in how and hours == pytest.approx(1 / (0.2 * dungeoneering.xp_per_hour(ctx.player)))
    assert xp == {"Dungeoneering": 5.0}  # 5 floor xp a token
    # A token box (Dragonkin V/VI's repeat reward) at what the gatherer says it costs: opened for 1.25 L^2 + 50.
    from rune_advisor.recommend.gathering import Source

    box = Source("craft", "hand in Dragonkin V", 0.1, xp_per_unit={"Archaeology": 10_000.0})
    monkeypatch.setattr(ctx.gatherer, "complete_source", lambda item: box if item == currencies.TOKEN_BOX else None)
    per_box = 1.25 * 100 ** 2 + 50
    hours, how, xp, _ = currencies.earning(ctx, "Dungeoneering tokens")
    assert currencies.TOKEN_BOX in how and hours == pytest.approx(0.1 / per_box)
    assert xp == {"Archaeology": pytest.approx(10_000 / per_box)}


def test_excavation_soil_caches_and_screening(monkeypatch):
    """Hotspots dig materials, damaged artefacts and soil; caches one material, soil only inside a dig site;
    screening follows the Screening page's formula; restoring is a workbench mix of what a hotspot digs up."""
    from rune_advisor.mechanics import archaeology

    p = _player(Archaeology=100)
    p.tools["mattock"] = "Elder rune"
    hot = {h["hotspot"]: h for h in archaeology.hotspot_rates(p)}["Acropolis debris"]
    assert {"Amphora (damaged)", "Rod of Asclepius (damaged)", "Keramos", "Saltwater mud"} <= set(hot["yields"])
    e = archaeology._excavation(p, 100, sprites=False)
    # Trips to a deposit spot (every 27 items, 6 s) take a little off the hits.
    hits = hot["yields"]["Saltwater mud"] / archaeology.SOIL_CHANCE
    assert 0.95 * e["hits"] < hits < e["hits"]
    assert e["precision"] == 70 + 8  # Elder rune + four qualification upgrades (assumed)
    p.boosts = {"mattock_upgrades": 1}
    assert archaeology._excavation(p, 100, sprites=False)["precision"] == 72
    monkeypatch.setattr(archaeology, "caches", lambda: [
        {"cache": "Material cache (keramos) at Everlight Dig Site", "level": 42, "material": "Keramos",
         "dig_site": "Everlight Dig Site", "soil": "Saltwater mud"},
        {"cache": "Material cache (keramos)", "level": 42, "material": "Keramos", "dig_site": None, "soil": None}])
    inside, outside = archaeology.cache_rates(p)
    assert set(inside["yields"]) == {"Keramos", "Saltwater mud"} and set(outside["yields"]) == {"Keramos"}
    # Screening at 100: 10,000 x floor(31 + 99 x (140 - 31) / 98) = 1,410,000 out of 2,550,000.
    assert archaeology.sift_chance(p) == pytest.approx(1_410_000 / 2_550_000)
    assert sum(archaeology.sift_yields(p, "Saltwater mud").values()) == pytest.approx(archaeology.sift_chance(p))
    mix = next(m for m in archaeology.restoration_mixes(100) if m["mix"] == "Acropolis debris")
    shares = dict(mix["materials"])
    assert shares["Amphora (damaged)"] == pytest.approx(0.5) and shares["Rod of Asclepius (damaged)"] == pytest.approx(0.5)


def test_collections_are_hand_ins(monkeypatch):
    from rune_advisor.recommend import catalogue

    monkeypatch.setattr(catalogue, "archaeology_collections", lambda: {"collections": {"Kin": {
        "artefacts": ["A", "B"], "chronotes": 500.0, "bonus_chronotes": 250.0, "rewards": {"Box": 2.0},
        "level": 77, "members": True}}, "artefact_chronotes": {}})
    made = {r["output"]: r for r in catalogue.collection_recipes()}
    assert made["chronotes"]["output_qty"] == 500 and made["Box"]["output_qty"] == 2
    assert made["Box"]["materials"] == [["A", 1.0], ["B", 1.0]] and made["Box"]["level"] == 77


@pytest.mark.parametrize("hotspot,level,guide", [("Armarium debris", 93, 125_000), ("Keshik tower debris", 95, 146_500),
                                                  ("Destroyed golem", 98, 172_000), ("Hellfire forge", 104, 210_000)])
def test_archaeology_loop_near_the_training_guide(hotspot, level, guide):
    """The training guide's rates (elder rune mattock, all upgrades, archaeologist's outfit; data/wiki_rates.json)
    against the full loop with time sprites: excavate, screen the soil, restore what's found, top up materials."""
    from rune_advisor.mechanics import archaeology

    p = _player(Archaeology=level)
    p.tools["mattock"] = "Elder rune"
    p.inventories = {"BANK": {f"Archaeologist's {x}": 1 for x in ("hat", "jacket", "trousers", "gloves", "boots")}}
    rows = [r for r in archaeology.method_rows(p) if r["method"].startswith(f"{hotspot}: excavate")
            and not r.get("afk") and r["level_max"] == level + 4]
    assert rows and 0.65 < rows[0]["xp_per_hour"] / guide < 1.25


def test_screening_is_part_of_excavation():
    from rune_advisor.mechanics import archaeology

    p = _player(Archaeology=100)
    p.vars = {"ARCH_SOIL_BOX_UPGRADE": 3}
    assert archaeology.soil_box(p) == 500 + archaeology.SOIL_BACKPACK
    dig = next(r for r in archaeology.method_rows(p) if r["method"].startswith("Acropolis debris (simulated"))
    assert "Saltwater mud" not in dig["yields"] and "soil screened as it piles up" in dig["assumptions"]


def test_guild_unlocks_research_and_hand_ins(monkeypatch):
    from rune_advisor import archaeology_guild as ag
    from rune_advisor.mechanics import archaeology

    p = _player(Archaeology=100)
    p.vars = {"ARCH_SOIL_BOX_UPGRADE": 3}
    left = {u["name"] for u, _ in ag.unlocks_left(p)}
    assert "Soil box upgrade" not in left and "Master archaeologist's hat" in left
    p.boosts = {"archaeology_unlocks": ["Material storage upgrade x3"]}
    assert "Material storage upgrade" not in {u["name"] for u, _ in ag.unlocks_left(p)}
    # The export's tier counts (0.5.0): the test account's 3/1/3/0 leaves workbench storage from Professor, precision at
    # Guildmaster and the relic loadout; "Workbench upgrade" is the series' first tier.
    p.boosts = {}
    p.vars = {"ARCH_SOIL_BOX_UPGRADE": 3, "ARCH_MATERIAL_STORAGE_UPGRADE": 3,
              "ARCH_RESTORATION_WORKBENCH_STORAGE_UPGRADE": 1, "ARCH_PRECISION_BASE_UPGRADE": 3,
              "ARCH_RELIC_LOADOUT_GUILD_REWARD_SHOP": 0}
    rows = [(u["name"], u["chronotes"]) for u, known in ag.unlocks_left(p) if not u["name"].startswith(("Master", "Arch"))]
    assert ("Workbench upgrade", 25000) not in rows and ("Workbench storage upgrade", 60000) in rows
    assert ("Mattock precision upgrade", 150000) in rows and ("Mattock precision upgrade", 75000) not in rows
    assert ("Additional relic loadout", 80000) in rows and not any(n == "Material storage upgrade" for n, _ in rows)
    assert archaeology.precision_upgrades(p, 100) == (6, False)
    # Research: 4,000 xp an hour at Kharid-et at 100, 110% at Stormguard; 5 x 100 x 1.1 = 550 chronotes an hour.
    assert ag.research_xp(100, 110) == pytest.approx(4400) and ag.research_cost(101, 110) == pytest.approx(550)
    assert ag.best_research(100)[1]["factor"] == 110
    # Restored artefacts handed in at collection value; research only once every unlock is bought.
    monkeypatch.setattr(ag, "unlocks_left", lambda player: [])
    restored = {"Amphora": 2.0}
    value = 2 * 744 * ag.COLLECTION_FACTOR
    yields, xp, notes = archaeology.hand_in(p, 100, restored, 100_000)
    spent = value - yields.get("chronotes", 0)
    assert xp == pytest.approx(100_000 + spent / 550 * 4400) and any("research" in n for n in notes)


def test_supply_fixes_from_the_skill_audit():
    """Fixes from auditing each skill's plan (2026-11-21): bosses use their main version, one-time builds aren't
    training, bait is a cost unless a prawn perk covers it, weekly claims match their drop pages."""
    from rune_advisor.mechanics import combat, fishing
    from rune_advisor.recommend.catalogue import one_time_build
    from rune_advisor.recommend.gathering import _same_npc

    assert combat.monster("Solak, Guardian of the Grove")["life_points"] == 2_000_000  # not a 35,000 leg
    assert one_time_build({"skill": "Construction", "output": "Canoe station (Havenhythe)",
                           "facility": "Dilapidated canoe station"})
    assert not one_time_build({"skill": "Construction", "output": "Mahogany long table",
                               "facility": "Furniture workbench"})
    assert one_time_build({"skill": "Prayer", "output": "Sani Piliu (statue)",
                           "facility": "Sani Piliu (statue)#Statue plinth"})
    p = _player(Fishing=99)
    assert fishing.bait_for(p, "Raw karambwan") == "Raw karambwanji"
    assert fishing.bait_for(p, "Raw trout") == "Feather" and fishing.bait_for(p, "Raw cave eel") == "Fishing bait"
    p.boosts = {"prawn_perks": ["bait", "feathers"]}
    assert fishing.bait_for(p, "Raw trout") is None and fishing.bait_for(p, "Raw cave eel") is None
    assert fishing.bait_for(p, "Leaping trout") == "Feather"  # barbarian fishing needs its own perk
    assert fishing.bait_for(p, "Raw karambwan") == "Raw karambwanji" and fishing.bait_for(p, "Raw rocktail")
    assert _same_npc("Razmire Keelgan", "Razmire in Mort'ton") and not _same_npc("Wizard Mizgog", "Wizard Cromperty in Ardougne")
    assert drops.assumed_rate(p, "Giant crayfish pool", STYLE) is None  # the fish farm's rows rate it


def test_a_spots_catch_is_costed_as_one_visit(monkeypatch):
    """A recipe mix over a multi-yield spot's catch (the fish farm's four fish, cooked) pays for the visit once: the
    longest need sets the hours, where costing each fish alone would charge every fish the whole visit."""
    ctx = Context(player=_player(Cooking=99, Fishing=99), cost=CostModel.default(use_wiki=False))
    g = ctx.gatherer
    monkeypatch.setattr(g, "multi_yield_spots", lambda: {"Farm": {"Raw a": 100.0, "Raw b": 50.0}})
    monkeypatch.setattr(g, "spot_hours", lambda m: 1.25)  # the hour plus its feed
    monkeypatch.setattr(g, "bulk_cost", lambda item, n, play=0.0: n / {"Raw a": 100.0, "Raw b": 50.0}[item])
    need = {"Raw a": 200, "Raw b": 100}
    assert ctx._shortfall_hours({"spot": "Farm"}, need) == pytest.approx(2.5)  # 2 visits' hours, with feed
    assert ctx._shortfall_hours({}, need) == pytest.approx(4.0)  # each item alone: 2 h + 2 h
    single = {"Raw a": (100.0, 200_000.0), "Raw b": (300.0, 300_000.0)}
    mix, = ctx._catch_mixes("Cooking", single)
    v = mix["recipe"]["variants"][0]
    assert mix["spot"] == "Farm" and dict(v["materials"]) == pytest.approx({"Raw a": 2 / 3, "Raw b": 1 / 3})
    assert v["xp"] == pytest.approx((100 * 100 + 50 * 300) / 150)
