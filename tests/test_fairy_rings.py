"""Fairy rings to build or repair (fairy_rings.py)."""

from __future__ import annotations

from rune_advisor import fairy_rings
from rune_advisor.models import PlayerState


def test_ring_flags():
    every = {"FAIRYRING_KHARAZI_UNLOCKED": 1, "FAIRYRING_THE_LOST_GROVE_UNLOCKED": 1, "FAIRYRING_FENMOOR_UNLOCKED": 1,
             "FT3_MOS_LE_HARMLESS_BRIDGE_1": 1, "FT3_MOS_LE_HARMLESS_BRIDGE_2": 1}
    done = PlayerState(name="PlayerOne", vars=every)
    # the test account built every ring: only the Ancient Cavern's, which no var shows, stays open (unknown).
    assert [(c, s) for c, _, s in fairy_rings.open_rings(done)] == [("BJQ", None)]
    half = PlayerState(name="PlayerOne", vars={**every, "FT3_MOS_LE_HARMLESS_BRIDGE_2": 0})
    assert fairy_rings.built(half, "DIP") is False  # one half of the bridge isn't a crossing
    assert fairy_rings.built(PlayerState(name="PlayerOne"), "CJS") is None
