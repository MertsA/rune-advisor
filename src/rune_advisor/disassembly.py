"""Invention materials from disassembly (data/disassembly.json, scripts/build_disassembly.py).

Disassembling an item rolls `draws` materials per item (Disassemble page): each roll is junk with the item's junk
chance (lowered by the junk chance reduction research tier: one multiplier, the highest tier researched; Junk
page), else one of the item's normal materials by weight. Special components roll separately per batch
(`chance` percent, `quantity` each), unaffected by junk. One batch takes 2 ticks (1.2 s); runes and ammo
disassemble 50 to a batch.

Yields are random, so plans ask for enough items to get the materials with CONFIDENCE (95%): the count is the
normal approximation's quantile of the total (mean and variance per item add up over items). A plan that comes
up short anyway is re-planned from the new bank. The research tier is the highest junk chance reduction the export
shows discovered, else every one the Invention level allows; no Invention master cape (x1.2 uncommon/rare weights).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import cache
from . import datapkg

from .models import PlayerState

JUNK = "Junk"
CONFIDENCE = 0.95
Z = 1.6449  # standard normal quantile for CONFIDENCE
BATCH_SECONDS = 1.2
# Invention levels of junk chance reduction research tiers 1-9 (Junk page).
JUNK_RESEARCH_LEVELS = [34, 49, 64, 69, 78, 83, 91, 95, 105]


@cache
def data() -> dict[str, dict]:
    path = datapkg.path("disassembly.json")
    return json.loads(path.read_text())["items"] if path.is_file() else {}


@cache
def sources() -> dict[str, list[str]]:
    """material -> items whose disassembly can give it."""
    out: dict[str, list[str]] = {}
    for item, d in data().items():
        for m in [*d["materials"], *d["special"]]:
            out.setdefault(m, []).append(item)
    # Junk comes from nearly everything; as a source it is the bulk items people break down for Simple parts
    # (logs and the like), which keeps the gatherer's search small. Junk feeds junk refiners.
    # (The refiner itself is made of junk: as a junk source it would be a cycle.)
    out[JUNK] = [item for item in out.get("Simple parts", []) if data()[item]["junk"] and item != "Junk refiner"]
    return out


def junk_tier(player: PlayerState) -> int:
    """The junk chance reduction tier researched: the export's discovered blueprints when it has them (research
    can be done on a potion boost, above the plain level), else every tier the Invention level allows."""
    from .invention import discovered

    found = discovered(player)
    if found is not None and any(k.startswith("Junk chance reduction") for k in found):
        return max((n for n in range(1, len(JUNK_RESEARCH_LEVELS) + 1) if found.get(f"Junk chance reduction {n}")),
                   default=0)
    return sum(1 for lvl in JUNK_RESEARCH_LEVELS if player.level("Invention") >= lvl)


@dataclass
class Yield:
    mean: float  # of the material per item disassembled
    var: float  # its variance per item


def per_item(item: str, material: str, tier: int) -> Yield | None:
    d = data().get(item)
    if d is None:
        return None
    if material == JUNK:
        if not d["junk"]:
            return None
        p = d["junk"][min(tier, len(d["junk"]) - 1)] / 100
        return Yield(d["draws"] * p, d["draws"] * p * (1 - p)) if p > 0 else None
    if material in d["special"]:
        s = d["special"][material]
        c, q = min(1.0, s["chance"] / 100), s["quantity"]
        return Yield(c * q / d["batch"], c * (1 - c) * q * q / d["batch"])
    w = d["materials"].get(material)
    if not w or not d["total"]:
        return None
    junk = d["junk"][min(tier, len(d["junk"]) - 1)] / 100 if d["junk"] else 0.0
    p = (1 - junk) * w / d["total"]
    return Yield(d["draws"] * p, d["draws"] * p * (1 - p))


def all_yields(item: str, tier: int) -> dict[str, Yield]:
    """Every material one item gives (mean and variance)."""
    d = data().get(item) or {}
    return {m: y for m in [*d.get("materials", {}), *d.get("special", {})] if (y := per_item(item, m, tier))}


def items_needed(n: float, y: Yield) -> int:
    """Items to disassemble so the total reaches `n` with CONFIDENCE: N with N*mean - Z*sqrt(N*var) >= n."""
    if n <= 0:
        return 0
    root = (Z * math.sqrt(y.var) + math.sqrt(Z * Z * y.var + 4 * y.mean * n)) / (2 * y.mean)
    return math.ceil(root * root)


def sure_amount(items: int, y: Yield) -> float:
    """What `items` disassemblies give with CONFIDENCE (the low quantile), for by-products counted in advance."""
    return max(0.0, items * y.mean - Z * math.sqrt(items * y.var))


def seconds_per_item(item: str) -> float:
    return BATCH_SECONDS / (data().get(item) or {}).get("batch", 1.0)
