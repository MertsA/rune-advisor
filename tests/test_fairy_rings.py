"""Fairy rings to build or repair (fairy_rings.py)."""

from __future__ import annotations

from rune_advisor import fairy_rings
from rune_advisor.models import PlayerState


def test_ring_flags():
    every = {"FAIRYRING_KHARAZI_UNLOCKED": 1, "FAIRYRING_THE_LOST_GROVE_UNLOCKED": 1, "FAIRYRING_FENMOOR_UNLOCKED": 1,
             "FT3_MOS_LE_HARMLESS_BRIDGE_1": 1, "FT3_MOS_LE_HARMLESS_BRIDGE_2": 1, "FAIRYRINGS_SLAYGUILD_RING": 1}
    done = PlayerState(name="PlayerOne", vars=every)
    assert fairy_rings.open_rings(done) == []
    cavern = PlayerState(name="PlayerOne", vars={**every, "FAIRYRINGS_SLAYGUILD_RING": 0})
    assert [(c, s) for c, _, s in fairy_rings.open_rings(cavern)] == [("BJQ", False)]
    half = PlayerState(name="PlayerOne", vars={**every, "FT3_MOS_LE_HARMLESS_BRIDGE_2": 0})
    assert fairy_rings.built(half, "DIP") is False  # one half of the bridge isn't a crossing
    assert fairy_rings.built(PlayerState(name="PlayerOne"), "CJS") is None


def test_advance_time_casts_left():
    from rune_advisor import advance_time
    assert advance_time.casts_left(PlayerState(name="PlayerOne")) is None
    assert advance_time.casts_left(PlayerState(name="PlayerOne", vars={advance_time.CASTS_LEFT_VAR: 2})) == 2
