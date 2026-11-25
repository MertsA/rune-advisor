import json
import math
import sqlite3
from pathlib import Path

import pytest

from rune_advisor.cost import CostModel
from rune_advisor.models import PlayerState, Quest, QuestStatus
from rune_advisor.player import apply_activity_feed, apply_overrides, apply_plugin_snapshot
from rune_advisor.recommend import Context, rank
from rune_advisor.recommend.tracks import MilestoneTrack, QuestPointsTrack
from rune_advisor.resources import Resource, ResourcePool, plan_level
from rune_advisor.sources import adventurers_log, hiscores, plugin_db, wiki
from rune_advisor.xp import xp_for_level

FIX = Path(__file__).parent / "fixtures"


# The dev plugin (session recorder, config dump) is not part of this repository: tests that run it are
# skipped when plugins/rune_advisor_dev is absent.
needs_dev_plugin = pytest.mark.skipif(
    not (Path(__file__).resolve().parents[1] / "plugins" / "rune_advisor_dev" / "main.lua").exists(),
    reason="plugins/rune_advisor_dev is not in this checkout")


def load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture
def player() -> PlayerState:
    p = PlayerState(name="PlayerOne")
    hs = load("hiscores_PlayerOne.json")
    p.skills = hiscores.parse_skills(hs)
    p.runescore = hiscores.parse_runescore(hs)
    quests = load("adventurers_log_quests_PlayerOne.json")
    p.quests = adventurers_log.parse_quests(quests)
    p.journal_quest_points = adventurers_log.parse_journal_qp(quests)
    apply_activity_feed(p, load("adventurers_log_events_PlayerOne.json"))
    return p


def test_xp_curve():
    assert xp_for_level(2) == 83
    assert xp_for_level(99) == 13_034_431
    assert xp_for_level(120) == 104_273_167


def test_hiscores_parse(player):
    assert player.skills["Mining"].level == 104
    assert player.runescore and player.runescore > 0


def test_quest_points_match_in_game(player):
    # The fixture is the 2026-11-04 capture rolled back to before Heralds of Crimson (3 QP) was finished:
    # 473/476, and the Adventurer's Log's own `qp` agrees (the retired RuneMetrics list showed replayed
    # Dimension of Disaster quests as unfinished, 468).
    assert player.max_quest_points == 476
    assert player.quest_points == 473 == player.journal_quest_points
    assert player.quests["Dimension of Disaster"].status == QuestStatus.COMPLETED
    assert player.quests["Cook's Assistant"].quest_id == 257
    assert player.quests["Wandering Ga'al (miniquest)"].status == QuestStatus.NOT_STARTED


def test_adventurers_log_events():
    done = adventurers_log.parse_event_quest_completions(load("adventurers_log_events_PlayerOne.json"))
    assert set(done) == {"Dimension of Disaster"}
    assert done["Dimension of Disaster"].tzinfo is not None
    # A completion in the feed beats a quest list that hasn't synced yet.
    p = PlayerState(name="PlayerOne", quests={"Dimension of Disaster": Quest("Dimension of Disaster", QuestStatus.STARTED, 2)})
    apply_activity_feed(p, load("adventurers_log_events_PlayerOne.json"))
    assert p.quests["Dimension of Disaster"].status == QuestStatus.COMPLETED
    assert p.quests["Dimension of Disaster"].source == "events"


def test_adventurers_log_replay_qp():
    # A replayed Dimension of Disaster shows STARTED in the list while the game's `qp` keeps its points.
    data = {"name": "X", "qp": 3, "qp_max": 476, "quests": {
        "1": {"title": "Cook's Assistant", "status": "COMPLETED", "qp": 1, "members": False, "eligible": True, "difficulty": 0},
        "371": {"title": "Dimension of Disaster", "status": "STARTED", "qp": 2, "members": True, "eligible": True, "difficulty": 1},
    }}
    p = PlayerState(name="X", quests=adventurers_log.parse_quests(data))
    p.journal_quest_points = adventurers_log.parse_journal_qp(data)
    from rune_advisor.quests import reconcile_journal_qp
    reconcile_journal_qp(p)
    assert p.quest_points == 3 and p.quests["Dimension of Disaster"].qp_earned


def test_wiki_quest_parse():
    info = wiki.parse_quest_row(load("wiki_quest_heralds.json")["bucket"][0])
    assert info.skill_reqs == {"Crafting": 51, "Herblore": 55, "Mining": 65, "Smithing": 60}
    assert info.length == "Long to Very Long"
    # "items required": counts read, "Combat gear" (no link) skipped, link targets capitalised.
    assert info.items == {"Maple logs": 1, "Havensilver bar": 4, "Kwuarm potion (unfinished)": 1,
                          "Silver spines": 1, "Sanguine spines": 1}


def test_wiki_quest_items_first_link_only():
    info = wiki.parse_quest_row(load("wiki_quest_gaal.json")["bucket"][0])
    assert info.items == {"Ring of stone": 1, "Ring of visibility": 1}  # not the "Sliske's gift" aside
    assert info.skill_reqs == {}


def test_ironman_quest_costs_missing_items(player):
    """Wandering Ga'al is a 12-minute miniquest for anyone holding a ring of stone; an ironman without one
    earns 303,000 Tokkul for TzHaar-Hur-Lek's store first (user report, 2026-10-31; the Tokkul shop since
    currency shops were modelled, 2026-11-21: a rare TzHaar drop before)."""
    from rune_advisor.recommend.plans import quests_plan

    gaal = wiki.parse_quest_row(load("wiki_quest_gaal.json")["bucket"][0])
    title = "Wandering Ga'al (miniquest)"
    quest_info = lambda t: gaal if t == title else None  # noqa: E731
    normal = Context(player=player, cost=CostModel.default(use_wiki=False), quest_info=quest_info)
    base = quests_plan(normal, [title]).hours
    assert base < 1  # a normal account buys the rings

    player.ironman = True
    iron = Context(player=player, cost=CostModel.default(use_wiki=False), quest_info=quest_info)
    plan = quests_plan(iron, [title])
    assert plan.hours > base + 1
    assert any("Ring of stone" in n for n in plan.needs)
    assert plan.uncertain  # the ring of visibility isn't in the fixture's export and has no drop source

    player.inventories["BANK"] = {"Ring of stone": 1, "Ring of visibility": 1}
    held = Context(player=player, cost=CostModel.default(use_wiki=False), quest_info=quest_info)
    assert quests_plan(held, [title]).hours == pytest.approx(base)


def test_heralds_tops_quest_track(player):
    heralds = wiki.parse_quest_row(load("wiki_quest_heralds.json")["bucket"][0])
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False),
                  quest_info=lambda t: heralds if t == "Heralds of Crimson" else None)
    best = rank(QuestPointsTrack.candidates(ctx))[0]
    assert best.title == "Heralds of Crimson"
    assert "unlocks quest cape" in best.details


def test_closer_milestone_can_beat_bigger_one(player):
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    by_title = {c.title: c for c in MilestoneTrack.candidates(ctx)}
    attack90, hunter99 = by_title["90 Attack"], by_title["99 Hunter"]
    assert hunter99.value > attack90.value
    assert attack90.efficiency > hunter99.efficiency


def test_resources_spent_before_grinding():
    cost = CostModel.default(use_wiki=False)
    pool = ResourcePool([Resource(name="lamp", xp_fixed=100_000, units=12, skills={"Woodcutting"})])
    start, target = xp_for_level(90), xp_for_level(92)
    with_lamps = plan_level(cost, pool, "Woodcutting", start, 90, target)
    without = plan_level(cost, None, "Woodcutting", start, 90, target)
    assert with_lamps.hours < without.hours
    assert with_lamps.resource_xp == 1_200_000
    assert any("lamp" in s for s in with_lamps.steps)


def test_set_lamps_and_penguin_points_join_the_pool():
    from rune_advisor.models import Skill
    from rune_advisor.resources import load_pool
    from rune_advisor.xp import lamp_xp

    p = PlayerState(name="X", inventories={"INV": {"Antique lamp (Elite Ardougne Tasks)": 4}}, penguin_points=16,
                    skills={"Thieving": Skill(name="Thieving", level=90, xp=xp_for_level(90))})
    pool = load_pool(p)
    [lamp] = [r for r in pool.resources if r.name.startswith("Antique lamp")]
    assert (lamp.units, lamp.xp_per_unit(90), lamp.min_level, lamp.verified) == (4, 50_000, 85, True)
    assert lamp.xp_per_unit(90, "Invention") == 50_000  # no elite-skill halving: tested on the beta 2026-11-11
    [peng] = [r for r in pool.resources if r.name == "Penguin points"]
    assert peng.units == 16 and peng.xp_per_unit(90) == lamp_xp(90, "small") and peng.skills is None
    assert peng.xp_per_unit(90, "Invention") == peng.xp_per_unit(90)
    cost = CostModel.default(use_wiki=False)
    # Below the set's level floor the lamps can't be rubbed on the skill; at 90 they go first.
    low = plan_level(cost, pool, "Thieving", xp_for_level(80), 80, xp_for_level(81))
    assert not any("Antique lamp" in s for s in low.steps)
    high = plan_level(cost, pool, "Thieving", xp_for_level(90), 90, xp_for_level(93))
    assert any(s.startswith("use 4 x Antique lamp (Elite Ardougne Tasks)") for s in high.steps)
    assert any("Penguin points" in s for s in high.steps)


def test_free_xp_spends_choice_lamps_where_they_save_most():
    from rune_advisor.models import Skill
    from rune_advisor.recommend.tracks.free_xp import FreeXpTrack

    p = PlayerState(name="X", inventories={"INV": {"Antique lamp (Elite Ardougne Tasks)": 4}},
                    skills={"Thieving": Skill(name="Thieving", level=99, xp=xp_for_level(99)),
                            "Mining": Skill(name="Mining", level=90, xp=xp_for_level(90)),
                            "Agility": Skill(name="Agility", level=80, xp=xp_for_level(80))})

    p.skills["Hitpoints"] = Skill(name="Hitpoints", level=90, xp=xp_for_level(90))
    rates = {"Thieving": 300_000.0, "Mining": 100_000.0, "Agility": 20_000.0, "Hitpoints": 10_000.0}

    class Ctx:
        player, resources = p, "pool"

        def level_plan(self, skill, level):
            assert self.resources is None  # the lamps don't pay for the training they replace
            hours = (xp_for_level(level) - p.skills[skill].xp) / rates[skill]
            return type("LP", (), {"hours": hours})()

    ctx = Ctx()
    [c] = FreeXpTrack.candidates(ctx)
    # Agility would save the most but is below the lamps' 85 floor; Hitpoints is trained on the side of everything
    # (PASSIVE_SKILLS); Mining beats Thieving at the given rates.
    assert c.title == "Spend 4 x Antique lamp (Elite Ardougne Tasks) on Mining"
    assert c.value == round(200_000 / 100_000, 2) and "skills at 85+" in c.details[1]
    assert "Hitpoints" not in c.details[1] and ctx.resources == "pool"


def test_urns_boost_grind():
    from rune_advisor.boosts import best_urn
    from rune_advisor.models import Skill

    p = PlayerState(name="X", ironman=True, members=True, skills={"Crafting": Skill("Crafting", 65, xp_for_level(65)),
                                                    "Woodcutting": Skill("Woodcutting", 70, xp_for_level(70))})
    assert best_urn(p, "Woodcutting", 70).name == "Strong woodcutting urn"  # decorated needs 76 Crafting
    assert best_urn(p, "Woodcutting", 80) is None  # strong only accepts logs up to 75
    assert best_urn(p, "Prayer", 70) is None
    p.skills["Crafting"] = Skill("Crafting", 80, xp_for_level(80))
    urn = best_urn(p, "Woodcutting", 90)
    assert urn.name == "Decorated woodcutting urn" and urn.share == 0.2 and urn.rune is None  # earth from a staff
    assert best_urn(p, "Divination", 90) is None  # decorated divination urn needs 82 Crafting

    cost = CostModel.default(use_wiki=False)
    start, target = xp_for_level(90), xp_for_level(95)
    plain = plan_level(cost, None, "Woodcutting", start, 90, target)
    boosted = plan_level(cost, None, "Woodcutting", start, 90, target, urn=urn)
    trained = (target - start) / 1.2
    assert abs(boosted.urn_xp - trained * 0.2) < 1
    assert boosted.grind_hours == pytest.approx(plain.grind_hours / 1.2)
    assert boosted.hours == pytest.approx(boosted.grind_hours + urn.count(trained) / 750)
    assert boosted.byproduct_xp["Crafting"] == pytest.approx(urn.count(trained) * urn.crafting_xp)
    assert urn.crafting_xp > 0  # forming + firing (+ rune) from the recipe catalogue

    p.inventories = {"INV": {"Urn enhancer": 1}}
    assert best_urn(p, "Woodcutting", 90).share == pytest.approx(0.25)  # +25% per payout


def test_gear_track_finds_boss_sets():
    from rune_advisor.models import Skill
    from rune_advisor.recommend.tracks.gear import GearTrack, expected_kills

    assert expected_kills([0.01]) == pytest.approx(100)
    assert expected_kills([0.01, 0.01]) == pytest.approx(150)  # 100 for the first, then 100 more half the time... = 1/p * (1 + 1/2)

    lvl = {"Attack": 80, "Strength": 80, "Defence": 75, "Hitpoints": 80, "Ranged": 60, "Magic": 60, "Necromancy": 1}
    p = PlayerState(name="X", ironman=True, members=True,
                    skills={s: Skill(s, v, xp_for_level(v)) for s, v in lvl.items()},
                    inventories={"BANK": {"Rune platebody": 1, "Rune platelegs": 1, "Abyssal whip": 1}})
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False))
    cands = {c.title.removeprefix("Farm ").split(" for ")[0]: c for c in GearTrack.candidates(ctx)
             if not c.title.startswith("craft ")}
    bandos = cands["General Graardor"]
    assert "Bandos chestplate" in bandos.title and "melee damage" in bandos.title
    assert "75 kills/h" in bandos.details[0] or "kills/h (Killing General Graardor" in bandos.details[0]
    assert not GearTrack.candidates(Context(player=PlayerState(name="N", members=True), cost=CostModel.default(use_wiki=False)))


def test_content_rates_join_guides():
    from rune_advisor.cost import _load_wiki_rates

    rows = {r["method"]: r for r in _load_wiki_rates()}
    colossi = rows["Colossi Divination (AFK charging, Agility shortcuts + canoes)"]
    assert colossi["yields"]["Remnant energy"] > 0 and colossi["side_xp"]["Agility"] > 0
    assert not any(r["method"].startswith("Colossi") for r in _load_wiki_rates(members=False))


def test_plugin_db_roundtrip(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "PluginData.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE DataStore (PluginID TEXT, Key TEXT, Type INTEGER, Value BLOB, PRIMARY KEY(PluginID, Key))")
    snap = {"player": "PlayerOne", "skills": {"HUNTER": {"level": 95, "xp": 9_000_000}},
            "quests": [{"name": "Heralds of Crimson", "finished": True, "type": "NORMAL"}],
            "achievements": [{"name": "X", "runeScore": 15, "completed": False, "progress": 2500}]}
    con.execute("INSERT INTO DataStore VALUES (?,?,?,?)", ("Rune Advisor Export", "snapshot:PlayerOne", 3, json.dumps(snap)))
    con.commit()
    con.close()

    snaps = plugin_db.snapshots(plugin_db.read_entries(db, "Rune Advisor Export"))
    p = PlayerState(name="PlayerOne")
    p.quests = adventurers_log.parse_quests(load("adventurers_log_quests_PlayerOne.json"))
    apply_plugin_snapshot(p, snaps["PlayerOne"])
    assert p.quests["Heralds of Crimson"].status == QuestStatus.COMPLETED
    assert p.skills["Hunter"].level == 95
    assert p.achievements["X"].progress == 0.25


def _export_via_lua(tmp_path, script: str = "run_wire.lua", plugin: str = "rune_advisor_export") -> Path:
    """Run a plugin's scripted test against the mock API and load its output into a PluginData.db
    under that plugin's manifest name."""
    import shutil
    import subprocess

    lua = shutil.which("lua5.4") or shutil.which("lua")
    if not lua:
        pytest.skip("no Lua interpreter")
    repo = Path(__file__).parent.parent
    out = subprocess.run([lua, f"plugins/{plugin}/tests/{script}"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    plugin_id = json.loads((repo / "plugins" / plugin / "plugin.json").read_text())["name"]
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "PluginData.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE DataStore (PluginID TEXT, Key TEXT, Type INTEGER, Value BLOB, PRIMARY KEY(PluginID, Key))")
    for line in out.splitlines():
        key, value = line.split("\t", 1)
        # Store like the real client: UTF-8 bytes with a trailing NUL.
        con.execute("INSERT INTO DataStore VALUES (?,?,?,?)", (plugin_id, key, 3, value.encode() + b"\x00"))
    con.commit()
    con.close()
    return db


@needs_dev_plugin
def test_session_recorder_end_to_end(tmp_path):
    from rune_advisor import sessions

    entries = plugin_db.read_entries(_export_via_lua(tmp_path, "run_session.lua", "rune_advisor_dev"), "Rune Advisor Dev")
    found = sessions.sessions(entries)
    assert len(found) == 1
    s = found[0]
    assert s.ended and s.seconds == 75
    assert s.xp == {"Attack": 875}  # the login-style resend at Start isn't a gain
    # 3 ores to the backpack, banked (nets out), 2 into the ore box (counted as the ore).
    assert s.gained == {"Copper ore": 5} and s.used == {}
    assert s.loc_lifetimes == {"COPPER_ROCK_DEPLETED": [5]}
    row = s.method(name="Copper test")
    assert row["skill"] == "Attack" and row["level_min"] == 90 and row["xp_per_hour"] == 42000
    assert row["yields"]["Copper ore"] == round(5 / (75 / 3600), 1)
    # 5 mining clicks + 1 bank click + a walk + a ladder, and 1 key press (the OS repeat isn't counted).
    assert s.clicks == 8 and s.keys == 1 and s.actions[("Mine", "Copper rock")] == 5
    assert row["clicks_per_hour"] == round(8 / (75 / 3600)) and row["input_gap_s"] == 10
    assert s.idle_share == 0  # no 60s+ gaps
    assert sessions.to_toml(row).startswith('[[methods]]\nskill = "Attack"')
    # Game messages only: the player's public chat, friends chat and a broadcast were never recorded.
    assert [text for _, _, text in s.chat] == ["You swing your pickaxe at the rock."] + ["You mine some copper."] * 3
    cadence = {m.pattern: m for m in sessions.message_cadence(s)}
    assert cadence["You mine some copper."].count == 3 and cadence["You mine some copper."].median_gap == 10
    assert sessions.message_pattern("<col=ff0000>You get 12 logs.</col>") == "You get # logs."
    assert s.started_at is not None and abs(s.started_at - (1793200000 + 1)) <= 1  # wall clock from chat stamps
    # The clicked rock is a MultiLoc: its variable's changes (depletion, respawn) are polled and logged.
    assert [(name, value) for _, name, value, _, _ in s.loc_vars] == [("COPPER_ROCK_STATE", 1), ("COPPER_ROCK_STATE", 0)]
    # The dev plugin's config dumps are read back as plain data, chunks joined.
    dumps = plugin_db.dumps(entries)
    quests = {q["name"]: q for q in dumps["config:quests"]}
    assert quests["Heralds of Crimson"]["progressVar"]["name"] == "HERALDS_PROGRESS"
    assert quests["Heralds of Crimson"]["params"] == {"QUEST_RELEASE_YEAR": 2024, "QUEST_SERIES_NAME": "Heralds"}
    after = next(a for a in dumps["config:achievements"] if a["name"] == "After the Heralds")
    assert [t["vars"][0]["name"] for t in after["requirements"] if t["kind"] == "varTotal"] == ["HERALDS_POST_KILLS"]
    assert after["requiredSubItems"] == [1, 1]


@needs_dev_plugin
def test_vars_only_export_smoke(tmp_path):
    """State derived from the watched vars matches the API's (plugins/rune_advisor_dev "Smoke vars" + rune_advisor.smoke)."""
    from datetime import date
    from rune_advisor import smoke
    from rune_advisor.sources.game_configs import GameConfigs, derive

    dev = plugin_db.read_entries(_export_via_lua(tmp_path, "run_session.lua", "rune_advisor_dev"), "Rune Advisor Dev")
    dumps = plugin_db.dumps(dev)
    configs = GameConfigs.from_dumps(dumps)
    vars_dump, state = dumps["smoke:vars"], dumps["smoke:state"]
    assert vars_dump["count"] == 5 and set(vars_dump["values"]) == {"HERALDS_PROGRESS", "COLD_FRONT_PROGRESS", "HERALDS_POST_KILLS", "HERALDS_FLAGS"}
    assert [n["name"] for n in vars_dump["nulls"]] == ["HERALDS_BEACON_NULL"] and vars_dump["nulls"][0]["baseValue"] == 10
    r = smoke.compare(configs, vars_dump, state, today=date(2026, 10, 31))
    assert r.quests_compared == 2 and r.quest_mismatches == []
    assert r.nulls_that_matter["HERALDS_BEACON_NULL"]["achievements"] == ["After the Heralds"]
    # Woodcutting 85 < 90, the quest unfinished, 7 < 20 kills, bit 1 of 2 set; the nil var's test is unknown.
    assert r.tests_by_kind[("skill", "agree")] == 1 and r.tests_by_kind[("quest", "agree")] == 1
    assert r.tests_by_kind[("varTotal", "agree")] == 1 and r.tests_by_kind[("varBit", "agree")] == 1
    assert r.tests_by_kind[("varBit", "unknown")] == 1 and r.test_mismatches == []
    # "After the Heralds" needs 1 of its 2 groups; the flag group is met, so it derives complete although the API
    # says 35%: a real mismatch the report must show (the mock's API state is made up).
    assert r.completion_mismatches == [("After the Heralds", True, "3500")]
    d = derive(configs, vars_dump["values"], {"WOODCUTTING": 85})
    assert d.quests == {501: "started", 502: "not started"} and d.complete[2] is False
    # Packed layout: only the bits tests can read. Quest vars 7 bits (end 100) and 4 bits (end 10), the kill
    # tally 5 bits (20), the flag var one tested bit, the nil var one bit. Saturation keeps derived state.
    from rune_advisor.sources.game_configs import var_layout
    layout = var_layout(configs)
    assert [(f.name, f.mode, f.width) for f in layout.fields] == [
        ("HERALDS_BEACON_NULL", "bits", 1), ("HERALDS_FLAGS", "bits", 1), ("HERALDS_PROGRESS", "count", 7),
        ("HERALDS_POST_KILLS", "count", 5), ("COLD_FRONT_PROGRESS", "count", 4)]
    assert layout.total_bits == 18 and len(layout.pack(vars_dump["values"])) == 3
    unpacked = layout.unpack(layout.pack({**vars_dump["values"], "HERALDS_POST_KILLS": 5000, "HERALDS_PROGRESS": 999}))
    assert unpacked["HERALDS_POST_KILLS"] == 31 and unpacked["HERALDS_PROGRESS"] == 127 and unpacked["HERALDS_FLAGS"] == 2
    d2 = derive(configs, unpacked, {"WOODCUTTING": 85})
    assert d2.quests[501] == "finished" and d2.tests[4][0] is True  # saturated: still past the end state / total
    assert layout.hash == layout.hash and 0 <= layout.hash < 2**32
    # VarTransmit names the base var: both VarBits in HERALDS_BASE map to their fields.
    assert layout.fields_by_base()["HERALDS_BASE"] == [0, 1] and layout.fields_by_base()["HERALDS_PROGRESS"] == [2]
    # Base-var manifest: the same bits organised per base var, packed from base values (HERALDS_BASE = 10:
    # bit 1 of its 3-bit field at bits 0-2 is set, bit 3 is the nil-reading VarBit) and unpacked to fields.
    from rune_advisor.sources.game_configs import base_manifest
    m = base_manifest(configs, layout)
    by_base = {e.base: e for e in m.entries}
    assert by_base["HERALDS_BASE"].ranges == [] and by_base["HERALDS_BASE"].bits == [1, 3]
    assert [(r.start, r.stop, r.size) for r in by_base["HERALDS_POST_KILLS"].ranges] == [(0, 31, 5)]
    assert m.total_bits == layout.total_bits == 18
    base_values = {"HERALDS_BASE": 10, "HERALDS_PROGRESS": 40, "HERALDS_POST_KILLS": 5000, "COLD_FRONT_PROGRESS": 0}
    fields = m.unpack(m.pack(base_values))
    assert fields == {"HERALDS_BEACON_NULL": 1, "HERALDS_FLAGS": 2, "HERALDS_PROGRESS": 40, "HERALDS_POST_KILLS": 31, "COLD_FRONT_PROGRESS": 0}
    assert derive(configs, fields, {"WOODCUTTING": 85}).tests[4] == [True, True, True]


@needs_dev_plugin
def test_wire_protocol_replay(tmp_path):
    """The exporter's wire posts (mock transport) replay on the desktop side to the same state and hashes."""
    from rune_advisor import wire
    from rune_advisor.sources.game_configs import GameConfigs, derive

    dev = plugin_db.read_entries(_export_via_lua(tmp_path / "dev", "run_session.lua", "rune_advisor_dev"), "Rune Advisor Dev")
    configs = GameConfigs.from_dumps(plugin_db.dumps(dev))
    entries = plugin_db.read_entries(_export_via_lua(tmp_path, "run_wire.lua"), "Rune Advisor Export")
    r = wire.replay(entries, configs, "PlayerOne")
    assert r.consistent, r.errors
    assert [(p.kind, p.verdict == "accepted") for p in r.posts] == [
        ("full", True), ("delta", True), ("delta", True), ("delta", True), ("delta", True), ("delta", True), ("delta", False),
        ("full", True), ("delta", True), ("delta", True)]
    # (Slayer points moving in their base var after the xp; then the metal bank loaded empty (marker), a relogin's
    # first delta)
    assert [p.bytes for p in r.posts][1:6] == [7, 7, 13, 10, 9]  # kills, saturated kills, logs, xp, points (17 bits)
    assert r.state.invs["METAL_BANK"] == {}  # known and empty, not unknown
    fields, named, skills, members = wire.values(r.layout, r.state)
    assert fields["HERALDS_POST_KILLS"] == 31 and fields["HERALDS_PROGRESS"] == 42 and fields["HERALDS_FLAGS"] == 2
    assert named["QP"] == 473 and named["BONUSXP_ATTACK"] == 25000 and named["STATUE_BAG_CLIENT"] == 1022623217
    assert skills["ATTACK"] == 5347000 and members is True
    assert r.state.invs["INV"] == {1511: 5, 23716: 1} and r.state.invs["BANK"] == {1511: 120, 23716: 12}  # no placeholder
    d = derive(configs, fields, {"WOODCUTTING": 85})
    assert d.quests[501] == "started" and d.tests[4][0] is True  # 31 >= 20 kills: the tally test passes


def test_exporter_version_matches_manifest():
    """The version the exporter sends with a full post is kept by hand: it must be the manifest's."""
    import re
    folder = Path(__file__).parent.parent / "plugins" / "rune_advisor_export"
    sent = re.search(r'PLUGIN_VERSION = "([^"]+)"', (folder / "main.lua").read_text()).group(1)
    assert sent == json.loads((folder / "plugin.json").read_text())["version"]


@needs_dev_plugin
def test_dev_plugin_profile_and_http_probe():
    """The dev plugin's profiler and HTTP probe run to the end against the mock API."""
    import subprocess
    repo = Path(__file__).parent.parent
    done = subprocess.run(["lua5.4", "plugins/rune_advisor_dev/tests/run_profile.lua"], cwd=repo, capture_output=True)
    assert done.returncode == 0 and b"http probe: ok" in done.stdout, done.stderr.decode(errors="replace")[-500:]


@needs_dev_plugin
def test_plugin_shared_libs_identical():
    """Each plugin folder is self-contained, so the libs both use are copies: keep them in step."""
    repo = Path(__file__).parent.parent / "plugins"
    for lib in ("json.lua", "configs.lua", "inventories.lua", "jobs.lua", "canon.lua", "configdump.lua", "bits.lua"):
        assert (repo / "rune_advisor_export" / "lib" / lib).read_text() == (repo / "rune_advisor_dev" / "lib" / lib).read_text(), lib


@needs_dev_plugin
def test_movement_trail_and_tile_store(tmp_path):
    from rune_advisor import movement, sessions

    found = sessions.sessions(plugin_db.read_entries(_export_via_lua(tmp_path, "run_session.lua", "rune_advisor_dev"), "Rune Advisor Dev"))
    s = found[0]
    # The start tile, four tiles walked (two a tick), then the ladder's upper tile.
    assert [(lv, x, z) for _, lv, x, z in s.moves] == [(0, 3200, 3200), (0, 3202, 3200), (0, 3204, 3200), (0, 3206, 3202),
                                                       (0, 3207, 3203), (1, 3207, 3204)]
    assert s.routes == [(s.routes[0][0], 0, [(3204, 3200), (3207, 3203)])]
    tr = movement.trail(s)
    # East along z=3200 from the start tile, then the diagonal: every tile of the route is walkable.
    assert {movement.unpack(p) for p in tr.tiles if p >> 28 == 0} == {(0, 3200 + i, 3200) for i in range(5)} | {
        (0, 3205, 3201), (0, 3206, 3202), (0, 3207, 3203)}
    assert {movement.unpack(p) for p in tr.tiles if p >> 28 == 1} == {(1, 3207, 3204)}
    assert (movement.pack(0, 3200, 3200), movement.pack(0, 3201, 3200)) in tr.steps  # the route's own tiles link up
    assert [(j.src, j.dst, j.action) for j in tr.jumps] == [((0, 3207, 3203), (1, 3207, 3204), "Climb-up Ladder")]
    assert movement.line_tiles((0, 0), (2, 5)) == [(0, 0), (2, 5)]  # not straight or diagonal: ends only
    path = tmp_path / "tiles.json"
    store = movement.load_store(path)
    assert movement.merge(store, found) == (1, 9)
    assert movement.merge(store, found) == (0, 0)  # the same session isn't counted twice
    movement.save_store(store, path)
    summary = movement.summary(movement.load_store(path))
    assert summary["tiles"] == 9 and summary["per_level"] == {0: 8, 1: 1} and summary["map_squares"] == 2
    assert summary["jumps_by_action"] == [("Climb-up Ladder", 1)]


def test_f2p_rate_rows_carry_recipes():
    """Free-to-play Prayer, Firemaking and teleport rows name what they consume (scripts/f2p_recipe_mapping.json)."""
    from rune_advisor.cost import _load_wiki_rates

    all_rows = _load_wiki_rates(members=False)
    rows = {r["method"]: r for r in all_rows}
    assert rows["Dragon bones (bought, buried)"]["recipe"]["variants"] == [
        {"output_qty": 1, "xp": 72, "ticks": None, "materials": [["Dragon bones", 1]]}]
    assert rows["Bones + Impious ashes (bought, buried)"]["recipe"]["variants"][0]["materials"] == [["Bones", 1], ["Impious ashes", 1]]
    # "Fire" is every log's output: the oak row only burns oak.
    assert rows["Oak logs, bonfire solo"]["recipe"]["variants"] == [{"output_qty": 1, "xp": 60.0, "ticks": None, "materials": [["Oak logs", 1.0]]}]
    lumbridge = rows["Casting Lumbridge Teleport repeatedly"]["recipe"]["variants"][0]
    assert lumbridge["materials"] == [["Earth rune", 1.0], ["Law rune", 1.0]]  # the air runes come from a staff
    assert sum(1 for r in all_rows if r.get("recipe")) >= 94


def test_exporter_end_to_end_plan(tmp_path, monkeypatch):
    """The planner reads the snapshot the API derives from the wire posts: skills, quest and achievement state,
    inventories (named from item ids), var-backed containers, currencies and bonus xp."""
    from rune_advisor import api_client
    from rune_advisor.export_api import ApiError, ItemNames, Service, Store

    monkeypatch.setattr(api_client, "CONFIGS_DIR", tmp_path / "api_configs")  # not the checkout's cached sets

    configs, _, posts = _mock_configs_and_posts(tmp_path)
    names = ItemNames({1511: {"name": "Logs", "config": "LOGS"}, 23716: {"name": "Huge XP lamp", "config": "HUGE_LAMP"},
                       9075: {"name": "Astral rune", "config": "ASTRAL_RUNE"}})
    service = Service(Store(tmp_path / "api"), configs, names)
    for post in posts:
        try:
            service.apply("andrew", post)
        except ApiError as e:
            assert e.status == 409
    snap = service.snapshot("andrew", "PlayerOne")
    assert snap["schema"] == 2 and snap["notes"] == []
    assert configs.quest_var_owners() == {"HERALDS_PROGRESS": "Heralds of Crimson", "COLD_FRONT_PROGRESS": "Cold Front"}
    p = PlayerState(name="PlayerOne")
    p.quests = adventurers_log.parse_quests(load("adventurers_log_quests_PlayerOne.json"))
    apply_plugin_snapshot(p, snap, configs=configs)
    assert not [n for n in p.notes if "missing from the game configs" in n]
    # The state is the end of the scenario: the quest is started, its post-quest achievement complete.
    assert p.quests["Heralds of Crimson"].status == QuestStatus.STARTED
    assert p.achievements["After the Heralds"].completed

    # Inventories arrive as item ids and counts (no placeholders on the wire) and are named by the server.
    assert p.bank["Huge XP lamp"] == 12 and p.bank["Logs"] == 120 and "Astral rune" not in p.bank
    assert set(p.inventories) == {"BANK", "INV", "WORN", "METAL_BANK", "ORE_BOX", "STATUE_BAG", "MATERIALS_STORAGE", "CRAMULET",
                                  "CURRENCIES"}  # exported Slayer points etc. are the gatherer's stock
    assert p.inventories["METAL_BANK"] == {}  # loaded empty: known, not unknown
    # The Invention materials storage and the cramulet's amulets come from named vars, read as containers.
    assert p.inventories["MATERIALS_STORAGE"] == {"Base parts": 3100, "Junk": 900}
    assert p.inventories["CRAMULET"] == {"Camulet": 1}
    assert p.last_rock_skill == {"strange": "Herblore", "golden": "Smithing"}
    assert any(n.startswith("statue rocks: the next rock is half as likely in Herblore, Smithing (") for n in p.notes)
    assert p.currencies == {"Slayer points": 700} and p.campfire_tier == "elder"  # 610 -> 700 in run_wire.lua
    assert p.bonus_xp == {"Attack": 2500} and any(n.startswith("bonus xp read from BONUSXP_") for n in p.notes)  # var in tenths
    assert p.holdings["Huge XP lamp"] == 13 and p.holdings["Astral rune"] == 50 and p.holdings["Logs"] == 125
    assert "Coins" not in p.holdings
    assert p.journal_quest_points == 473
    # Vars travel only when listed by name or under a listed prefix: a tool on the belt does; interface state,
    # and a var that merely has a listed word in its name, do not.
    assert snap["vars"]["TOOLBELT_HAMMER"] == 1
    for unlisted in ("FRIEND_SORT_MODE", "TOOLBELT_LAST_SLOT", "CHEEVO_TOOLBELT", "DNDTRACKER_PENGUIN_HUNTING_FAV"):
        assert unlisted not in snap["vars"], unlisted
    assert p.members_world is True
    assert p.inventories["ORE_BOX"] == {"Copper": 120, "Gold stone spirit": 936}
    assert p.quests["Cold Front"].seasonal
def test_replayed_quests_keep_their_qp():
    from rune_advisor.models import Quest
    from rune_advisor.quests import reconcile_journal_qp

    p = PlayerState(name="X")
    p.quests = {
        "Dimension of Disaster": Quest("Dimension of Disaster", QuestStatus.STARTED, 2),
        "Dimension of Disaster: Defender of Varrock": Quest("Dimension of Disaster: Defender of Varrock", QuestStatus.NOT_STARTED, 2),
        "Dimension of Disaster: Curse of Arrav": Quest("Dimension of Disaster: Curse of Arrav", QuestStatus.NOT_STARTED, 1),
        "Heralds of Crimson": Quest("Heralds of Crimson", QuestStatus.STARTED, 3),
        "Done": Quest("Done", QuestStatus.COMPLETED, 10),
    }
    p.journal_quest_points = 15  # 10 + the 5 DoD points; Heralds (3) must not be picked
    reconcile_journal_qp(p)
    assert p.quest_points == 15
    assert not p.quests["Heralds of Crimson"].done
    assert p.quests["Dimension of Disaster"].qp_earned and p.quests["Dimension of Disaster"].status == QuestStatus.STARTED

    # A gap that replays can't explain exactly is left alone.
    p2 = PlayerState(name="Y", quests={"Dimension of Disaster": Quest("Dimension of Disaster", QuestStatus.STARTED, 2)})
    p2.journal_quest_points = 3
    reconcile_journal_qp(p2)
    assert p2.quest_points == 0


def test_wiki_requirement_tree_fills_flag_only_achievements(player):
    from rune_advisor.models import Achievement
    from rune_advisor.recommend.plans import achievement_plan

    player.achievements["Re-enact"] = Achievement(
        name="Re-enact", runescore=25, completed=False,
        requirements=[{"kind": "varTotal", "group": 0, "totalValue": 1, "currentValue": 0, "complete": False}])
    tree = {"type": "and", "units": [
        {"type": "skill", "name": "Constitution", "level": 91},          # alias -> Hitpoints (have 90)
        {"type": "or", "units": [{"type": "skill", "name": "Attack", "level": 99},
                                 {"type": "skill", "name": "Magic", "level": 75}]},  # Magic already met
    ]}
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False), achievement_reqs={"Re-enact": tree})
    plan = achievement_plan(ctx, "Re-enact")
    steps = " | ".join(plan.steps)
    assert "91 Hitpoints" in steps
    assert "Attack" not in steps  # the OR takes the free branch
    assert plan.runescore == 25


def test_constitution_is_a_combat_byproduct(player):
    from rune_advisor.cost import CONSTITUTION_SHARE

    # Make Necromancy clearly the fastest combat skill.
    cost = CostModel.default(use_wiki=False, overrides={s: 300_000 for s in ("Attack", "Strength", "Defence", "Ranged", "Magic")}
                             | {"Necromancy": 900_000})
    ctx = Context(player=player, cost=cost)
    plan = ctx.level_plan("Hitpoints", 92)
    gap = xp_for_level(92) - player.xp("Hitpoints")
    # Constitution gets 0.33x the xp of the best combat skill.
    assert plan.hours == pytest.approx(gap / (900_000 * CONSTITUTION_SHARE), rel=0.01)
    assert any("train Necromancy" in s and "on the side" in s for s in plan.steps)

    # An explicit Hitpoints rate in rune_advisor.toml wins over the derived one.
    ctx = Context(player=player, cost=CostModel.default({"Hitpoints": 100_000}, use_wiki=False))
    assert ctx.level_plan("Hitpoints", 92).hours == pytest.approx(gap / 100_000, rel=0.01)


def test_wiki_rate_bands_rules():
    from rune_advisor.cost import wiki_rate_bands

    def row(method, lvl, lo, hi=None, **tags):
        return {"skill": "Smithing", "method": method, "level_min": lvl, "level_max": None,
                "xp_per_hour_low": lo, "xp_per_hour_high": hi, **tags}

    rows = [
        row("ore smelting", 80, 200_000),
        row("burial sets", 90, 1_200_000, bought_supplies=True),
        row("team thing", 90, 2_000_000, solo=False),
        row("minigame wave", 90, 3_000_000, regular_training=False),
        row("typo", 90, 9_000_000, suspicious=True),
        row("wisps", 85, 100_000, 400_000, low_high_meaning="variants"),
    ]
    bands = dict((lvl, (rate, m)) for lvl, rate, m in wiki_rate_bands(rows)["Smithing"])
    assert bands[90] == (1_200_000, "burial sets")  # group/minigame/suspicious rows are ignored
    assert bands[85] == (200_000, "ore smelting")   # "variants" uses the plain 100k figure, not 250k
    assert "Smithing" not in wiki_rate_bands(rows[:2])  # too few methods: keep the hand-written rates

    slayer = [{"skill": "Slayer", "method": f"task {n}", "level_min": 80, "level_max": None,
               "xp_per_hour_low": n * 100_000, "xp_per_hour_high": None, "figure": "max"} for n in (1, 2, 3, 4, 9)]
    [(lvl, rate, label)] = wiki_rate_bands(slayer)["Slayer"]
    assert rate == 300_000 and "median" in label  # best-case figures -> median, not the 900k outlier


def test_material_aware_grind(player):
    from rune_advisor.cost import CostModel

    def method(name, rate, output, xp, mats):
        return {"skill": "Fletching", "method": name, "level_min": 80, "level_max": None,
                "xp_per_hour_low": rate, "xp_per_hour_high": None,
                "recipe": {"output": output, "variants": [{"output_qty": 10, "xp": xp, "materials": mats}]}}

    darts = method("Rune darts", 1_000_000, "Rune dart", 188.0, [["Rune dart tip", 10], ["Feather", 10]])
    arrows = method("Broad arrows", 400_000, "Broad arrow", 150.0, [["Broad arrowheads", 15], ["Headless arrow", 15]])
    filler = method("Something else", 100_000, "Thing", 10.0, [["Stuff", 1]])
    cost = CostModel.default(use_wiki=False)
    cost.rates["Fletching"] = [(1, 1_000_000.0)]
    cost.rate_source["Fletching"] = "wiki"
    cost.wiki_rows["Fletching"] = [darts, arrows, filler]

    gap = xp_for_level(99) - player.xp("Fletching")

    # Nothing in the bank: keep the fastest method's rate and list what's missing.
    player.inventories = {}
    plan = Context(player=player, cost=cost).level_plan("Fletching", 99)
    assert plan.hours == pytest.approx(gap / 1_000_000, rel=0.01)
    assert set(plan.materials_short) == {"Rune dart tip", "Feather"}
    assert any("still need" in s for s in plan.steps)

    # Enough broad arrow materials in the bank: use them, at their slower rate, with nothing missing.
    plenty = 10_000_000
    player.inventories = {"BANK": {"Broad arrowheads": plenty, "Headless arrow": plenty}}
    plan = Context(player=player, cost=cost).level_plan("Fletching", 99)
    assert plan.hours == pytest.approx(gap / 400_000, rel=0.01)
    assert plan.materials_short == {}
    assert any(s.startswith("Broad arrows: make") for s in plan.steps)


def test_snapshot_only_player():
    from rune_advisor.player import player_from_snapshot

    snap = {
        "skills": {"WOODCUTTING": {"level": 1, "xp": 0}},  # a reset character: must not be "upgraded"
        "quests": [
            {"name": "Cook's Assistant", "questPoints": 1, "finished": False, "started": False, "canStart": True},
            {"name": "Romeo and Juliet", "questPoints": 0, "finished": False, "started": False, "canStart": True},
        ],
        "achievements": [{"name": "A", "runeScore": 10, "completed": True, "progress": -2},
                         {"name": "B", "runeScore": 5, "completed": False, "progress": 0}],
        "vars": {"QP": 0},
    }
    p = player_from_snapshot("Fresh", snap, current_quests={"Cook's Assistant"})
    assert p.level("Woodcutting") == 1 and p.xp("Woodcutting") == 0
    # Retired configs are kept for requirement/members checks but never offered as goals.
    assert not p.quests["Romeo and Juliet"].listed and p.quests["Cook's Assistant"].listed
    from rune_advisor.recommend.tracks import QuestPointsTrack
    titles = {c.title for c in QuestPointsTrack.candidates(Context(player=p, cost=CostModel.default(use_wiki=False)))}
    assert titles == {"Cook's Assistant"}
    assert p.runescore == 10 and p.quest_points == 0 and p.max_quest_points == 1


def test_free_to_play_rules(player):
    from rune_advisor.models import Achievement
    from rune_advisor.recommend.plans import achievement_plan, skill_plan
    from rune_advisor.recommend.tracks import MilestoneTrack, QuestPointsTrack, RuneScoreTrack
    from rune_advisor.xp import level_cap

    assert level_cap("Attack", members=False) == 120
    assert level_cap("Agility", members=False) == 5 and level_cap("Archaeology", members=False) == 20
    assert level_cap("Invention", members=False) == 1

    player.members = False
    player.achievements = {
        "Members thing": Achievement("Members thing", 10, False, requirements=[]),
        "Needs agility": Achievement("Needs agility", 10, False, requirements=[
            {"kind": "skill", "group": 0, "skills": ["AGILITY"], "totalLevels": 99, "complete": False}]),
        "Free thing": Achievement("Free thing", 10, False, requirements=[]),
    }
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False), members_only_achievements={"Members thing"})

    assert skill_plan(ctx, "Agility", 99).needs_membership   # beyond the F2P cap and not yet reached
    assert not skill_plan(ctx, "Agility", 50).needs_membership  # already have it (levels survive losing membership)
    assert achievement_plan(ctx, "Needs agility").needs_membership
    assert [c.title for c in RuneScoreTrack.candidates(ctx)] == ["Free thing"]

    quests = QuestPointsTrack.candidates(ctx)
    # The fixture's only open free quests are seasonal 0-QP ones, which aren't quest point goals.
    assert all(not player.quests[c.title].members and c.value > 0 for c in quests)
    assert not any("quest cape" in " ".join(c.details) for c in quests)  # no quest cape goal on F2P

    milestones = {c.title for c in MilestoneTrack.candidates(ctx)}
    assert not any("Agility" in t or "Hunter" in t for t in milestones)  # already at or past the free cap
    assert "Max cape (all 99)" not in milestones
    # A members' skill still below its free cap is a goal at that cap (20 Necromancy for a new free account).
    from rune_advisor.models import Skill
    player.skills["Necromancy"] = Skill("Necromancy", 5, xp_for_level(5))
    player.skills["Hunter"] = Skill("Hunter", 1, 0)  # a level-5 cap: minutes of play, not a goal
    capped = {c.title: c for c in MilestoneTrack.candidates(ctx)}
    assert "20 Necromancy (free-to-play cap)" in capped
    assert not any("Hunter" in t for t in capped)
    assert capped["20 Necromancy (free-to-play cap)"].needs == ["20 Necromancy (have 5, ~%.1fh)" % capped["20 Necromancy (free-to-play cap)"].cost_hours]
    assert not any(t.startswith(("50 Necromancy", "99 Necromancy")) for t in capped)


def test_expand_wiki_expr():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    from expand_wiki_expr import expand

    text, replaced, left = expand(
        "{{#expr:48*28*15}} | {{#expr:(185*485/1000) round 0}},000 xp/h | {{#expr:{{GEP|Bones|28}}}} gp | {{#expr:2^3}}")
    assert text == "20160 | 90,000 xp/h | {{#expr:{{GEP|Bones|28}}}} gp | 8"
    assert (replaced, left) == (3, 1)  # templates wrapping other templates (GE prices) are left alone


def test_wiki_bands_keep_guesses_below_first_wiki_level():
    from rune_advisor import cost as cost_mod

    rows = [{"skill": "Ranged", "method": f"m{n}", "level_min": 70, "level_max": None,
             "xp_per_hour_low": 110_000, "xp_per_hour_high": None} for n in range(3)]
    original = cost_mod._load_wiki_rates
    cost_mod._load_wiki_rates = lambda members=True: rows
    try:
        c = cost_mod.CostModel.default()
    finally:
        cost_mod._load_wiki_rates = original
    guess = cost_mod.CostModel.default(use_wiki=False)
    assert c.rate("Ranged", 40) == guess.rate("Ranged", 40)  # not the level-70 method's rate
    assert c.rate("Ranged", 75) == 110_000


def test_expand_wiki_vars_and_functions():
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    from expand_wiki_expr import expand

    text, replaced, left = expand(
        "{{#vardefine:furniture|{{#expr:6000/(ceil(160/1.4)*3) round 0}}}}"
        "{{formatnum:{{#expr: 58 * {{#var:furniture}}}}}} xp/h | {{#expr:459000/40120round2}} | "
        "{{#expr:floor(30 * 87.5 * 5)}} | {{#vardefine:gp|{{#expr:{{GEP|Plank}}*2}}}}{{#var:gp}}")
    assert text.startswith("986 xp/h | 11.44 | 13125 | ")
    assert "{{GEP|Plank}}" in text  # price-dependent values stay as templates


def test_group_ironman_borrows_materials(player):
    from rune_advisor.recommend.materials import choose_run

    darts = {"skill": "Fletching", "method": "Rune darts", "level_min": 80, "level_max": None,
             "xp_per_hour_low": 1_000_000, "xp_per_hour_high": None,
             "recipe": {"output": "Rune dart", "variants": [{"output_qty": 1, "xp": 18.8,
                                                             "materials": [["Rune dart tip", 1], ["Feather", 1]]}]}}
    xp = 18.8 * 1000  # 1,000 darts
    own = {"Rune dart tip": 300, "Feather": 1000}
    group = {"PlayerOne": {"Rune dart tip": 500}, "Third": {"Rune dart tip": 400}}

    run = choose_run([darts], xp, own, group, recipient="PlayerTwo")
    # Own stock first, then the biggest groupmate stack, then the next one.
    assert run.transfers == [("PlayerOne", "Rune dart tip", 500), ("Third", "Rune dart tip", 200)]
    assert run.covered
    steps = run.describe()
    assert steps[0] == "PlayerOne: put 500 Rune dart tip in group storage (or trade them) for PlayerTwo"
    assert steps[-1].startswith("Rune darts: make 1,000 Rune dart")

    # Without the group the same plan is short 700 tips.
    assert choose_run([darts], xp, own).shortfall == {"Rune dart tip": 700}


def test_group_settings_and_holdings(tmp_path):
    from rune_advisor.config import load_settings
    from rune_advisor.player import load_group

    snap = {"inventories": {"BANK": {"items": [{"id": 1, "name": "Feather", "count": 50},
                                               {"id": 2, "name": "Logs", "count": 0, "placeholder": True}]},
                            "WORN": {"items": [{"id": 3, "name": "Bronze sword", "count": 1}]}}}
    (tmp_path / "snap.json").write_text(json.dumps(snap))
    (tmp_path / "rs.toml").write_text(f'''
group = ["PlayerTwo", "PlayerOne"]
[players.playerone]
snapshot = "{tmp_path / 'snap.json'}"
[players.PlayerTwo]
members = false
''')
    s = load_settings(tmp_path / "rs.toml")
    s.plugin_db = tmp_path / "missing.db"
    s.archive_dir = tmp_path  # no real archives in a test
    assert s.in_group("playertwo") and s.player("PLAYERTWO").members is False
    p = PlayerState(name="PlayerTwo")
    load_group(p, s)
    assert p.group == {"PlayerOne": {"Feather": 50, "Bronze sword": 1}}  # placeholders hold nothing


def test_bonus_xp_shortens_grinds():
    from rune_advisor.resources import load_pool
    from rune_advisor.xp import star_xp

    cost = CostModel.default({"Attack": 100_000}, use_wiki=False)
    start, target = xp_for_level(88), xp_for_level(90)
    need = target - start
    plain = plan_level(cost, None, "Attack", start, 88, target)
    # Bonus xp pays out 1:1 on top of trained xp: a pool of half the gap halves the grind...
    full = plan_level(cost, None, "Attack", start, 88, target, banked_bonus=need / 2)
    assert full.hours == pytest.approx(plain.hours / 2)
    assert full.bonus_xp == pytest.approx(need / 2, abs=1)
    # ...a bigger one can't do more, a smaller one saves exactly its size in trained xp.
    assert plan_level(cost, None, "Attack", start, 88, target, banked_bonus=need).hours == pytest.approx(full.hours)
    part = plan_level(cost, None, "Attack", start, 88, target, banked_bonus=100_000)
    assert part.hours == pytest.approx((need - 100_000) / 100_000)
    # Free-to-play can't use it.
    f2p = plan_level(cost, None, "Attack", start, 88, target, banked_bonus=need, use_bonus=False)
    assert f2p.hours == pytest.approx(plain.hours)
    assert any("free-to-play" in s for s in f2p.steps)

    # Rested xp and stars top the pool up; rested goes first (it refills), then stars.
    p = PlayerState(name="x", rested_xp=1_500,
                    inventories={"BANK": {"Huge prismatic fallen star": 50, "Large fallen star (Attack)": 1,
                                          "Small fallen star (Constitution)": 2}})
    pool = load_pool(p)
    stars = {r.name: r for r in pool.resources if r.star}
    assert stars["Small fallen star (Constitution)"].skills == {"Hitpoints"}
    plan = plan_level(cost, pool, "Attack", start, 88, target)
    rested = 1_500 * star_xp(88, "small") / 100
    assert plan.steps[0].startswith("cash in 1,500 rested xp")
    assert plan.bonus_xp == pytest.approx(need / 2, abs=1)  # enough stars to cover half
    assert "Large fallen star (Attack)" in plan.steps[1]  # skill star before prismatic
    huge_used = int(plan.steps[2].split(" x ")[0].removeprefix("use "))
    assert huge_used == math.ceil((need / 2 - rested - star_xp(88, "large")) / star_xp(88, "huge"))


def test_penguin_points_from_var():
    from rune_advisor.player import penguin_points_from_var
    assert penguin_points_from_var({"PENG_SPY_PERM1": 0x47B80186}) == 184  # the beta's value with 184 points
    assert penguin_points_from_var({"PENG_SPY_PERM1": 0x47B70186}) == 183  # after a 1-point Spy Club lamp
    assert penguin_points_from_var({}) is None
    # Plugin 0.4.3 sends the VarBit itself, which wins over the packed variable of older exports.
    assert penguin_points_from_var({"PENG_SPY_POINTS": 184}) == 184
    assert penguin_points_from_var({"PENG_SPY_POINTS": 0, "PENG_SPY_PERM1": 0x47B80186}) == 0


def test_bonus_xp_overrides_and_snapshot():
    p = PlayerState(name="PlayerOne")
    apply_overrides(p, {"players": {"playerone": {"bonus_xp": {"Attack": 25_000, "Constitution": 10}, "rested_xp": 900}}})
    assert p.bonus_xp == {"Attack": 25_000, "Hitpoints": 10} and p.rested_xp == 900
    apply_plugin_snapshot(p, {"bonusXp": {"STRENGTH": 5}, "restedXp": 12})
    assert p.bonus_xp["Strength"] == 5 and p.rested_xp == 12
    # The planner uses the banked pool for that skill.
    ctx = Context(player=p, cost=CostModel.default({"Attack": 100_000}, use_wiki=False))
    assert any("25,000 banked Attack bonus xp" in s for s in ctx.level_plan("Attack", 90).steps)


def test_stale_snapshot_keeps_newer_quest_completions():
    from rune_advisor.models import Quest
    from rune_advisor.quests import reconcile_journal_qp

    p = PlayerState(name="X", quests={
        "Heralds of Crimson": Quest("Heralds of Crimson", QuestStatus.COMPLETED, 3),  # RuneMetrics, today
        "Dimension of Disaster": Quest("Dimension of Disaster", QuestStatus.STARTED, 2),  # being replayed
        "Done": Quest("Done", QuestStatus.COMPLETED, 10),
    })
    # A snapshot from before Heralds was finished, journal counting Done + the DoD replay.
    apply_plugin_snapshot(p, {"quests": [{"name": "Heralds of Crimson", "started": True, "finished": False},
                                         {"name": "Dimension of Disaster", "started": True, "finished": False},
                                         {"name": "Done", "finished": True}],
                              "vars": {"QP": 12}})
    assert p.quests["Heralds of Crimson"].status == QuestStatus.COMPLETED
    assert p.journal_quest_points == 15
    reconcile_journal_qp(p)
    assert p.quest_points == 15 and p.quests["Dimension of Disaster"].qp_earned


def test_ironman_trains_from_the_bank():
    from rune_advisor.recommend.catalogue import bank_mix

    p = PlayerState(name="X", ironman=True, inventories={"BANK": {
        "Pouch": 500, "Spirit shards": 30_000, "Blue charm": 300, "Fire talisman": 100, "Potato cactus": 400,
        "Crimson charm": 50, "Fire titan pouch": 5}})
    p.skills = {"Summoning": __import__("rune_advisor.models", fromlist=["Skill"]).Skill("Summoning", 91, xp_for_level(91))}
    mix = bank_mix(p, "Summoning", xp_for_level(91), 100_000, lambda lvl: 600_000)
    made = {out: n for out, _, n, _, _ in mix.runs}
    # Titans first (more xp per action, same capped speed), at the Shrine of Inanna (+50% xp): 96 do it.
    assert made["Fire titan pouch"] == 96 and "Spirit kalphite pouch" not in made
    assert any("Shrine of Inanna" in s for s in mix.describe("Summoning"))
    assert mix.xp >= 100_000
    # Pouches aren't turned into scrolls: far too little xp for what they use up.
    assert not any("scroll" in out for out in made)
    assert mix.stock["Fire titan pouch"] == 101
    # Groupmates' items fill gaps, as transfer steps.
    poor = PlayerState(name="Y", ironman=True, inventories={"BANK": {"Pouch": 10}}, skills=p.skills)
    mix = bank_mix(poor, "Summoning", xp_for_level(91), 5_000, lambda lvl: 600_000,
                   group={"X": {"Spirit shards": 5_000, "Blue charm": 10, "Fire talisman": 10}})
    assert ("X", "Fire talisman", 5) in mix.transfers and mix.describe("Summoning")[0].startswith("X: put")


def test_method_prerequisites():
    from rune_advisor.models import Quest
    from rune_advisor.requirements import parse, unmet

    titles = {"The Chosen Commander", "Plague's End", "Cold War"}
    req = parse("The Chosen Commander quest; 60 Ranged and 60 Strength; enhanced grappling hook", titles)
    assert req.quests == ["The Chosen Commander"] and req.skills == {"Ranged": 60, "Strength": 60}
    assert req.unparsed == ["enhanced grappling hook"]
    assert parse("Plague's End; Ranged, Agility and Strength 90", titles).skills == {"Ranged": 90, "Agility": 90, "Strength": 90}
    assert parse("Partial Cold War quest (clockwork suit)", titles).quests == []

    p = PlayerState(name="X", quests={"Plague's End": Quest("Plague's End", QuestStatus.NOT_STARTED, 2)})
    assert unmet("Plague's End quest; 75 Slayer", p) == ["Plague's End", "75 Slayer"]
    # The cost model drops methods the player can't use yet.
    cost = CostModel.default(player=p)
    assert any("Plague's End" in m for _, _, m in cost.excluded)
    assert all("Plague's End" not in (r.get("requirements") or "") for rows in cost.wiki_rows.values() for r in rows)
    assert parse("15 Dungeoneering (recommended); 29 Combat", titles).skills == {}
    assert unmet("60 Smithing; 30 Dungeoneering", PlayerState(name="Y"), own_skills=("Smithing",)) == ["30 Dungeoneering"]


def test_spirit_gems_and_voice_of_seren():
    from rune_advisor.models import Quest, Skill
    from rune_advisor.recommend.catalogue import bank_mix

    skills = {"Summoning": Skill("Summoning", 91, xp_for_level(91))}
    stock = {"Pouch": 500, "Spirit shards": 100_000, "Blue charm": 50, "Fire talisman": 500}
    plain = bank_mix(PlayerState(name="X", skills=skills, inventories={"BANK": stock}), "Summoning",
                     xp_for_level(91), 10**6, lambda lvl: 600_000)
    assert sum(n for out, _, n, _, _ in plain.runs if out == "Fire titan pouch") == 55  # charm-limited: 50 charms, 10% saved at the shrine
    # A spirit diamond (40 charges) saves 40 more blue charms. The Voice of Seren in Amlodd would add 20%,
    # but only to pouches infused in Amlodd itself: these are made at the Shrine of Inanna in Havenhythe
    # (+50%), so the Voice doesn't apply and the plan mustn't say to wait for it (user report, 2026-10-31).
    p = PlayerState(name="X", skills=skills, inventories={"BANK": {**stock, "Spirit diamond": 1}},
                    quests={"Plague's End": Quest("Plague's End", QuestStatus.COMPLETED, 2)})
    mix = bank_mix(p, "Summoning", xp_for_level(91), 10**6, lambda lvl: 600_000)
    titans = [(n, xp) for out, _, n, xp, _ in mix.runs if out == "Fire titan pouch"]
    assert titans[0][0] == 100 and mix.charms_saved == 40
    assert titans[0][1] == pytest.approx(100 * 695.2 * 1.5)
    assert mix.vos_clan is None
    steps = mix.describe("Summoning")
    assert "Shrine of Inanna" in steps[0] and "spirit gems" in steps[1]
    assert not any("Voice of Seren" in s for s in steps)


def test_drops_that_matter():
    from rune_advisor.drops import kills_per_hour, notable_drops, player_needs, sources_of
    from rune_advisor.resources import LevelPlan

    # Dragon pickaxe: a unique (few sources) the player doesn't own; uncut gems never are.
    assert any(src == "Chaos dwarf (Chaos Dwarf Battlefield)" for src, _, _ in sources_of("Dragon pickaxe"))
    row = {"method": "Chaos dwarves", "drop_sources": ["Chaos dwarf (Chaos Dwarf Battlefield)"]}
    p = PlayerState(name="X", ironman=True)
    kph = kills_per_hour(row, "Attack", 50_000)
    assert kph == pytest.approx(1000)  # 50 combat xp per kill
    needs = player_needs({"Summoning": LevelPlan(1.0, [], materials_unmodelled=True),
                          "Fletching": LevelPlan(1.0, [], materials_short={"Dragon dart tip": 100})})
    assert needs["Blue charm"] == "Summoning" and "Fletching" in needs["Dragon dart tip"]
    notes = {n.item: n for n in notable_drops(row, "Attack", 50_000, p, needs)}
    assert notes["Dragon pickaxe"].kind == "unique" and notes["Dragon pickaxe"].per_hour == pytest.approx(1000 / 3000)
    assert notes["Blue charm"].kind == "charm" and notes["Blue charm"].per_hour > 0
    assert not any(n.kind == "unique" and n.item.startswith("Uncut") for n in notes.values())
    # Owning it (bank, or ticked off in a boss-log achievement) removes it.
    p.inventories = {"BANK": {"Dragon pickaxe": 1}}
    assert "Dragon pickaxe" not in {n.item for n in notable_drops(row, "Attack", 50_000, p, needs)}


def test_gathering_costs_for_ironmen():
    from rune_advisor.models import Skill
    from rune_advisor.recommend.gathering import Gatherer

    p = PlayerState(name="X", ironman=True, skills={s: Skill(s, 99, xp_for_level(99)) for s in
                                                    ("Slayer", "Attack", "Strength", "Defence", "Smithing", "Fletching")})
    p.inventories = {"MONEY_POUCH": {"Coins": 1_000_000}}
    ctx = Context(player=p, cost=CostModel.default(player=p))
    g = Gatherer(ctx)
    # Pouches come from a shop (coins), pure essence from a spot the player can use (with Slayer xp).
    pouch = g.source("Pouch")
    assert pouch.kind == "shop" and pouch.coins_per_unit > 0
    plan = g.plan({"Pouch": 7_000, "Pure essence": 1_000})
    assert plan.hours > 0 and plan.coins == pytest.approx(7_000 * pouch.coins_per_unit)
    assert any("Pure essence" in s and s.startswith("spend") for s in plan.steps)
    assert sum(plan.xp.values()) > 0
    assert g.plan({"Item nobody has": 5}).short == {"Item nobody has": 5}


def test_joint_plan_shares_side_xp_and_items():
    from rune_advisor.models import Skill
    from rune_advisor.recommend.joint import _use_items, joint_plan

    p = PlayerState(name="X", ironman=True, skills={
        **{s: Skill(s, 99, xp_for_level(99)) for s in ("Attack", "Strength", "Defence", "Ranged", "Magic", "Hitpoints")},
        "Slayer": Skill("Slayer", 94, xp_for_level(94)), "Runecraft": Skill("Runecraft", 94, xp_for_level(94))})
    ctx = Context(player=p, cost=CostModel.default(player=p))
    plan = joint_plan(ctx, [("Slayer", 95), ("Runecraft", 95)])
    # Gathering pure essence for Runecrafting earns Slayer xp, so Runecraft goes first and Slayer gets cheaper.
    assert [s.skill for s in plan.steps] == ["Runecraft", "Slayer"]
    slayer = plan.steps[1]
    assert slayer.credited_xp > 0 and slayer.plan.hours < slayer.hours_alone
    assert plan.hours < plan.hours_alone
    assert p.xp("Slayer") == xp_for_level(94)  # the real player isn't changed

    # Items one goal uses up are gone for the next (bank first, then groupmates).
    q = PlayerState(name="Y", inventories={"BANK": {"Maple logs": 10}}, group={"Z": {"Maple logs": 5}})
    _use_items(q, {"Maple logs": 12})
    assert q.bank["Maple logs"] == 0 and q.group["Z"]["Maple logs"] == 3


def test_gear_picker():
    from rune_advisor.gear import best_loadout, owned_equipment
    from rune_advisor.models import Skill

    p = PlayerState(name="X", skills={s: Skill(s, 90, xp_for_level(90)) for s in ("Attack", "Ranged", "Magic", "Defence")},
                    inventories={"BANK": {"Augmented Sunspear (melee)": 1, "Abyssal whip": 1, "Amulet of fury (t)": 1,
                                          "Ferocious ring (5)": 1, "Fire cape": 1}})
    names = {i.name for i in owned_equipment(p)}
    # A free style-switch family: owning one Sunspear variant owns them all; charge suffixes are stripped.
    assert {"Augmented Sunspear (ranged)", "Augmented Sunspear (magic)", "Ferocious ring"} <= names
    melee = best_loadout(p, "melee")
    assert melee.slots["2h weapon"].name == "Augmented Sunspear (melee)"  # beats a lone whip
    assert melee.slots["neck"].name == "Amulet of fury (t)" and melee.slots["back"].name == "Fire cape"
    assert best_loadout(p, "ranged").slots["2h weapon"].name == "Augmented Sunspear (ranged)"
    # Level requirements apply.
    p.skills["Attack"] = Skill("Attack", 60, xp_for_level(60))
    assert "2h weapon" not in best_loadout(p, "melee").slots


def test_activity_costs_for_achievements():
    from rune_advisor.models import Achievement, Quest, Skill
    from rune_advisor.recommend.plans import achievement_plan

    p = PlayerState(name="X", skills={"Divination": Skill("Divination", 99, xp_for_level(99))},
                    quests={"Ode of the Devourer": Quest("Ode of the Devourer", QuestStatus.COMPLETED, 1)})
    p.achievements = {
        "Pull Yourself Together VI": Achievement("Pull Yourself Together VI", 5, False, requirements=[
            {"kind": "varTotal", "currentValue": 52, "totalValue": 100, "complete": False, "group": 0}]),
        "After the Flood": Achievement("After the Flood", 10, False, requirements=[
            {"kind": "varTotal", "currentValue": 0, "totalValue": 1, "description": "After the Flood",
             "complete": False, "group": 0}]),
        "Something Else": Achievement("Something Else", 10, False, requirements=[
            {"kind": "varTotal", "currentValue": 2, "totalValue": 4, "complete": False, "group": 0}]),
    }
    ctx = Context(player=p, cost=CostModel.default(player=p),
                  achievement_descriptions={"Pull Yourself Together VI": "Cleanse the Gate of Elidinis. (X/100)",
                                            "Something Else": "Complete a challenge."})
    gate = achievement_plan(ctx, "Pull Yourself Together VI")
    assert gate.hours == pytest.approx(48 / 24) and not gate.uncertain  # 48 cleanses at 24/h
    flood = achievement_plan(ctx, "After the Flood")
    assert flood.hours == pytest.approx(10 / 24, rel=0.01)  # a 1/10 drop
    other = achievement_plan(ctx, "Something Else")
    assert other.uncertain and other.hours >= 1.0  # unknown activities aren't free
    assert ("After the Flood", 10) in ctx.activities.achievements_at("The Gate of Elidinis")


def test_achievement_drops_need_a_real_chance_and_a_missing_item():
    """Bury All's item tests: a source counts only when it drops the item at a usable chance (Vyrewatch's
    mega-rare Hardened dragon bones, 1/2,796,203, don't), and not at all for bones already in the bank."""
    from rune_advisor.models import Achievement

    def bury(item):
        return {"kind": "varBit", "description": item, "complete": False, "group": 0}

    p = PlayerState(name="X")
    p.achievements = {"Bury All": Achievement("Bury All", 20, False, requirements=[
        bury("Hardened dragon bones"), bury("Monkey bones"), bury("Frost dragon bones")])}
    p.inventories = {"BANK": {"Frost dragon bones": 3}}
    acts = Context(player=p, cost=CostModel.default(player=p), achievement_descriptions={}).activities
    assert ("Bury All", 20) not in acts.achievements_at("Vyrewatch (Darkmeyer)")  # 1/2.8m: no route
    assert ("Bury All", 20) in acts.achievements_at("Monkey")  # always drops monkey bones
    assert ("Bury All", 20) not in acts.achievements_at("Frost dragon")  # held: just bury one


def test_item_prerequisites():
    from rune_advisor.requirements import items_named, missing_items

    assert items_named("Ghosts Ahoy (ectophial)") == ["Ectophial"]
    assert "Protean trap" in items_named("Protean traps; no drops from trapped jadinkos")
    assert items_named("Morytania legs 4 recommended") == []
    p = PlayerState(name="X", inventories={"BANK": {"Mithril grapple": 1}})
    assert missing_items("Enhanced grappling hook or mithril grapple", p) == []  # either will do
    assert missing_items("Protean traps", p) == ["Protean trap"]


def test_gear_matches_tiered_items_by_id():
    from rune_advisor.gear import best_loadout
    from rune_advisor.models import Skill

    p = PlayerState(name="X", skills={"Necromancy": Skill("Necromancy", 99, xp_for_level(99))})
    # The game calls it "Death guard"; the wiki page (and stats) is "Death guard (tier 90)".
    apply_plugin_snapshot(p, {"inventories": {"BANK": {"items": [
        {"id": 55540, "name": "Death guard", "count": 1}, {"id": 55542, "name": "Skull lantern", "count": 1},
        {"id": 56142, "name": "Deathwarden nexus", "count": 0}]}}})
    lo = best_loadout(p, "necromancy")
    assert lo.slots["main hand weapon"].name == "Death guard (tier 90)"
    assert lo.slots["off-hand weapon"].name == "Skull lantern (tier 90)"


def test_roadmap_merges_shared_needs():
    from rune_advisor.models import Achievement, Skill
    from rune_advisor.recommend.roadmap import roadmap

    p = PlayerState(name="X", ironman=True, skills={"Agility": Skill("Agility", 90, xp_for_level(90)),
                                                    "Thieving": Skill("Thieving", 95, xp_for_level(95))})
    ach = lambda name, reqs, done=False: Achievement(name, 10, done, requirements=reqs)  # noqa: E731
    sub = lambda name, done=False: {"kind": "achievement", "achievement": name, "complete": done, "group": 0}  # noqa: E731
    p.achievements = {
        "Big Cape": ach("Big Cape", [sub("Master Skiller"), sub("Agility Thing"), sub("Done Already", True)]),
        "Master Skiller": ach("Master Skiller", [sub("Agility 99"), sub("Thieving 99")]),
        "Agility 99": ach("Agility 99", [{"kind": "varTotal", "currentValue": 0, "totalValue": 1, "complete": False, "group": 0}]),
        "Thieving 99": ach("Thieving 99", [{"kind": "varTotal", "currentValue": 0, "totalValue": 1, "complete": False, "group": 0}]),
        # Another achievement that also needs Agility, at a lower level: covered by the 99.
        "Agility Thing": ach("Agility Thing", [{"kind": "skill", "skills": ["AGILITY"], "totalLevels": 95, "complete": False,
                                                "group": 0},
                                               {"kind": "varTotal", "currentValue": 1, "totalValue": 4, "complete": False, "group": 0}]),
        "Done Already": ach("Done Already", [], done=True),
    }
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False))
    rm = roadmap(ctx, "Big Cape")
    assert rm.skills == {"Agility": 99, "Thieving": 99}  # merged: Agility's highest need only
    assert {s.skill for s in rm.joint.steps} == {"Agility", "Thieving"}
    assert [a.achievement for a in rm.activities] == ["Agility Thing"] and rm.activities[0].plan.hours >= 1
    assert rm.hours == pytest.approx(rm.skill_hours + rm.activity_hours)
    assert rm.joint.player.level("Agility") == 99 and p.level("Agility") == 90


def test_herbs_can_be_farmed():
    from rune_advisor.models import Skill
    from rune_advisor.recommend.gathering import Gatherer

    p = PlayerState(name="X", ironman=True, skills={"Farming": Skill("Farming", 99, xp_for_level(99))},
                    inventories={"BANK": {"Torstol seed": 100}})
    ctx = Context(player=p, cost=CostModel.default(player=p))
    g = Gatherer(ctx)
    src = g.source("Grimy torstol")
    assert src.kind == "farm" and src.calendar_per_unit > 0
    plan = g.plan({"Grimy torstol": 630})  # 10 runs of 9 patches x 7
    assert plan.calendar_hours == pytest.approx(630 * src.calendar_per_unit)
    assert plan.xp["Farming"] > 0 and any("herb patch runs" in s for s in plan.steps)


def test_custom_methods_from_settings():
    from rune_advisor.models import Skill
    from rune_advisor.recommend.gathering import Gatherer

    method = {"skill": "Mining", "method": "Exalted Quarry essence chunks", "level_min": 85, "xp_per_hour": 90_000,
              "yields": {"Essence chunk": 400}, "side_xp": {"Runecraft": 20_000}}
    p = PlayerState(name="X", ironman=True, skills={"Mining": Skill("Mining", 99, xp_for_level(99)),
                                                    "Runecraft": Skill("Runecraft", 99, xp_for_level(99))})
    cost = CostModel.default(player=p, extra_methods=[method])
    assert any(m["method"] == method["method"] for m in cost.available_methods("Mining", 99))
    ctx = Context(player=p, cost=cost)
    # It's a gathering source for what it yields, with both skills' xp on the side...
    g = Gatherer(ctx).plan({"Essence chunk": 800})
    assert g.hours == pytest.approx(2.0) and g.xp == {"Mining": 180_000, "Runecraft": 40_000}
    # ...and when it's the Mining method, its side xp counts toward Runecrafting.
    plan = ctx.level_plan("Mining", 100)
    if cost.available_methods("Mining", 99)[0]["method"] == method["method"]:
        assert plan.byproduct_xp.get("Runecraft", 0) > 0


def test_reputation_and_campfire():
    from rune_advisor import reputation
    from rune_advisor.drops import campfire_needs, player_needs
    from rune_advisor.models import Achievement, Skill
    from rune_advisor.recommend.plans import achievement_plan

    rep_test = {"kind": "varTotal", "currentValue": 900_000, "totalValue": 3_000_000, "complete": False, "group": 0}
    p = PlayerState(name="X", ironman=True, skills={"Woodcutting": Skill("Woodcutting", 99, xp_for_level(99)),
                                                    "Firemaking": Skill("Firemaking", 99, xp_for_level(99))},
                    achievements={"Reputation - Class Crown": Achievement("Reputation - Class Crown", 40, False,
                                                                          requirements=[rep_test])})
    assert reputation.remaining(p, "Imperial") == 2_100_000
    # Cutting acadia in Menaphos earns Imperial reputation, which makes it cheaper net of by-products.
    ctx = Context(player=p, cost=CostModel.default(player=p))
    src = ctx.gatherer.source("Acadia logs")
    assert src.rep == {"Imperial": 4.5} and ctx.gatherer.net_cost(src) < src.cost_per_unit
    g = ctx.gatherer.plan({"Acadia logs": 1000})
    assert g.reputation == {"Imperial": 4_500}
    # The achievement is costed from reputation, not as 2.1m flat tallies.
    plan = achievement_plan(ctx, "Reputation - Class Crown")
    assert plan.hours == pytest.approx(2_100_000 / reputation.BEST_REP_PER_HOUR)
    assert reputation.add(p, "Imperial", 5_000_000) == 2_100_000 and p.achievements["Reputation - Class Crown"].completed

    # Campfire upgrades above the unlocked tier (and within Firemaking level) need 1,000 logs each.
    p.campfire_tier = "magic"
    assert campfire_needs(p) == {"Elder logs": 1000}
    assert "campfire" in player_needs({}, p)["Elder logs"]


def test_skilling_simulation():
    from rune_advisor.models import Skill
    from rune_advisor.sim import data, gather_rate, mining_rate, woodcutting_chance, woodcutting_rates

    # Wiki example (Woodcutting#Mechanics): yew, 75 Woodcutting, 700-power hatchet -> ~24.5% (24.42 with
    # the module's integer maths).
    yew = next(t for t in data()["trees"] if t["logs"] == "Yew logs")
    assert woodcutting_chance(yew, 700, 75) == pytest.approx(0.2442, abs=0.0005)
    assert woodcutting_chance(yew, 700, 75, [1.07]) > woodcutting_chance(yew, 700, 75)  # outfit boost
    p = PlayerState(name="X", skills={"Woodcutting": Skill("Woodcutting", 75, xp_for_level(75)),
                                      "Mining": Skill("Mining", 60, xp_for_level(60)),
                                      "Strength": Skill("Strength", 60, xp_for_level(60))},
                    inventories={"BANK": {"Rune hatchet": 1}})
    rates = {r.item: r for r in woodcutting_rates(p)}
    assert rates["Yew logs"].tool == "Rune hatchet" and not rates["Yew logs"].assumed_tool
    assert rates["Willow logs"].items_per_hour > rates["Yew logs"].items_per_hour
    # Mining: copper with a mithril pickaxe at 30/30 (wiki: 48-78 damage per swing vs 40 durability): an
    # ore every swing at full stamina.
    copper = next(r for r in data()["rocks"] if r["ore"] == "Copper ore")
    mithril = next(pk for pk in data()["picks"] if pk["label"] == "Mithril")
    ore, xp = mining_rate(copper, mithril, 30, 30, "4-Ticking")
    assert ore >= 1500 and xp > 0
    skill, r = gather_rate(p, "Yew logs")
    assert skill == "Woodcutting" and r.items_per_hour > 0


def test_remnant_energy_converts_to_divine_energies():
    from rune_advisor.recommend.catalogue import catalogue

    conv = {r["output"]: r for r in catalogue() if r["materials"] == [["Remnant energy", 150.0]]}
    assert conv["Incandescent energy"]["output_qty"] == 100 and conv["Incandescent energy"]["level"] == 95
    assert conv["Lustrous energy"]["output_qty"] == 200 and all(r["conversion"] for r in conv.values())


def test_exalted_quarry_nodes():
    from rune_advisor import sim
    from rune_advisor.models import Skill

    p = PlayerState(name="X", members=True, skills={"Mining": Skill("Mining", 99, xp_for_level(99))})
    rates = {r.activity: r for r in sim.mining_rates(p)}
    # The colossus converts carried essence (catalogue conversions); it isn't an ore rock.
    assert "Exalted colossus" not in rates
    # One crack (the infobox hp), then 46 chunks: far more than one hp-worth of swings per chunk.
    one_per_crack = sim.data()["mining"]["swing_hr"] / (29000 / 100)
    assert rates["Essence deposit"].items_per_hour > 10 * one_per_crack / 46


def test_prices_value_untradeables_by_processing():
    from rune_advisor.prices import Prices

    p = Prices(prices={"Exalted essence": 285, "Pure essence": 19, "%LAST_UPDATE_F%": "now"},
               volumes={"Exalted essence": 400_000})
    chunk = p.value("Essence chunk")  # colossus conversion, then 9 exalted essence at the processor
    assert chunk.gp == 9 * 285 and "Exalted essence" in chunk.how
    gp, notes = p.gp_per_hour({"Essence chunk": 100, "Mystery item": 5})
    assert gp == 100 * 9 * 285 and any("no price" in n for n in notes)


def test_multi_item_rocks():
    from rune_advisor import sim
    from rune_advisor.models import Skill

    p = PlayerState(name="X", members=True, skills={"Mining": Skill("Mining", 90, xp_for_level(90))})
    granite = next(r for r in sim.mining_rates(p) if r.activity == "Granite")
    y = granite.yields()
    assert set(y) == {"Granite (500g)", "Granite (2kg)", "Granite (5kg)"}
    assert sum(y.values()) == pytest.approx(granite.items_per_hour)
    # Asking for one size charges the rock's xp to it alone.
    _, small = sim.gather_rate(p, "Granite (500g)")
    assert small.items_per_hour == pytest.approx(0.33 * granite.items_per_hour)
    assert next(r for r in sim.mining_rates(p) if r.activity == "Red sandstone (Oo'glog)").item == "Red sandstone"


def _skilled(levels: dict, **kw) -> PlayerState:
    from rune_advisor.models import Skill

    return PlayerState(name="X", members=True, skills={s: Skill(s, v, xp_for_level(v)) for s, v in levels.items()}, **kw)


def test_trips_and_mining_boosts():
    from rune_advisor import sim
    from rune_advisor.mechanics import trips

    bare = _skilled({"Mining": 80, "Strength": 99})
    geared = _skilled({"Mining": 80, "Strength": 99}, inventories={"BANK": {"Varrock armour 4": 1, "Bane ore box": 1}})
    geared.boosts = {"perks": {"pickaxe": ["Fortune 3"]}}
    plain = {r.activity: r for r in sim.mining_rates(bare)}
    boosted = {r.activity: r for r in sim.mining_rates(geared)}
    # Varrock armour 4 covers banite (a core rock) and Fortune adds double ore: more ore, same xp.
    assert boosted["Banite"].raw_items_per_hour > plain["Banite"].raw_items_per_hour
    assert boosted["Banite"].xp_per_hour == pytest.approx(plain["Banite"].xp_per_hour)
    # Bank trips: the ore box (120 banite at 80 Mining) means fewer trips.
    assert trips.ore_box_capacity(geared, "Banite ore", 79) == 120
    assert boosted["Banite"].items_per_hour / boosted["Banite"].raw_items_per_hour > \
        plain["Banite"].items_per_hour / plain["Banite"].raw_items_per_hour
    # The xp model is near the money making guide (banite at 80: 120k xp/h with its pickaxe and boosts).
    assert 90_000 < plain["Banite"].xp_per_hour < 130_000


def test_sims_replace_guide_rows():
    from rune_advisor.cost import replaced_by_sims

    rows = [{"skill": "Woodcutting", "method": "Maple trees", "level_min": 45},
            {"skill": "Woodcutting", "method": "Golden bamboo", "level_min": 90},
            {"skill": "Woodcutting", "method": "Maple Tree (simulated)", "activity": "Maple Tree",
             "basis": "simulated", "level_min": 45}]
    kept, replaced = replaced_by_sims(rows)
    assert [r["method"] for r in kept] == ["Golden bamboo", "Maple Tree (simulated)"]
    assert replaced == [("Woodcutting", "Maple trees", "Maple Tree")]


def test_fishing_and_thieving_sims():
    from rune_advisor.mechanics import fishing, thieving

    p = _skilled({"Fishing": 99, "Thieving": 99})
    spots = {s["spot"]: s for s in fishing.spot_rates(p)}
    for s in spots.values():
        assert sum(pr for _, pr, _, _ in s["fish"]) <= 1 + 1e-9  # rolled in order: at most one catch
    trout = spots["Lure/bait: lure"]
    assert {i for i, *_ in trout["fish"]} >= {"Raw trout", "Raw salmon"}
    # Pickpocketing: 0.75 + 1% per effective level over the requirement (+25 for level 51+ targets).
    t = {x["target"]: x for x in thieving.target_rates(p)}
    assert t["Man/Women"]["chance"] == 1.0 and t["Man/Women"]["per_hour"] == pytest.approx(2000)


def test_magic_processing_and_smithing_sims():
    from rune_advisor.mechanics import magic, processing, smithing

    p = _skilled({"Magic": 99, "Runecraft": 91, "Cooking": 60, "Smithing": 90, "Firemaking": 90})
    hla = next(s for s in magic.spells(p, 99) if s["name"] == "High Level Alchemy")
    assert hla["xp_per_hour"] == pytest.approx(78_000)  # 5-tick cast, the wiki's 1,200 casts/h
    assert processing.runes_per_essence("Nature rune", 91) == 2
    shark = {"skill": "Cooking", "output": "Shark", "level": 80, "noburn": "Never", "gauntlets": "94",
             "xp": 210, "output_qty": 1, "ticks": 4, "materials": [["Raw shark", 1]]}
    assert processing.cooking_success(p, shark) == processing.BURN_FLOOR  # below the recipe's level
    chicken = {**shark, "output": "Cooked chicken", "level": 1, "noburn": "34"}
    assert processing.cooking_success(p, chicken) == 1.0  # its success chart (128/512) is certain by level 60
    elder = max((f for f in smithing.forging(p, 90) if f["metal"] == "Elder rune"), key=lambda f: f["xp_per_hour"])
    assert elder["item"].endswith("+ 5") and 200_000 < elder["xp_per_hour"] < 320_000  # guide: ~250k at high heat


def test_rituals_farming_archaeology_sims():
    from rune_advisor.mechanics import archaeology, farming, rituals
    from rune_advisor.recommend.gathering import seeds

    p = _skilled({"Necromancy": 99, "Farming": 99, "Archaeology": 98})
    r = {x["ritual"]: x for x in rituals.ritual_rates(p)}
    assert r["Lesser communion"]["materials"] and r["Lesser communion"]["xp_per_hour"] > 0
    ranarr = next(s for s in seeds() if s["seed"] == "Ranarr seed")
    assert 6 < farming.expected_yield(p, ranarr) < 9  # 3 lives + supercompost, keep chance at 99
    p.tools["mattock"] = "Elder rune"
    golem = next(h for h in archaeology.hotspot_rates(p) if h["hotspot"] == "Destroyed golem")
    assert golem["mattock"].startswith("Elder Rune mattock,") and "mattock, assumed" not in golem["mattock"]
    assert golem["xp_per_hour"] > 20_000  # excavation only: restoring is a workbench recipe


def test_ritual_alteration_glyphs():
    from rune_advisor.mechanics import rituals
    from rune_advisor.mechanics import data as mech_data

    p = _skilled({"Necromancy": 100})
    bare = {r["ritual"]: r for r in rituals.ritual_rates(p, alterations=False)}
    assert bare["Powerful essence"]["glyphs"] == "no alteration glyphs"
    p.inventories["BANK"] = {"Alteration necklace": 1, "Underworld Grimoire 3": 1, "Hooded necromancy cape (t)": 1}
    g = rituals.gear(p, 100)
    assert (g.necklace, g.kayazu, g.grimoire, g.cape) == (True, False, 3, True)
    ritual = mech_data()["rituals"]["rituals"]["Powerful essence"]
    # Three Speed II glyphs with the necklace: -10% x 1.2 each = -36%, so 83 ritual ticks become ceil(83 x 0.64) = 54.
    fast = rituals.evaluate("Powerful essence", ritual, 100, {"Speed II": 3}, None, g)
    assert fast["ticks"] == 54 and fast["per_hour"] > bare["Powerful essence"]["per_hour"]
    assert sum(fast["materials"].values()) > sum(bare["Powerful essence"]["materials"].values())  # the glyphs' inks
    # Multiply glyphs raise the outputs; the grimoire's +10% only applies to necroplasm rituals.
    more = rituals.evaluate("Powerful essence", ritual, 100, {"Multiply II": 2}, None, g)
    assert all(more["outputs"][k] >= v for k, v in bare["Powerful essence"]["outputs"].items())
    necro = mech_data()["rituals"]["rituals"]["Powerful necroplasm"]
    plain = rituals.evaluate("Powerful necroplasm", necro, 100, {}, None, rituals.Gear())
    grim = rituals.evaluate("Powerful necroplasm", necro, 100, {}, None, g)
    assert grim["outputs"]["Powerful necroplasm"] > plain["outputs"]["Powerful necroplasm"]
    assert grim["outputs"]["Ectoplasm"] == plain["outputs"]["Ectoplasm"]
    # The best loadout beats the bare ritual and is named in the method rows.
    best = {r["ritual"]: r for r in rituals.ritual_rates(p)}
    assert best["Powerful essence"]["xp_per_hour"] > bare["Powerful essence"]["xp_per_hour"]
    assert "Speed II" in best["Powerful essence"]["glyphs"]
    assert any("Speed II" in r["method"] for r in rituals.method_rows(p))


def test_invention_discoveries_are_xp_in_hand():
    from rune_advisor.invention import discoveries, potion_boost
    from rune_advisor.models import Achievement, Skill
    from rune_advisor.recommend.tracks.free_xp import FreeXpTrack

    tests = [{"var": {"name": "INVENT_BLUEPRINT_UNLOCKED_0"}, "kind": "varBit", "description": d, "complete": c}
             for d, c in (("Gizmo", True), ("Charge pack", True), ("Oceanic components", False),  # lvl 77
                          ("Ancient gizmos", False), ("Alchemical onyx", False), ("Kinetic cyclone", False),
                          ("Made-up blueprint", False))]
    p = PlayerState(name="X", skills={"Invention": Skill(name="Invention", level=70, xp=8_500_000)},
                    inventories={"BANK": {"Extreme invention (3)": 91}},
                    achievements={"Draught Includer": Achievement(name="Draught Includer", runescore=0, completed=False,
                                                                  requirements=tests)})
    assert potion_boost(p) == ("Extreme invention", 17)
    d = discoveries(p)
    assert d.level == 87 and [b["name"] for b in d.ready] == ["Oceanic components"]  # 77 <= 87, not gated
    assert {b["name"] for b in d.gated} == {"Ancient gizmo", "Kinetic cyclone"}  # 85 and 70, footnoted
    # A complete dwarven tech tree opens its devices; the achievement for it says so.
    p.achievements["Invention Gives Me the Blues"] = Achievement(
        name="Invention Gives Me the Blues", runescore=0, completed=False,
        requirements=[{"description": "Dwarven", "complete": True}, {"description": "Goblin", "complete": False}])
    assert {b["name"] for b in discoveries(p).ready} == {"Kinetic cyclone", "Oceanic components"}
    assert [b["name"] for b in d.locked] == ["Alchemical onyx"] and d.unknown == ["Made-up blueprint"]  # 107
    p.inventories["BANK"] = {}
    assert discoveries(p).level == 70 and potion_boost(p) == (None, 0)

    class Ctx:
        player, resources = p, None

        def level_plan(self, skill, level):
            return type("LP", (), {"hours": 10.0})()

    p.achievements.pop("Invention Gives Me the Blues")
    p.inventories["BANK"] = {"Super invention (4)": 1}  # +5: Invention 75, short of Oceanic components' 77
    assert FreeXpTrack.candidates(Ctx()) == []
    p.inventories["BANK"] = {"Extreme invention (3)": 91}
    [c] = FreeXpTrack.candidates(Ctx())
    assert c.title.startswith("Discover 1 Invention blueprints") and "Extreme invention" in c.title and c.value > 0


def test_combat_formulas():
    from rune_advisor.mechanics import combat

    # Hit chance page examples: 90 x 2,905 / floor(1,608 + f(70)) caps at 100%; 50 x 4,068 / 2,843 = 71.5%.
    assert combat.hit_chance(2905, {"armour": 1608, "defence_level": 70, "melee_affinity": 90}, "melee") == 1.0
    assert combat.hit_chance(4068, {"armour": 2074, "defence_level": 80, "melee_affinity": 50}, "melee") == \
        pytest.approx(0.715, abs=0.001)
    # Ability damage: dual wield = main + half an off-hand; a two-hander uses 1.5x the bonus.
    mh = combat.ability_damage(99, main=90, bonus=0)
    assert combat.ability_damage(99, main=90, off=90) == mh + mh // 2
    assert combat.ability_damage(99, two_h=90, bonus=10) > combat.ability_damage(99, two_h=90)
    assert combat.crit_bonus(90) == pytest.approx(0.5) and combat.crit_bonus(1) == pytest.approx(0.1)


def test_measured_sessions_replace_sims_and_guides():
    from collections import Counter

    from rune_advisor.cost import replaced_by_sims
    from rune_advisor.sessions import Session

    s = Session(player="X", number=3, label=None, seconds=3600, ended=True, xp={"Mining": 400_000},
                gained={"Essence chunk": 500}, used={}, start_levels={"Mining": 99}, worn=[],
                actions=Counter({("Mine", "Essence deposit"): 40, ("Bank", "Bank chest"): 3}))
    row = s.method()
    assert row["activity"] == "Essence deposit" and row["yields"]["Essence chunk"] == 500
    rows = [{"skill": "Mining", "method": "Essence deposit (simulated)", "activity": "Essence deposit", "basis": "simulated",
             "level_min": 85},
            {"skill": "Mining", "method": "Mining essence deposits", "level_min": 85},
            {"skill": "Mining", "method": "Runite (simulated)", "activity": "Runite", "basis": "simulated", "level_min": 50},
            {**row, "basis": "measured"}]
    kept, replaced = replaced_by_sims(rows)
    assert [r["method"] for r in kept] == ["Runite (simulated)", row["method"]]
    assert {m for _, m, _ in replaced} == {"Essence deposit (simulated)", "Mining essence deposits"}


def test_tool_gated_conversions():
    from rune_advisor.recommend.catalogue import _usable, catalogue

    desert = next(r for r in catalogue() if r["output"] == "Molten glass" and "Desert amulet 4" in r.get("tools", []))
    assert desert.get("daily")
    assert not _usable(desert, PlayerState(name="X"))
    assert _usable(desert, PlayerState(name="X", inventories={"BANK": {"Desert amulet 4": 1}}))
    lit = next(r for r in catalogue() if r["output"] == "Adamant fire arrows (lit)" and r["skill"] == "")
    assert _usable(lit, PlayerState(name="X"))  # a tinderbox is on the tool belt


def test_swing_stalls():
    from rune_advisor.sessions import Session, swings

    # Logs on a 4-tick grid from a click at 1.5s: short gaps, plus a 16-swing gap right after a wood box fill.
    swing_s = 2.4
    steps = [1, 2, 1, 3, 1, 2, 2, 1, 4, 1, 2, 1, 16, 2, 1, 3, 1, 2]
    times, t = [], 1.5
    for n in steps:
        t += n * swing_s
        times.append(round(t, 1))
    s = Session(player="X", number=1, label=None, seconds=times[-1] + 5, ended=True, xp={}, gained={}, used={},
                start_levels={}, worn=[], chat=[(x, "SPAM", "You get some elder logs.") for x in times],
                clicks_seq=[(1.5, "Chop down", "Elder tree"), (times[11] + 1.0, "Fill", "Elder wood box")])
    sw = swings(s)
    assert sw.interval_ticks == 4 and sw.swings == sum(steps) and sw.successes == len(steps)
    assert [(st.swings, st.after.split(" (")[0]) for st in sw.stalls] == [(16, "Fill Elder wood box")]
    assert sw.chance == pytest.approx((len(steps) - 1) / (sum(steps) - 16))


def test_var_containers():
    from rune_advisor.player import var_containers

    got = var_containers({"DINO_HERB_BAG_DWARF_WEED": 12, "DINO_HERB_BAG_UPGRADED": 1, "DINO_HERB_BAG_GUAM": 0,
                          "ARCH_SOIL_BOX_AERATED_SEDIMENT": 40, "ARCH_SOIL_BOX_UPGRADE": 2,
                          "RAND_REWARD_GEM_BAG_UPGRADE_SAPPHIRE_100": 2, "RAND_REWARD_GEM_BAG_UPGRADE_SAPHIRE": 2,
                          "INVENT_MATERIAL_BASE_PARTS": 3100, "INVENT_MATERIAL_ARMADYL_COMPONENTS": 4,
                          "INVENT_MATERIAL_JUNK": 900, "INVENT_MATERIAL_CLEAR_PARTS": 0,
                          "CRAMULET_HAS_CATSPEAK_ENCHANTED": 1, "CRAMULET_HAS_CATSPEAK": 1,  # both set once enchanted
                          "CRAMULET_HAS_CAMULET": 0, "CRAMULET_CAN_RECLAIM": 1})
    assert got == {"HERB_BAG": {"Grimy dwarf weed": 12}, "SOIL_BOX": {"Aerated sediment": 40},
                   "GEM_BAG": {"Uncut sapphire": 2},
                   "MATERIALS_STORAGE": {"Base parts": 3100, "Armadyl components": 4, "Junk": 900},
                   "CRAMULET": {"Catspeak amulet (e)": 1}}


def test_last_rock_skill_halves_the_next_rock():
    from rune_advisor.player import last_rock_skills, rock_halved_skills
    from rune_advisor.recommend.base import Candidate
    from rune_advisor.recommend.guide import tags_of

    assert last_rock_skills({"STATUE_LAST_SKILL": 9, "ELFCITY_STATUE_LAST_SKILL": 8}) == {"strange": "Herblore", "golden": "Agility"}
    assert last_rock_skills({"ELFCITY_STATUE_LAST_SKILL": 99}) == {"golden": "code 99"}  # golden-only skills: codes not seen yet
    assert rock_halved_skills(PlayerState(name="X", last_rock_skill={"golden": "code 99"})) == set()
    p = PlayerState(name="X", last_rock_skill={"strange": "Woodcutting", "golden": "Melee"})
    assert rock_halved_skills(p) == {"Woodcutting", "Attack", "Strength", "Defence"}
    # Both Woodcutting strange rocks in the bag (bits 20 and 21): no penalty worth noting for that skill.
    p.vars = {"STATUE_BAG_CLIENT": (1 << 20) | (1 << 21)}
    assert rock_halved_skills(p) == {"Attack", "Strength", "Defence"}
    c = Candidate(track="milestones", title="Attack 99", value=1, cost_hours=5, skills={"Attack"})
    assert "half-rocks" in tags_of(c, halved=rock_halved_skills(p))
    assert "half-rocks" not in tags_of(c, halved=set())


def test_collection_log_counts_as_owned():
    from rune_advisor.drops import owned

    p = PlayerState(name="X", item_log={"Dragon axe": 2})
    assert owned(p, "Dragon axe") and not owned(p, "Abyssal whip")


def test_statue_bag_bits():
    from rune_advisor.player import statue_bag

    # Readings from 2026-10-29: the bag before, and after adding Agility rock 2 and both Herblore rocks.
    before = statue_bag({"STATUE_BAG_CLIENT": 1022623217, "ELFCITY_PERM6": -52576001})
    assert "Strange rock (Agility 1)" in before and "Strange rock (Agility 2)" not in before
    assert not any("Farming" in r or "Firemaking" in r or "Herblore" in r for r in before if r.startswith("Strange"))
    assert "Golden rock (Melee 1)" in before and "Golden rock (Melee 2)" not in before
    assert not any("Slayer" in r for r in before)
    after = statue_bag({"STATUE_BAG_CLIENT": 1039924735, "ELFCITY_PERM6": 1114112})
    assert {"Strange rock (Agility 2)", "Strange rock (Herblore 1)", "Strange rock (Herblore 2)"} <= set(after)
    # After 10 pairs went into the statue (session 11): only Melee 1 and Prayer 1 are left.
    assert {r for r in after if r.startswith("Golden")} == {"Golden rock (Melee 1)", "Golden rock (Prayer 1)"}
    # Plugin 0.4.3 sends the per-rock flags instead of ELFCITY_PERM6 (the game's own fields of that variable); they
    # win over it when both are there.
    flags = {f"ELFCITY_ROCK_{s}{n}_STORED": 0 for s in ("MELEE", "PRAYER", "SLAYER") for n in (1, 2)}
    flags |= {"ELFCITY_ROCK_MELEE1_STORED": 1, "ELFCITY_ROCK_PRAYER1_STORED": 1, "ELFCITY_PERM6": -52576001}
    assert {r for r in statue_bag(flags) if r.startswith("Golden")} == {"Golden rock (Melee 1)", "Golden rock (Prayer 1)"}
    from rune_advisor.player import golden_rocks_var
    assert golden_rocks_var({"ELFCITY_ROCK_HERBLORE1_STORED": 1}) == 1 << 12  # bit 12, as ELFCITY_PERM6 has it
    assert golden_rocks_var({"ELFCITY_PERM6": 1114112}) == 1114112 and golden_rocks_var({}) is None


def test_free_xp_golden_rock_pairs():
    from rune_advisor.models import Skill
    from rune_advisor.recommend.tracks.free_xp import FreeXpTrack, golden_rock_xp

    assert golden_rock_xp(96) == 10948 and golden_rock_xp(104) == 12849  # measured 2026-10-29
    p = PlayerState(name="X", vars={"ELFCITY_PERM6": -52576001},  # 10 full pairs + Melee 1 + Prayer 1
                    skills={s: Skill(name=s, level=96, xp=10_000_000) for s in
                            ("Agility", "Construction", "Crafting", "Divination", "Magic", "Mining", "Ranged",
                             "Smithing", "Summoning", "Woodcutting", "Prayer", "Attack")})

    class Cost:
        def rate(self, skill, level):
            return 100_000.0

    ctx = type("Ctx", (), {"player": p, "cost": Cost()})()
    [c] = FreeXpTrack.candidates(ctx)
    assert c.title.startswith("Place 10 golden rock pairs") and c.value == round(10 * 10948 / 100_000, 2)
    # Strange rocks: a pair in the bank (the bag wasn't carried) plus one held in the bag and one in the backpack.
    from rune_advisor.player import held_rocks
    from rune_advisor.recommend.tracks.free_xp import strange_rock_xp
    assert strange_rock_xp(96) == 9124 and strange_rock_xp(100) == 9900  # measured 2026-10-31
    p.inventories = {"BANK": {"Strange rock (Herblore)": 2, "Golden rock (Melee)": 1}, "INV": {"Strange rock (Agility)": 1}}
    p.vars["STATUE_BAG_CLIENT"] = 1  # Agility rock 1 in the bag
    p.skills["Herblore"] = Skill(name="Herblore", level=100, xp=15_000_000)
    held = held_rocks(p)
    assert held["Strange"] == {"Herblore": 2, "Agility": 2} and held["Golden"]["Melee"] == 2
    golden, strange = FreeXpTrack.candidates(ctx)
    assert strange.title == "Place 2 strange rock pairs on the Statue of Dahmaroc"
    assert strange.value == round((9900 + strange_rock_xp(96)) / 100_000, 2)
    assert golden.title.startswith("Place 11 golden rock pairs")  # the bank's Melee rock completes that pair


def test_quest_reward_hours():
    from rune_advisor.models import Skill
    from rune_advisor.recommend.plans import quest_reward_hours

    # Ritual of the Mahjarrat (wiki): set xp in Agility/Crafting/Mining/Prayer plus 240k in any skill at 72+.
    p = PlayerState(name="X", skills={s: Skill(name=s, level=80, xp=2_000_000) for s in
                                       ("Agility", "Crafting", "Mining", "Prayer", "Herblore")})

    class Cost:
        def rate(self, skill, level):
            return 20_000.0 if skill == "Herblore" else 100_000.0  # Herblore is the slow one

    ctx = type("Ctx", (), {"player": p, "cost": Cost()})()
    hours, xp = quest_reward_hours(ctx, "Ritual of the Mahjarrat")
    assert xp == 110_000 + 40_000 + 40_000 + 3_000 + 240_000
    assert hours == (193_000 / 100_000) + 240_000 / 20_000  # the any-skill choice goes to slow Herblore
    assert quest_reward_hours(ctx, "No such quest") == (0.0, 0.0)


def test_task_set_lamps():
    from rune_advisor.models import Skill
    from rune_advisor.recommend.plans import achievement_xp, reward_hours

    lamps = achievement_xp()["Ardougne Set Tasks - Elite"]["choice"]
    assert len(lamps) == 4 and all(l["xp"] == 50_000 and l["min_level"] == 85 for l in lamps)
    # Below the lamps' level 85 nothing is usable; at 90 each lamp is 50k xp.
    p = PlayerState(name="X", skills={"Agility": Skill(name="Agility", level=80, xp=2_000_000)})
    ctx = type("Ctx", (), {"player": p, "cost": type("C", (), {"rate": lambda self, s, l: 100_000.0})()})()
    assert reward_hours(ctx, achievement_xp()["Ardougne Set Tasks - Elite"]) == (0.0, 0.0)
    p.skills["Agility"] = Skill(name="Agility", level=90, xp=5_400_000)
    assert reward_hours(ctx, achievement_xp()["Ardougne Set Tasks - Elite"]) == (2.0, 200_000)


def test_elite_outfit_states(tmp_path):
    from rune_advisor.models import Skill
    from rune_advisor.outfits import fragments_per_hour, outfit_state, outfits, seen_pieces

    wc = outfits()["Woodcutting"]
    p = PlayerState(name="X", skills={"Woodcutting": Skill(name="Woodcutting", level=96, xp=10_000_000),
                                      "Thieving": Skill(name="Thieving", level=103, xp=22_000_000),
                                      "Runecraft": Skill(name="Runecraft", level=94, xp=8_000_000)},
                    vars={"CURRENCY_FRAGMENTS_WOODCUTTING": 0, "CURRENCY_FRAGMENTS_CAMOUFLAGE": 7_300,
                          "CURRENCY_FRAGMENTS_ETHEREAL": 0},
                    inventories={"WORN": {piece: 1 for piece in wc["combined"]["pieces"]},
                                 "BANK": {outfits()["Thieving"]["components"][0]["pieces"][0]: 1}})
    seen = seen_pieces(p, tmp_path / "seen.json")
    assert outfit_state(p, "Woodcutting", seen).status == "complete"
    # Zero fragments with nothing seen: the game deletes them once the combined outfit exists.
    assert outfit_state(p, "Runecrafting", seen).status == "likely complete"
    thieving = outfit_state(p, "Thieving", seen)
    assert thieving.status == "in progress" and thieving.pieces_left == 14 and thieving.craftable == 2
    assert outfit_state(p, "Thieving", seen, {"Thieving": 6}).fragments_needed == 6 * 3600 - 7_300
    # Pieces seen once are remembered after they leave the exports (stored with Diango, say).
    p.inventories = {}
    assert outfits()["Thieving"]["components"][0]["pieces"][0] in seen_pieces(p, tmp_path / "seen.json")
    # Thieving: 50 per 5-minute drop, doubled at 99, plus the 1/2,500 bonus of 3,600.
    assert fragments_per_hour(p, "Thieving") == 12 * (100 + 3600 / 2500)


def test_outfit_blueprint_blocks_crafting(tmp_path):
    from rune_advisor.models import Skill
    from rune_advisor.outfits import fragments_per_hour, outfit_state

    p = PlayerState(name="X", skills={"Hunter": Skill(name="Hunter", level=93, xp=7_600_000)},
                    vars={"CURRENCY_FRAGMENTS_TRAPPER": 10_946, "DINO_GAME_SHOP_ELITE_OUTFIT_BLUEPRINT": 0,
                          "DINO_GAME_HUNTER_MARKS": 40})
    state = outfit_state(p, "Hunter", set())
    assert state.blocked and "100 Hunter marks" in state.blocked and "(you have 40)" in state.blocked
    assert state.craftable == 0
    p.vars["DINO_GAME_SHOP_ELITE_OUTFIT_BLUEPRINT"] = 1
    assert outfit_state(p, "Hunter", set()).craftable == 3
    assert fragments_per_hour(p, "Hunter") == 12 * (100.5 + 3600 / 2500)  # trapper drops are ~100, not ~50


def test_unbought_blueprint_means_no_pieces():
    from rune_advisor.models import Skill
    from rune_advisor.outfits import outfit_state

    p = PlayerState(name="X", skills={"Hunter": Skill(name="Hunter", level=93, xp=7_600_000)},
                    vars={"CURRENCY_FRAGMENTS_TRAPPER": 10_946, "DINO_GAME_SHOP_ELITE_OUTFIT_BLUEPRINT": 0})
    assert outfit_state(p, "Hunter", set()).pieces_left == 15


def test_ironman_pieces_imply_blueprint():
    from rune_advisor.models import Skill
    from rune_advisor.outfits import outfit_state, outfits

    dung = outfits()["Dungeoneering"]
    seen = {dung["components"][0]["pieces"][0]}
    p = PlayerState(name="X", skills={"Dungeoneering": Skill(name="Dungeoneering", level=100, xp=14_000_000)},
                    vars={"CURRENCY_FRAGMENTS_DUNGEONEERING": 1_685})
    assert outfit_state(p, "Dungeoneering", seen).blocked  # a main could have the piece from Treasure Hunter
    p.ironman = True
    assert not outfit_state(p, "Dungeoneering", seen).blocked  # an ironman crafted it: blueprint bought


def test_task_set_guide_entry(player):
    """A set of sub-achievements shows "N of M left", each open part's first unmet need, and why it's unsure."""
    from rune_advisor.models import Achievement
    from rune_advisor.recommend.guide import guide
    from rune_advisor.recommend.tracks import TaskSetsTrack

    flag = {"kind": "varTotal", "group": 0, "totalValue": 1, "currentValue": 0, "complete": False}
    player.achievements = {
        "Set - Elite": Achievement("Set - Elite", 25, False, requirements=[
            {"kind": "achievement", "group": 0, "achievement": a, "complete": a != "Valet"}
            for a in ("Rays", "Valet", "Knock")]),
        "Valet": Achievement("Valet", 25, False, requirements=[dict(flag)]),
        "Rays": Achievement("Rays", 10, True), "Knock": Achievement("Knock", 10, True),
    }
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False),
                  achievement_reqs={"Valet": {"type": "skill", "name": "Constitution", "level": 91}},
                  achievement_descriptions={"Valet": "Have a titan hold essence."})
    [c] = TaskSetsTrack.candidates(ctx)
    assert c.title == "Set - Elite: 1 of 3 left" and "2 of 3 done" in c.why
    assert c.needs == ["Valet: 91 Hitpoints (have 90, ~%.1fh); Have a titan hold essence." % ctx.level_plan("Hitpoints", 91).hours]
    assert any("Have a titan hold essence." in u and "guessed" in u for u in c.unknowns)  # labelled by the wiki
    [e] = guide({"task_sets": [c]})["task_sets"]
    assert e.confidence == "mostly guessed" and "combat" in e.tags  # the 1h flag guess outweighs a cheap level
    assert guide({"task_sets": [c]}, mood="quest")["task_sets"] == []
    assert guide({"task_sets": [c]}, max_hours=0.01)["task_sets"] == []


def test_guide_sorts_mostly_guessed_last():
    from rune_advisor.recommend.base import Candidate
    from rune_advisor.recommend.guide import guide

    guessed = Candidate("runescore", "flag", value=100, cost_hours=1.0, guessed_hours=1.0, uncertain=True)
    modelled = Candidate("runescore", "skill", value=10, cost_hours=1.0, skills={"Mining"})
    entries = guide({"runescore": [guessed, modelled]})["runescore"]
    assert [e.headline for e in entries] == ["skill", "flag"]  # despite the flag's better value per hour
    assert entries[1].confidence == "mostly guessed" and entries[0].tags == {"skilling", "short"}


def test_focus_ranks_fast_options_by_extras(player):
    from rune_advisor.recommend.focus import FocusOption, parse_goal, rank_options

    fast_plain = FocusOption("fast", None, "", [], speed=100)
    fast_extras = FocusOption("nearly as fast", None, "", ["achievement: X", "+5 Agility xp/h"], speed=80)
    slow_extras = FocusOption("slow", None, "", ["a", "b", "c"], speed=50)
    assert [o.activity for o in rank_options([fast_plain, slow_extras, fast_extras])] == \
        ["nearly as fast", "fast", "slow"]  # extras win only within FAST_SHARE of the fastest

    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    assert parse_goal(ctx, "thieving outfit").kind == "outfit"
    g = parse_goal(ctx, "runecrafting:99")
    assert (g.kind, g.name, g.level) == ("level", "Runecraft", 99)
    assert parse_goal(ctx, "item:Dragon pickaxe").name == "Dragon pickaxe"
    with pytest.raises(ValueError):
        parse_goal(ctx, "something vague")


def test_kills_to_collect_matches_inclusion_exclusion():
    from rune_advisor.recommend.tracks.collection_logs import kills_to_collect
    from rune_advisor.recommend.tracks.gear import expected_kills

    for chances in ([1 / 256] * 3 + [1 / 50], [1 / 1000], [1 / 128, 1 / 512]):
        assert kills_to_collect(chances) == pytest.approx(expected_kills(chances), rel=0.01)


def test_item_log_config_names_resolve():
    from rune_advisor.drops import resolve_item_log

    log = resolve_item_log({"ITEMLOG_ITEM_BARROWS_VERAC_FLAIL": 1, "ITEMLOG_ITEM_WARPRIEST_BANDOS_BODY": 1,
                            "ITEMLOG_ITEM_BARROWS_ADDON": 1, "Hermodic plate": 4})
    assert log == {"Verac's flail": 1, "Warpriest of Bandos cuirass": 1, "ITEMLOG_ITEM_BARROWS_ADDON": 1,
                   "Hermodic plate": 4}  # ambiguous/unknown suffixes stay as they are


def test_toolbelt_unlocks_from_vars(player):
    from rune_advisor.recommend.tracks import ToolbeltTrack
    from rune_advisor.recommend.tracks.toolbelt import belt_vars

    player.vars = {"TOOLBELT_LOCKPICK": 0, "TOOLBELT_STETHOSCOPE": 1, "TOOLBELT_GOLD_ACC": 0,
                   "TOOLBELT_GOLD_ACC_BOUGHT": 0, "TOOLBELT_HATCHET_LEVEL": 14}
    assert belt_vars(player.vars) == {"Master thief's lockpick": ["TOOLBELT_LOCKPICK"],
                                      "Master thief's stethoscope": ["TOOLBELT_STETHOSCOPE"],
                                      "Advanced gold accumulator": ["TOOLBELT_GOLD_ACC"]}
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    cands = {c.title: c for c in ToolbeltTrack.candidates(ctx)}
    assert set(cands) == {"Master thief's lockpick", "Advanced gold accumulator"}  # the stethoscope is on the belt
    lockpick = cands["Master thief's lockpick"]
    assert lockpick.cost_hours == pytest.approx(2500 / 900, rel=0.01) and lockpick.skills == {"Thieving"}


def test_normal_account_buys_supplies_at_ge_prices():
    from rune_advisor.models import Skill
    from rune_advisor.prices import Prices
    from rune_advisor.resources import LevelPlan

    p = PlayerState(name="X", members=True, skills={"Herblore": Skill("Herblore", 90, xp_for_level(90))})
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False), prices=Prices(prices={"Grimy torstol": 5000}))
    plan = LevelPlan(1.0, [])
    plan.materials_short = {"Grimy torstol": 400, "Mystery item": 3}
    ctx._buy(plan)
    assert plan.gather_coins == 2_000_000 and plan.hours == pytest.approx(2.0)  # +1h at 2m gp/h
    assert "no price or source for Mystery item" in plan.steps[-1]


def test_prayer_urns_fill_from_ashes():
    from rune_advisor.boosts import prayer_urn
    from rune_advisor.models import Skill

    p = PlayerState(name="X", ironman=True, members=True, skills={"Crafting": Skill("Crafting", 70, xp_for_level(70))})
    row = {"method": "Black demons", "drop_sources": ["Black demon"]}
    xp_per_kill = __import__("rune_advisor.drops", fromlist=["tables"]).tables()["monsters"]["Black demon"]["xp"]
    pu = prayer_urn(p, row, "Strength", 100 * xp_per_kill)  # 100 kills/h, each dropping infernal ashes (62.5 xp)
    assert pu.name == "Infernal urn"
    assert pu.prayer_xp_per_hour == pytest.approx(100 * 62.5 * 1.2)
    assert pu.urns_per_hour == pytest.approx(100 * 62.5 / 1562.5)
    assert prayer_urn(p, {"method": "Cows", "drop_sources": ["Cow"]}, "Strength", 5000) is None  # no ashes


def test_exquisite_urn_only_when_worth_its_gathering():
    from rune_advisor.models import Skill
    p = PlayerState(name="X", ironman=True, members=True, skills={
        "Crafting": Skill("Crafting", 110, xp_for_level(110)), "Mining": Skill("Mining", 95, xp_for_level(95))})
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False))

    ctx._urn_chain_hours = lambda item, family, depth=0: 1e-6  # nearly free: the exquisite urn pays more
    assert ctx._urn("Mining").name == "Exquisite mining urn"
    ctx._urn_chain_hours = lambda item, family, depth=0: 10.0  # ten hours of gathering per urn: not worth it
    ctx._urn_options = {}
    assert ctx._urn("Mining").name == "Decorated mining urn"


def test_guide_afk_tag():
    from rune_advisor.recommend.base import Candidate
    from rune_advisor.recommend.guide import guide

    c = Candidate("milestones", "99 Divination", value=10, cost_hours=5, skills={"Divination"})
    both = Candidate("milestones", "mixed", value=1, cost_hours=5, skills={"Divination", "Agility"})
    out = guide({"milestones": [c, both]}, afk={"Divination"})["milestones"]
    assert "afk" in out[0].tags and "afk" not in out[1].tags
    assert [e.headline for e in guide({"milestones": [c, both]}, mood="afk", afk={"Divination"})["milestones"]] == ["99 Divination"]


def test_category_prior_replaces_flat_guess(player):
    from rune_advisor.models import Achievement
    from rune_advisor.recommend.plans import MIN_PRIOR_SAMPLES, achievement_plan

    quest = next(t for t, q in player.quests.items() if not q.done and q.members)
    player.achievements = {f"Dig {i}": Achievement(f"Dig {i}", 10, False, category="Skills / Mining", requirements=[
        {"kind": "quest", "group": 0, "quest": quest, "complete": False}]) for i in range(MIN_PRIOR_SAMPLES)}
    player.achievements["Level"] = Achievement("Level", 10, False, category="Skills / Mining", requirements=[
        {"kind": "skill", "group": 0, "skills": ["MINING"], "totalLevels": 99, "complete": False}])
    player.achievements["Flag"] = Achievement("Flag", 10, False, category="Skills / Mining", requirements=[
        {"kind": "varTotal", "group": 0, "totalValue": 1, "currentValue": 0, "complete": False}])
    player.achievements["Other flag"] = Achievement("Other flag", 10, False, category="Lore / Books", requirements=[
        {"kind": "varTotal", "group": 0, "totalValue": 1, "currentValue": 0, "complete": False}])
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    flag = achievement_plan(ctx, "Flag")
    # The quest-costed ones set the median; the 99 Mining grind isn't a sample.
    assert flag.hours == pytest.approx(achievement_plan(ctx, "Dig 0").hours)
    assert "typical for Skills / Mining: median of 5" in flag.unknowns[0]
    other = achievement_plan(ctx, "Other flag")  # no modelled Lore at all: every category pooled
    assert "typical for all achievements" in other.unknowns[0]


def test_collection_log_supply_tax_and_needed_drops(player):
    from rune_advisor.drops import tables
    from rune_advisor.models import Achievement
    from rune_advisor.recommend.gathering import Source
    from rune_advisor.recommend.tracks import CollectionLogsTrack

    player.achievements = {"Exiled": Achievement("Exiled", 10, False)}
    player.item_log = {"Hermodic plate": 1}
    from rune_advisor.drops import collection_logs
    from rune_advisor.recommend.tracks.collection_logs import _source
    src = _source(list(collection_logs()["Kalphite King"]["items"]))
    common = next(i for i, c, q, _ in tables()["sources"][src] if isinstance(c, (int, float)) and c >= 0.05)

    def kk(ironman, need):
        player.ironman = ironman
        ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
        ctx._needs = {common: "test"} if need else {}
        real = ctx.gatherer.source
        ctx.gatherer.source = lambda name: Source("gather", "stub", 0.5) if name == common else real(name)
        return next(c for c in CollectionLogsTrack.candidates(ctx) if "Kalphite King" in c.title)

    normal, iron, iron_need = kk(False, False), kk(True, False), kk(True, True)
    assert any("supplies" in u for u in iron.unknowns) and not any("supplies" in u for u in normal.unknowns)
    assert 0 < iron_need.cost_hours <= iron.cost_hours  # KK loot alone can hit the 10% floor
    assert "needed drops save" in iron_need.why


def test_alch_income_from_training_drops(player):
    from rune_advisor.drops import tables
    from rune_advisor.recommend.base import MIN_ALCH_GP, alch_values

    xp = tables()["monsters"]["Black demon"]["xp"]
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    ctx._needs = {}
    ctx._top_method = lambda skill: {"method": "Black demons", "drop_sources": ["Black demon"],
                                     "xp_per_hour_low": 100 * xp, "xp_per_hour_high": None}
    gp, casts = ctx.alch_income("Strength", 2.0)  # 200 kills
    expected = sum(c * (q or 1) * (1 if i == "Coins" else alch_values().get(i, 0))
                   for i, c, q, _ in tables()["sources"]["Black demon"]
                   if isinstance(c, (int, float)) and (i == "Coins" or alch_values().get(i, 0) >= MIN_ALCH_GP))
    assert gp == pytest.approx(expected * 200) and gp > 0
    assert ctx.alch_income("Strength", 0) == (0.0, 0)


def test_joint_plan_banks_spare_alch_coins(player, monkeypatch):
    from rune_advisor.recommend.base import Context as Ctx
    from rune_advisor.recommend.joint import joint_plan

    player.ironman = True
    monkeypatch.setattr(Ctx, "alch_income", lambda self, skill, hours: (1_000_000.0, 100) if hours else (0.0, 0))
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    goals = [("Strength", player.level("Strength") + 1), ("Attack", player.level("Attack") + 1)]
    before = player.bank.get("Coins", 0)
    jp = joint_plan(ctx, goals)
    first = jp.steps[0].plan
    assert any("high-alch the rest" in s for s in first.steps)
    assert jp.player.bank.get("Coins", 0) >= before + 1_000_000 - first.gather_coins - 1


def test_crafted_upgrade_chain_counts_held_drops(player):
    from rune_advisor.recommend.tracks.gear import _expand

    player.inventories = {"BANK": {"Hermodic plate": 1}}
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    cache = {"Hermodic plate": ("Hermod, the Spirit of War", 1 / 9, (20.0, "test", False))}
    drops, other, levels = {}, {}, {}
    assert _expand(ctx, "Deathdealer robe top (tier 80)", 1, drops, other, levels, dict(player.holdings), cache)
    assert drops["Hermodic plate"] == pytest.approx(2)  # 2 (tier 80) + 1 (tier 70) - 1 held
    assert levels["Crafting"] == 75


def test_category_prior_falls_back_to_main_category(player):
    from rune_advisor.models import Achievement
    from rune_advisor.recommend.plans import MIN_PRIOR_SAMPLES, achievement_plan

    quest = next(t for t, q in player.quests.items() if not q.done and q.members)
    req = [{"kind": "quest", "group": 0, "quest": quest, "complete": False}]
    flag = [{"kind": "varTotal", "group": 0, "totalValue": 1, "currentValue": 0, "complete": False}]
    player.achievements = {f"A{i}": Achievement(f"A{i}", 10, False, category=f"Skills / Sub{i}", requirements=req)
                           for i in range(MIN_PRIOR_SAMPLES)}
    player.achievements["Flag"] = Achievement("Flag", 10, False, category="Skills / Lonely", requirements=flag)
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    plan = achievement_plan(ctx, "Flag")
    assert "typical for Skills:" in plan.unknowns[0] and plan.hours == pytest.approx(achievement_plan(ctx, "A0").hours)


def test_next_session_picker():
    from rune_advisor.recommend.guide import GuideEntry
    from rune_advisor.recommend.picker import pick

    def e(track, title, hours, conf="modelled", left=()):
        return GuideEntry(track, title, hours, "", "", list(left), set(), conf)

    sections = {
        "free_xp": [e("free_xp", "Place rocks", 0.1)],
        "runescore": [e("runescore", "Ardougne Set Tasks - Elite", 0.5, left=["Abyssal Valet: 93 Summoning (have 92, ~1.5h)"]),
                      e("runescore", "Big thing", 5.0)],
        "task_sets": [e("task_sets", "Ardougne Set Tasks - Elite: 1 of 5 left", 0.5),
                      e("task_sets", "Karamja Set Tasks - Elite: 2 of 8 left", 1.0, left=["Boxing Clever: 93 Summoning (~1.5h)"])],
        "milestones": [e("milestones", "Guess", 0.2, conf="mostly guessed")],
    }
    picks = pick(sections, 2.0)
    assert [p.entry.headline for p in picks] == ["Place rocks", "Ardougne Set Tasks - Elite",
                                                 "Karamja Set Tasks - Elite: 2 of 8 left"]  # dup and guess skipped
    assert picks[2].shares == ["93 Summoning (also for Ardougne Set Tasks - Elite)"]
    free = pick({"a": [e("a", "Set", 1.0, left=["Mini: quest Mini (~1.0h)"])],
                 "b": [e("b", "Mini", 1.0, left=["quest Mini (~1.0h)"])]}, 1.0)
    assert [p.entry.headline for p in free] == ["Set", "Mini"]  # Mini is done inside Set: no extra time
    lvl = pick({"milestones": [e("milestones", "93 Summoning", 1.5, left=["93 Summoning (have 92, ~1.5h)"])],
                "runescore": [e("runescore", "Valet", 2.5, left=["Abyssal Valet: 92 Summoning (have 91, ~1.0h)",
                                                                  "Titan flag"])]}, 3.0)
    assert lvl[1].extra_hours == pytest.approx(1.5)  # its 92 Summoning comes with the 93 milestone
    # The budget is play time: a set whose lamps net its cost down to 0.2h still takes 1.8h at the keyboard.
    lamp = GuideEntry("task_sets", "Ardougne Set Tasks - Elite: 1 of 5 left", 0.2, "", "", [], set(), "modelled", play_hours=1.8)
    assert pick({"task_sets": [lamp]}, 1.0) == []
    assert [p.extra_hours for p in pick({"task_sets": [lamp]}, 2.0)] == [pytest.approx(1.8)]


def test_exquisite_urn_note_below_its_crafting_level():
    from rune_advisor.models import Skill

    p = PlayerState(name="X", ironman=True, members=True, skills={
        "Crafting": Skill("Crafting", 100, xp_for_level(100)), "Mining": Skill("Mining", 95, xp_for_level(95))})
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False))
    note = ctx._exquisite_note("Mining")
    assert note.startswith("at 102 Crafting (have 100) Exquisite mining urns would pay") and "Decorated mining urn" in note
    p.skills["Crafting"] = Skill("Crafting", 110, xp_for_level(110))
    assert ctx._exquisite_note("Mining") is None


def test_upgrade_saves_planned_style_training(player):
    from rune_advisor.recommend.tracks.gear import upgrade_hours_saved

    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    planned = sum(ctx.level_plan(s, 99).hours for s in ("Attack", "Strength", "Defence") if player.level(s) < 99)
    assert upgrade_hours_saved(ctx, "melee", 0.25) == pytest.approx(planned * 0.25 / 1.25)
    assert upgrade_hours_saved(ctx, "melee", 0.0) == 0


def test_exquisite_urn_counts_the_crafting_levels_it_needs(monkeypatch):
    from rune_advisor.models import Skill

    p = PlayerState(name="X", ironman=True, members=True, skills={
        "Crafting": Skill("Crafting", 100, xp_for_level(100)), "Mining": Skill("Mining", 95, xp_for_level(95))})
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False))
    monkeypatch.setattr(Context, "_urn_chain_hours", lambda self, item, family, depth=0: 1e-6)
    big = ctx._urn("Mining", 500_000_000)  # a huge grind: two Crafting levels are worth it
    assert big.name == "Exquisite mining urn" and big.prereq.startswith("get 102 Crafting first")
    assert ctx._urn("Mining", 50_000).name == "Decorated mining urn"  # a short one: not worth the levels


def test_unique_value_setting_nets_off_log_cost(player):
    from rune_advisor.models import Achievement
    from rune_advisor.recommend.tracks import CollectionLogsTrack

    player.achievements = {"Exiled": Achievement("Exiled", 10, False)}
    player.item_log = {"Hermodic plate": 1}

    def kk(value):
        ctx = Context(player=player, cost=CostModel.default(use_wiki=False), unique_value_hours=value)
        ctx._needs = {}
        return next(c for c in CollectionLogsTrack.candidates(ctx) if "Kalphite King" in c.title)

    assert kk(1.0).cost_hours < kk(0.0).cost_hours


def test_replanned_picker_applies_levels(player):
    from rune_advisor.recommend.guide import GuideEntry
    from rune_advisor.recommend.picker import effects, pick_replanned

    e = lambda t, h, left=(): GuideEntry("milestones", t, h, "", "", list(left), set(), "modelled")  # noqa: E731
    lvl = player.level("Hunter") + 1
    assert effects(e(f"{lvl} Hunter", 1, ["quest Cook's Assistant (~0.5h)"])) == ({"Hunter": lvl}, {"Cook's Assistant"})
    builds = []

    def build(ctx, tracks=None):
        builds.append(ctx.player.level("Hunter"))
        if ctx.player.level("Hunter") < lvl:
            return {"milestones": [e(f"{lvl} Hunter", 1.0)], "runescore": [e("Needs Hunter", 2.0, [f"{lvl} Hunter (~1.0h)"])]}
        return {"milestones": [], "runescore": [e("Needs Hunter", 1.0)]}  # re-planned: the level is done

    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    picks = pick_replanned(ctx, build, 2.0)
    assert [p.entry.headline for p in picks] == [f"{lvl} Hunter", "Needs Hunter"]
    assert builds == [lvl - 1, lvl] and player.level("Hunter") == lvl - 1  # the real player is untouched


def test_prefer_exquisite_urns_setting():
    from rune_advisor.models import Skill

    p = PlayerState(name="X", ironman=True, members=True, skills={
        "Crafting": Skill("Crafting", 110, xp_for_level(110)), "Mining": Skill("Mining", 95, xp_for_level(95))})
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False), prefer_exquisite_urns=True)
    ctx._urn_chain_hours = lambda item, family, depth=0: 10.0  # far too slow on the numbers...
    assert ctx._urn("Mining").name == "Exquisite mining urn"  # ...but the player asked for them


def test_prefer_exquisite_urns_plans_the_crafting_level(monkeypatch):
    from rune_advisor.models import Skill

    p = PlayerState(name="X", ironman=True, members=True, skills={
        "Crafting": Skill("Crafting", 100, xp_for_level(100)), "Mining": Skill("Mining", 95, xp_for_level(95))})
    monkeypatch.setattr(Context, "_urn_chain_hours", lambda self, item, family, depth=0: 10.0)
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False), prefer_exquisite_urns=True)
    plan = ctx.level_plan("Mining", 99)
    assert any(s.startswith("get 102 Crafting first for Exquisite mining urns") for s in plan.steps)


def test_drop_coins_per_kill():
    from rune_advisor.drops import tables
    from rune_advisor.recommend.base import MIN_ALCH_GP, alch_values, drop_coins_per_kill

    rows = [r for r in tables()["sources"]["Black demon"] if isinstance(r[1], (int, float))]
    gp, casts = drop_coins_per_kill("Black demon")
    assert gp == pytest.approx(sum(c * (q or 1) * (1 if i == "Coins" else alch_values()[i]) for i, c, q, _ in rows
                                   if i == "Coins" or alch_values().get(i, 0) >= MIN_ALCH_GP))
    assert drop_coins_per_kill("Black demon", {i for i, *_ in rows})[0] == 0  # everything excluded


def test_reserve_holds_items_back(tmp_path):
    from rune_advisor.player import load_group

    p = PlayerState(name="PlayerOne", inventories={"BANK": {"Red sandstone": 2000}, "INV": {"Red sandstone": 500}})
    apply_overrides(p, {"players": {"playerone": {"reserve": {"Red sandstone": 2400, "Soda ash": 10}}}})
    assert p.holdings["Red sandstone"] == 100 and p.bank["Red sandstone"] == 0  # bank emptied first
    assert p.reserved == {"Red sandstone": 2400, "Soda ash": 0}
    assert any("only 0 held" in n for n in p.notes)
    # A group mate's reserve is held back from what the others can borrow.
    snap = {"inventories": {"BANK": {"items": [{"id": 1, "name": "Red sandstone", "count": 3000}]}}}
    (tmp_path / "snap.json").write_text(json.dumps(snap))
    (tmp_path / "rs.toml").write_text(f'group = ["PlayerTwo", "PlayerOne"]\n[players.playerone]\nsnapshot = "{tmp_path / "snap.json"}"\n')
    (tmp_path / "overrides.toml").write_text('[players.PlayerOne]\nreserve = { "Red sandstone" = 2400 }\n')
    from rune_advisor.config import load_settings
    s = load_settings(tmp_path / "rs.toml")
    s.plugin_db = tmp_path / "missing.db"
    s.archive_dir = tmp_path  # no real archives in a test
    mate = PlayerState(name="PlayerTwo")
    load_group(mate, s, overrides_file=tmp_path / "overrides.toml")
    assert mate.group == {"PlayerOne": {"Red sandstone": 600}}


def test_gatherer_goes_all_the_way_down(monkeypatch):
    from rune_advisor.models import Skill
    from rune_advisor.recommend import gathering
    from rune_advisor.recommend.gathering import Gatherer

    def recipe(skill, output, level, materials):
        return {"skill": skill, "output": output, "output_qty": 1.0, "level": level, "xp": 10.0, "ticks": 2.0,
                "materials": materials, "members": True, "quests": [], "other_skills": [], "facility": ""}
    # Gizmo <- 2 Widget <- (Bolt <- Pouch from a shop) + Mystery (no source): three steps down.
    book = {"Gizmo": [recipe("Crafting", "Gizmo", 50, [["Widget", 2]])],
            "Widget": [recipe("Crafting", "Widget", 1, [["Bolt", 1], ["Mystery", 1]])],
            "Bolt": [recipe("Smithing", "Bolt", 1, [["Pouch", 1]])]}
    monkeypatch.setattr(gathering, "by_output", lambda: book)
    p = PlayerState(name="X", ironman=True, skills={s: Skill(s, 40, xp_for_level(40)) for s in ("Crafting", "Smithing")})
    p.inventories = {"MONEY_POUCH": {"Coins": 1_000_000}, "BANK": {"Pouch": 3}}
    ctx = Context(player=p, cost=CostModel.default(player=p, use_wiki=False))
    g = Gatherer(ctx)
    src = g.source("Gizmo")
    # A chain with an unsourced input and a level to train still counts, flagged.
    assert src.kind == "craft" and src.unsourced == {"Mystery"} and src.unlock == {"Crafting": 50}
    assert g.complete_source("Gizmo") is None and g.complete_source("Bolt").kind == "craft"
    plan = g.plan({"Gizmo": 2})
    # 4 Widgets -> 4 Bolts -> 4 Pouches: 3 from the bank, 1 bought; Mystery stays short.
    assert plan.short == {"Mystery": 4}
    assert any("1 Pouch" in s and "buy" in s for s in plan.steps)
    assert plan.unlock_hours > 0 and plan.steps[0].startswith("first get 50 Crafting for Gizmo")
    assert g.gaps["Mystery"] == (4, {"Gizmo"})
    # Training Crafting anyway (the plan's own skill): no separate unlock.
    assert g.plan({"Gizmo": 1}, training="Crafting").unlock_hours == 0
    # Siblings share the stock: 3 bank Pouches cover 3 Bolts in total, not 3 each.
    two = g.plan({"Bolt": 2, "Widget": 2})
    assert sum(1 for s in two.steps if "buy" in s and "Pouch" in s) == 1
    assert any(s.strip().startswith("get 1 Pouch") for s in two.steps)


def test_potions_decant_between_doses():
    from rune_advisor.recommend.catalogue import by_output, catalogue

    decant = next(r for r in by_output()["Extreme prayer (4)"] if r.get("conversion"))
    assert decant["xp"] == 0 and decant["materials"] == [["Extreme prayer (3)", 4 / 3]] and decant["members"]
    # The recipe bucket's "Extreme prayer potion (3)" is the same item ("Extreme prayer (3)").
    assert "Extreme prayer (3)" in by_output() and "Extreme prayer potion (3)" not in by_output()
    # Decanting only flows out of the dose Herblore makes, so it can't loop.
    assert not any(r.get("conversion") for r in by_output()["Extreme prayer (3)"])
    assert all(r["output"] != r["materials"][0][0] for r in catalogue() if r.get("facility") == "Backpack (decant)")


def test_disassembly_buffer_and_byproducts(monkeypatch):
    import random
    from rune_advisor import disassembly as dis
    from rune_advisor.models import Skill
    from rune_advisor.recommend import gathering
    from rune_advisor.recommend.gathering import Gatherer

    # A made-up item: 2 rolls each, 50% junk, then Tensile 30 / Base 70; a 10% special component.
    fake = {"Widget": {"level": 1, "batch": 1.0, "draws": 2.0, "materials": {"Tensile parts": 30, "Base parts": 70},
                       "total": 100, "junk": [50.0] * 10, "special": {"Rare components": {"chance": 10, "quantity": 1}},
                       "xp": 1.0}}
    monkeypatch.setattr(dis, "data", lambda: fake)
    monkeypatch.setattr(dis, "sources", lambda: {"Tensile parts": ["Widget"], "Base parts": ["Widget"],
                                                 "Rare components": ["Widget"]})
    y = dis.per_item("Widget", "Tensile parts", 0)
    assert y.mean == pytest.approx(2 * 0.5 * 0.3)
    k = dis.items_needed(300, y)
    assert k > 300 / y.mean  # buffered beyond the expected count
    # Simulated: the buffered count reaches 300 about 95% of the time.
    rng = random.Random(1)
    hits = sum(sum(1 for _ in range(2 * k) if rng.random() < 0.5 and rng.random() < 0.3) >= 300 for _ in range(400))
    assert 0.9 < hits / 400 < 0.99
    # Junk research follows the Invention level.
    p = PlayerState(name="X", ironman=True, skills={"Invention": Skill("Invention", 69, xp_for_level(69))})
    assert dis.junk_tier(p) == 4
    # In a plan: the rare component's disassembly comes first and its sure parts cover the parts needed.
    p.inventories = {"BANK": {"Widget": 100_000}}
    ctx = Context(player=p, cost=CostModel.default(player=p, use_wiki=False))
    monkeypatch.setattr(gathering, "equipment", lambda: {"items": {}})
    monkeypatch.setattr(gathering, "tables_shops", lambda: {"Widget": [("Widget shop", 10_000, 1, True)]})
    g = Gatherer(ctx)
    assert g.source("Rare components").kind == "disassemble"
    plan = g.plan({"Tensile parts": 50, "Rare components": 40})
    assert plan.steps[0].startswith("disassemble") and "Rare components" in plan.steps[0]
    assert any(s.startswith("50 Tensile parts from the disassembly above") for s in plan.steps)
    assert plan.short == {} and plan.xp["Invention"] > 0 and "no Invention materials storage" in plan.steps[-1]
    # With the materials storage exported (callers net holdings, this storage included, off the shortfall
    # before planning), the note is gone.
    p.inventories["MATERIALS_STORAGE"] = {"Base parts": 500}
    assert p.holdings["Base parts"] == 500
    plan = Gatherer(ctx).plan({"Tensile parts": 50, "Rare components": 40})
    assert plan.steps[0].startswith("disassemble") and not any("materials storage" in s for s in plan.steps)


def test_disassembly_uses_banked_inputs_not_gear(monkeypatch):
    from rune_advisor import disassembly as dis
    from rune_advisor.models import Skill
    from rune_advisor.recommend import gathering
    from rune_advisor.recommend.gathering import Gatherer

    def item(per_draw):
        return {"level": 1, "batch": 1.0, "draws": 1.0, "materials": {"Heavy components": per_draw, "Base parts": 100 - per_draw},
                "total": 100, "junk": [0.0] * 10, "special": {}, "xp": 1.0}
    # The lantern is cheaper per unit (a slow shop beats buying bars at 100k), but the sword's bars are banked.
    monkeypatch.setattr(dis, "data", lambda: {"Lantern": item(10), "Sword": item(10)})
    monkeypatch.setattr(dis, "sources", lambda: {"Heavy components": ["Lantern", "Sword"]})
    monkeypatch.setattr(gathering, "tables_shops", lambda: {"Lantern": [("Shop", 1, 1, True)],
                                                            "Bar": [("Shop", 10, 100_000, True)]})
    monkeypatch.setattr(gathering, "by_output", lambda: {"Sword": [
        {"skill": "Smithing", "output": "Sword", "output_qty": 1.0, "level": 1, "xp": 1.0, "ticks": 1.0,
         "materials": [["Bar", 1]], "members": True, "quests": [], "other_skills": [], "facility": ""}]})
    monkeypatch.setattr(gathering, "equipment", lambda: {"items": {"Sword": {}}})
    p = PlayerState(name="X", ironman=True, skills={s: Skill(s, 99, xp_for_level(99)) for s in ("Invention", "Smithing")},
                    inventories={"BANK": {"Bar": 10_000, "Sword": 5}, "MONEY_POUCH": {"Coins": 10**9}})
    g = Gatherer(Context(player=p, cost=CostModel.default(player=p, use_wiki=False)))
    assert g.source("Heavy components").disassemble == "Lantern"
    plan = g.plan({"Heavy components": 50})
    assert plan.steps[0].startswith("disassemble") and " Sword " in plan.steps[0]
    # The banked swords (gear) aren't taken: every sword is smithed.
    k = int(plan.steps[0].split()[1].replace(",", ""))
    assert f"gathering {k:,} Sword" in plan.steps[0]


def test_level_boosts_reach_and_doses(monkeypatch):
    from rune_advisor import level_boosts as lb
    from rune_advisor.models import Skill
    from rune_advisor.recommend.gathering import Gatherer

    fake = [{"name": "Super woodcutting potion", "label": "Super woodcutting potion", "skills": ["Woodcutting"],
             "flat": 5, "percent": 0.0, "static": False},
            {"name": "Mature stout", "label": "Mature stout", "skills": ["Smithing"], "flat": 2, "percent": 1.0,
             "static": False},
            {"name": "God banner", "label": "God banner", "skills": ["Woodcutting", "Smithing"], "flat": 2,
             "percent": 0.0, "static": True, "daily_minutes": 30}]
    monkeypatch.setattr(lb, "boosts", lambda: fake)
    p = PlayerState(name="X", ironman=True, skills={"Woodcutting": Skill("Woodcutting", 96, xp_for_level(96)),
                                                    "Smithing": Skill("Smithing", 99, xp_for_level(99))},
                    inventories={"BANK": {"Super woodcutting potion (4)": 7, "Super woodcutting potion (2)": 1}})
    # Boosts don't stack: the best single one counts. Percentages round down (1% of 99 = 0).
    assert lb.reach(p, "Woodcutting") == 101 and lb.reach(p, "Smithing") == 101
    g = Gatherer(Context(player=p, cost=CostModel.default(player=p, use_wiki=False)))
    # +5 over a 4-level gap lasts 2 minutes a dose: an hour of cutting takes 30 doses, all banked (7 x 4 + 2).
    plan = lb.plan_boost(g, "Woodcutting", 100, 1.0)
    assert plan.doses == 30 and plan.held_doses == 30 and plan.level == 101
    # The banner needs `banner` in the player's boosts; then a short job costs a day of calendar time.
    # The made-up stout has no source and there's no banner yet: no boost to 101 Smithing.
    assert lb.plan_boost(g, "Smithing", 101, 0.2) is None
    p.boosts = {"banner": "Zaros"}
    banner = lb.plan_boost(g, "Smithing", 101, 0.6)  # over 30 minutes of work: two days of sessions
    assert banner.doses == 0 and banner.calendar_hours == 48
    assert lb.plan_boost(g, "Woodcutting", 102, 0.2) is None  # out of reach


def test_boostable_gates_boost_others_train(monkeypatch):
    from rune_advisor import level_boosts as lb
    from rune_advisor.models import Skill
    from rune_advisor.recommend import gathering
    from rune_advisor.recommend.gathering import Gatherer

    monkeypatch.setattr(lb, "boosts", lambda: [{"name": "God banner", "label": "God banner", "skills": ["Smithing"],
                                                "flat": 2, "percent": 0.0, "static": True, "daily_minutes": 30}])

    def recipe(output, boostable):
        return {"skill": "Smithing", "output": output, "output_qty": 1.0, "level": 100, "xp": 10.0, "ticks": 2.0,
                "materials": [["Pouch", 1]], "members": True, "quests": [], "other_skills": [], "facility": "",
                **({"boostable": ["Smithing"]} if boostable else {})}
    monkeypatch.setattr(gathering, "by_output", lambda: {"Test plate": [recipe("Test plate", True)],
                                                         "Test hatchet": [recipe("Test hatchet", False)]})
    p = PlayerState(name="X", ironman=True, skills={"Smithing": Skill("Smithing", 99, xp_for_level(99))},
                    inventories={"BANK": {"Pouch": 10}}, boosts={"banner": "Zaros"})
    g = Gatherer(Context(player=p, cost=CostModel.default(player=p, use_wiki=False)))
    plate = g.plan({"Test plate": 1})
    assert plate.steps[0].startswith("boost Smithing to 101 with the Zaros banner") and "instead of training" in plate.steps[0]
    assert plate.calendar_hours == 24 and plate.unlock_hours < 1
    hatchet = g.plan({"Test hatchet": 1})
    assert hatchet.steps[0].startswith("first get 100 Smithing for Test hatchet") and hatchet.unlock_hours > 1


def test_urn_input_xp_counts():
    from rune_advisor.boosts import Urn

    cost = CostModel.default({"Mining": 100_000}, use_wiki=False)
    start, target = xp_for_level(90), xp_for_level(95)
    plain = Urn("Test urn", 0.2, 4_000, None, 0.01)
    fed = Urn("Test urn", 0.2, 4_000, None, 0.01, material_xp={"Mining": 400.0, "Crafting": 20.0})
    a = plan_level(cost, None, "Mining", start, 90, target, urn=plain)
    b = plan_level(cost, None, "Mining", start, 90, target, urn=fed)
    # Mining the urns' clay is Mining xp: the grind shrinks by that share; Crafting xp is a by-product.
    n = plain.count((target - start) / 1.2)
    assert b.grind_hours == pytest.approx(a.grind_hours * (1 - n * 400 / ((target - start) / 1.2)), rel=1e-3)
    assert b.byproduct_xp["Crafting"] == pytest.approx(n * 20) and "counted toward the goal" in b.steps[0]


# --- Ironman guide audit fixes (2026-10-30) -----------------------------------


def test_catalogue_casts_enchants_and_skips_sacred_clay():
    from rune_advisor.recommend.catalogue import catalogue

    rows = catalogue()
    assert not any(r.get("facility") == "Creation kiln" for r in rows)
    fury = next(r for r in rows if r["output"] == "Amulet of fury")
    assert fury["skill"] == "Magic" and fury["level"] == 87 and fury["xp"] == 97
    assert ["Cosmic rune", 1.0] in fury["materials"] and ["Onyx amulet", 1.0] in fury["materials"]
    assert not any(m.lower().startswith("lvl-") for m, _ in fury["materials"])
    # Ranging potion (4) was only a sacred-clay recipe, so (3)s never decanted up; now every family has a (4).
    assert any(r["output"] == "Ranging potion (4)" and r["materials"] == [["Ranging potion (3)", 4 / 3]] for r in rows)
    assert any(r["output"] == "Extreme necromancy (4)" and r.get("conversion") for r in rows)


def test_drop_aliases_and_unfarmable_monsters():
    from rune_advisor.drops import estimated_kills_per_hour, tables, unfarmable
    from rune_advisor.models import Skill

    assert any(item == "Black mask" for item, *_ in tables()["sources"]["Cave horror"])
    assert {"Nail beast", "Nightmare"} <= unfarmable()  # Temple Trekking event, Daemonheim
    p = PlayerState(name="X", members=True, skills={s: Skill(s, 99, xp_for_level(99)) for s in
                                                    ("Slayer", "Attack", "Strength", "Defence", "Hitpoints")})
    assert estimated_kills_per_hour(p, "Nail beast", ("Attack", 400_000)) is None
    assert estimated_kills_per_hour(p, "Cave horror", ("Attack", 400_000)) is not None


def test_dialogue_sellers_join_the_shop_tables():
    """Wyson the gardener sells woad leaves through dialogue, so the Storeline bucket has no row for him
    (data/shops_extra.json fills that in; user report, 2026-10-31)."""
    from rune_advisor.recommend import gathering

    offers = gathering.tables_shops()["Woad leaf"]
    assert any(shop.startswith("Wyson the gardener") and not members and price == 25 for shop, _, price, members in offers)
    assert offers == sorted(offers, key=lambda o: (o[2], -o[1]))  # merged offers stay cheapest first
    p = PlayerState(name="F", ironman=True, members=False)
    src = gathering.Gatherer(Context(player=p, cost=CostModel.default(use_wiki=False))).source("Woad leaf")
    assert src is not None and src.kind == "shop" and "Wyson" in src.how


def test_group_mates_lend_coins_through_group_storage(monkeypatch):
    """Coins move through group storage like any item (user, 2026-10-31): a plan's shop spending comes from the
    pouch, then group mates' purses, and only the rest is earned. Coins a recipe charges are spent, not gathered."""
    from rune_advisor.recommend import gathering
    from rune_advisor.recommend.gathering import Gatherer
    from rune_advisor.recommend.plans import item_plan

    p = PlayerState(name="Axo", ironman=True, members=True, inventories={"MONEY_POUCH": {"Coins": 100}},
                    group={"PlayerOne": {"Coins": 1_000_000}})
    ctx = Context(player=p, cost=CostModel.default(player=p, use_wiki=False))
    assert ctx.cover_coins(50) == (0.0, [])
    short, steps = ctx.cover_coins(500)
    assert short == 0 and steps == ["PlayerOne: put 400 coins in group storage (or trade them) for Axo"]
    short, steps = ctx.cover_coins(2_000_000)
    assert short == pytest.approx(999_900) and len(steps) == 1

    g = Gatherer(ctx).plan({"Coins": 5})
    assert g.coins == 5 and g.short == {} and g.hours == 0 and not any("Coins" in s for s in g.steps)

    monkeypatch.setattr(gathering, "tables_shops", lambda: {"Widget": [("Widget shop", 10_000, 1_000, True)]})
    plan = item_plan(ctx, "Widget", 10)
    assert plan is not None and plan.hours == pytest.approx(0, abs=0.01)  # 10,000 gp lent, no money making
    assert any("PlayerOne: put 9,900 coins" in s for s in plan.steps)
    assert not any("more gp" in s for s in plan.steps)
    p.group = {}
    plan = item_plan(ctx, "Widget", 10)
    assert any("earn ~9,900 more gp" in s for s in plan.steps)


def test_treasure_hunter_supplies_and_potion_tiers():
    from rune_advisor.requirements import _holds_potion, th_only_items
    from rune_advisor.models import Skill

    assert th_only_items("Protean traps; no drops from trapped jadinkos") == ["Protean trap"]
    assert th_only_items("Marasamaw plant") == []
    held = {"Extreme attack (3)": 5, "Extreme strength (2)": 1, "Super defence (4)": 2, "Grand ranging potion (6)": 1}
    assert _holds_potion(held, "Super melee potion")  # a stronger tier of each melee potion
    assert _holds_potion(held, "Super ranging potion") and _holds_potion(held, "Grand ranging potion")
    assert not _holds_potion(held, "Extreme ranging potion") and not _holds_potion(held, "Super magic potion")
    p = PlayerState(name="X", ironman=True, members=True, skills={"Hunter": Skill("Hunter", 99, xp_for_level(99))})
    cost = CostModel.default(player=p)
    assert any("Protean trap (Treasure Hunter only)" in missing for _, m, missing in cost.excluded if "protean" in m.lower())
    assert not any("protean" in r["method"].lower() for r in cost.wiki_rows.get("Hunter", []))
    # The free-to-play rows name the supply in the method, with no requirement text (user report, 2026-10-31).
    from rune_advisor.requirements import th_only_mentions
    assert th_only_mentions("Logs, portable brazier") == ["Portable brazier"]
    assert th_only_mentions("Maple logs, lit individually") == []
    free = PlayerState(name="Y", ironman=True, members=False, skills={"Firemaking": Skill("Firemaking", 1, 0)})
    free_cost = CostModel.default(player=free, members=False)
    assert any("Portable brazier (Treasure Hunter only)" in missing for _, _, missing in free_cost.excluded)
    assert not any("portable" in r["method"].lower() for r in free_cost.wiki_rows.get("Firemaking", []))


def test_weekly_shop_stock_and_perennial_patches(monkeypatch):
    from rune_advisor.models import Skill
    from rune_advisor.recommend import gathering
    from rune_advisor.recommend.gathering import Gatherer

    monkeypatch.setattr(gathering, "tables_shops", lambda: {
        "Widget": [["Gem Shop (Anachronia)", 35, 200.0, True]],  # a weekly-stock shop since DailyScape
        "Gadget": [["Some Store", 35, 200.0, True]],  # an ordinary one
        "Fake seed": [["Some Store", 100, 10.0, True]]})
    monkeypatch.setattr(gathering, "seeds", lambda: [
        {"seed": "Fake seed", "level": 1, "patch": ["Bush patch"], "crops": ["Fake fruit"], "plant_xp": 10.0,
         "harvest_xp": 5.0, "grow_minutes": 400, "harvest_life": None, "max_harvests": 4.0, "yield_per_harvest": None,
         "check_xp": 0}])
    p = PlayerState(name="X", ironman=True, members=True, skills={"Farming": Skill("Farming", 99, xp_for_level(99))})
    p.inventories = {"MONEY_POUCH": {"Coins": 10_000_000}}
    g = Gatherer(Context(player=p, cost=CostModel.default(use_wiki=False)))
    widget, gadget = g.source("Widget"), g.source("Gadget")
    assert widget.calendar_per_unit == pytest.approx(7 * 24 / 35) and gadget.calendar_per_unit == 0
    assert widget.cost_per_unit > gadget.cost_per_unit
    plan = g.plan({"Widget": 3_480})
    assert plan.calendar_hours == pytest.approx(3_480 * 7 * 24 / 35)
    assert any("weeks of stock" in s for s in plan.steps)
    # A bush is planted once: the seed's cost is spread over many harvests and each waits only for regrowth.
    fruit = g.source("Fake fruit")
    patches, crop, _ = g.patches("Bush patch", gathering.seeds()[0])
    assert fruit.kind == "farm" and fruit.calendar_per_unit == pytest.approx(gathering.PERENNIAL_PATCHES["Bush patch"] / 60 / (patches * crop))
    _, _, minutes = g.patches("Bush patch", gathering.seeds()[0])
    seed_share = fruit.hours_per_unit - minutes / 60 / (patches * crop)
    assert seed_share == pytest.approx(g.source("Fake seed").cost_per_unit / crop / gathering.PERENNIAL_HARVESTS)


def test_daily_nodes_and_pickpocket_sources():
    from rune_advisor import sim
    from rune_advisor.cost import custom_row
    from rune_advisor.mechanics import thieving
    from rune_advisor.models import Skill
    from rune_advisor.recommend.gathering import Gatherer

    p = PlayerState(name="X", ironman=True, members=True, skills={
        "Woodcutting": Skill("Woodcutting", 99, xp_for_level(99)), "Mining": Skill("Mining", 104, xp_for_level(104)),
        "Thieving": Skill("Thieving", 103, xp_for_level(103))})
    assert not any("Golden Bamboo" in r["method"] or "Alaea" in r["method"] for r in sim.method_rows(p))
    g = Gatherer(Context(player=p, cost=CostModel.default(use_wiki=False)))
    bamboo = g._simulated("Golden bamboo")
    assert bamboo.kind == "gather" and bamboo.calendar_per_unit == pytest.approx(24 / 300) and "a day" in bamboo.how
    # The Bamboo Market sells it for 30 chimes (currencies.py), which can beat the daily node.
    assert g.source("Golden bamboo").cost_per_unit <= bamboo.cost_per_unit
    assert g.source("Alaea sea salt").kind == "gather"
    farmer = next(r for r in thieving.method_rows(p) if "Master Farmer" in r["method"])
    assert farmer["drop_sources"] == ["Master Farmer"] and farmer["actions_per_hour"] > 0
    assert custom_row(farmer)["drop_sources"] == ["Master Farmer"]


def test_toolbelt_prices_the_belt_unlock_for_a_held_tool(player):
    from rune_advisor.recommend.tracks import ToolbeltTrack
    from rune_advisor.recommend.tracks.toolbelt import SLAYER_POINTS_PER_HOUR

    player.vars = {"TOOLBELT_CHARMING_IMP": 0, "TOOLBELT_HERBICIDE": 0}
    player.inventories = {"BANK": {"Charming imp": 1}}
    ctx = Context(player=player, cost=CostModel.default(use_wiki=False))
    cands = {c.title: c for c in ToolbeltTrack.candidates(ctx)}
    imp = cands["Charming imp"]  # owned: only the 500 Slayer points for the belt are left
    assert imp.cost_hours == pytest.approx(500 / SLAYER_POINTS_PER_HOUR, rel=0.01) and imp.skills == {"Slayer"}
    assert any("already have one" in d for d in imp.details)
    assert not any(d.startswith("earn") and "Dungeoneering" in d for d in imp.details)
    herbicide = cands["Herbicide"]  # not owned: the tokens and the unlock
    assert herbicide.cost_hours > imp.cost_hours and any("belt unlock" in d for d in herbicide.details)
    # An exported Slayer point balance pays for the unlock: no earning time, and a partial balance shortens it.
    player.currencies = {"Slayer points": 610}
    imp = {c.title: c for c in ToolbeltTrack.candidates(Context(player=player, cost=CostModel.default(use_wiki=False)))}["Charming imp"]
    assert imp.cost_hours == 0 and any("you have 610" in d for d in imp.details)
    player.currencies = {"Slayer points": 250}
    imp = {c.title: c for c in ToolbeltTrack.candidates(Context(player=player, cost=CostModel.default(use_wiki=False)))}["Charming imp"]
    assert imp.cost_hours == pytest.approx(250 / SLAYER_POINTS_PER_HOUR, rel=0.01)
    assert not any("isn't exported" in u for u in imp.unknowns)


def test_group_storage_override_counts_as_held():
    from rune_advisor.models import PlayerState
    p = PlayerState(name="PlayerTwo")
    p.inventories["BANK"] = {"Flax": 5}
    apply_overrides(p, {"group_storage": {"Flax": 5000, "Feather": 5000}, "players": {"PlayerOne": {}}})
    assert p.holdings["Flax"] == 5005 and p.holdings["Feather"] == 5000
    assert "GROUP_STORAGE" in p.inventories
    assert any("group storage" in n for n in p.notes)


def test_effigy_lamps_matched_by_item_id():
    """Both Effigy lamps are named "Effigy lamp": the export's item id picks the variant and its skills."""
    from rune_advisor.resources import load_pool
    from rune_advisor.xp import lamp_xp

    p = PlayerState(name="x", inventories={"INV": {"Effigy lamp": 1}}, item_ids={"Effigy lamp": 50529})
    lamps = {r.name: r for r in load_pool(p).resources if r.item == "Effigy lamp"}
    assert lamps["Effigy lamp (erratic)"].units == 1 and lamps["Effigy lamp (volatile)"].units == 0
    assert lamps["Effigy lamp (erratic)"].skills == {"Divination", "Farming", "Hunter", "Runecraft"}
    assert lamps["Effigy lamp (erratic)"].xp_per_unit(95) == lamp_xp(95, "medium")
    p.item_ids["Effigy lamp"] = 50528
    lamps = {r.name: r for r in load_pool(p).resources if r.item == "Effigy lamp"}
    assert lamps["Effigy lamp (volatile)"].units == 1 and lamps["Effigy lamp (erratic)"].units == 0
    # An unknown id (older export) counts for neither variant rather than both.
    p.item_ids = {}
    assert all(r.units == 0 for r in load_pool(p).resources if r.item == "Effigy lamp")


def _older_configs(configs):
    """The mock dump minus its newest achievement: a server built before the game's latest update."""
    from rune_advisor.sources.game_configs import GameConfigs

    older = GameConfigs(quests=dict(configs.quests), achievements=dict(configs.achievements))
    older.achievements.pop(max(older.achievements))
    return older


def _mock_configs_and_posts(tmp_path):
    from rune_advisor.sources.game_configs import GameConfigs

    # The mock game's configs, built by the exporter's own lib/configdump.lua (the dev plugin's "Dump configs"
    # saves the same entries: test_config_dump_matches_the_dev_plugins).
    dump = plugin_db.read_entries(_export_via_lua(tmp_path / "configs", "run_configs.lua"), "Rune Advisor Export")
    configs = GameConfigs.from_dumps(plugin_db.dumps(dump))
    entries = plugin_db.read_entries(_export_via_lua(tmp_path, "run_wire.lua"), "Rune Advisor Export")
    posts = sorted((int(k.split(":")[3]), v) for k, v in entries.items()
                   if k.startswith("wire:PlayerOne:post:") and isinstance(v, dict))
    return configs, entries, [dict(p, player="PlayerOne") for _, p in posts]


@needs_dev_plugin
def test_config_dump_matches_the_dev_plugins(tmp_path):
    """tests/run_configs.lua (the exporter's) prints the entries the dev plugin's "Dump configs" saves."""
    dev = plugin_db.dumps(plugin_db.read_entries(
        _export_via_lua(tmp_path / "dev", "run_session.lua", "rune_advisor_dev"), "Rune Advisor Dev"))
    ours = plugin_db.dumps(plugin_db.read_entries(
        _export_via_lua(tmp_path / "configs", "run_configs.lua"), "Rune Advisor Export"))
    for key in ("config:quests", "config:achievements"):
        assert ours[key] and sorted(ours[key], key=lambda e: e["id"]) == sorted(dev[key], key=lambda e: e["id"]), key


def test_export_api_applies_posts_like_the_replay(tmp_path):
    """The API server plays the same hash checks as rune_advisor.wire.replay and rebuilds the plugin's snapshot."""
    from rune_advisor import wire
    from rune_advisor.export_api import ApiError, ItemNames, Service, Store
    from rune_advisor.sources.game_configs import expand_snapshot

    configs, entries, posts = _mock_configs_and_posts(tmp_path)
    names = ItemNames({1511: {"name": "Logs", "config": "LOGS"}, 23716: {"name": "Crystal hatchet", "config": "CRYSTAL_AXE"}})
    service = Service(Store(tmp_path / "api"), configs, names)
    verdicts = []
    for post in posts:
        try:
            reply = service.apply("andrew", post)
            verdicts.append((post["kind"], True, reply["hash"]))
        except ApiError as e:
            assert e.status == 409, e.reason
            verdicts.append((post["kind"], False, None))
    r = wire.replay(entries, configs, "PlayerOne")
    assert [(v[0], v[1]) for v in verdicts] == [(p.kind, p.verdict == "accepted") for p in r.posts]
    assert verdicts[-1][2] == wire.state_hash(r.layout, r.state)
    # The stored record resumes: a delta against the stored hash applies, a stale one is refused.
    stale = dict(posts[1])  # the first delta, against the first full state's hash
    with pytest.raises(ApiError) as e:
        service.apply("andrew", stale)
    assert e.value.status == 409 and "base hash" in e.value.reason
    assert service.store.players("andrew")[0]["player"] == "PlayerOne"
    assert service.store.players("someone else") == []

    snap = service.snapshot("andrew", "PlayerOne")
    assert snap["schema"] == 2 and snap["player"] == "PlayerOne" and snap["membersWorld"] is True
    assert snap["skills"]["ATTACK"] == {"xp": 5347000, "level": 90}
    assert snap["vars"]["QP"] == 473 and snap["inventories"]["INV"]["items"][0] == {
        "id": 1511, "name": "Logs", "config": "LOGS", "count": 5, "placeholder": False}
    # Named vars come back signed, as the client reports them (a mirror reading -1 = "not loaded yet").
    from rune_advisor.export_api import _signed32
    assert _signed32(0xFFFFFFFF) == -1 and _signed32(1022623217) == 1022623217 and _signed32(0x80000000) == -(1 << 31)
    # The derived snapshot expands into the planner's shape: quest states and achievement tests with values.
    expanded, notes = expand_snapshot(snap, configs)
    assert notes == []
    heralds = next(q for q in expanded["quests"] if q["name"] == "Heralds of Crimson")
    assert heralds["started"] is True and heralds["finished"] is False
    # By the end of the scenario its post-quest achievement is complete (31 >= 20 kills, both flags set), so
    # the derived snapshot reports it done rather than listing tests.
    after = next(x for x in expanded["achievements"] if x["name"] == "After the Heralds")
    assert after["completed"] is True and "requirements" not in after


def test_export_api_static_manifest(tmp_path):
    """A full post names lib/exported_vars.lua's list by hash (the server holds every released one in
    wire_manifests/) and lists the entries its client lacks; those are 0 on the wire and left out of the
    snapshot. A 0.4.x post with its `named` list still works; a manifest the server lacks is refused."""
    import base64

    from rune_advisor import wire
    from rune_advisor.export_api import ApiError, ItemNames, Service, Store

    configs, entries, posts = _mock_configs_and_posts(tmp_path)
    full = posts[0]
    fields = wire.load_manifest(full["manifest"])
    assert fields is not None and ("SMKI_SLAYER_POINTS", 17, "bit") in fields and ("QP", 32, "var") in fields
    assert ("STATUE_BAG_CLIENT", 32, "client") in fields
    absent = {fields[i][0] for i in full["absent"]}
    assert "QP" not in absent and "ITEMLOG_ITEM_ABYSSAL_SLAYER_HELM" in absent  # the mock game has a few dozen
    service = Service(Store(tmp_path / "api"), configs, ItemNames({}))
    assert service.apply("andrew", full)["accepted"]
    rec = service.store.load_state("andrew", "PlayerOne")
    assert rec["layout"]["manifest"] == full["manifest"] and rec["layout"]["absent"] == sorted(full["absent"])
    snap = service.snapshot("andrew", "PlayerOne")
    assert snap["vars"]["SMKI_SLAYER_POINTS"] == 610 and "ITEMLOG_ITEM_ABYSSAL_SLAYER_HELM" not in snap["vars"]
    # A delta keeps the record's absent list.
    assert service.apply("andrew", posts[1])["accepted"]
    assert service.store.load_state("andrew", "PlayerOne")["layout"]["absent"] == sorted(full["absent"])
    # Bounds: an unknown manifest, absent indexes out of range or repeated.
    for bad in ({"manifest": full["manifest"] ^ 1}, {"absent": [len(fields)]}, {"absent": [0, 0]}, {"absent": [True]},
                {"skills": ["no spaces"]}, {"inventories": ["BANK", "BANK"]}):
        with pytest.raises(ApiError) as e:
            service.apply("someone else", dict(full, **bad))
        assert e.value.status == 400, bad
    # A 0.4.x post: the lists in the post, every name 32 bits, described as "|named:".
    r = wire.replay(entries, configs, "PlayerOne")
    names = [n for n, _, _ in fields]
    legacy = wire.WireLayout.build_named(configs, names, full["skills"], full["inventories"])
    assert legacy.description() != r.layout.description() and "|named:QP," in legacy.description()
    state = wire.WireState(list(r.state.slots), {k: dict(v) for k, v in r.state.invs.items()})
    canon = wire.canonical(legacy, state)
    old = {"player": "PlayerTwo", "kind": "full", "layout": legacy.hash, "new": wire.hash_canonical(legacy, canon),
           "seq": 1, "configs": configs.fingerprint, "plugin": "0.4.1", "named": names,
           "skills": full["skills"], "inventories": full["inventories"], "body": base64.b64encode(canon).decode()}
    assert service.apply("andrew", old)["accepted"]
    assert "named" in service.store.load_state("andrew", "PlayerTwo")["layout"]
    assert service.snapshot("andrew", "PlayerTwo")["vars"]["QP"] == 473


def test_exported_vars_manifest_is_on_the_server():
    """lib/exported_vars.lua's list is in wire_manifests/ entry for entry (scripts/build_exported_vars.py
    --check), so the server can read what a plugin built from it posts."""
    import subprocess
    import sys

    repo = Path(__file__).parent.parent
    result = subprocess.run([sys.executable, "scripts/build_exported_vars.py", "--check"], cwd=repo,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_export_api_over_http(tmp_path):
    """A real HTTP round trip: bearer auth, a full post, a delta, a stale delta (409), and the snapshot GET."""
    import http.client
    import threading

    from rune_advisor.export_api import Store, serve

    configs, _, posts = _mock_configs_and_posts(tmp_path)
    server = serve(tmp_path / "api", "127.0.0.1:0", "/export_api", configs=configs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = Store(tmp_path / "api").new_token("andrew")
        port = server.server_address[1]

        def call(method, path, body=None, auth=token, player=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": "application/json", **({"X-Player": player} if player else {})}
            if auth:
                headers["Authorization"] = f"Bearer {auth}"
            conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return resp.status, data

        assert call("GET", "/export_api/v1/health", auth=None) == (200, {"ok": True})
        assert call("POST", "/export_api/v1/state", posts[0], auth=None)[0] == 401
        status, reply = call("POST", "/export_api/v1/state", posts[0], auth="nope")
        assert status == 401 and reply["token"] == "unknown" and reply["retry_after"] == 300
        status, reply = call("POST", "/export_api/v1/state", posts[0])
        assert status == 200 and reply["accepted"] and reply["kind"] == "full" and reply["interval"] == 10
        # Load control: the interval is a server setting, picked up without a restart; a 409 carries it too.
        Store(tmp_path / "api").set_setting("post_interval", 5)
        status, reply = call("POST", "/export_api/v1/state", posts[1])
        assert status == 200 and reply["kind"] == "delta"
        status, reply = call("POST", "/export_api/v1/state", posts[1])  # the same delta again: base is stale
        assert status == 409 and not reply["accepted"] and "base hash" in reply["reason"] and reply["interval"] == 5
        with pytest.raises(ValueError):
            Store(tmp_path / "api").set_setting("post_interval", 1)  # below the plugin's clamp
        stats = Store(tmp_path / "api").stats(minutes=10)
        assert stats["recent_players"] == 1 and stats["tokens"] == 1 and stats["post_interval"] == 5
        status, reply = call("POST", "/v1/state", {"player": "PlayerOne", "kind": "delta"})  # prefix stripped upstream, malformed
        assert status == 400
        status, snap = call("GET", "/export_api/v1/snapshot", player="PlayerOne")
        assert status == 200 and snap["skills"]["ATTACK"]["xp"] == 5346332 and snap["vars"]["QP"] == 473
        status, listing = call("GET", "/export_api/v1/players")
        assert status == 200 and listing["players"][0]["player"] == "PlayerOne" and listing["players"][0]["posts"] == 2
        assert call("GET", "/export_api/v1/snapshot", player="Nobody")[0] == 404
        assert call("GET", "/export_api/v1/nothing")[0] == 404
        # A revoked token is told apart from an unknown one, so the plugin can forget it.
        assert Store(tmp_path / "api").revoke(token[:6]) == 1
        status, reply = call("POST", "/export_api/v1/state", posts[1])
        assert status == 401 and reply["token"] == "revoked"
        assert Store(tmp_path / "api").stats()["revoked_tokens"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_export_http_transport_lua():
    """plugins/rune_advisor_export/tests/run_http.lua: the token panel and the HTTP transport against the mock API."""
    import subprocess

    result = subprocess.run(["lua5.4", "plugins/rune_advisor_export/tests/run_http.lua"], capture_output=True, text=True,
                            cwd=Path(__file__).parent.parent)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "run_http: ok" in result.stdout


@needs_dev_plugin
def test_config_hashes_match_between_lua_and_python(tmp_path):
    """lib/canon.lua and game_configs.canonical hash every dumped config to the same number, and the set to the
    same fingerprint, so the server can tell which configs a client has from the hashes alone."""
    from rune_advisor.sources.game_configs import GameConfigs, canonical, config_hash, fingerprint

    dev = plugin_db.read_entries(_export_via_lua(tmp_path / "dev", "run_session.lua", "rune_advisor_dev"), "Rune Advisor Dev")
    dumps = plugin_db.dumps(dev)
    configs = GameConfigs.from_dumps(dumps)
    lua = dumps["config:hashes"]
    assert len(lua["achievements"]) == len(configs.achievements) and len(lua["quests"]) == len(configs.quests)
    for kind, table in (("quests", configs.quests), ("achievements", configs.achievements)):
        for cid, entry in table.items():
            assert config_hash(entry) == lua[kind][str(cid)], (kind, cid, canonical(entry)[:200])
    assert fingerprint(configs.hashes()) == lua["fingerprint"] == configs.fingerprint
    # State keys don't count: progressing an achievement leaves its hash alone; a changed test doesn't.
    a = dict(configs.achievements[4])
    a["progress"] = 5000
    a["requirements"] = [dict(t, complete=not t.get("complete")) for t in a["requirements"]]
    assert config_hash(a) == lua["achievements"]["4"]
    a["requirements"][0] = dict(a["requirements"][0], totalValue=999)
    assert config_hash(a) != lua["achievements"]["4"]
    # Canonical rules: whole floats as integers, sorted keys, empty containers alike.
    assert canonical({"b": 2.0, "a": [1, 1.5, "x\"y"], "c": {}, "d": []}) == '{"a":[1,1.5,"x\\"y"],"b":2,"c":{},"d":{}}'


def test_export_api_learns_new_configs_from_the_client(tmp_path):
    """A full post naming configs the server doesn't know gets 428 with the server's hashes; the client sends
    only the difference; the server composes it, checks the fingerprint, and decodes the layout from then on."""
    from rune_advisor.export_api import ApiError, Service, Store
    from rune_advisor.sources.game_configs import GameConfigs, config_hash, fingerprint

    configs, entries, posts = _mock_configs_and_posts(tmp_path)
    full = dict(posts[0])
    # The server runs an older dump: one achievement missing, one with a different test, one it alone has.
    older = GameConfigs(quests=dict(configs.quests), achievements=dict(configs.achievements))
    new_id = max(configs.achievements)  # the client's newest achievement is unknown to the server
    newest = older.achievements.pop(new_id)
    changed_id = 4
    older.achievements[changed_id] = dict(configs.achievements[4], requirements=[
        dict(t, totalValue=999) if t.get("kind") == "varTotal" else t for t in configs.achievements[4]["requirements"]])
    older.achievements[99999] = {"id": 99999, "name": "Gone", "requirements": []}
    service = Service(Store(tmp_path / "api"), older)
    assert full["configs"] == configs.fingerprint and full["configs"] != older.fingerprint
    with pytest.raises(ApiError) as e:
        service.apply("andrew", full)
    assert e.value.status == 428 and e.value.extra["need"] == "configs" and e.value.extra["base"] == older.fingerprint
    known = e.value.extra["known"]
    assert known["achievements"]["99999"] == config_hash(older.achievements[99999]) and str(new_id) not in known["achievements"]
    # The client's difference: changed + new entries, and the id the server listed that it lacks.
    mine = configs.hashes()
    added = {"quests": [], "achievements": [configs.achievements[i] for i in sorted(mine["achievements"])
                                             if known["achievements"].get(str(i)) != mine["achievements"][i]]}
    removed = {"quests": [], "achievements": [int(i) for i in known["achievements"] if int(i) not in mine["achievements"]]}
    assert [a["id"] for a in added["achievements"]] == sorted([changed_id, new_id]) and removed["achievements"] == [99999]
    # Two parts, in order; a wrong fingerprint is refused with the server's map again.
    reply = service.upload_configs("andrew", {"fingerprint": configs.fingerprint, "base": older.fingerprint, "part": 1,
                                              "parts": 2, "added": {"achievements": added["achievements"][:1]}, "removed": removed})
    assert reply["accepted"] and "learned" not in reply
    with pytest.raises(ApiError) as e:
        service.upload_configs("andrew", {"fingerprint": configs.fingerprint ^ 1, "base": older.fingerprint, "part": 1,
                                          "parts": 1, "added": added, "removed": removed})
    assert e.value.status == 409 and "hashes to" in e.value.reason and e.value.extra["need"] == "configs"
    reply = service.upload_configs("andrew", {"fingerprint": configs.fingerprint, "base": older.fingerprint, "part": 2,
                                              "parts": 2, "added": {"achievements": added["achievements"][1:]}})
    assert reply["accepted"] and reply["learned"]
    assert service.generation(configs.fingerprint).achievements[new_id] == newest
    # Learned from an upload, so pending: kept by content hash, not in the global generations/ folder.
    assert not (tmp_path / "api" / "generations" / f"{configs.fingerprint:08x}").exists()
    assert list((tmp_path / "api" / "pending_configs").glob("*/achievements.json.gz"))
    # Now the full post and the deltas go through, and the snapshot is derived with the learned configs.
    assert service.apply("andrew", full)["accepted"]
    for post in posts[1:]:
        try:
            service.apply("andrew", post)
        except ApiError as err:
            assert err.status == 409
    snap = service.snapshot("andrew", "PlayerOne")
    assert snap["configs"] == configs.fingerprint and snap["plugin"] == full.get("plugin")
    assert any(a["id"] == new_id for a in snap["achievements"])
    # A restarted server reloads the generation from disk, and an older plugin (no fingerprint) still works.
    again = Service(Store(tmp_path / "api"), older)
    assert again.generation(configs.fingerprint) is not None
    legacy = dict(full)
    del legacy["configs"]
    with pytest.raises(ApiError) as e:  # the layout from the older dump differs, so it's a 409, not a 428
        again.apply("legacy", legacy)
    assert e.value.status == 409


def test_events_log_merge(tmp_path):
    from datetime import datetime, timezone

    from rune_advisor import events as ev

    def e(i, text="level"):
        return {"date": f"2026-11-04T10:{i:02d}:00Z", "type": "rs_plog_levelup_count", "text": f"{text} {i}", "details": ""}

    window = lambda lo, hi: [e(i) for i in range(hi, lo - 1, -1)]  # newest first, like the API
    path = tmp_path / "x.jsonl"
    # First poll with fewer than 20 events: the whole history.
    m = ev.merge([], window(1, 5))
    assert [x["text"] for x in m.new] == [f"level {i}" for i in range(1, 6)] and not m.gap
    ev.append(path, m.new)
    # Overlapping window: only the newer events are appended, even when the oldest ones repeat verbatim.
    stored = ev.read_log(path)
    m = ev.merge(stored, window(3, 8))
    assert [x["text"] for x in m.new] == ["level 6", "level 7", "level 8"] and not m.gap
    ev.append(path, m.new)
    stored = ev.read_log(path)
    # Same window again: nothing new.
    assert ev.merge(stored, window(3, 8)).new == []
    # Two identical events in a row are kept as two (sequence alignment, no per-event dedupe).
    dup = [e(9), e(9)]
    m = ev.merge(stored, list(reversed([e(8), *dup])))
    assert [x["text"] for x in m.new] == ["level 9", "level 9"]
    ev.append(path, m.new)
    stored = ev.read_log(path)
    # A full window with no overlap means events scrolled past: a gap marker, then the window.
    m = ev.merge(stored, window(40, 59), now=datetime(2026, 11, 4, 12, tzinfo=timezone.utc))
    assert m.gap and m.new[0] == {"gap": True, "at": "2026-11-04T12:00:00+00:00"} and len(m.new) == 21
    ev.append(path, m.new)
    stored = ev.read_log(path)
    assert sum(1 for x in stored if x.get("gap")) == 1
    # A batched event rewritten in place (same date and type, new text) still aligns, loosely.
    changed = [dict(e(59), text="killed 31"), *window(41, 58)]
    m = ev.merge(stored, [e(60), *changed])
    assert m.loose and [x["text"] for x in m.new] == ["level 60"]


def test_export_api_token_add_registers_an_existing_value(tmp_path):
    """`token add` mirrors a token minted elsewhere (the public server's) so a plugin holding it posts here too."""
    from rune_advisor.export_api import Store, main

    store = Store(tmp_path / "api")
    assert store.owner("chvUnEn7LDTYpuUiKEMy7o8ciLAQsBpFSeWZsiyh3Ts") == (None, "unknown")
    assert main(["--data-dir", str(tmp_path / "api"), "token", "add", "andrew", "chvUnEn7LDTYpuUiKEMy7o8ciLAQsBpFSeWZsiyh3Ts"]) == 0
    assert store.owner("chvUnEn7LDTYpuUiKEMy7o8ciLAQsBpFSeWZsiyh3Ts") == ("andrew", "ok")
    assert [e["prefix"] for e in store.list_tokens()] == ["chvUnE"]
    # Too short to be a token: refused, nothing stored.
    assert main(["--data-dir", str(tmp_path / "api"), "token", "add", "andrew", "short"]) == 2
    assert len(store.list_tokens()) == 1
    # Re-adding a revoked token brings it back.
    store.revoke("chvUnE")
    assert store.owner("chvUnEn7LDTYpuUiKEMy7o8ciLAQsBpFSeWZsiyh3Ts") == (None, "revoked")
    store.add_token("andrew", "chvUnEn7LDTYpuUiKEMy7o8ciLAQsBpFSeWZsiyh3Ts")
    assert store.owner("chvUnEn7LDTYpuUiKEMy7o8ciLAQsBpFSeWZsiyh3Ts") == ("andrew", "ok")


class _HumanOnly:
    """A stand-in captcha: only the answer "human" passes."""

    def verify(self, response, ip):
        return response == "human"


class _ApiHarness:
    """A real server on a free port plus a tiny JSON client, for the accounts tests."""

    def __init__(self, tmp_path, captcha=None, older=False):
        import threading

        from rune_advisor.export_api import Captcha, Store, serve

        self.configs, _, self.posts = _mock_configs_and_posts(tmp_path)
        self.dir = tmp_path / "api"
        self.older = _older_configs(self.configs) if older else self.configs  # what the server was built with
        self.server = serve(self.dir, "127.0.0.1:0", "/export_api", configs=self.older, captcha=captcha or Captcha())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]
        self.store = self.server.service.store
        self.Store = Store

    def call(self, method, path, body=None, auth=None, headers=None):
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if auth:
            hdrs["Authorization"] = f"Bearer {auth}"
        conn.request(method, "/export_api" + path, body=json.dumps(body) if body is not None else None, headers=hdrs)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_export_api_accounts_tokens_and_scopes(tmp_path, monkeypatch):
    """Self-service accounts: key shown once, tokens with scopes, a read token limited to a character."""
    import hashlib

    from rune_advisor.export_api import _today
    from datetime import date

    h = _ApiHarness(tmp_path, captcha=_HumanOnly())
    try:
        full, delta = h.posts[0], h.posts[1]
        assert h.call("POST", "/v1/accounts", {})[0] == 403  # captcha refused
        assert h.call("POST", "/v1/accounts", {"captcha": "robot"})[0] == 403
        status, made = h.call("POST", "/v1/accounts", {"captcha": "human"})
        key = made["key"]
        assert status == 201 and key.startswith("raa_")
        # Only the key's hash is stored: the key itself appears nowhere in the database.
        dump = "\n".join(h.store.db.iterdump())
        assert key not in dump and hashlib.sha256(key.encode()).hexdigest() in dump

        # Token management needs the account key; a token is not a key and a key is not a token.
        assert h.call("GET", "/v1/tokens")[0] == 401
        assert h.call("GET", "/v1/tokens", auth="nope")[0] == 401
        status, plugin = h.call("POST", "/v1/tokens", {"label": "plugin", "scopes": ["submit"]}, auth=key)
        assert status == 201 and plugin["scopes"] == ["submit"]
        status, planner = h.call("POST", "/v1/tokens", {"label": "me", "scopes": ["read"]}, auth=key)
        status, mate = h.call("POST", "/v1/tokens", {"label": "mate", "scopes": ["read"], "characters": ["playerone"]}, auth=key)
        status, other = h.call("POST", "/v1/tokens", {"label": "other", "scopes": ["read"], "characters": ["Someone"]}, auth=key)
        assert h.call("POST", "/v1/tokens", {"scopes": ["admin"]}, auth=key)[0] == 400
        assert h.call("POST", "/v1/tokens", {"scopes": ["submit"], "characters": ["x"]}, auth=key)[0] == 400
        assert h.call("GET", "/v1/tokens", auth=plugin["token"])[0] == 401
        assert h.call("POST", "/v1/state", full, auth=key)[0] == 401

        # The submit token posts exactly as the plugin always did, may list characters, and cannot read them back.
        status, reply = h.call("POST", "/v1/state", full, auth=plugin["token"])
        assert status == 200 and reply["accepted"] and reply["interval"] == 10 and reply["kind"] == "full"
        assert h.call("POST", "/v1/state", delta, auth=plugin["token"])[0] == 200
        status, listing = h.call("GET", "/v1/players", auth=plugin["token"])
        assert status == 200 and listing["players"][0]["player"] == "PlayerOne"
        assert h.call("GET", "/v1/snapshot", auth=plugin["token"], headers={"X-Player": "PlayerOne"})[0] == 403
        assert h.call("GET", "/v1/state", auth=plugin["token"], headers={"X-Player": "PlayerOne"})[0] == 403
        # A read token reads but cannot post.
        status, snap = h.call("GET", "/v1/snapshot", auth=planner["token"], headers={"X-Player": "PlayerOne"})
        assert status == 200 and snap["vars"]["QP"] == 473
        status, reply = h.call("POST", "/v1/state", delta, auth=planner["token"])
        assert status == 403 and not reply["accepted"]
        assert h.call("POST", "/v1/configs", {}, auth=planner["token"])[0] == 403
        # A character-limited read token: its character (case-insensitive) only, the rest looks absent.
        assert h.call("GET", "/v1/snapshot", auth=mate["token"], headers={"X-Player": "PlayerOne"})[0] == 200
        assert h.call("GET", "/v1/snapshot", auth=other["token"], headers={"X-Player": "PlayerOne"})[0] == 404
        assert h.call("GET", "/v1/players", auth=other["token"]) == (200, {"players": []})
        assert [p["player"] for p in h.call("GET", "/v1/players", auth=mate["token"])[1]["players"]] == ["PlayerOne"]

        # Metadata: label, scopes, day-precision dates, never the token value.
        status, listed = h.call("GET", "/v1/tokens", auth=key)
        rows = {t["label"]: t for t in listed["tokens"]}
        assert status == 200 and set(rows) == {"plugin", "me", "mate", "other"}
        assert rows["mate"]["characters"] == ["playerone"] and rows["plugin"]["last_used"] == _today() == rows["plugin"]["created"]
        assert rows["other"]["last_used"] is not None  # the players call above
        assert not any(plugin["token"] in json.dumps(t) for t in listed["tokens"])
        assert h.call("GET", "/v1/account", auth=key)[1]["characters"] == 1

        # The account page's data: the characters held, and which token posts for which character.
        status, info = h.call("GET", "/v1/account", auth=key)
        assert status == 200 and [p["player"] for p in info["players"]] == ["PlayerOne"]
        assert info["players"][0]["posts"] >= 1 and len(info["players"][0]["last_post"]) == 10
        assert [p["player"] for p in rows["plugin"]["posted"]] == ["PlayerOne"]
        assert rows["plugin"]["posted"][0]["last_post"] == _today() and rows["me"]["posted"] == []
        assert h.store._q("SELECT COUNT(*) AS n FROM token_posts")[0]["n"] == 1  # noted once a day, not per post

        # Revoking one token tells that plugin "revoked", leaves the others alone.
        assert h.call("DELETE", "/v1/tokens", auth=key, headers={"X-Token-Id": plugin["id"]})[0] == 200
        assert h.store._q("SELECT COUNT(*) AS n FROM token_posts")[0]["n"] == 0  # the link went with the token
        status, reply = h.call("POST", "/v1/state", delta, auth=plugin["token"])
        assert status == 401 and reply["token"] == "revoked"
        assert h.call("DELETE", "/v1/tokens", auth=key, headers={"X-Token-Id": plugin["id"]})[0] == 404
        # Nothing about whose request it is goes in the address: an id in the path is no route, a missing
        # header is said so, and a query string is refused outright.
        assert h.call("DELETE", "/v1/tokens/" + plugin["id"], auth=key)[0] in (404, 405)
        assert h.call("DELETE", "/v1/tokens", auth=key)[0] == 400
        assert h.call("GET", "/v1/account?key=" + key)[0] == 400
        from rune_advisor.export_api import loggable_path

        assert loggable_path("/v1/tokens/0a1b2c3d/posted") == "/v1/tokens/<id>/posted"
        assert loggable_path("/v1/tokens/posted") == "/v1/tokens/posted"
        assert h.call("GET", "/v1/snapshot", auth=planner["token"], headers={"X-Player": "PlayerOne"})[0] == 200
        assert len(h.call("GET", "/v1/tokens", auth=key)[1]["tokens"]) == 3

        # The planner side: several tokens, a character found under whichever one holds it.
        from rune_advisor import api_client
        from rune_advisor.config import Settings

        monkeypatch.setattr(api_client, "CACHE_DIR", tmp_path / "cache")
        settings = Settings(api_url=f"http://127.0.0.1:{h.port}/export_api", api_tokens=[other["token"], mate["token"]])
        assert api_client.fetch_snapshot(settings, "playerone", max_age=0)["vars"]["QP"] == 473
        assert [p["player"] for p in api_client.players(settings)] == ["PlayerOne"]
        assert api_client.fetch_snapshot(Settings(api_url=settings.api_url, api_tokens=[other["token"]]), "Nobody", max_age=0) is None

        # Deleting one character's state, then the whole account.
        # The name travels in the X-Player header (percent-encoded), so it is in no URL and no access log.
        named = {"X-Player": "PlayerOne"}
        assert h.call("GET", "/v1/snapshot", auth=planner["token"], headers=named)[0] == 200
        assert h.call("GET", "/v1/state", auth=planner["token"], headers={"X-Player": "%50layerOne"})[0] == 200
        assert h.call("GET", "/v1/snapshot", auth=planner["token"])[0] == 400  # no name given
        assert h.call("GET", "/v1/snapshot", auth=planner["token"], headers={"X-Player": "Nobody"})[0] == 404
        assert h.call("DELETE", "/v1/players", auth=key)[0] == 400
        assert h.call("DELETE", "/v1/players", auth=plugin["token"], headers=named)[0] == 401  # tokens can't delete
        assert h.call("DELETE", "/v1/players/PlayerOne", auth=key)[0] == 404  # a name in the path is not a route
        assert h.call("GET", "/v1/snapshot/PlayerOne", auth=planner["token"])[0] == 404
        assert h.call("DELETE", "/v1/players", auth=key, headers=named)[0] == 200
        assert h.call("GET", "/v1/snapshot", auth=planner["token"], headers=named)[0] == 404
        assert h.call("DELETE", "/v1/players", auth=key, headers=named)[0] == 404
        h.store.save_state(made["account"], "PlayerOne", {"player": "PlayerOne", "updated": 1.0, "seq": 1, "posts": 1})
        status, gone = h.call("DELETE", "/v1/account", auth=key)
        assert status == 200 and gone == {"deleted": True, "characters": 1, "tokens": 3}
        assert h.store.stats()["players"] == 0 and h.store.stats()["accounts"] == 0
        assert h.call("GET", "/v1/account", auth=key)[0] == 401
        status, reply = h.call("GET", "/v1/players", auth=planner["token"])
        assert status == 401 and reply["token"] == "revoked"  # a bare revoked hash remains: no account, label or characters
        # What is left of a deleted account: one bare row per token (its hash, the day it was revoked, a random id).
        cols = "account, prefix, label, scopes, characters, last_used, created = revoked AS same_day"
        assert [dict(r) for r in h.store.db.execute(f"SELECT {cols} FROM tokens WHERE revoked IS NOT NULL")] == [
            {"account": "", "prefix": "", "label": "", "scopes": "", "characters": None, "last_used": None, "same_day": 1}] * 4
        assert h.store.stats()["tokens"] == 0 and h.store.stats()["revoked_tokens"] == 4
        # A row revoked by an older server (prefix, scopes and dates kept) is stripped by the next sweep.
        h.store.db.execute("UPDATE tokens SET prefix = 'abcdef', scopes = 'read', last_used = '2026-11-01', created = '2026-10-01'")
        h.store.sweep()
        assert {tuple(r) for r in h.store.db.execute("SELECT prefix, scopes, last_used, created = revoked FROM tokens")} == {("", "", None, 1)}
    finally:
        h.close()


def test_export_api_account_creation_limit_and_proxy(tmp_path):
    """Per-IP creation limit, in memory; X-Forwarded-For counts only with trust_proxy (last entry: nginx's)."""
    h = _ApiHarness(tmp_path)
    try:
        h.store.set_setting("account_ip_limit", 2)
        spoof = {"X-Forwarded-For": "1.1.1.1"}
        assert h.call("POST", "/v1/accounts", {})[0] == 201
        assert h.call("POST", "/v1/accounts", {})[0] == 201
        assert h.call("POST", "/v1/accounts", {})[0] == 429
        assert h.call("POST", "/v1/accounts", {}, headers=spoof)[0] == 429  # forwarded address ignored without trust_proxy
        h.store.set_setting("trust_proxy", 1)
        behind = {"X-Forwarded-For": "6.6.6.6, 9.9.9.9"}  # a forged first entry, nginx's real one last
        assert h.call("POST", "/v1/accounts", {}, headers=behind)[0] == 201
        assert h.call("POST", "/v1/accounts", {}, headers={"X-Forwarded-For": "7.7.7.7, 9.9.9.9"})[0] == 201
        assert h.call("POST", "/v1/accounts", {}, headers=behind)[0] == 429
        assert h.call("POST", "/v1/accounts", {}, headers={"X-Forwarded-For": "6.6.6.6, 8.8.8.8"})[0] == 201
        from rune_advisor import export_api
        # IPv6 counts per /64: other addresses in the same allocation share the limit, the next /64 does not.
        v6 = lambda tail: {"X-Forwarded-For": f"2001:db8:1:2:{tail}"}
        assert h.call("POST", "/v1/accounts", {}, headers=v6("a::1"))[0] == 201
        assert h.call("POST", "/v1/accounts", {}, headers=v6("b::2"))[0] == 201
        assert h.call("POST", "/v1/accounts", {}, headers=v6("c::3"))[0] == 429
        assert h.call("POST", "/v1/accounts", {}, headers={"X-Forwarded-For": "2001:db8:1:3::1"})[0] == 201
        assert export_api.limit_key("::ffff:9.9.9.9") == "9.9.9.9" and export_api.limit_key("not an address") == "not an address"
        assert export_api.loggable_path("/v1/snapshot/Some%20Name?x=1") == "/v1/snapshot/<player>"
        assert export_api.loggable_path("/v1/players") == "/v1/players" and export_api.loggable_path("/v1/state") == "/v1/state"
        # Nothing about the creator is stored: the tables hold no address column at all.
        columns = {c[1] for t in ("accounts", "tokens", "states") for c in h.store.db.execute(f"PRAGMA table_info({t})")}
        assert not {c for c in columns if "ip" in c.split("_") or "agent" in c}
        assert h.store.stats()["accounts"] == 8
    finally:
        h.close()


def test_export_api_quotas(tmp_path):
    h = _ApiHarness(tmp_path)
    try:
        key = h.call("POST", "/v1/accounts", {})[1]["key"]
        h.store.set_setting("max_tokens", 2)
        h.store.set_setting("max_characters", 1)
        status, t1 = h.call("POST", "/v1/tokens", {"scopes": ["submit"]}, auth=key)
        assert status == 201 and h.call("POST", "/v1/tokens", {"scopes": ["read"]}, auth=key)[0] == 201
        status, reply = h.call("POST", "/v1/tokens", {"scopes": ["read"]}, auth=key)
        assert status == 403 and "token limit" in reply["reason"]
        assert h.call("DELETE", "/v1/tokens", auth=key, headers={"X-Token-Id": t1["id"]})[0] == 200  # revoked ones don't count
        assert h.call("POST", "/v1/tokens", {"scopes": ["submit"]}, auth=key)[0] == 201
        assert h.call("GET", "/v1/account", auth=key)[1]["max_tokens"] == 2
    finally:
        h.close()


def test_export_api_character_quota(tmp_path):
    """A full account refuses a new character, but its existing ones keep posting."""
    from rune_advisor.export_api import ApiError, ItemNames, Service, Store

    configs, _, posts = _mock_configs_and_posts(tmp_path)
    store = Store(tmp_path / "api")
    service = Service(store, configs, ItemNames())
    store.set_setting("max_characters", 1)
    assert service.apply("acct", posts[0])["accepted"]
    with pytest.raises(ApiError) as e:
        service.apply("acct", dict(posts[0], player="Second"))
    assert e.value.status == 403 and "character limit" in e.value.reason
    assert service.apply("acct", dict(posts[0]))["accepted"]  # PlayerOne again: not a new character
    assert service.apply("other account", dict(posts[0], player="Second"))["accepted"]  # quotas are per account
    assert store.count_states("acct") == 1


def test_export_api_token_quota_counts_live_tokens_only(tmp_path):
    from rune_advisor.export_api import ApiError, ItemNames, Service, Store

    configs, _, _ = _mock_configs_and_posts(tmp_path)
    store = Store(tmp_path / "api")
    service = Service(store, configs, ItemNames())
    account, _ = store.create_account()
    store.set_setting("max_tokens", 2)
    first = service.create_token(account, {"label": "a"})
    service.create_token(account, {"label": "b"})
    with pytest.raises(ApiError) as e:
        service.create_token(account, {"label": "c"})
    assert e.value.status == 403
    store.revoke_id(account, first["id"])
    assert service.create_token(account, {"label": "c"})["label"] == "c"


def test_export_api_accounts_copy_leaves_plugin_data_out(tmp_path):
    """The long-lived backup copy: accounts and tokens stay, character states and name-limited tokens do not,
    and a service started on it gets the states back from the plugins."""
    import sqlite3

    from rune_advisor import export_api
    from rune_advisor.export_api import Store

    store = Store(tmp_path / "live")
    account, key = store.create_account()
    _, plugin = store.create_token(account, "plugin", ["submit"], None)
    _, planner = store.create_token(account, "planner", ["read"], None)
    mate_id, mate = store.create_token(account, "mate", ["read"], ["Zezima"])
    store.save_state(account, "Zezima", {"player": "Zezima", "updated": 1.0, "seq": 1, "posts": 1})
    planner_info, mate_info = store.lookup(planner)[0], store.lookup(mate)[0]
    store.put_blob(planner_info, b"sealed settings, kept" + bytes(16), 0)
    store.put_blob(mate_info, b"sealed settings, dropped" + bytes(16), 0)
    out = tmp_path / "long" / "export_api.sqlite3"
    assert export_api.main(["--data-dir", str(tmp_path / "live"), "accounts-copy", str(out)]) == 0
    assert store.players(account) and store.lookup(mate)[1] == "ok"  # the live store is untouched
    store.close()
    assert b"Zezima" not in out.read_bytes()
    counts, problems = export_api.check_data_dir(out.parent)
    assert problems == [] and (counts["accounts"], counts["players"], counts["tokens"], counts["revoked_tokens"]) == (1, 0, 2, 1)
    restored = Store(out.parent)
    assert restored.account_by_key(key) == account and restored.players(account) == []
    assert restored.lookup(plugin)[1] == "ok" and restored.lookup(planner)[1] == "ok"
    assert restored.lookup(mate) == (None, "revoked")  # never unlimited
    # The user's settings stay (they are not plugin data), except those of a token the copy made bare.
    assert restored.blob(planner_info.id)["data"].startswith(b"sealed settings, kept") and restored.blob(mate_id) is None
    restored.close()
    assert export_api.main(["--data-dir", str(tmp_path / "nothing"), "accounts-copy", str(tmp_path / "x.sqlite3")]) == 2


def test_site_policy_pages_are_rendered_from_their_documents():
    """site/privacy and site/terms are generated from docs/policy/*.md (the authoritative text): the committed
    pages must be exactly what the documents render to. After editing a document run
    `uv run python scripts/build_site_pages.py`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_site_pages", Path(__file__).parent.parent / "scripts" / "build_site_pages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rendered = module.pages()
    assert {p.parent.name for p in rendered} == {"privacy", "terms"}
    for path, page in rendered.items():
        assert path.read_text(encoding="utf-8") == page, f"{path} is stale: run scripts/build_site_pages.py"
        assert "<script" not in page and "style=" not in page  # the site's CSP allows neither inline


def test_listing_description_matches_its_source():
    """plugins/listing/description.html (pasted into Mod.io, which takes HTML) is rendered from the exporter's
    README by `uv run python scripts/build_listing_html.py`."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_listing_html", Path(__file__).parent.parent / "scripts" / "build_listing_html.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    html_text = mod.render()
    assert mod.OUT.read_text(encoding="utf-8") == html_text, f"{mod.OUT} is stale: run scripts/build_listing_html.py"
    assert "subject to the Jagex EULA" in html_text and "<script" not in html_text and "\n<" not in html_text.replace(">\n<", "><")


def test_export_api_manual_data_requests(tmp_path, capsys, monkeypatch):
    """`request find/delete-player/delete-account`: by character name, by account id, by a key or token; a dry
    run without --yes; a limited read token cannot delete; an ambiguous name needs the account."""
    import io

    from rune_advisor import export_api
    from rune_advisor.export_api import Store

    data = tmp_path / "api"
    store = Store(data)
    a, key_a = store.create_account()
    b, _ = store.create_account()
    _, plugin = store.create_token(a, "plugin", ["submit"], None)
    _, mate = store.create_token(a, "mate", ["read"], ["Zezima"])
    for account, player in ((a, "Zezima"), (a, "Alt One"), (b, "zezima")):
        store.save_state(account, player, {"player": player, "updated": 1.0, "seq": 1, "posts": 1})
    store.close()

    def run(*argv, stdin=None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        code = export_api.main(["--data-dir", str(data), "request", *argv])
        return code, capsys.readouterr().out

    code, out = run("find", "--player", "ZEZIMA")
    assert code == 0 and a in out and b in out and "1970-01-01" in out
    assert run("find", "--player", "Nobody")[0] == 1
    code, out = run("find", "--secret", "-", stdin=plugin + "\n")  # read from stdin, not the command line
    assert code == 0 and f"token of account {a}" in out and "2 character(s)" in out
    assert run("find", "--secret", "nope")[0] == 1
    # An export goes to a file, only with --yes, and never to the terminal.
    dump = tmp_path / "export.json"
    code, out = run("export", f"--secret={plugin}", "--out", str(dump))
    assert code == 0 and "would write" in out and not dump.exists()
    assert run("export", f"--secret={mate}", "--out", str(dump), "--yes")[0] == 2
    assert run("export", "--player", "zezima", "--out", str(dump), "--yes")[0] == 2  # two accounts hold it
    code, out = run("export", f"--secret={plugin}", "--out", str(dump), "--yes")
    exported = json.loads(dump.read_text())
    assert code == 0 and "Alt One" not in out and dump.stat().st_mode & 0o777 == 0o600
    assert exported["account"]["id"] == a and len(exported["tokens"]) == 2 and len(exported["characters"]) == 2
    assert plugin not in dump.read_text() and all("hash" not in t for t in exported["tokens"])
    assert run("export", "--player", "alt one", "--out", str(dump), "--yes")[0] == 0
    assert list(json.loads(dump.read_text())) == ["exported", "service", "characters"]
    # A name two accounts hold is ambiguous until the account is named; without --yes nothing is deleted.
    assert run("delete-player", "--player", "zezima", "--yes")[0] == 2
    code, out = run("delete-player", "--player", "zezima", "--account", b)
    assert code == 0 and "would delete" in out
    assert run("delete-player", "--player", "zezima", "--account", b, "--yes")[0] == 0
    assert run("find", "--player", "zezima", "--account", b)[0] == 1
    # With a token as the proof these commands refuse and point at `request delete`; the account key is fine.
    assert run("delete-player", "--player", "Zezima", f"--secret={mate}", "--yes")[0] == 2
    assert run("delete-account", f"--secret={mate}", "--yes")[0] == 2
    code, out = run("delete-player", "--player", "Alt One", f"--secret={plugin}", "--yes")
    assert code == 2 and "request delete --secret" in out
    assert run("delete-player", "--player", "Alt One", "--account", a, "--yes")[0] == 0
    assert run("delete-account", f"--secret={key_a}", "--account", b, "--yes")[0] == 2  # the key is not b's
    code, out = run("delete-account", f"--secret={key_a}", "--yes")
    assert code == 0 and "1 character(s)" in out
    check = Store(data)
    assert check.account_info(a) is None and check.account_info(b) is not None and check.lookup(plugin) == (None, "revoked")
    check.close()
    assert run("delete-account", "--account", a, "--yes")[0] == 1


def test_export_api_delete_by_what_a_token_proves(tmp_path, capsys, monkeypatch):
    """`request delete --secret`: the report of characters and tokens, the suggestion (the whole account when the
    plugin's token posted for every character, its own characters otherwise, nothing for a read token), the
    operator's override, and that a reused character name gives no hold over another account."""
    import io

    from rune_advisor import export_api
    from rune_advisor.export_api import Store

    data = tmp_path / "api"
    store = Store(data)

    def account_with(*characters):
        account, key = store.create_account()
        tokens = {}
        for label, names in characters:
            token_id, token = store.create_token(account, label, ["submit"], None)
            tokens[label] = (token_id, token)
            for name in names:
                store.save_state(account, name, {"player": name, "updated": 1.0, "seq": 1, "posts": 1})
                store.note_post(token_id, account, name)
        return account, key, tokens

    solo, _, solo_tokens = account_with(("plugin", ["Zezima"]))
    shared, shared_key, shared_tokens = account_with(("mine", ["Durial"]), ("mate", ["Elvarg"]))
    _, reader = store.create_token(shared, "planner", ["read"], None)
    # Someone else now plays under a name the first account once posted: their own account, their own token.
    other, _, other_tokens = account_with(("plugin", ["zezima"]))
    assert [p["player"] for p in store.posted_by(other_tokens["plugin"][0])] == ["zezima"]
    store.close()

    def run(*argv, stdin=None):
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin or ""))
        code = export_api.main(["--data-dir", str(data), "request", "delete", *argv])
        return code, capsys.readouterr().out

    # One token that posted for all the account holds: the account is suggested, and Enter takes it. The
    # report names characters and tokens by their first characters, never a whole token.
    code, out = run("--secret", "-", stdin=other_tokens["plugin"][1] + "\n\n")
    assert code == 0 and "suggested: account" in out and f"deleted account {other}" in out
    assert "character 'zezima'" in out and other_tokens["plugin"][1][:6] + "..." in out
    assert other_tokens["plugin"][1] not in out
    check = Store(data)
    assert check.account_info(other) is None
    # ...and the account of the same name is untouched: the claim never went by the name.
    assert [p["player"] for p in check.players(solo)] == ["Zezima"]
    check.close()

    # Two plugins in one account: one token covers only its own character, and that is the suggestion.
    code, out = run("--secret", shared_tokens["mine"][1], "--choice", "none")
    assert code == 0 and "suggested: characters" in out and "'Durial'" in out and "nothing deleted" in out
    code, out = run("--secret", shared_tokens["mine"][1], stdin="\n")
    assert code == 0 and "deleted 1 character(s)" in out and "Durial" in out and "revoke it" in out
    check = Store(data)
    assert [p["player"] for p in check.players(shared)] == ["Elvarg"] and check.lookup(shared_tokens["mine"][1])[1] == "ok"
    check.close()
    # A read token: nothing by default; the operator may still choose more.
    code, out = run("--secret", reader, stdin="\n")
    assert code == 0 and "suggested: none" in out and "nothing deleted" in out
    assert run("--secret", reader, stdin="x\n")[0] == 2  # not a choice: nothing happens
    # The operator overrides a narrow suggestion and deletes the whole account.
    code, out = run("--secret", shared_tokens["mate"][1], stdin="a\n")
    assert code == 0 and "suggested: account" in out  # by now 'mate' posted for everything that is left
    check = Store(data)
    assert check.account_info(shared) is None and check.account_info(solo) is not None
    check.close()
    assert run("--secret", "nope")[0] == 1


def test_export_api_delete_what_a_token_posted_over_http(tmp_path):
    """`GET/DELETE /v1/posted` with a plugin's token, and `DELETE /v1/tokens/posted` (the id in X-Token-Id) with the account key:
    exactly the characters that token posted for go, the token is revoked, and other accounts are out of reach."""
    h = _ApiHarness(tmp_path)
    try:
        full = h.posts[0]
        key = h.call("POST", "/v1/accounts", {})[1]["key"]
        plugin = h.call("POST", "/v1/tokens", {"label": "plugin", "scopes": ["submit"]}, auth=key)[1]
        idle = h.call("POST", "/v1/tokens", {"label": "spare", "scopes": ["submit"]}, auth=key)[1]
        reader = h.call("POST", "/v1/tokens", {"label": "planner", "scopes": ["read"]}, auth=key)[1]
        assert h.call("POST", "/v1/state", full, auth=plugin["token"])[0] == 200
        account = h.call("GET", "/v1/account", auth=key)[1]["id"]
        # Another account holds a character of the same name, posted by its own token.
        other_account, _ = h.store.create_account()
        other_id, other_token = h.store.create_token(other_account, "plugin", ["submit"], None)
        h.store.save_state(other_account, "PlayerOne", {"player": "PlayerOne", "updated": 1.0, "seq": 1, "posts": 1})
        h.store.note_post(other_id, other_account, "PlayerOne")

        status, seen = h.call("GET", "/v1/posted", auth=plugin["token"])
        assert status == 200 and [p["player"] for p in seen["posted"]] == ["PlayerOne"] and seen["token"]["label"] == "plugin"
        assert h.call("GET", "/v1/posted", auth=reader["token"])[0] == 403  # a read token posted nothing
        assert h.call("GET", "/v1/posted", auth=key)[0] == 401
        assert h.call("GET", "/v1/posted", auth=idle["token"])[1]["posted"] == []
        # A token that posted nothing deletes nothing (and is revoked, as asked).
        assert h.call("DELETE", "/v1/posted", auth=idle["token"])[1] == {"deleted": [], "revoked": True}
        assert [p["player"] for p in h.store.players(account)] == ["PlayerOne"]
        # The key cannot use another account's token id, and the token route never reaches another account.
        assert h.call("DELETE", "/v1/tokens/posted", auth=key, headers={"X-Token-Id": other_id})[0] == 404
        status, gone = h.call("DELETE", "/v1/posted", auth=plugin["token"])
        assert status == 200 and gone == {"deleted": ["PlayerOne"], "revoked": True}
        assert h.store.players(account) == [] and [p["player"] for p in h.store.players(other_account)] == ["PlayerOne"]
        status, reply = h.call("POST", "/v1/state", full, auth=plugin["token"])
        assert status == 401 and reply["token"] == "revoked"  # the plugin is told to stop
        # With the account key, for one of the account's tokens.
        again = h.call("POST", "/v1/tokens", {"label": "plugin 2", "scopes": ["submit"]}, auth=key)[1]
        assert h.call("POST", "/v1/state", full, auth=again["token"])[0] == 200
        status, gone = h.call("DELETE", "/v1/tokens/posted", auth=key, headers={"X-Token-Id": again["id"]})
        assert status == 200 and gone["deleted"] == ["PlayerOne"] and h.store.players(account) == []
        assert h.store.lookup(again["token"]) == (None, "revoked") and h.store.lookup(other_token)[1] == "ok"
    finally:
        h.close()


def test_token_keys_hkdf_and_split():
    """The derivation the account page uses: RFC 5869 HKDF-SHA256, and a planner token split at the dot."""
    from rune_advisor import token_keys

    # RFC 5869 test case 3: no salt, no info.
    assert token_keys.hkdf_sha256(bytes([0x0B] * 22), b"", 42).hex() == (
        "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8")
    key, tid = "raa_" + "k" * 43, "0" * 32
    held = token_keys.planner_token(key, tid, 0)
    sent, enc = token_keys.split(held)
    assert sent == token_keys.bearer(key, tid, 0) and len(sent) == 47 and enc == token_keys.settings_key(key, tid, 0)
    assert token_keys.bearer(key, tid, 1) != sent and token_keys.split(sent) == (sent, None)


def test_token_keys_javascript_matches_python(tmp_path):
    """site/assets/tokenkeys.js (account page, planner page) derives the same tokens as token_keys.py, and a
    blob it seals opens again only with its own token's key."""
    import shutil
    import subprocess

    from rune_advisor import token_keys

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = tmp_path / "check.mjs"
    script.write_text("""
import { readFileSync } from "node:fs";
new Function(readFileSync(process.argv[2], "utf8"))();
const K = globalThis.RATokenKeys;
const key = "raa_" + "k".repeat(43), id = "0123456789abcdef0123456789abcdef";
const held = await K.plannerToken(key, id, 3);
const sealed = await K.sealBlob(K.split(held).enc, { format: 1, settings: { "overrides.toml": "x = 1" } });
const back = await K.openBlob(K.split(held).enc, sealed);
let other = "opened";
try { await K.openBlob(await K.settingsKey(key, id, 4), sealed); } catch (e) { other = "refused"; }
console.log(JSON.stringify({ bearer: await K.bearer(key, id, 3), held, back, other, id: K.randomId() }));
""")
    out = json.loads(subprocess.run([node, str(script), str(Path(__file__).parent.parent / "site" / "assets" / "tokenkeys.js")],
                                    capture_output=True, text=True, check=True).stdout)
    key, tid = "raa_" + "k" * 43, "0123456789abcdef0123456789abcdef"
    assert out["bearer"] == token_keys.bearer(key, tid, 3)
    assert out["held"] == token_keys.planner_token(key, tid, 3)
    assert out["back"] == {"format": 1, "settings": {"overrides.toml": "x = 1"}} and out["other"] == "refused"
    assert len(out["id"]) == 32 and int(out["id"], 16) >= 0


def test_export_api_derived_tokens_rotation_and_blobs(tmp_path):
    """Tokens the account page derives from the key, settings blobs per token, rotation by the operator and by the page."""
    import base64

    from rune_advisor import token_keys

    h = _ApiHarness(tmp_path)
    try:
        key = h.call("POST", "/v1/accounts", {})[1]["key"]
        account = h.call("GET", "/v1/account", auth=key)[1]["id"]
        tid = "a" * 32
        bearer = token_keys.bearer(key, tid, 0)
        made = {"id": tid, "token": bearer, "label": "Web planner", "scopes": ["read"], "kind": "web"}
        status, reply = h.call("POST", "/v1/tokens", made, auth=key)
        assert status == 201 and reply["derived"] and reply["epoch"] == 0 and "token" not in reply
        # One web token per account; an id or a value is never taken twice; malformed values are refused.
        assert h.call("POST", "/v1/tokens", {**made, "id": "b" * 32, "token": token_keys.bearer(key, "b" * 32, 0)}, auth=key)[0] == 409
        assert h.call("POST", "/v1/tokens", {**made, "kind": ""}, auth=key)[0] == 409
        assert h.call("POST", "/v1/tokens", {**made, "token": "rat_short", "kind": ""}, auth=key)[0] == 400
        assert h.call("POST", "/v1/tokens", {**made, "id": "XYZ", "kind": ""}, auth=key)[0] == 400
        assert h.call("POST", "/v1/tokens", {**made, "kind": "web", "scopes": ["read", "submit"]}, auth=key)[0] == 400
        assert h.call("POST", "/v1/tokens", {"label": "x", "scopes": ["read"], "kind": "web"}, auth=key)[0] == 400
        # The key itself, and the settings half of the planner token, appear nowhere in the store.
        held = token_keys.planner_token(key, tid, 0)
        assert h.call("GET", "/v1/players", auth=held)[0] == 400  # the part after the dot is refused, not used
        assert h.call("GET", "/v1/players", auth=bearer)[0] == 200
        dump = "\n".join(h.store.db.iterdump())
        assert bearer not in dump and held.split(".")[1] not in dump

        # Blobs: versioned, size-limited, read tokens only.
        assert h.call("GET", "/v1/blob", auth=bearer)[0] == 404
        data = base64.b64encode(b"n" * 12 + b"ciphertext and a tag....").decode()
        assert h.call("POST", "/v1/blob", {"version": 0, "data": data}, auth=bearer) == (200, {"version": 1})
        status, stale = h.call("POST", "/v1/blob", {"version": 0, "data": data}, auth=bearer)
        assert status == 412 and stale["version"] == 1
        assert h.call("POST", "/v1/blob", {"version": 1, "data": "!!"}, auth=bearer)[0] == 400
        assert h.call("POST", "/v1/blob", {"version": 1, "data": base64.b64encode(b"x" * 70000).decode()}, auth=bearer)[0] == 413
        assert h.call("GET", "/v1/blob", auth=bearer)[1] == {"version": 1, "data": data}
        plugin = h.call("POST", "/v1/tokens", {"label": "plugin", "scopes": ["submit"]}, auth=key)[1]
        assert h.call("GET", "/v1/blob", auth=plugin["token"])[0] == 403
        listed = {t["id"]: t for t in h.call("GET", "/v1/tokens", auth=key)[1]["tokens"]}
        assert listed[tid]["blob"]["version"] == 1 and listed[tid]["kind"] == "web" and listed[plugin["id"]]["blob"] is None

        # The operator rotates after a leak: the old value is told "revoked", the blob waits for the account page.
        assert h.store.rotate(tid) == "pending" and h.store.rotate(tid) == "already pending"
        status, reply = h.call("GET", "/v1/blob", auth=bearer)
        assert status == 401 and reply["token"] == "revoked"
        listed = {t["id"]: t for t in h.call("GET", "/v1/tokens", auth=key)[1]["tokens"]}
        assert listed[tid]["pending"] and listed[tid]["epoch"] == 1 and listed[tid]["blob"]["epoch"] == 0
        # The page reads the old blob with the key, registers the new value, and re-encrypts.
        status, old = h.call("GET", "/v1/tokens/blob", auth=key, headers={"X-Token-Id": tid})
        assert status == 200 and old == {"version": 1, "epoch": 0, "data": data}
        assert h.call("GET", "/v1/tokens/blob", auth=bearer, headers={"X-Token-Id": tid})[0] == 401
        new = token_keys.bearer(key, tid, 1)
        assert h.call("POST", "/v1/tokens/rotate", {"epoch": 2, "token": new}, auth=key, headers={"X-Token-Id": tid})[0] == 409
        assert h.call("POST", "/v1/tokens/rotate", {"epoch": 1, "token": bearer}, auth=key, headers={"X-Token-Id": tid})[0] == 409
        assert h.call("POST", "/v1/tokens/rotate", {"epoch": 1, "token": new}, auth=key, headers={"X-Token-Id": tid})[0] == 200
        status, reply = h.call("GET", "/v1/blob", auth=new)
        assert status == 409 and reply["reason"] == "migrate" and reply["epoch"] == 0
        assert h.call("POST", "/v1/blob", {"version": 1, "data": data}, auth=new) == (200, {"version": 2})
        assert h.call("GET", "/v1/blob", auth=new)[0] == 200

        # The page's own "replace": the next epoch at once, the old value retired; the blob follows the token.
        newer = token_keys.bearer(key, tid, 2)
        assert h.call("POST", "/v1/tokens/rotate", {"epoch": 2, "token": newer}, auth=key, headers={"X-Token-Id": tid})[0] == 200
        assert h.call("GET", "/v1/players", auth=new)[1]["token"] == "revoked"
        assert h.call("GET", "/v1/blob", auth=newer)[0] == 409  # still at epoch 1 until re-encrypted
        # A random token cannot be derived again: rotating it revokes it.
        assert h.store.rotate(plugin["id"]) == "revoked" and h.store.lookup(plugin["token"]) == (None, "revoked")
        assert h.call("POST", "/v1/tokens/rotate", {"epoch": 1, "token": token_keys.bearer(key, "c" * 32, 1)},
                      auth=key, headers={"X-Token-Id": plugin["id"]})[0] == 404

        # Any token revokes itself without the key; its blob goes with it.
        assert h.call("DELETE", "/v1/token", auth=newer) == (200, {"revoked": True})
        assert h.call("GET", "/v1/players", auth=newer)[1]["token"] == "revoked"
        assert h.store._q("SELECT COUNT(*) AS n FROM blobs")[0]["n"] == 0
        # Revoked rows say nothing about whose they were, derived or not.
        cols = "account, prefix, label, scopes, epoch, derived, kind, pending"
        assert {tuple(r) for r in h.store.db.execute(f"SELECT {cols} FROM tokens WHERE revoked IS NOT NULL")} == {
            ("", "", "", "", 0, 0, "", None)}
        assert account
    finally:
        h.close()


def test_export_api_consent_to_analysis(tmp_path):
    """A character's consent to analysis: off by default, changed with the account key or the token that posted
    it, told to the plugin with every accepted post, and read when an analysis asks (never copied onto the data)."""
    h = _ApiHarness(tmp_path)
    try:
        full = h.posts[0]
        me = {"X-Player": "PlayerOne"}
        key = h.call("POST", "/v1/accounts", {})[1]["key"]
        plugin = h.call("POST", "/v1/tokens", {"label": "plugin", "scopes": ["submit"]}, auth=key)[1]
        spare = h.call("POST", "/v1/tokens", {"label": "spare", "scopes": ["submit"]}, auth=key)[1]
        reader = h.call("POST", "/v1/tokens", {"label": "planner", "scopes": ["read"]}, auth=key)[1]
        # Nothing stored yet: there is no character to consent for.
        assert h.call("POST", "/v1/consent", {"consent": True}, auth=key, headers=me)[0] == 404
        # Off by default: the post is stored as ever, the reply says so, and no analysis may read it.
        status, reply = h.call("POST", "/v1/state", full, auth=plugin["token"])
        assert status == 200 and reply["consent"] is False
        assert list(h.store.analysis_states()) == []
        # Given in the plugin (its token posted the character): it covers what is already stored.
        status, given = h.call("POST", "/v1/consent", {"consent": True}, auth=plugin["token"], headers=me)
        assert status == 200 and given["consent"] is True and given["consent_since"]
        assert h.call("GET", "/v1/consent", auth=key, headers=me)[1] == given
        assert h.call("GET", "/v1/posted", auth=plugin["token"])[1]["posted"][0]["consent"] is True
        assert h.call("GET", "/v1/account", auth=key)[1]["players"][0]["consent_since"] == given["consent_since"]
        assert h.call("POST", "/v1/state", full, auth=plugin["token"])[1]["consent"] is True
        # What an analysis gets: the state under the character's random id, no name, no account.
        [state] = h.store.analysis_states()
        assert set(state) == {"id", "record"} and "player" not in state["record"] and state["record"]["state"]
        assert "PlayerOne" not in json.dumps(state)
        # Withdrawn on the site with the account key: later analyses see nothing, and the plugin hears of it.
        status, off = h.call("POST", "/v1/consent", {"consent": False}, auth=key, headers=me)
        assert status == 200 and off == {"consent": False, "consent_since": None}
        assert list(h.store.analysis_states()) == [] and h.store.stats()["consenting_players"] == 0
        assert h.call("POST", "/v1/state", full, auth=plugin["token"])[1]["consent"] is False
        # A token speaks only for characters it posted; a read token for none; the name is never in the address.
        assert h.call("POST", "/v1/consent", {"consent": True}, auth=spare["token"], headers=me)[0] == 404
        assert h.call("POST", "/v1/consent", {"consent": True}, auth=reader["token"], headers=me)[0] == 403
        assert h.call("POST", "/v1/consent", {"consent": "yes"}, auth=key, headers=me)[0] == 400
        assert h.call("POST", "/v1/consent", {"consent": True}, auth=key)[0] == 400
        assert h.call("POST", "/v1/consent/PlayerOne", {"consent": True}, auth=key)[0] == 404
        other_key = h.call("POST", "/v1/accounts", {})[1]["key"]
        assert h.call("POST", "/v1/consent", {"consent": True}, auth=other_key, headers=me)[0] == 404
        assert list(h.store.analysis_states()) == []
        # Consent goes with the character: deleted and posted again, it starts off.
        assert h.call("POST", "/v1/consent", {"consent": True}, auth=key, headers=me)[0] == 200
        assert h.call("DELETE", "/v1/players", auth=key, headers=me)[0] == 200
        assert h.call("POST", "/v1/state", full, auth=plugin["token"])[1]["consent"] is False
    finally:
        h.close()


def test_export_api_consent_survives_a_restore(tmp_path):
    """A change of consent is noted in the deletion log, so data restored from an older backup ends up with
    the player's latest answer."""
    import shutil

    from rune_advisor.export_api import Store

    store = Store(tmp_path / "data", deletions=tmp_path / "log")
    store.ensure_account("a")
    store.save_state("a", "PlayerOne", {"player": "PlayerOne", "updated": 1.0, "seq": 1, "posts": 1})
    store.set_consent("a", "PlayerOne", True)
    backup = tmp_path / "backup.sqlite3"
    store.db.execute("VACUUM INTO ?", (str(backup),))  # the backup holds consent given
    store.set_consent("a", "PlayerOne", False)  # withdrawn after the backup
    store.close()
    shutil.copy(backup, tmp_path / "data" / "export_api.sqlite3")
    for extra in ("-wal", "-shm"):
        (tmp_path / "data" / ("export_api.sqlite3" + extra)).unlink(missing_ok=True)
    restored = Store(tmp_path / "data", deletions=tmp_path / "log")
    assert restored.consent_of("a", "PlayerOne")["consent"] is True  # as the backup had it
    assert restored.reapply_deletions() == 1
    assert restored.consent_of("a", "PlayerOne") == {"consent": False, "consent_since": None}
    assert restored.reapply_deletions() == 0
    restored.close()


def test_export_api_expiry_sweep(tmp_path):
    """Accounts with no post and no read for retention_days are deleted; use renews; revoked hashes age out."""
    from datetime import datetime, timedelta, timezone

    from rune_advisor.export_api import Store

    store = Store(tmp_path / "api")
    account, key = store.create_account()
    token = store.create_token(account, "plugin", ["submit", "read"], None)[1]
    store.save_state(account, "PlayerOne", {"player": "PlayerOne", "updated": 1.0, "seq": 1, "posts": 1})
    idle, _ = store.create_account()
    today = datetime.now(timezone.utc).date()
    assert store.settings()["retention_days"] == 365
    assert store.sweep(today=today + timedelta(days=365)) == []  # exactly at the limit: kept
    # A token's use counts as activity for its account only.
    store._x("UPDATE accounts SET last_active = ? WHERE id = ?", ((today - timedelta(days=355)).isoformat(), account))
    store._x("UPDATE accounts SET last_active = ? WHERE id = ?", ((today - timedelta(days=355)).isoformat(), idle))
    info, _ = store.lookup(token)
    store.touch(info)
    assert store.sweep(today=today + timedelta(days=20)) == [idle]
    assert store.account_info(account) and store.account_info(idle) is None
    # Past the limit with no use: the account, its character and its tokens go; the key stops working.
    store.set_setting("retention_days", 30)
    assert store.sweep(today=today + timedelta(days=31)) == [account]
    assert store.account_by_key(key) is None and store.load_state(account, "PlayerOne") is None
    assert store.lookup(token)[0] is None  # (revoked or forgotten: either way it no longer works)
    # Revoked hashes outlive a deleted account for the tombstone period, then are forgotten.
    account2, _ = store.create_account()
    token2 = store.create_token(account2, "plugin", ["submit"], None)[1]
    store.delete_account(account2)
    store.sweep(today=today + timedelta(days=89))
    assert store.lookup(token2) == (None, "revoked")
    store.sweep(today=today + timedelta(days=91))
    assert store.lookup(token2) == (None, "unknown")


def test_export_api_migrates_a_json_data_dir(tmp_path):
    """tokens.json owners become accounts, their tokens read+submit tokens, players/ states move across."""
    from urllib.parse import quote

    from rune_advisor.export_api import Store, _token_hash, main

    legacy = tmp_path / "api"
    old, gone = "chvUnEn7LDTYpuUiKEMy7o8ciLAQsBpFSeWZsiyh3Ts", "Zzzzzz7LDTYpuUiKEMy7o8ciLAQsBpFSeWZsiyh3Ts"
    (legacy / "players" / "andrew").mkdir(parents=True)
    (legacy / "tokens.json").write_text(json.dumps({
        _token_hash(old): {"owner": "andrew", "created": "2026-11-01T10:00:00+00:00", "prefix": old[:6]},
        _token_hash(gone): {"owner": "pal", "created": "2026-11-02T10:00:00+00:00", "prefix": gone[:6],
                            "revoked": "2026-11-03T10:00:00+00:00"}}))
    record = {"player": "PlayerOne", "schema": 2, "hash": 7, "seq": 3, "posts": 4, "updated": 1762000000.5, "state": "AAAA"}
    (legacy / "players" / "andrew" / (quote("PlayerOne", safe="") + ".json")).write_text(json.dumps(record))
    (legacy / "players" / "andrew" / "broken.json").write_text("{not json")

    store = Store(legacy)  # a new database next to old files: migrated on first open
    assert store.owner(old) == ("andrew", "ok") and store.owner(gone) == (None, "revoked")
    info, _ = store.lookup(old)
    assert info.scopes == {"submit", "read"} and info.characters is None  # the running plugin and planner keep working
    assert store.load_state("andrew", "PlayerOne") == record
    assert store.players("andrew") == [{"player": "PlayerOne", "updated": 1762000000.5, "seq": 3, "posts": 4}]
    assert sorted(a["id"] for a in store.accounts()) == ["andrew", "pal"]
    assert store.account_info("andrew")["last_active"] == store.account_info("andrew")["created"]  # not instantly stale
    assert store.sweep() == []
    assert (legacy / "tokens.json").exists()  # the old files are left for a rollback
    # Idempotent, and a later open does not repeat it.
    assert store.migrate_legacy() == {"accounts": 0, "tokens": 0, "states": 0}
    store.close()
    store = Store(legacy)
    assert store.stats()["tokens"] == 1 and store.stats()["players"] == 1
    # The CLI command does the same by hand on a data dir whose database was created empty first.
    other = tmp_path / "api2"
    Store(other).close()
    (other / "tokens.json").write_text((legacy / "tokens.json").read_text())
    assert main(["--data-dir", str(other), "migrate"]) == 0
    assert Store(other).owner(old) == ("andrew", "ok")


def test_export_api_turnstile_and_secret_settings(tmp_path):
    from rune_advisor.export_api import Captcha, ItemNames, Service, Store, TurnstileCaptcha, main
    from rune_advisor.sources.game_configs import GameConfigs

    calls = []

    def post(url, fields):
        calls.append((url, fields))
        if fields["response"] == "boom":
            raise OSError("unreachable")
        return {"success": fields["response"] == "good"}

    captcha = TurnstileCaptcha("s3cret", post)
    assert captcha.verify("good", "1.2.3.4") and not captcha.verify("bad", None)
    assert not captcha.verify(None, None) and not captcha.verify("boom", None)  # no answer / Cloudflare down: refused
    assert calls[0][1] == {"secret": "s3cret", "response": "good", "remoteip": "1.2.3.4"} and len(calls) == 3
    assert Captcha().verify(None, None)  # the no-op verifier: tests and self-hosting

    store = Store(tmp_path / "api")
    service = Service(store, GameConfigs.read(), ItemNames())

    # The bot check is told the caller's own address, while the creation limit counts an IPv6 caller by /64.
    class Seen(Captcha):
        def verify(self, response, ip):
            seen.append(ip)
            return True
    seen = []
    told = Service(store, GameConfigs.read(), ItemNames(), captcha=Seen())
    told.create_account({"captcha": "x"}, "2001:db8:1:2:aaaa::7")
    told.create_account({"captcha": "x"}, "192.0.2.9")
    assert seen == ["2001:db8:1:2:aaaa::7", "192.0.2.9"]
    assert type(service._captcha()) is Captcha  # no secret file: no check
    with pytest.raises(ValueError):
        store.set_setting("trust_proxy", 2)


def test_export_api_turnstile_secret_comes_from_a_file(tmp_path, capsys):
    """The Turnstile secret is never a setting: `serve` reads it from a named file or the systemd credential,
    refuses to start when that file is unusable, and nothing prints or stores the value."""
    from rune_advisor.export_api import Store, main, turnstile_secret

    assert turnstile_secret(None, {}) == ("", "")  # nothing named: the check is off
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cret\n")
    assert turnstile_secret(secret_file, {}) == ("s3cret", f"the file {secret_file}")  # one trailing newline dropped
    assert turnstile_secret(None, {"RUNE_ADVISOR_API_TURNSTILE_SECRET_FILE": str(secret_file)})[0] == "s3cret"
    creds = tmp_path / "creds"
    creds.mkdir()
    assert turnstile_secret(None, {"CREDENTIALS_DIRECTORY": str(creds)}) == ("", "")  # no such credential
    (creds / "turnstile_secret").write_text("fromcred")
    assert turnstile_secret(None, {"CREDENTIALS_DIRECTORY": str(creds)}) == ("fromcred", "the credential 'turnstile_secret'")
    assert turnstile_secret(secret_file, {"CREDENTIALS_DIRECTORY": str(creds)})[0] == "s3cret"  # the named file wins
    # Named but missing or empty: the service must not start with the check silently off.
    for bad in (tmp_path / "missing", tmp_path / "empty"):
        if bad.name == "empty":
            bad.write_text("\n")
        with pytest.raises(SystemExit) as stop:
            turnstile_secret(bad, {})
        assert "not starting" in str(stop.value)
    (creds / "turnstile_secret").write_text("")
    with pytest.raises(SystemExit):
        turnstile_secret(None, {"CREDENTIALS_DIRECTORY": str(creds)})

    # It is not a setting any more: `settings set` refuses and points at the file; an old settings.json that
    # holds one loses it at the next change; `settings show` says only where the running service got it.
    data = tmp_path / "api"
    capsys.readouterr()
    assert main(["--data-dir", str(data), "settings", "set", "turnstile_secret", "s3cret"]) == 2
    assert "s3cret" not in capsys.readouterr().err.replace("turnstile_secret", "")
    (data / "settings.json").write_text('{"turnstile_secret": "old-secret", "post_interval": 12}')
    store = Store(data)
    assert store.settings()["post_interval"] == 12 and store.settings_lines()["turnstile_secret"] == "unset"
    store.set_setting("post_interval", 7)
    assert "old-secret" not in (data / "settings.json").read_text()
    # The file holds only what was set: a default a release changes reaches a server that never set it.
    assert json.loads((data / "settings.json").read_text()) == {"post_interval": 7.0}
    store.set_setting("trust_proxy", 1)
    assert store.chosen_settings() == {"post_interval", "trust_proxy"}
    assert store.unset_setting("post_interval")["post_interval"] == 10 and store.chosen_settings() == {"trust_proxy"}
    assert main(["--data-dir", str(data), "settings", "unset", "trust_proxy"]) == 0
    assert json.loads((data / "settings.json").read_text()) == {}
    store.note_turnstile("the credential 'turnstile_secret'")
    shown = store.settings_lines()["turnstile_secret"]
    assert shown.startswith("set (from the credential") and "s3cret" not in shown
    store.close()


def test_export_api_runs_without_a_settings_file(tmp_path):
    """settings.json is operator configuration and is in no backup: a restored data dir has none, and both
    `check` and a start must be right without it (the defaults apply until the unit sets what it needs)."""
    from rune_advisor import export_api
    from rune_advisor.export_api import DEFAULT_SETTINGS, Store

    data = tmp_path / "api"
    store = Store(data)
    account, _ = store.create_account()
    store.save_state(account, "PlayerOne", {"player": "PlayerOne", "updated": 1.0, "seq": 1, "posts": 1})
    store.close()
    assert not (data / "settings.json").exists()
    counts, problems = export_api.check_data_dir(data)
    assert problems == [] and counts["accounts"] == 1 and counts["players"] == 1
    again = Store(data)
    assert again.settings() == DEFAULT_SETTINGS and again.sweep() == []
    again.close()
    server = export_api.serve(data, "127.0.0.1:0", "/export_api")
    try:
        assert server.service.interval() == DEFAULT_SETTINGS["post_interval"]
    finally:
        server.server_close()
        server.service.store.close()


def test_export_api_admin_cli_on_the_sqlite_store(tmp_path, capsys):
    from rune_advisor.export_api import Store, main

    d = str(tmp_path / "api")
    assert main(["--data-dir", d, "token", "new", "andrew", "--scopes", "read", "--label", "planner", "--characters", "PlayerOne,Pal"]) == 0
    token = capsys.readouterr().out.strip()
    info, status = Store(tmp_path / "api").lookup(token)
    assert status == "ok" and info.scopes == {"read"} and info.characters == ("PlayerOne", "Pal") and info.can_read("pal")
    assert not info.can_read("Other")
    assert main(["--data-dir", d, "token", "new", "andrew", "--scopes", "root"]) == 2
    assert main(["--data-dir", d, "token", "list"]) == 0
    listing = capsys.readouterr().out
    assert token[:6] in listing and "planner" in listing and "limited to PlayerOne, Pal" in listing and token not in listing
    assert main(["--data-dir", d, "account", "key", "andrew"]) == 0
    key = capsys.readouterr().out.strip()
    assert Store(tmp_path / "api").account_by_key(key) == "andrew"
    assert main(["--data-dir", d, "stats"]) == 0 and "accounts = 1" in capsys.readouterr().out
    assert main(["--data-dir", d, "settings", "set", "max_tokens", "3"]) == 0
    assert "max_tokens = 3" in capsys.readouterr().out
    assert main(["--data-dir", d, "sweep"]) == 0
    assert main(["--data-dir", d, "account", "delete", "andrew"]) == 0
    assert Store(tmp_path / "api").lookup(token) == (None, "revoked")


def test_planner_settings_take_several_api_tokens(tmp_path, monkeypatch):
    from rune_advisor.config import load_settings

    monkeypatch.delenv("RUNE_ADVISOR_API_TOKEN", raising=False)
    path = tmp_path / "rune_advisor.toml"
    path.write_text('[api]\nurl = "http://x/export_api"\ntokens = ["b" , "c"]\ntoken = "a"\n')
    assert load_settings(path).api_tokens == ["a", "b", "c"] and load_settings(path).api_token == "a"
    path.write_text('[api]\nurl = "http://x"\ntoken = "only"\n')
    assert load_settings(path).api_tokens == ["only"]
    path.write_text('[api]\nurl = "http://x"\n')
    assert load_settings(path).api_tokens == [] and load_settings(path).api_token is None
    monkeypatch.setenv("RUNE_ADVISOR_API_TOKEN", "fromenv")
    assert load_settings(path).api_tokens == ["fromenv"]


def test_stale_inventories_come_from_the_last_archive_that_had_them(tmp_path):
    """An inventory the fresh export lacks (never loaded on this server) is what it held when last exported."""
    from rune_advisor.player import PlayerState, apply_plugin_snapshot, fill_stale_inventories

    def archive(folder, snap):
        d = tmp_path / folder
        d.mkdir()
        (d / "snapshot_PlayerOne.json").write_text(json.dumps(snap))

    bank_now = {"items": [{"id": 1511, "name": "Logs", "count": 5, "placeholder": False}]}
    archive("2026-10-29_PlayerOne_beta", {"exportedAt": 1, "inventories": {
        "METAL_BANK": {"items": [{"id": 436, "name": "Copper ore", "count": 9, "placeholder": False}]},
        "ARCH_SOIL_BOX": {"items": [{"id": 49517, "name": "Ancient gravel", "count": 3, "placeholder": False}]}}})
    archive("2026-11-01_PlayerOne_beta", {"exportedAt": 2, "inventories": {
        "METAL_BANK": {"items": [{"id": 436, "name": "Copper ore", "count": 1, "placeholder": False}]},
        "BANK": {"items": [{"id": 1511, "name": "Logs", "count": 999, "placeholder": False}]}}})
    archive("2026-10-27_Other_beta", {"exportedAt": 0, "inventories": {"CURRENCY_POUCH": {"items": []}}})
    fresh = {"exportedAt": 3, "schema": 1, "inventories": {"BANK": bank_now}, "notes": ["from the server"]}
    archive("2026-11-09_PlayerOne_api", fresh)  # an archive of this very export adds nothing

    snap = fill_stale_inventories(fresh, "PlayerOne", archive_dir=tmp_path)
    assert snap["inventories"]["BANK"] == bank_now  # the fresh export wins where it has the inventory
    assert snap["inventories"]["METAL_BANK"]["items"][0]["count"] == 1  # the newest archive with it
    assert snap["inventories"]["ARCH_SOIL_BOX"]["items"][0]["count"] == 3  # older, but the last to have it
    assert "CURRENCY_POUCH" not in snap["inventories"]  # another player's archive
    assert snap["notes"][0] == "from the server"
    assert any("2026-11-01 beta" in n and "METAL_BANK" in n for n in snap["notes"])
    assert any("2026-10-29 beta" in n and "ARCH_SOIL_BOX" in n for n in snap["notes"])
    assert fresh["inventories"] == {"BANK": bank_now}  # the input is left alone

    p = PlayerState(name="PlayerOne")
    apply_plugin_snapshot(p, snap)
    assert p.inventories["METAL_BANK"] == {"Copper ore": 1} and p.inventories["BANK"] == {"Logs": 5}
    assert "from the server" in p.notes and any("taken as last exported" in n for n in p.notes)
    # Nothing to fill: the snapshot comes back as is.
    assert fill_stale_inventories({"inventories": {"BANK": bank_now}}, "Nobody", archive_dir=tmp_path) == {"inventories": {"BANK": bank_now}}


def test_pouches_made_for_xp_are_swapped_back_for_shards():
    """Lord Amlodd/Bogrog return 70% of a pouch's shards, so training pouches cost ~0.2 x shards + 1 at the
    shrine; a pouch the gatherer makes as an item keeps the full cost, and so does a free player's."""
    from rune_advisor.mechanics.processing import effective

    recipe = {"skill": "Summoning", "output": "Rune minotaur pouch", "output_qty": 1, "level": 86, "xp": 756.8,
              "materials": [["Pouch", 1.0], ["Spirit shards", 100.0], ["Blue charm", 1.0], ["Rune bar", 1.0]]}
    member = PlayerState(name="M", members=True, ironman=True)
    trained = effective(member, recipe)
    shards = dict(map(tuple, trained["materials"]))["Spirit shards"]
    assert shards == pytest.approx(0.9 * 100 + 1 - 70)  # shrine saving and extra shard, then the swap
    assert "Lord Amlodd" in trained["boost"] and "Shrine of Inanna" in trained["boost"] and trained["swap"]
    wanted = effective(member, recipe, for_xp=False)
    assert dict(map(tuple, wanted["materials"]))["Spirit shards"] == pytest.approx(91) and not wanted.get("swap")
    free = effective(PlayerState(name="F", members=False), recipe)
    assert dict(map(tuple, free["materials"]))["Spirit shards"] == 100


def test_bank_mix_folds_leftover_runs():
    from rune_advisor.recommend.catalogue import BankMix

    mix = BankMix(xp=10_000, runs=[("Fire titan pouch", 29, 32, 9_500, {"Blue charm": 29}),
                                    ("Spirit terrorbird pouch", 1, 1, 70, {"Raw bird meat": 1}),
                                    ("Spirit mosquito pouch", 5, 5, 30, {"Proboscis": 5})])
    steps = mix.describe("Summoning")
    assert steps[0].startswith("from the bank: make 32 Fire titan pouch")
    assert steps[1] == "from the bank: odds and ends with what's left: 1 Spirit terrorbird pouch, 5 Spirit mosquito pouch (~100 Summoning xp)"
    assert len(steps) == 2
    # A single run is never folded, however small.
    assert len(BankMix(xp=70, runs=[("Spirit terrorbird pouch", 1, 1, 70, {})]).describe("Summoning")) == 1


def test_shop_packs_count_as_offers_of_their_contents():
    """Raw bird meat packs (50 noted each, 3,150 gp) make Fresh Meat a 1,050-a-week source of raw bird meat,
    without the backpack-trip cap; the pack itself stays on offer too."""
    from rune_advisor.recommend import gathering

    packs = gathering.pack_contents()
    assert packs["Raw bird meat pack"] == ("Raw bird meat", 50.0) and packs["Spirit shard pack"] == ("Spirit shards", 5000.0)
    offers = gathering.tables_shops()["Raw bird meat"]
    fresh = next(o for o in offers if o[0] == "Fresh Meat" and len(o) == 5)
    assert fresh[1] == 21 * 50 and fresh[2] == pytest.approx(63.0) and fresh[4] == {"pack": "Raw bird meat pack", "each": 50.0}
    assert [o for o in offers if o[0] == "Fresh Meat" and len(o) == 4]  # the loose 35 still listed
    p = PlayerState(name="X", ironman=True, members=True)
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False))
    src = ctx.gatherer.source("Raw bird meat")
    assert src is not None and "pack" in src.how and "noted" in src.how, src.how


def test_picker_keeps_choice_resources_for_free_xp():
    from rune_advisor.recommend.picker import without_choice
    from rune_advisor.resources import Resource, ResourcePool

    pool = ResourcePool([Resource(name="Penguin points", lamp="small", units=10),
                         Resource(name="Antique lamp (Elite Ardougne Tasks)", xp_fixed=50_000, units=4, min_level=85),
                         Resource(name="Smithing lamp", xp_fixed=1_000, units=1, skills=["Smithing"])])
    assert [r.name for r in without_choice(pool).resources] == ["Smithing lamp"]
    assert without_choice(None) is None


def test_goal_levels_skip_level_milestones_and_120():
    from rune_advisor.models import Achievement, Skill
    from rune_advisor.recommend.plans import goal_levels

    p = PlayerState(name="X", skills={"Firemaking": Skill(name="Firemaking", level=100, xp=xp_for_level(100)),
                                      "Thieving": Skill(name="Thieving", level=103, xp=xp_for_level(103)),
                                      "Smithing": Skill(name="Smithing", level=90, xp=xp_for_level(90))})
    p.achievements = {
        "Firemaking 110": Achievement(name="Firemaking 110", runescore=5, completed=False, category="Skills / Firemaking",
                                      requirements=[{"kind": "skill", "skills": ["FIREMAKING"], "totalLevels": 110}]),
        "Firemaking 120": Achievement(name="Firemaking 120", runescore=5, completed=False, category="Skills / Firemaking", requirements=[]),
        "Master Skiller": Achievement(name="Master Skiller", runescore=5, completed=False, category="Skills", requirements=[]),
        "Asuran Arsenal: Clean Sweep": Achievement(name="Asuran Arsenal: Clean Sweep", runescore=5, completed=False,
                                                   category="Skills / Thieving", requirements=[{"kind": "varTotal"}]),
    }
    reqs = {"Firemaking 120": {"type": "skill", "name": "Firemaking", "level": 120},
            "Master Skiller": {"type": "and", "units": [{"type": "skill", "name": "Firemaking", "level": 110}]},
            "Asuran Arsenal: Clean Sweep": {"type": "skill", "name": "Thieving", "level": 114}}
    ctx = Context(player=p, cost=CostModel.default(), achievement_reqs=reqs)
    goals = goal_levels(ctx)
    assert "Firemaking" not in goals  # only level milestones and the 120 cap want more Firemaking
    assert goals["Thieving"] == 114 and goals["Smithing"] == 110  # masterwork recipes at 110 (99 alone otherwise)


def test_item_xp_invention_twins():
    from rune_advisor.models import Skill
    from rune_advisor.mechanics import item_xp

    assert item_xp.item_level_cap(71) == 15 and item_xp.siphon_level(71) == 12 and item_xp.siphon_level(26) is None
    assert item_xp.siphon_level(30) == 10  # cap 10 below Invention 60
    assert item_xp.invention_per_item_xp(71, 80) == pytest.approx(540_000 / 81_960)  # 6.59 at the wiki's tier 80
    assert item_xp.invention_per_item_xp(71, 90) == pytest.approx(540_000 * 1.15 / 81_960)
    p = PlayerState(name="X", inventories={"BANK": {"Augmented Sunspear (melee)": 1, "Superior tetsu body": 1,
                                                    "Superior tetsu platelegs": 1, "Fishing rod-o-matic": 1}},
                    skills={"Invention": Skill(name="Invention", level=71, xp=xp_for_level(71)),
                            "Attack": Skill(name="Attack", level=90, xp=xp_for_level(90)),
                            "Defence": Skill(name="Defence", level=90, xp=xp_for_level(90)),  # tetsu needs 85
                            "Fishing": Skill(name="Fishing", level=90, xp=xp_for_level(90))})
    setup = item_xp.combat_setup(p, "melee")
    assert {(s.slot, s.tier, s.ok, s.augmented) for s in setup.slots} == {("2h weapon", 78, True, True),
                                                                           ("torso", 85, True, False),
                                                                           ("legs", 85, True, False)}
    rows = item_xp.method_rows(p, [
        {"skill": "Attack", "method": "Abyssal demons", "level_min": 85, "xp_per_hour": 500_000.0, "kills_per_hour": 300},
        {"skill": "Attack", "method": "Alchemy", "level_min": 1, "xp_per_hour": 500_000.0},  # no hits: no item xp
        {"skill": "Fishing", "method": "Sailfish", "level_min": 97, "xp_per_hour": 100_000.0},  # above Fishing 90
        {"skill": "Fishing", "method": "Rocktail", "level_min": 90, "xp_per_hour": 80_000.0},
    ])
    by = {r["base_method"]: r for r in rows}
    assert set(by) == {"Abyssal demons", "Rocktail"}
    melee = by["Abyssal demons"]
    expected = 500_000 * (0.09 * item_xp.invention_per_item_xp(71, 78) + 2 * 0.045 * item_xp.invention_per_item_xp(71, 85))
    assert melee["xp_per_hour"] == pytest.approx(expected) and melee["side_xp"] == {"Attack": 500_000.0}
    assert melee["skill"] == "Invention" and melee["level_min"] == 27 and melee["kills_per_hour"] == 300
    fish = by["Rocktail"]
    assert fish["xp_per_hour"] == pytest.approx(80_000 * 0.118 * item_xp.invention_per_item_xp(71, 70))


def test_tier_names_and_wanted_items():
    from rune_advisor.player import tier_names
    from rune_advisor.gear import equipment

    ids = equipment()["ids"]
    top = next(i for i, n in ids.items() if n == "Deathdealer robe top (tier 70)")
    guard = next(i for i, n in ids.items() if n == "Death guard (tier 90)")
    p = PlayerState(name="X", inventories={"BANK": {"Deathdealer robe top": 1, "Death guard": 1, "Logs": 5}})
    p.item_ids = {"Deathdealer robe top": int(top), "Death guard": int(guard), "Logs": 1511}
    tier_names(p)
    assert p.bank == {"Deathdealer robe top (tier 70)": 1, "Death guard (tier 90)": 1, "Logs": 5}
    assert p.item_ids["Death guard (tier 90)"] == int(guard) and "Death guard" not in p.item_ids
    apply_overrides(p, {"players": {"x": {"wanted": ["Deathwarden robe top (tier 80)", "Logs"]}}})
    assert p.wanted_items == ["Deathwarden robe top (tier 80)"]  # held items drop off the list


def test_boss_sim_rate_and_darts_track():
    from rune_advisor.models import Achievement, Skill
    from rune_advisor.drops import boss_sim_kills_per_hour, bosses
    from rune_advisor.recommend.tracks.darts import DART, DartsTrack, kill_hours, targets

    p = PlayerState(name="X", inventories={"BANK": {DART: 7, "Death guard": 1, "Skull lantern": 1}},
                    skills={s: Skill(name=s, level=99, xp=xp_for_level(99)) for s in
                            ("Attack", "Strength", "Defence", "Ranged", "Magic", "Necromancy", "Hitpoints", "Prayer")})
    assert "Hermod, the Spirit of War" in bosses()
    sim = boss_sim_kills_per_hour(p, "Hermod, the Spirit of War")
    assert sim and sim[0] > 0 and sim[1] in ("Attack", "Strength", "Defence", "Ranged", "Magic", "Necromancy")
    assert "Hermod, the Spirit of War" not in targets() and "Corporeal Beast" in targets()  # the wiki's immune list

    class Gatherer:
        def complete_source(self, item):
            return None
        def net_cost(self, g):
            return 0.0

    class Ctx:
        player = p
        achievement_descriptions = {}
        gatherer = Gatherer()
        def needs(self):
            return {}
    p.ironman = False
    p.achievements = {"Core Problem": Achievement(name="Core Problem", runescore=10, completed=False)}
    hours, how = kill_hours(Ctx(), "Corporeal Beast")
    assert hours and ("combat sim" in how or "kills/h" in how)
    assert DartsTrack.candidates(Ctx()) == []  # an achievement alone has no hours value: nothing to throw at
    from rune_advisor.recommend.tracks.darts import drop_value
    assert any("Core Problem" in r for r in drop_value(Ctx()).get("Corporeal Beast", (0, []))[1])


def test_dnd_reset_tokens():
    from datetime import date

    from rune_advisor import dnd
    from rune_advisor.models import Skill
    from rune_advisor.recommend.tracks.dnd_tokens import DndTokensTrack, Valued, periods, schedule
    from rune_advisor.xp import lamp_xp

    today, removal = date(2026, 11, 11), date(2026, 11, 23)  # a Wednesday; the tokens' removal date
    assert len(periods("daily", today, removal)) == 12
    assert [label for _, label in periods("weekly", today, removal)] == ["this week", "week of 18 Nov"]
    assert [label for _, label in periods("monthly", today, removal)] == ["this month"]
    assert len(periods("monthly", date(2026, 10, 15), removal)) == 2

    assert dnd.troll_invasion_xp(99) == 77_624  # the wiki's formula: 8 x (L^2 - 2L + 100)
    assert dnd.sinkhole_xp(100) == pytest.approx((52033 + 59466 + 70853 + 92916) / 5)
    levels = {"Hunter": 93, "Agility": 96, "Dungeoneering": 100, "Herblore": 100, "Slayer": 95, "Prayer": 99, "Magic": 94,
              "Ranged": 90, "Thieving": 103, "Firemaking": 100, "Farming": 105, "Construction": 99, "Fishing": 99,
              "Invention": 71, "Smithing": 99, "Crafting": 101, "Runecraft": 95}
    p = PlayerState(name="X", skills={s: Skill(name=s, level=l, xp=xp_for_level(l)) for s, l in levels.items()},
                    inventories={"BANK": {"Distraction & Diversion reset token (weekly)": 3, "Hooded Quest Cape": 1}})
    by_name = {r.dnd: r for r in dnd.rewards(p)}
    assert by_name["Big Chinchompa"].xp["Hunter"] == pytest.approx(2 * 1500 * (450 / 21 + 5))
    assert by_name["Wisps of the Grove"].xp == {"Hunter": lamp_xp(93, "medium"), "Farming": lamp_xp(105, "medium")}
    assert dnd.tears_skill(p) == "Invention"  # the lowest level
    assert by_name["Tears of Guthix"].xp["Invention"] == pytest.approx(30 * 180)  # 0 QP: the cape's 30 s only, 180 xp a tear
    assert by_name["Herby Werby"].xp["Herblore"] == pytest.approx(100 * 889.2 + lamp_xp(100, "small"))
    assert by_name["God Statues"].xp["Construction"] == pytest.approx(5 * lamp_xp(99, "large"))
    assert by_name["Giant Oyster"].xp["Fishing"] == pytest.approx(30 * 6966)

    # Scheduling: one reset per D&D per period, best net first, a D&D that costs more time than it saves never.
    good = Valued(dnd.Reward("A", "weekly", minutes=30), hours=2.0, lines=[])
    ok = Valued(dnd.Reward("B", "weekly", minutes=30), hours=1.0, lines=[])
    bad = Valued(dnd.Reward("C", "weekly", minutes=60), hours=0.5, lines=[])
    s = schedule("weekly", 5, [good, ok, bad], today, removal)
    assert [(x.valued.reward.dnd, x.period) for x in s.slots] == [("A", "this week"), ("A", "week of 18 Nov"), ("B", "this week"), ("B", "week of 18 Nov")]
    assert s.unused == 1 and not s.spare

    ctx = Context(player=p, cost=CostModel.default(use_wiki=False))
    cands = DndTokensTrack.candidates(ctx, today)
    assert cands and all(c.track == "dnd_tokens" for c in cands)
    assert len(cands) == 1 and "this week" in cands[0].title and any(d.startswith("hold ") for d in cands[0].details)
    assert all("Distraction & Diversion reset token (weekly)" in c.title for c in cands)
    assert DndTokensTrack.candidates(Context(player=PlayerState(name="Y"), cost=CostModel.default(use_wiki=False)), today) == []


def test_gizmo_odds_match_wiki_calculator():
    """mechanics/perks reproduces the game's sort and the probabilities the wiki's perk pages state."""
    from rune_advisor.mechanics import perks as mech

    # The Calculator:Perks page's worked example of the game's quicksort.
    arr = [("Cautious", 0), ("Blunted", 0), ("Eruptive", 0), ("Precise", 65), ("Flanking", 0)]
    mech.quicksort(arr, key=lambda t: t[1])
    assert [name for name, _ in arr] == ["Blunted", "Eruptive", "Flanking", "Cautious", "Precise"]

    assert sum(mech.budget_distribution(71)) == pytest.approx(1.0)
    assert not any(mech.budget_distribution(71)[:71])  # never below the level
    assert mech.potion_level(120, "extreme") == 137 and mech.potion_level(24, "extreme") == 24
    assert mech.slot_units("Spiritual parts") == 5 and mech.slot_units("Precise components") == 1

    odds = mech.gizmo_odds(88, "weapon", ("Precise components",) * 5)
    assert sum(odds.values()) == pytest.approx(1.0)
    made = mech.per_gizmo(odds)
    assert sum(made.values()) == pytest.approx(1.0) and () not in made
    # A normal gizmo never rolls an ancient-only rank.
    assert max(dict(o).get("Precise", 0) for o in made) <= 5

    # Published probabilities (editor-typed, a few stale): the bulk must reproduce to the stated precision.
    checks = json.loads((FIX / "perk_checks.json").read_text())["checks"]
    hits = 0
    for c in checks:
        got = mech.gizmo_odds(mech.potion_level(c["level"], c["potion"]), c["gizmo"], c["materials"], c["ancient"])
        hits += abs(100 * mech.chance_of(got, [tuple(w) for w in c["want"]]) - c["percent"]) < 0.06
    assert len(checks) >= 60 and hits / len(checks) > 0.85


def test_perk_values_and_candidates():
    from rune_advisor.mechanics import item_xp
    from rune_advisor.recommend import perks as rp

    assert rp.crit_damage(100) == 0.5 and rp.crit_damage(85) == pytest.approx(0.45) and rp.crit_damage(5) == 0.10
    assert rp.damage_gain({"Precise": 6}, 0.5) == pytest.approx(0.04875)  # the Precise page's example
    # Equilibrium 1: +8% ability damage for the base 10% chance of a 50% critical strike.
    assert rp.damage_gain({"Equilibrium": 1}, 0.5) == pytest.approx(1.08 / 1.05 - 1)
    # Biting adds nothing once Equilibrium stops critical strikes.
    assert rp.damage_gain({"Equilibrium": 1, "Biting": 3}, 0.5) == rp.damage_gain({"Equilibrium": 1}, 0.5)
    assert rp.damage_gain({"Biting": 2}, 0.5) > 0 > rp.damage_gain({"Blunted": 1}, 0.5)

    usable = {"Precise components", "Armadyl components", "Timeworn components", "Connector parts"}
    recipes = rp.candidates("weapon", False, ["Precise"], usable)
    assert ("Precise components",) * 5 in recipes
    assert not any("Timeworn components" in r for r in recipes)  # ancient materials need an ancient gizmo
    assert all(len(r) <= 5 for r in recipes)
    assert max(len(r) for r in rp.candidates("weapon", True, ["Precise"], usable)) == 9
    units = rp.recipe_units(("Spiritual parts",) * 5, "armour", False)
    assert units["Spiritual parts"] == 25 and units["Deflecting parts"] == 10 and units["Protective components"] == 2

    # The augmentor takes one divine energy; the held stack that makes the most augmentors is suggested.
    assert item_xp.augmentor_energy({"Incandescent energy": 21_623, "Elder energy": 7_281}) == ("Incandescent energy", 225, 96)
    assert item_xp.augmentor_energy({"Pale energy": 1_999}) is None


def test_charge_drain_and_prep_sizing():
    """Charge drain per the Charge pack page; PlayerOne' two tier-90 Necromancy weapons read 4.95/s in game."""
    from rune_advisor import charge

    class P:
        vars = {"INVENT_CHARGE": 23_984_562}
        achievements = {}

        def level(self, skill):
            return 88  # research 6 (level 83) by level: x0.88

    p = P()
    assert charge.research_factor(p) == 0.88
    both = charge.drain_per_second(p, "main hand weapon", 90, 1) + charge.drain_per_second(p, "off-hand weapon", 90, 1)
    assert both == pytest.approx(4.95)
    assert charge.drain_per_second(p, "2h weapon", 50, 1) == pytest.approx(7 / 8 * 1.5 * 0.88)  # below 70 counts as 67
    assert charge.drain_per_second(p, "torso", 70, 5) == pytest.approx(1.25 * 0.88 * 0.9)
    assert 0.9 < charge.level_factor(88) < 0.92  # most of a siphon cycle is spent at item level 5+
    assert charge.pack_charge(p) == pytest.approx(7_994.854)
    assert charge.pack_charge(p) / both / 60 == pytest.approx(26.9, abs=0.05)  # the game projected about 26 minutes
    # A plan preps for its expected time, a session at most.
    assert charge.trip_hours(0.4) == 0.4 and charge.trip_hours(9.0) == charge.SESSION_HOURS
    assert charge.trip_hours(9.0, session=3.0) == 3.0


def test_blessings_of_the_wolf_thresholds():
    """The shrine's rewards unlock in order at running totals; the export proves the last one unlocked."""
    from types import SimpleNamespace

    from rune_advisor import blessings

    def player(done):
        reqs = [{"description": name, "complete": name in done} for name in blessings.THRESHOLDS]
        return SimpleNamespace(achievements={blessings.ACHIEVEMENT: SimpleNamespace(completed=False, requirements=reqs)})

    assert blessings.known_blessings(player(())) == 0
    assert blessings.known_blessings(player(("Shaman's headdress", "Shaman's poncho"))) == 1200
    assert blessings.known_blessings(SimpleNamespace(achievements={})) == 0
    assert blessings.next_unlock(0) == ("Shaman's headdress", 600)
    assert blessings.next_unlock(1200) == ("Shaman's leggings", 600)
    assert blessings.next_unlock(4200) == ("'the Spirit Wolf' title", 800) and blessings.next_unlock(5000) is None
    assert blessings.TOTAL == 5000


def test_weekly_claims_by_task_set_tier():
    """Area task sets unlock weekly free supplies; the best tier met counts."""
    from types import SimpleNamespace

    from rune_advisor import claims

    def player(done, quests=()):
        return SimpleNamespace(members=True,
                               achievements={n: SimpleNamespace(completed=True) for n in done},
                               quests={q: SimpleNamespace(done=True) for q in quests})

    got = {c.item: c for c in claims.available(player(["Seers' Village Set Tasks - Easy", "Seers' Village Set Tasks - Hard",
                                                         "Desert Set Tasks - Medium"]))}
    assert got["Flax"].qty == 840 and got["Potato cactus"].qty == 140 and "Pineapple" not in got
    # Bert's sand needs the quest; the Elite Ardougne set raises it.
    assert not claims.available(player(["Ardougne Set Tasks - Elite"]))
    sand = claims.available(player(["Ardougne Set Tasks - Elite"], ["The Hand in the Sand"]))
    assert [(c.item, c.qty) for c in sand] == [("Bucket of sand", 420)]
    assert claims.available(player([], ["The Hand in the Sand"]))[0].qty == 294
    planks = claims.available(player(["Morytania Set Tasks - Medium"]))
    assert {c.item for c in planks} == {"Plank", "Oak plank", "Teak plank"} and len({c.choice for c in planks}) == 1
    free = player(["Seers' Village Set Tasks - Elite"])
    free.members = False
    assert claims.available(free) == []


def test_artisans_respect_prices_and_xp():
    """1% respect per 10,000 Smithing xp at the workshop, +5% with the Falador shield 4; open rewards from the tests."""
    from types import SimpleNamespace

    from rune_advisor import respect

    plain = SimpleNamespace(holdings={}, achievements={})
    shield = SimpleNamespace(holdings={"Falador shield 4": 1}, achievements={})
    assert respect.xp_for(plain, 100) == 1_000_000 and respect.percent_for(plain, 18_240) == pytest.approx(1.824)
    assert respect.xp_for(shield, 105) == pytest.approx(1_000_000) and respect.percent_for(shield, 10_000) == 1.05
    reqs = [{"description": "Solemn Smith V", "complete": True}, {"description": "Golden Cannon", "complete": False},
            {"description": "Royale Cannon", "complete": False}, {"description": "not a reward", "complete": False}]
    p = SimpleNamespace(holdings={}, achievements={
        "Work on Your Artisan": SimpleNamespace(completed=False, requirements=reqs),
        "Work on Your Artisan II": SimpleNamespace(completed=True, requirements=[{"description": "Necturion Outfit"}])})
    assert respect.open_rewards(p) == {"Golden Cannon": 50, "Royale Cannon": 100}


def _learn(service, owner, full, target):
    """Play the plugin's config sync for `owner`: a full post naming `target`'s fingerprint draws the 428, then the
    difference against the base it names goes up in one part. Returns the 428 body."""
    from rune_advisor.export_api import ApiError

    with pytest.raises(ApiError) as e:
        service.apply(owner, dict(full, configs=target.fingerprint))
    need = e.value.extra
    mine = target.hashes()
    added = {"quests": [target.quests[i] for i in sorted(mine["quests"]) if need["known"]["quests"].get(str(i)) != mine["quests"][i]],
             "achievements": [target.achievements[i] for i in sorted(mine["achievements"])
                              if need["known"]["achievements"].get(str(i)) != mine["achievements"][i]]}
    removed = {k: [int(i) for i in need["known"][k] if int(i) not in mine[k]] for k in ("quests", "achievements")}
    reply = service.upload_configs(owner, {"fingerprint": target.fingerprint, "base": need["base"], "part": 1, "parts": 1,
                                           "added": added, "removed": removed})
    assert reply["accepted"] and reply["learned"]
    return need


def _scoped_service(tmp_path):
    from rune_advisor.export_api import Service, Store

    configs, _, posts = _mock_configs_and_posts(tmp_path)
    older = _older_configs(configs)
    return Service(Store(tmp_path / "api"), older), configs, older, posts[0]


def _variant(configs, n):
    """A different set from `configs` (one achievement renamed), so a different fingerprint."""
    from rune_advisor.sources.game_configs import GameConfigs

    out = GameConfigs(quests=dict(configs.quests), achievements=dict(configs.achievements))
    aid = min(out.achievements)
    out.achievements[aid] = dict(out.achievements[aid], name=f"variant {n}")
    return out


def test_pending_config_set_is_private_to_its_uploader(tmp_path):
    """A set learned from one account's upload decodes that account's posts only; another account with the same
    fingerprint gets the 428 handshake, and its base is never the first account's pending set."""
    from rune_advisor.export_api import ApiError

    service, configs, older, full = _scoped_service(tmp_path)
    _learn(service, "x", full, configs)
    assert service.apply("x", dict(full, configs=configs.fingerprint))["accepted"]
    with pytest.raises(ApiError) as e:
        service.apply("y", dict(full, configs=configs.fingerprint))
    assert e.value.status == 428 and e.value.extra["base"] == older.fingerprint
    assert service.generation(configs.fingerprint, "y") is None and service.generation(configs.fingerprint, "x") is not None
    assert service.snapshot("y", "PlayerOne") is None
    # X's own next 428 offers its newer pending set as the base; Y's offer stays the package dump.
    with pytest.raises(ApiError) as e:
        service.apply("x", dict(full, configs=1234))
    assert e.value.extra["base"] == configs.fingerprint
    with pytest.raises(ApiError) as e:
        service.apply("y", dict(full, configs=1234))
    assert e.value.extra["base"] == older.fingerprint
    # Y can't name X's pending set as the base of its own upload either.
    with pytest.raises(ApiError) as e:
        service.upload_configs("y", {"fingerprint": 1234, "base": configs.fingerprint, "part": 1, "parts": 1})
    assert e.value.status == 409 and e.value.extra["base"] == older.fingerprint
    assert service.visible_generations("x") == ([older.fingerprint], [configs.fingerprint])
    assert service.visible_generations("y") == ([older.fingerprint], [])


def test_identical_uploads_are_stored_once_and_counted(tmp_path):
    service, configs, older, full = _scoped_service(tmp_path)
    _learn(service, "x", full, configs)
    _learn(service, "y", full, configs)  # Y gets its own 428 and its own reference
    assert service.apply("y", dict(full, configs=configs.fingerprint))["accepted"]
    rows = [r for r in service.store.config_rows() if r["status"] == "pending"]
    assert len(rows) == 1 and rows[0]["accounts"] == 2 and rows[0]["fp"] == configs.fingerprint
    assert rows[0]["a_added"] == 1 and rows[0]["a_removed"] == 0 and rows[0]["q_added"] == 0 and rows[0]["base"] == older.fingerprint
    assert len(list((tmp_path / "api" / "pending_configs").iterdir())) == 1


def test_confirming_a_config_set_makes_it_global_in_a_running_service(tmp_path, capsys):
    from rune_advisor.export_api import ApiError, Service, Store, main

    service, configs, older, full = _scoped_service(tmp_path)
    _learn(service, "x", full, configs)
    fp = f"{configs.fingerprint:08x}"
    assert main(["--data-dir", str(tmp_path / "api"), "configs", "list"]) == 0
    listing = capsys.readouterr().out
    assert "pending" in listing and fp in listing and "1 account(s)" in listing and "+1/-0 achievements" in listing
    # Another process (the CLI) confirms; the running service sees it at once.
    assert main(["--data-dir", str(tmp_path / "api"), "configs", "confirm", fp]) == 0
    assert service.apply("z", dict(full, configs=configs.fingerprint))["accepted"]
    assert service.generation(configs.fingerprint, "z") is not None
    assert (tmp_path / "api" / "generations" / fp / "achievements.json.gz").exists()
    assert not list((tmp_path / "api" / "pending_configs").iterdir())
    # Global sets are what the 428 body offers everyone as the base now.
    with pytest.raises(ApiError) as e:
        service.apply("w", dict(full, configs=1234))
    assert e.value.extra["base"] == configs.fingerprint
    assert main(["--data-dir", str(tmp_path / "api"), "configs", "list"]) == 0
    assert "\nglobal  " + fp in capsys.readouterr().out
    assert Service(Store(tmp_path / "api"), older).generation(configs.fingerprint, "anyone") is not None  # survives a restart
    assert main(["--data-dir", str(tmp_path / "api"), "configs", "confirm", fp]) == 2  # already global


def test_confirm_needs_a_hash_when_contents_share_a_fingerprint_and_drop_forgets(tmp_path):
    service, configs, older, full = _scoped_service(tmp_path)
    _learn(service, "x", full, configs)
    store = service.store
    real = store.config_rows()[0]["hash"]
    store.db.execute("INSERT INTO config_contents VALUES (?, ?, ?, '2026-11-12', 0, 0, 1, 0)", ("ab" * 32, configs.fingerprint, older.fingerprint))
    store.db.execute("INSERT INTO config_refs VALUES ('mallory', ?, 1.0)", ("ab" * 32,))
    with pytest.raises(ValueError, match="--hash"):
        store.confirm_config(configs.fingerprint)
    assert store.confirm_config(configs.fingerprint, real[:10]) == real
    assert [r["status"] for r in store.config_rows()] == ["global"]  # the other claimant went with the fingerprint
    other = _variant(configs, 1)
    _learn(service, "x", full, other)
    assert store.drop_config(other.fingerprint) == 1
    assert store.pending_of("x") == [] and not list((tmp_path / "api" / "pending_configs").iterdir())


def test_pending_config_sets_are_capped_per_account_and_staging_expires(tmp_path):
    import time

    from rune_advisor.export_api import MAX_PENDING_PER_ACCOUNT, MAX_STAGED_PER_ACCOUNT, STAGING_SECONDS

    service, configs, older, full = _scoped_service(tmp_path)
    variants = [_variant(configs, n) for n in range(MAX_PENDING_PER_ACCOUNT + 1)]
    for v in variants:
        _learn(service, "x", full, v)
        time.sleep(0.01)
    kept = [p["fp"] for p in service.store.pending_of("x")]
    assert kept == [variants[2].fingerprint, variants[1].fingerprint]  # the oldest was replaced
    assert service.generation(variants[0].fingerprint, "x") is None
    assert len(list((tmp_path / "api" / "pending_configs").iterdir())) == MAX_PENDING_PER_ACCOUNT
    # Unfinished uploads: capped per account, and forgotten after a while.
    for fp in range(100, 100 + MAX_STAGED_PER_ACCOUNT + 2):
        service.upload_configs("x", {"fingerprint": fp, "base": older.fingerprint, "part": 1, "parts": 2})
    assert len([k for k in service._staging if k[0] == "x"]) == MAX_STAGED_PER_ACCOUNT
    for staged in service._staging.values():
        staged["t"] -= STAGING_SECONDS + 1
    service.upload_configs("y", {"fingerprint": 99, "base": older.fingerprint, "part": 1, "parts": 2})
    assert list(service._staging) == [("y", 99)]


def test_deleting_an_account_drops_its_pending_config_sets(tmp_path):
    service, configs, older, full = _scoped_service(tmp_path)
    _learn(service, "x", full, configs)
    _learn(service, "y", full, configs)
    _learn(service, "x", full, _variant(configs, 7))
    store = service.store
    store.delete_account("x")
    rows = [r for r in store.config_rows() if r["status"] == "pending"]
    assert [r["accounts"] for r in rows] == [1] and store.pending_of("x") == []  # the shared set stays for y
    assert len(list((tmp_path / "api" / "pending_configs").iterdir())) == 1
    store.ensure_account("y")
    store.sweep(retention_days=-1)  # the expiry sweep removes y, so the last copy goes too
    assert store.config_rows() == [] and not list((tmp_path / "api" / "pending_configs").iterdir())


def test_generations_on_disk_from_before_pending_sets_count_as_confirmed(tmp_path):
    from rune_advisor.export_api import Service, Store

    configs, _, posts = _mock_configs_and_posts(tmp_path)
    older = _older_configs(configs)
    configs.write(tmp_path / "api" / "generations" / f"{configs.fingerprint:08x}")
    service = Service(Store(tmp_path / "api"), older)
    assert service.generation(configs.fingerprint, "anyone") is not None
    assert service.apply("anyone", dict(posts[0], configs=configs.fingerprint))["accepted"]
    assert [(r["status"], r["fp"]) for r in service.store.config_rows()] == [("global", configs.fingerprint)]


def test_planner_fetches_and_caches_the_config_set_a_snapshot_names(tmp_path, monkeypatch):
    """A snapshot from a client newer than the data package names a fingerprint the package lacks: api_client
    fetches that set with the same token, caches it by fingerprint, and the snapshot expands with it."""
    from rune_advisor import api_client
    from rune_advisor.config import Settings
    from rune_advisor.sources import game_configs

    h = _ApiHarness(tmp_path, older=True)
    try:
        token = h.store.new_token("andrew")
        for _ in range(2):  # the plugin: post, get the 428, upload the difference, post again
            status, reply = h.call("POST", "/v1/state", h.posts[0], auth=token)
            if status == 428:
                known, mine = reply["known"]["achievements"], h.configs.hashes()["achievements"]
                added = [a for i, a in h.configs.achievements.items() if known.get(str(i)) != mine[i]]
                assert h.call("POST", "/v1/configs", {"fingerprint": h.configs.fingerprint, "base": reply["base"], "part": 1,
                                                      "parts": 1, "added": {"achievements": added}}, auth=token)[0] == 200
        assert status == 200
        new_id = max(h.configs.achievements)
        assert h.call("GET", "/v1/configs", auth=token)[1]["pending"] == [h.configs.fingerprint]
        other = h.store.new_token("someone-else")
        assert h.call("GET", "/v1/configs", auth=other)[1]["pending"] == []
        assert h.call("GET", f"/v1/configs/0x{h.configs.fingerprint:08x}", auth=other)[0] == 404
        assert h.call("GET", f"/v1/configs/0x{h.configs.fingerprint:08x}", auth=token)[0] == 200

        monkeypatch.setattr(api_client, "CACHE_DIR", tmp_path / "cache")
        monkeypatch.setattr(api_client, "CONFIGS_DIR", tmp_path / "api_configs")
        monkeypatch.setattr(game_configs, "load", lambda: h.older)  # the planner's data package predates the update
        settings = Settings(api_url=f"http://127.0.0.1:{h.port}/export_api", api_tokens=[token])
        snap = api_client.fetch_snapshot(settings, "PlayerOne", max_age=0)
        assert snap["configs"] == h.configs.fingerprint
        cached = api_client.cached_configs(h.configs.fingerprint)
        assert cached is not None and cached.fingerprint == h.configs.fingerprint and new_id in cached.achievements
        # Expanded with the fetched set the new achievement is known; with the package's alone it is not.
        p = PlayerState(name="PlayerOne")
        apply_plugin_snapshot(p, snap, configs=h.older)
        assert not any("achievement id(s) missing" in n for n in p.notes)
        monkeypatch.setattr(api_client, "CONFIGS_DIR", tmp_path / "empty")
        p = PlayerState(name="PlayerOne")
        apply_plugin_snapshot(p, snap, configs=h.older)
        assert any("achievement id(s) missing" in n for n in p.notes)
    finally:
        h.close()


def test_export_api_health_reads_the_store_and_cors_is_one_origin(tmp_path):
    """GET /v1/health keeps its body but fails when the database does; CORS answers exactly the site's origin."""
    import http.client

    h = _ApiHarness(tmp_path)
    try:
        def raw(method, path, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=5)
            conn.request(method, "/export_api" + path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body

        site = {"Origin": "https://runeadvisor.com"}
        assert h.call("GET", "/v1/health") == (200, {"ok": True})
        # Preflight from the site: allowed, with the methods and headers the account page uses.
        status, headers, body = raw("OPTIONS", "/v1/account", {**site, "Access-Control-Request-Method": "DELETE",
                                                             "Access-Control-Request-Headers": "authorization"})
        assert status == 204 and body == b"" and headers["access-control-allow-origin"] == "https://runeadvisor.com"
        assert "DELETE" in headers["access-control-allow-methods"] and "Authorization" in headers["access-control-allow-headers"]
        assert "access-control-allow-credentials" not in headers
        # Replies (errors included) carry the header for the site only; any other origin gets none, never "*".
        status, headers, _ = raw("GET", "/v1/account", {**site, "Authorization": "Bearer nonsense"})
        assert status == 401 and headers["access-control-allow-origin"] == "https://runeadvisor.com" and headers["vary"] == "Origin"
        for origin in ("https://evil.example", "http://runeadvisor.com", "https://www.runeadvisor.com", "null"):
            status, headers, _ = raw("GET", "/v1/health", {"Origin": origin})
            assert status == 200 and "access-control-allow-origin" not in headers
            assert raw("OPTIONS", "/v1/account", {"Origin": origin})[0] == 403
        assert raw("OPTIONS", "/v1/account")[0] == 403  # no Origin: not a browser preflight
        assert "access-control-allow-origin" not in raw("GET", "/v1/health")[1]
        # A database that cannot be read is not healthy: 503 {"ok": false}.
        h.store.db.execute("ALTER TABLE accounts RENAME TO accounts_gone")
        assert h.call("GET", "/v1/health") == (503, {"ok": False})
        h.store.db.execute("ALTER TABLE accounts_gone RENAME TO accounts")
        assert h.call("GET", "/v1/health") == (200, {"ok": True})
    finally:
        h.close()


def test_export_api_check_verifies_a_copy_without_writing(tmp_path, capsys):
    """`check`: exit 0 on a sound backup copy, 1 with `problem:` lines on a damaged one, 2 when there is no database;
    it writes nothing (no -wal/-shm beside the copy, no directory created)."""
    import shutil
    import sqlite3

    from rune_advisor import export_api
    from rune_advisor.export_api import Store

    configs, _, _ = _mock_configs_and_posts(tmp_path)
    live = tmp_path / "live"
    store = Store(live)
    account, _ = store.create_account()
    _, token = store.create_token(account, "plugin", ["submit"], None)
    store.revoke_id(account, store.create_token(account, "old", ["read"], None)[0])
    store.save_state(account, "PlayerOne", {"player": "PlayerOne", "updated": 1.0, "seq": 1, "posts": 1})
    older = _older_configs(configs)
    store.add_pending(account, configs, older)
    fp = configs.fingerprint
    second, _ = store.create_account()
    store.add_pending(second, older, configs)
    store.confirm_config(older.fingerprint)
    store.set_setting("post_interval", 30)

    def backup(name):
        copy = tmp_path / name
        copy.mkdir()
        dest = sqlite3.connect(str(copy / "export_api.sqlite3"))
        store.db.backup(dest)  # what `sqlite3 ... ".backup"` does
        dest.close()
        for part in ("settings.json", "generations", "pending_configs"):
            src = live / part
            (shutil.copytree if src.is_dir() else shutil.copy)(src, copy / part)
        return copy

    def check(folder):
        code = export_api.main(["--data-dir", str(folder), "check"])
        return code, capsys.readouterr().out.splitlines()

    good = backup("good")
    before = sorted(p.name for p in good.iterdir())
    code, lines = check(good)
    assert code == 0 and lines[-1] == "check: ok"
    assert lines[:-1] == ["accounts = 2", "players = 1", "tokens = 1", "revoked_tokens = 1", "generations = 1", "pending_sets = 1"]
    assert sorted(p.name for p in good.iterdir()) == before  # nothing written: no -wal, no -shm
    assert all(f"{k} = {v}" in lines for k, v in store.stats().items() if k in ("accounts", "players", "tokens", "revoked_tokens"))
    assert check(live)[0] == 0  # the live dir (a -wal beside it) reads through the WAL

    # Damage, one kind at a time.
    lost = backup("lost")
    shutil.rmtree(next((lost / "pending_configs").iterdir()))
    shutil.rmtree(lost / "generations" / f"{older.fingerprint:08x}")
    code, lines = check(lost)
    assert code == 1 and lines[-1] == "check: FAILED (2 problem(s))"
    assert sum(line.startswith("problem: pending_configs/") and "files missing" in line for line in lines) == 1
    assert f"problem: generations/{older.fingerprint:08x}: confirmed in the database, missing on disk" in lines

    wrong = backup("wrong")
    (wrong / "generations" / f"{older.fingerprint:08x}").rename(wrong / "generations" / f"{fp:08x}")
    (wrong / "settings.json").write_text("{not json")
    db = sqlite3.connect(str(wrong / "export_api.sqlite3"))
    db.execute("DELETE FROM accounts WHERE id = ?", (account,))
    db.execute("PRAGMA journal_mode=DELETE")
    db.commit()
    db.close()
    code, lines = check(wrong)
    problems = [line for line in lines if line.startswith("problem: ")]
    assert code == 1 and len(problems) == 5, problems
    assert "problem: 1 live token(s) without an account" in lines and "problem: 1 character state(s) without an account" in lines
    assert any("settings.json" in p for p in problems) and any("content has fingerprint" in p for p in problems)

    torn = backup("torn")
    data = bytearray((torn / "export_api.sqlite3").read_bytes())
    data[4096:4096 + 2048] = b"\xff" * 2048
    (torn / "export_api.sqlite3").write_bytes(data)
    code, lines = check(torn)
    assert code == 1 and lines[-1].startswith("check: FAILED")

    assert check(tmp_path / "nowhere") == (2, [f"check: no export_api.sqlite3 in {tmp_path / 'nowhere'}"])
    assert not (tmp_path / "nowhere").exists()
    store.close()


def test_wire_fast_paths_match_the_bit_by_bit_reference(tmp_path):
    """`wire.canonical` / `unpack_full` pack the inventories as 32-bit words in one go: byte-identical to writing
    every value through the bit writer (what the plugin's Lua does), for any slot width total."""
    import random

    import pytest

    from rune_advisor import wire

    def reference(layout, state):
        w = wire._Writer()
        for slot, value in zip(layout.slots, state.slots):
            w.write(value, slot.width)
        for name in layout.inventories:
            items = state.invs.get(name)
            if items is None:
                w.write(wire.UNKNOWN, 32)
                continue
            w.write(len(items), 32)
            for item_id in sorted(items):
                w.write(item_id, 32)
                w.write(items[item_id], 32)
        return w.bytes()

    rng = random.Random(7)
    for widths in ([1], [3, 5], [7, 1, 32, 13], [8, 8, 16], [1] * 9, [31, 2]):  # byte-aligned totals and not
        slots = [wire.WireSlot("named", f"V{i}", w) for i, w in enumerate(widths)]
        layout = wire.WireLayout(slots, [s.name for s in slots], [], ["A", "B", "C", "D"], None)
        layout.__dict__["_hash"] = 0x1234  # no manifest to describe in this bare layout
        state = wire.WireState([rng.getrandbits(w) for w in widths],
                               {"A": {rng.randrange(1, 60000): rng.randrange(1, 2**32) for _ in range(40)},
                                "B": {}, "D": {0xFFFFFFFE: 1, 5: 0xFFFFFFFF}})  # C never loaded
        packed = wire.canonical(layout, state)
        assert packed == reference(layout, state)
        back = wire.unpack_full(layout, packed)
        assert back.slots == state.slots and back.invs == state.invs
        assert wire.state_hash(layout, state) == wire.hash_canonical(layout, packed)
        for cut in (len(packed) - 1, len(packed) - 9, 1):  # a body shorter than its own counts is refused
            with pytest.raises(ValueError):
                wire.unpack_full(layout, packed[:cut])
    # A count of billions used to loop that long; now it is a short body.
    layout = wire.WireLayout([], [], [], ["A"], None)
    with pytest.raises(ValueError):
        wire.unpack_full(layout, (0xFFFFFFF0).to_bytes(4, "little") + b"\0" * 64)


def test_export_api_load_stretches_the_interval_and_sheds(tmp_path, monkeypatch):
    """Under load the interval in replies grows (bounded, switchable), a full house answers 503 "busy" at once,
    connections past the cap are closed, and a token posting far too fast gets 429."""
    import http.client

    from rune_advisor import export_api
    from rune_advisor.export_api import Load

    # The controller alone, with a fake clock.
    load, settings = Load(), dict(export_api.DEFAULT_SETTINGS)
    t0 = load.since
    assert load.interval(settings) == 10
    load.finished(9.0, settings, now=t0 + 10)            # 90% of a core over the window
    assert load.interval(settings) == 15
    load.finished(4.5, settings, now=t0 + 20)            # 45%: inside the band, stays
    assert load.interval(settings) == 15
    for i in range(3, 30):
        load.finished(9.0, settings, now=t0 + 10 * i)
    assert load.interval(settings) == 300                # never past auto_interval_max
    assert load.interval({**settings, "auto_interval": 0.0}) == 10
    assert load.interval({**settings, "post_interval": 600.0}) == 600  # an operator's longer interval is kept
    for i in range(30, 80):
        load.finished(0.1, settings, now=t0 + 10 * i)    # quiet again: back to the configured interval
    assert load.interval(settings) == 10
    load.shed = 1                                        # a busy answer in a quiet window still stretches it
    load.finished(0.1, settings, now=t0 + 10 * 81)
    assert load.interval(settings) == 15

    h = _ApiHarness(tmp_path)
    try:
        service = h.server.service
        _, made = h.call("POST", "/v1/accounts", {})
        _, plugin = h.call("POST", "/v1/tokens", {"label": "plugin", "scopes": ["submit"]}, auth=made["key"])
        full = next(p for p in h.posts if p["kind"] == "full")
        status, reply = h.call("POST", "/v1/state", full, auth=plugin["token"])
        assert status == 200 and reply["interval"] == 10
        service.load.factor = 3.0                        # as after a few loaded windows
        status, reply = h.call("POST", "/v1/state", full, auth=plugin["token"])
        assert status == 200 and reply["interval"] == 30
        assert h.call("GET", "/v1/players", auth="nonsense")[1]["interval"] == 30  # error replies carry it too
        h.store.set_setting("auto_interval", 0)
        assert h.call("POST", "/v1/state", full, auth=plugin["token"])[1]["interval"] == 10
        h.store.set_setting("auto_interval", 1)

        # Busy: more requests in hand than BUSY_REQUESTS. No token is looked at; health still answers.
        service.load.active = export_api.BUSY_REQUESTS
        conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=5)
        conn.request("POST", "/export_api/v1/state", body=json.dumps(full), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        busy = json.loads(resp.read())
        conn.close()
        assert resp.status == 503 and resp.getheader("Retry-After") == "60"
        assert busy["accepted"] is False and busy["retry_after"] == 60 and busy["interval"] == 30 and "busy" in busy["reason"]
        assert h.call("GET", "/v1/health") == (200, {"ok": True})
        assert service.load.totals["busy"] == 1 and service.load.shed == 1
        service.load.active = 0

        # The connection cap: closed unanswered, counted.
        import time as _time
        for _ in range(50):  # let the earlier connections' threads finish counting themselves out
            if service.load.connections == 0:
                break
            _time.sleep(0.02)
        service.load.connections = export_api.MAX_CONNECTIONS
        conn = http.client.HTTPConnection("127.0.0.1", h.port, timeout=5)
        try:
            conn.request("GET", "/export_api/v1/health")
            conn.getresponse()
            answered = True
        except (OSError, http.client.HTTPException):
            answered = False
        conn.close()
        assert not answered and service.load.totals["dropped"] == 1
        service.load.connections = 0
        assert h.call("GET", "/v1/health")[0] == 200

        # A token posting far faster than a plugin does: 429, per token and per kind.
        service.load.factor = 1.0
        service.token_limiter = export_api.RateLimiter()  # forget the posts made above
        monkeypatch.setitem(export_api.TOKEN_LIMITS, "state", lambda interval: 3.0)
        codes = [h.call("POST", "/v1/state", full, auth=plugin["token"])[0] for _ in range(5)]
        assert codes == [200, 200, 200, 429, 429]
        _, other = h.call("POST", "/v1/tokens", {"label": "second", "scopes": ["submit"]}, auth=made["key"])
        assert h.call("POST", "/v1/state", full, auth=other["token"])[0] == 200
        assert h.call("GET", "/v1/players", auth=plugin["token"])[0] == 200  # the token check is not a post
        assert service.load.totals["limited"] == 2
    finally:
        h.close()


def test_export_api_listens_before_the_slow_part_of_start_up(tmp_path, monkeypatch):
    """`serve` binds and listens before it builds the Service, so a request made during start-up waits in the
    backlog and is answered, instead of being refused (the 502s at a restart)."""
    import http.client
    import threading
    import time

    from rune_advisor import export_api

    configs, _, _ = _mock_configs_and_posts(tmp_path)
    seen: dict = {}
    real = export_api.Service

    def slow_service(*args, **kwargs):
        # While the Service is being built the socket must already accept connections (into the backlog).
        probe = threading.Thread(target=lambda: seen.update(reply=_get(seen["port"])), daemon=True)
        seen["probe"] = probe
        return real(*args, **kwargs)

    def _get(port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/v1/health")
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    real_init = export_api.ApiServer.__init__

    def init(self, address, service, *args, **kwargs):
        real_init(self, address, service, *args, **kwargs)
        seen["port"] = self.server_address[1]
        seen["service_at_bind"] = service

    monkeypatch.setattr(export_api.ApiServer, "__init__", init)
    monkeypatch.setattr(export_api, "Service", slow_service)
    server = export_api.serve(tmp_path / "api", "127.0.0.1:0", "", configs=configs)
    try:
        assert seen["service_at_bind"] is None and server.service is not None
        seen["probe"].start()       # connects now: nobody is accepting yet
        time.sleep(0.3)
        assert "reply" not in seen
        threading.Thread(target=server.serve_forever, daemon=True).start()
        seen["probe"].join(5)
        assert seen["reply"] == (200, {"ok": True})
    finally:
        server.shutdown()
        server.server_close()


import sys as _sys

_REPO = Path(__file__).resolve().parent.parent


def test_publish_data_package_prune_rule():
    """Which files the publish script deletes: per format, all but the indexed version and the one before it;
    anything newer than the index, other formats without an index entry, and stray files are left alone."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("publish_data_package", _REPO / "scripts" / "publish_data_package.py")
    pub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pub)
    pkg = lambda fmt, v: f"rune-advisor-data-{fmt}-{v}.tar.xz"
    names = [n for v in ("2026.11.1", "2026.11.2", "2026.11.10", "2026.12.1") for n in (pkg(1, v), pkg(1, v) + ".sig")]
    names += [pkg(2, "2026.11.5"), "index.json", "notes.txt", pkg(1, "2026.10.1")]
    index = {"packages": {"1": {"version": "2026.11.10", "file": pkg(1, "2026.11.10")}}}
    assert sorted(pub.superseded(names, index)) == sorted(
        [pkg(1, "2026.10.1"), pkg(1, "2026.11.1"), pkg(1, "2026.11.1") + ".sig"])  # .2 and .10 stay; 12.1 is newer
    assert pub.superseded(names, {"packages": {}}) == []
    assert pub.NAME.match(pkg(1, "2026.11.10")) and not pub.NAME.match(pkg(1, "2026.11.10") + ".sig")
    assert not pub.NAME.match("rune-advisor-data-1-2026.11.10.tar.xz.part") and not pub.NAME.match("../x.tar.xz")


def test_publish_data_package_over_git(tmp_path, capsys, monkeypatch):
    """The publish script against a local bare repository standing in for `dl.git` (a post-receive hook exports
    `main` and switches a symlink onto it, like the server's) and a local web server serving that symlink:
    refuses without a key, a dry run pushes nothing, the first push creates `main` with package, signature and
    index in one commit, verifies over HTTP, never overwrites, prunes old versions, leaves everything outside
    `data/` alone, and when `main` moved between clone and push makes the commit again on the new tip."""
    import functools
    import http.server
    import importlib.util
    import shutil
    import subprocess
    import threading

    import pytest

    from rune_advisor import datapkg

    if not shutil.which("git") or not shutil.which("tar"):
        pytest.skip("no git/tar here")
    spec = importlib.util.spec_from_file_location("publish_data_package", _REPO / "scripts" / "publish_data_package.py")
    pub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pub)

    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                              env={**__import__("os").environ, **env}).stdout

    bare, exports, site = tmp_path / "dl.git", tmp_path / "exports", tmp_path / "site"
    exports.mkdir()
    git("init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    (bare / "hooks" / "post-receive").write_text(
        "#!/bin/sh\nset -e\nunset GIT_QUARANTINE_PATH\nwhile read old new ref; do\n"
        '  [ "$ref" = refs/heads/main ] || continue\n'
        f'  d="{exports}/$new"; mkdir -p "$d"; git archive "$new" | tar -x -C "$d"\n'
        f'  ln -sfn "$d" "{site}.tmp"; mv -T "{site}.tmp" "{site}"\n'
        '  echo "test dl now serves $new" >&2\ndone\n')
    (bare / "hooks" / "post-receive").chmod(0o755)
    web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(site)))
    web.RequestHandlerClass.log_message = lambda *a: None
    threading.Thread(target=web.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{web.server_address[1]}"
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    try:
        private, public = datapkg.generate_keypair()
        data = tmp_path / "tree"
        data.mkdir()
        (data / "lamp_xp.json").write_text("{}")
        out = tmp_path / "dist"

        def build(version):
            archive, _ = datapkg.build(out, version, private, "notice", root=data)
            return archive

        def publish(*extra, key=True):
            argv = [*map(str, extra), "--remote", str(bare), "--base-url", base_url, "--verify-seconds", "5"]
            code = pub.main(argv + (["--public-key", public] if key else []))
            captured = capsys.readouterr()
            return code, captured.out + captured.err

        def tree():
            listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=bare, capture_output=True, text=True)
            return sorted(listed.stdout.split())

        commits = lambda: int(git("rev-list", "--count", "main", cwd=bare)) if tree() else 0
        served = lambda name: (site / "data" / name)

        first = build("2026.11.1")
        code, text = publish(first, key=False)
        assert code == 1 and "no public key" in text and tree() == []   # nobody could verify it: refused
        code, text = publish(first, "--dry-run")
        assert code == 0 and "dry run complete" in text and "would push" in text and tree() == []
        code, text = publish(first)
        assert code == 0 and "publish: ok" in text and "verified over HTTPS" in text and "test dl now serves" in text, text
        assert tree() == ["data/index.json", f"data/{first.name}", f"data/{first.name}.sig"] and commits() == 1
        assert json.loads(served("index.json").read_text()) == {"packages": {"1": {"version": "2026.11.1", "file": first.name}}}
        assert served(first.name).read_bytes() == first.read_bytes()

        code, text = publish(first)
        assert code == 1 and "already published" in text and commits() == 1    # immutable
        older = build("2026.10.1")
        code, text = publish(older)
        assert code == 1 and "downgrade" in text and commits() == 1

        # SysAdmin's pushes own everything outside data/ (planner assets, the CDN's header file).
        other = tmp_path / "other"
        git("clone", "--quiet", str(bare), str(other))
        (other / "planner-assets").mkdir()
        (other / "planner-assets" / "a.js").write_text("1")
        (other / "data" / "index.json").write_text(json.dumps({"packages": {
            "1": {"version": "2026.11.1", "file": first.name}, "0": {"version": "2025.1.1", "file": "x"}}}))
        git("add", "-A", cwd=other)
        git("commit", "--quiet", "-m", "assets", cwd=other)
        git("push", "--quiet", "origin", "HEAD:main", cwd=other)

        second, third = build("2026.11.2"), build("2026.11.3")
        assert publish(second)[0] == 0
        # main moves between the clone and the push: the commit is made again on the new tip, never forced.
        real_push, moved = pub.Repo.push, []

        def push_after_someone_else(self):
            if not moved:
                (other / "planner-assets" / "b.js").write_text("2")
                git("pull", "--quiet", "--ff-only", cwd=other)
                git("add", "-A", cwd=other)
                git("commit", "--quiet", "-m", "more assets", cwd=other)
                git("push", "--quiet", "origin", "HEAD:main", cwd=other)
                moved.append(True)
            return real_push(self)

        pub.Repo.push = push_after_someone_else
        try:
            code, text = publish(third)
        finally:
            pub.Repo.push = real_push
        assert code == 0 and "main moved" in text and f"removed: {first.name}, {first.name}.sig" in text, text
        assert tree() == ["data/index.json", f"data/{second.name}", f"data/{second.name}.sig", f"data/{third.name}",
                          f"data/{third.name}.sig", "planner-assets/a.js", "planner-assets/b.js"]
        index = json.loads(served("index.json").read_text())["packages"]
        assert index["1"] == {"version": "2026.11.3", "file": third.name} and index["0"]["version"] == "2025.1.1"
        assert (site / "planner-assets" / "b.js").read_text() == "2"
        # A tampered package never leaves the machine.
        bad = build("2026.11.4")
        bad.write_bytes(bad.read_bytes() + b"x")
        before = commits()
        code, text = publish(bad)
        assert code == 1 and "does not verify" in text and commits() == before
        # What the app sees through its own update check.
        offer = datapkg.check(base_url)
        assert offer.version == "2026.11.3"
    finally:
        web.shutdown()


def test_export_api_takes_over_a_socket_from_the_service_manager(tmp_path):
    """Socket activation (systemd's LISTEN_PID / LISTEN_FDS, fd 3): `serve` uses the passed listening socket and
    ignores --listen; while the service is down, connections wait in the manager's socket and the next start
    answers them (no refused connection across a restart). Without the variables it binds --listen as before."""
    import http.client
    import os
    import socket
    import subprocess
    import threading
    import time

    from rune_advisor import export_api

    # The parsing rules, on a fake environment: wrong pid, no count, nonsense -> the ordinary start.
    for env in ({}, {"LISTEN_PID": "1", "LISTEN_FDS": "1"}, {"LISTEN_PID": str(os.getpid())},
                {"LISTEN_PID": "x", "LISTEN_FDS": "1"}, {"LISTEN_PID": str(os.getpid()), "LISTEN_FDS": "0"}):
        env = dict(env)
        assert export_api.inherited_socket(env) is None and not env  # and the variables are cleared

    manager = socket.socket()                      # plays systemd's .socket unit
    manager.bind(("127.0.0.1", 0))
    manager.listen(16)
    port = manager.getsockname()[1]
    data = tmp_path / "api"
    # exec keeps the pid, so the child can name itself in LISTEN_PID as systemd does.
    launcher = ("import os, sys; os.environ['LISTEN_PID'] = str(os.getpid()); os.environ['LISTEN_FDS'] = '1'; "
                "os.execv(sys.executable, [sys.executable, '-m', 'rune_advisor.export_api', '--data-dir', sys.argv[1], "
                "'serve', '--listen', '127.0.0.1:9', '--prefix', ''])")   # port 9: would fail to bind if it were used
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(_REPO / "src"), os.environ.get("PYTHONPATH", "")])}

    def start():
        return subprocess.Popen([_sys.executable, "-c", launcher, str(data)], env=env, stderr=subprocess.PIPE,
                                preexec_fn=lambda: os.dup2(manager.fileno(), 3), pass_fds=(3,))

    def health(timeout=20):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/v1/health")
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())

    first = start()
    try:
        assert health() == (200, {"ok": True})
        first.terminate()
        log = first.stderr.read().decode()
        first.wait(10)
        assert f"listening on 127.0.0.1:{port} (socket passed in by the service manager" in log, log

        # The service is down. A request made now is not refused: it waits in the manager's socket...
        answer: dict = {}
        waiting = threading.Thread(target=lambda: answer.update(reply=health()), daemon=True)
        waiting.start()
        time.sleep(0.5)
        assert "reply" not in answer
        second = start()                           # ...and the restarted service answers it.
        try:
            waiting.join(20)
            assert answer.get("reply") == (200, {"ok": True})
        finally:
            second.terminate()
            second.wait(10)
    finally:
        if first.poll() is None:
            first.kill()
        manager.close()

    # No variables: binds --listen itself, exactly as before.
    server = export_api.serve(tmp_path / "plain", "127.0.0.1:0", "", configs=_mock_configs_and_posts(tmp_path)[0])
    try:
        assert not server.inherited and server.server_address[1] != 0
    finally:
        server.server_close()


def test_export_api_deletions_survive_a_restore(tmp_path):
    """With a deletion log outside the data dir, restoring an older copy of the data dir does not bring back what
    was deleted since: the next sweep deletes the account, the character and revokes the token again. The log
    holds ids and hashes only, entries age out, and without a log place nothing is written."""
    import shutil
    import sqlite3
    from datetime import date, timedelta

    from rune_advisor.export_api import DELETION_LOG_DAYS, Store

    live, log_dir = tmp_path / "live", tmp_path / "outside"
    store = Store(live, deletions=log_dir)
    gone, _ = store.create_account()
    kept, _ = store.create_account()
    _, kept_token = store.create_token(kept, "plugin", ["submit"], None)
    old_id, old_token = store.create_token(kept, "old laptop", ["read"], None)
    for account, player in ((gone, "Zezima"), (kept, "PlayerOne"), (kept, "Second Char")):
        store.save_state(account, player, {"player": player, "updated": 1.0, "seq": 1, "posts": 1})
    backup = tmp_path / "backup"
    backup.mkdir()
    dest = sqlite3.connect(str(backup / "export_api.sqlite3"))
    store.db.backup(dest)
    dest.close()

    # After the backup: one account deleted, one character deleted, one token revoked.
    store.delete_account(gone)
    assert store.delete_state(kept, "second char") or store.delete_state(kept, "Second Char")
    assert store.revoke_id(kept, old_id)
    text = (log_dir / "deletions.jsonl").read_text()
    assert len(text.splitlines()) == 3 and "Zezima" not in text and "Second" not in text and old_token not in text
    import uuid
    character = next(json.loads(line)["character"] for line in text.splitlines() if "character" in line)
    assert uuid.UUID(character).version == 4  # a random id of ours, nothing derived from the name
    store.close()

    # Disaster: the data dir is restored from the backup. Everything deleted is back...
    shutil.rmtree(live)
    shutil.copytree(backup, live)
    restored = Store(live, deletions=log_dir)
    assert restored.stats()["accounts"] == 2 and restored.stats()["players"] == 3 and restored.lookup(old_token)[1] == "ok"
    # ...until the sweep (which runs at every start) applies the log again.
    restored.sweep()
    assert restored.stats()["accounts"] == 1 and [p["player"] for p in restored.players(kept)] == ["PlayerOne"]
    assert restored.lookup(old_token) == (None, "revoked") and restored.lookup(kept_token)[1] == "ok"
    assert restored.reapply_deletions() == 0                         # nothing left to redo
    # Entries age out after DELETION_LOG_DAYS (the oldest backup that could hold the data).
    saved_log = tmp_path / "log-from-a-newer-backup.jsonl"
    shutil.copy(log_dir / "deletions.jsonl", saved_log)
    restored.reapply_deletions(today=date.today() + timedelta(days=DELETION_LOG_DAYS + 2))
    assert (log_dir / "deletions.jsonl").read_text() == ""
    restored.close()

    # A restore where the live log was lost too: the copies in newer backups are merged in before starting.
    shutil.rmtree(live)
    shutil.rmtree(log_dir)
    shutil.copytree(backup, live)
    from rune_advisor import export_api
    assert export_api.main(["--data-dir", str(live), "--deletions", str(log_dir), "deletions", "merge",
                            str(saved_log), str(saved_log)]) == 0
    import os
    os.environ.pop(export_api.DELETIONS_ENV, None)  # main() sets it for the process; later tests must not see it
    again = Store(live, deletions=log_dir)
    assert len((log_dir / "deletions.jsonl").read_text().splitlines()) == 3  # each entry once
    assert again.stats()["accounts"] == 1 and [p["player"] for p in again.players(kept)] == ["PlayerOne"]
    assert again.lookup(old_token) == (None, "revoked")
    again.close()

    plain = Store(tmp_path / "plain")                                # no place given: no log, nothing written
    account, _ = plain.create_account()
    plain.delete_account(account)
    assert plain.deletions_file is None and plain.reapply_deletions() == 0
    assert sorted(p.name for p in (tmp_path / "plain").iterdir())[0].startswith("export_api.sqlite3")
    plain.close()


def test_reward_books_and_black_pearls_are_xp_in_hand():
    """D&D reward books (told apart by item id: both are "Reward book" in game) are xp in any skill, black
    pearls a medium star's bonus xp; an id the server could not name takes this planner's name."""
    from rune_advisor import player as players
    from rune_advisor.resources import load_pool, rush_book_xp, troll_book_xp
    from rune_advisor.xp import star_xp

    p = PlayerState(name="PlayerOne")
    players.apply_plugin_snapshot(p, {"schema": 1, "inventories": {"INV": {"items": [
        {"id": 31371, "name": "Unknown item 31371", "count": 2},
        {"id": 32193, "name": "Reward book (Rush of Blood)", "count": 1}]},
        "BANK": {"items": [{"id": 20935, "name": "Reward book", "count": 1}]}}})
    assert p.holdings["Black pearl"] == 2 and p.item_ids["Black pearl"] == 31371
    pool = {r.source or r.name: r for r in load_pool(p).resources if r.units}
    pearl, troll, rush = pool["Black pearl"], pool["Troll Invasion"], pool["Rush of Blood"]
    assert pearl.bonus and pearl.units == 2 and pearl.xp_per_unit(90) == star_xp(90, "medium")
    assert not troll.bonus and troll.skills is None and troll.xp_per_unit(99) == troll_book_xp(99) == 77_624
    assert rush.xp_per_unit(99) == rush_book_xp(99) == 7_500 and rush.xp_per_unit(50) < 7_500 and not rush.verified


def test_free_xp_places_unbankable_and_capped_bonus_xp(monkeypatch):
    """Black pearls (not bankable) and rested xp from the lowest Town Hall cap up are placed by free_xp: on the
    skill where they save the most planned training, or on `bonus_xp_skill`; other tracks' plans leave them."""
    from rune_advisor.models import Skill
    from rune_advisor.recommend import picker
    from rune_advisor.recommend.tracks import free_xp
    from rune_advisor.resources import load_pool
    from rune_advisor.xp import star_xp

    p = PlayerState(name="X", inventories={"INV": {"Black pearl": 2}}, rested_xp=400,
                    skills={"Mining": Skill(name="Mining", level=90, xp=xp_for_level(90)),
                            "Agility": Skill(name="Agility", level=80, xp=xp_for_level(80)),
                            "Hitpoints": Skill(name="Hitpoints", level=90, xp=xp_for_level(90))})
    rates = {"Mining": 100_000.0, "Agility": 20_000.0, "Hitpoints": 10_000.0}
    monkeypatch.setattr(free_xp, "goal_levels", lambda ctx: {"Mining": 99, "Agility": 99, "Hitpoints": 99})

    class Ctx:
        player, resources, skill_weights, bonus_xp_skill = p, None, {}, None

        def level_plan(self, skill, level):
            return type("LP", (), {"hours": (xp_for_level(level) - p.skills[skill].xp) / rates[skill]})()

    ctx = Ctx()
    (pearls,) = free_xp.FreeXpTrack.candidates(ctx)  # 400 rested xp is under every cap: left to level plans
    gained = 2 * star_xp(80, "medium")
    assert pearls.title == "Crack 2 x Black pearl on Agility" and pearls.skills == {"Agility"}  # the slow skill
    assert pearls.value == round(gained / rates["Agility"], 2) and "can't be banked" in pearls.why

    ctx.bonus_xp_skill = "Mining"
    assert free_xp.FreeXpTrack.candidates(ctx)[0].title == "Crack 2 x Black pearl on Mining"
    ctx.bonus_xp_skill = None

    # Bonus xp already banked in a skill uses up its room: only half of what is left to the goal can be bonus.
    left = xp_for_level(99) - xp_for_level(80)
    p.bonus_xp = {"Agility": left / 2 - 1000}
    assert free_xp.FreeXpTrack.candidates(ctx)[0].title == "Crack 2 x Black pearl on Mining"
    p.bonus_xp = {}

    p.rested_xp = 1_000
    titles = [c.title for c in free_xp.FreeXpTrack.candidates(ctx)]
    assert "Cash in 1,000 rested xp with Aster on Agility" in titles and len(titles) == 2
    kept = {r.name for r in picker.without_choice(load_pool(p)).resources if r.units}
    assert "Black pearl" not in kept and "rested xp" not in kept
    p.rested_xp = 400
    assert "rested xp" in {r.name for r in picker.without_choice(load_pool(p)).resources if r.units}
    p.members = False  # bonus xp can't be used on free-to-play
    assert free_xp.FreeXpTrack.candidates(ctx) == []


def test_tool_belt_tools_and_slayer_task_come_from_the_export():
    """TOOLBELT_*_LEVEL name the pickaxe and hatchet on the belt (the wiki's value tables), so the sims stop
    assuming one; overrides still win. SLAYER_TARGET / COUNT / MASTER give the current assignment."""
    from rune_advisor import player as players, sim
    from rune_advisor.models import Skill

    p = PlayerState(name="PlayerOne", skills={"Mining": Skill("Mining", 99, xp_for_level(99)),
                                              "Woodcutting": Skill("Woodcutting", 99, xp_for_level(99))})
    assert sim.best_pickaxe(p)[1] and sim.best_hatchet(p)[1]  # nothing known: assumed
    players.apply_plugin_snapshot(p, {"schema": 1, "vars": {
        "TOOLBELT_PICKAXE_LEVEL": 29, "TOOLBELT_HATCHET_LEVEL": 7,
        "SLAYER_TARGET": 42, "SLAYER_COUNT": 37, "SLAYER_MASTER": 6}})
    assert p.tools == {"pickaxe": "Crystal", "hatchet": "Dragon"}
    assert sim.best_pickaxe(p) == (next(x for x in sim.data()["picks"] if x["label"] == "Crystal"), False)
    assert sim.best_hatchet(p)[0]["label"] == "Dragon" and not sim.best_hatchet(p)[1]
    assert p.slayer_task == ("Abyssal Demons", 37, "Kuradal")
    assert any(n.startswith("Slayer task: 37 Abyssal Demons left (from Kuradal)") for n in p.notes)
    assert players.belt_tools({"TOOLBELT_PICKAXE_LEVEL": 24}) == {"pickaxe": "Dragon"}  # gilded: the same tool
    assert players.belt_tools({"TOOLBELT_HATCHET_LEVEL": 14}) == {"hatchet": "Ember and glade"}
    assert players.slayer_task_from_vars({"SLAYER_TARGET": 0, "SLAYER_COUNT": 0}) is None
    # Every value the tables name is a tool the sims know.
    values = players._var_values()
    assert {players.belt_tools({"TOOLBELT_PICKAXE_LEVEL": int(v)})["pickaxe"] for v in values["TOOLBELT_PICKAXE_LEVEL"]} \
        <= {x["label"] for x in sim.data()["picks"]}
    assert {players.belt_tools({"TOOLBELT_HATCHET_LEVEL": int(v)})["hatchet"] for v in values["TOOLBELT_HATCHET_LEVEL"]} \
        <= {x["label"] for x in sim.data()["hatchets"]}
