"""Non-combat Magic from Bucket:Infobox_spell: a spell is cast every `cooldown` ticks for its xp.

Teleports and alchemy only: other skilling spells (superheat, enchanting, Lunar crafting spells) need
a target item per cast that these rows can't name, and combat spells need a target. Runes are the
recipe (elemental runes come from a staff), so ironman plans cost them like any material. Casting
alchemy on noted items is pure action time; teleport casting assumes no banking (runes stack).
"""

from __future__ import annotations

from ..models import PlayerState
from . import bands, data, row

TICK = 0.6
ELEMENTAL = {"Air rune", "Water rune", "Earth rune", "Fire rune"}
ANY_ITEM_SPELLS = {"High Level Alchemy", "Low Level Alchemy"}
KEEP_PER_BAND = 5
DEFAULT_TELEPORT_TICKS = 4


def spells(player: PlayerState, level: int) -> list[dict]:
    out = []
    for s in data().get("spells", []):
        if not s.get("xp") or (s.get("level") or 1) > level or (s.get("members") and not player.members):
            continue
        if s["name"].startswith(("Tele-other", "Tele-group")) or (s.get("type") != "teleport" and s["name"] not in ANY_ITEM_SPELLS):
            continue
        if s.get("spellbook") not in (None, "standard", "ancient", "lunar"):
            continue
        ticks = s.get("cooldown") or DEFAULT_TELEPORT_TICKS
        runes = [[n, q] for n, q in s.get("runes", []) if n not in ELEMENTAL]
        if not runes:
            continue
        out.append({**s, "ticks": ticks, "xp_per_hour": 3600 / (ticks * TICK) * s["xp"], "materials": runes})
    return sorted(out, key=lambda s: -s["xp_per_hour"])


def method_rows(player: PlayerState) -> list[dict]:
    rows = []
    for band in bands(player, "Magic"):
        for s in spells(player, band)[:KEEP_PER_BAND]:
            rows.append(row("Magic", f"{s['name']} (simulated, {s['ticks']:g} ticks a cast)", max(band, s["level"] or 1),
                            band + 4, s["xp_per_hour"],
                            recipe={"output": f"cast {s['name']}", "variants": [
                                {"output_qty": 1, "xp": s["xp"], "ticks": s["ticks"], "materials": s["materials"]}]},
                            assumptions=["elemental runes from a staff"] +
                                        (["items to alch not counted"] if s["name"] in ANY_ITEM_SPELLS else []),
                            activity=s["name"]))
    return rows
