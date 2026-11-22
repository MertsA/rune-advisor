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
    sources = {"Old chest": [["Zq widget", 0.5, 2.0, 0]], "Nice NPC": [["Zq gadget", 1.0, 10.0, 0]],
               "Gift box": [["Zq sprocket", 0.25, 4.0, 0]], "Some Quest": [["Zq trinket", 1.0, 1.0, 0]],
               "Santa's Grotto": [["Zq bauble", 1.0, 1.0, 0]], "Box seller": [["Gift box", 1.0, 1.0, 0]]}
    kinds = {"Old chest": "scenery", "Nice NPC": "npc", "Gift box": "item", "Some Quest": "quest",
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
    assert drops.assumed_rate(p, "Old chest", STYLE)[0] == drops.ASSUMED_RATES["scenery"][0]
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
    assert any(w.startswith("Old chest") for w in used) and any("Nice NPC" in w for w in used)
    hours, goals = next(v for k, v in g.flag_use.items() if k.startswith("Old chest"))
    assert hours == pytest.approx(10 * widget.hours_per_unit) and goals == {"Zq widget", "Zq gadget"}


def test_daemonheim_recipes_never_train():
    from rune_advisor.recommend.catalogue import catalogue

    dg = [r for r in catalogue() if r.get("daemonheim")]
    assert dg and all("(Dungeoneering)" in str(r) or "(Daemonheim)" in str(r) for r in dg)
    p = _player(Smithing=99, Crafting=99)
    ctx = Context(player=p, cost=CostModel.default(use_wiki=False))
    rows = ctx._recipe_methods([{"skill": "Smithing", "level_min": 99}], 1_000_000)
    assert not [r for r in rows if "(Dungeoneering)" in r["method"] or "(Daemonheim)" in r["method"]]
