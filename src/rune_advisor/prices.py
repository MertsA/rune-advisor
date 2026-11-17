"""Grand Exchange prices, for sanity-checking rates as gp/h (not used by the planner yet).

Prices and daily volumes come from the wiki's GE modules (Module:GEPrices/data.json,
Module:GEVolumes/data.json; refreshed by the wiki a few times a day). An untradeable item is valued by
its best processing recipe in the catalogue whose only material it is (an essence chunk is 9 exalted
essence at the essence processor, or 25 pure essence), a few steps deep, without counting the time
the processing takes or the levels it needs (red sandstone counts as potion flasks, which need 89
Crafting). A method's gp/h is the value of its yields per hour.

The point is a smell test: a new level-105 gathering node that comes out at 100k gp/h, when lower
nodes make millions, most likely means the simulation or the rate is wrong. Prices of new or thin items
swing, so volumes are shown beside them.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from .http import CachedClient
from .recommend.catalogue import catalogue

PRICES_URL = "https://runescape.wiki/?title=Module:GEPrices/data.json&action=raw"
VOLUMES_URL = "https://runescape.wiki/?title=Module:GEVolumes/data.json&action=raw"
PRICE_TTL = 6 * 3600
MAX_DEPTH = 3
THIN_VOLUME = 1_000  # traded per day: the price is a guess


@dataclass
class Value:
    gp: float
    how: str  # "GE", or the processing chain ("9 Exalted essence (GE)")
    volume: int | None = None  # daily GE volume of the item priced


class Prices:
    def __init__(self, client: CachedClient | None = None, prices: dict[str, float] | None = None,
                 volumes: dict[str, int] | None = None):
        if prices is None:
            client = client or CachedClient()
            prices = client.get_json(PRICES_URL, ttl=PRICE_TTL)
            volumes = client.get_json(VOLUMES_URL, ttl=PRICE_TTL)
        self.prices = {k: v for k, v in prices.items() if not k.startswith("%")}
        self.volumes = {k: v for k, v in (volumes or {}).items() if not k.startswith("%")}
        self.updated = prices.get("%LAST_UPDATE_F%", "")
        self._cache: dict[str, Value | None] = {}

    def value(self, item: str, depth: int = 0) -> Value | None:
        """GE price, else the best single-material processing chain to something with one."""
        if item in self._cache:
            return self._cache[item]
        self._cache[item] = None  # guards recipe cycles
        best = None
        if item in self.prices:
            best = Value(float(self.prices[item]), "GE", self.volumes.get(item))
        elif depth < MAX_DEPTH:
            for r in _processing(item):
                out = self.value(r["output"], depth + 1)
                if out is None:
                    continue
                gp = out.gp * r["output_qty"] / r["materials"][0][1]
                if best is None or gp > best.gp:
                    per = r["output_qty"] / r["materials"][0][1]
                    best = Value(gp, f"{per:g} {r['output']} ({out.how})", out.volume)
        self._cache[item] = best
        return best

    def gp_per_hour(self, yields: dict[str, float]) -> tuple[float, list[str]]:
        """(gp/h, per-item notes) for a method's yields per hour; unpriced items count as 0."""
        total, notes = 0.0, []
        for item, per_hour in sorted(yields.items(), key=lambda kv: -kv[1]):
            v = self.value(item)
            if v is None:
                notes.append(f"{item}: no price")
                continue
            total += v.gp * per_hour
            thin = " [thin]" if v.volume is not None and v.volume < THIN_VOLUME else ""
            notes.append(f"{per_hour:,.0f} {item} x {v.gp:,.0f} gp" + ("" if v.how == "GE" else f" as {v.how}") + thin)
        return total, notes


def _processing(item: str) -> list[dict]:
    return _by_material().get(item, [])


@cache
def _by_material() -> dict[str, list[dict]]:
    """Catalogue recipes with a single material, by that material."""
    out: dict[str, list[dict]] = {}
    for r in catalogue():
        if len(r["materials"]) == 1 and r["materials"][0][1] > 0:
            out.setdefault(r["materials"][0][0], []).append(r)
    return out
