"""Blessings of the Wolf: the Shrine of the Spirit Wolves' unlocks (Havenhythe), costed as pouches to infuse.

One Blessing per pouch infused at the Shrine of Inanna's empowered obelisk, whatever the pouch (scrolls give
none). The shop unlocks its eight rewards in order at running totals (THRESHOLDS: the Blessings aren't spent),
5,000 for the lot, which is the achievement "No Need to Pikkenchoose". The game tests one "owned" bit per
reward, not the balance, and the balance isn't exported: it is taken as the threshold of the last reward
unlocked (0 with none), so up to 599 too low, and said so. Players who owned Shaman's outfit pieces before
Havenhythe started with those unlocked.

Cost: the pouches still to infuse, each at the cheapest pouch the player can make there: the gatherer's whole
chain for it (the shrine recipe: 10% of materials saved) less the spirit shards Bogrog swaps the finished pouch
for (70%). Pouches infused there for any other reason (Summoning training, pouches to disassemble) count too.

Source: RuneScape Wiki, Blessings of the Wolf (CC BY-NC-SA 3.0).
"""

from __future__ import annotations

from .models import PlayerState

ACHIEVEMENT = "No Need to Pikkenchoose"
THRESHOLDS = {"Shaman's headdress": 600, "Shaman's poncho": 1200, "Shaman's leggings": 1800,
              "Shaman's moccasins": 2400, "Shaman's hand wraps": 3000, "Shaman's headdress add-on": 3600,
              "Fang of Mohegan": 4200, "'the Spirit Wolf' title": 5000}
TOTAL = max(THRESHOLDS.values())


def known_blessings(player: PlayerState) -> int:
    """Blessings the export proves: the threshold of the dearest reward unlocked."""
    a = player.achievements.get(ACHIEVEMENT)
    if a is None:
        return 0
    if a.completed:
        return TOTAL
    return max((THRESHOLDS.get(str(r.get("description")), 0) for r in a.requirements if r.get("complete")), default=0)


def next_unlock(blessings: int) -> tuple[str, int] | None:
    """(reward, Blessings still short) of the next unlock."""
    return next(((name, at - blessings) for name, at in THRESHOLDS.items() if at > blessings), None)


def cheapest_pouch(ctx) -> tuple[str, float] | None:
    """(pouch, hours each net of its swap shards) of the cheapest pouch to infuse, among those the player's
    Summoning level makes and the gatherer fully sources."""
    from .recommend.catalogue import catalogue

    p, g = ctx.player, ctx.gatherer
    level = p.level("Summoning")
    shards = g.complete_source("Spirit shards")
    shard_hours = shards.cost_per_unit if shards is not None else 0.0
    best = None
    for name in dict.fromkeys(r["output"] for r in catalogue() if r["skill"] == "Summoning"
                              and r["output"].endswith(" pouch") and r["level"] <= level
                              and any(m == "Spirit shards" for m, _ in r["materials"])):
        src = g.complete_source(name)
        if src is None or src.kind != "craft":
            continue
        net = max(0.0, src.cost_per_unit - g.swap_shards(name) * shard_hours)
        if best is None or net < best[1]:
            best = (name, net)
    return best
