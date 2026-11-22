"""Build data/archaeology_caches.json: every Archaeology material cache (its level, material and dig site) and each
dig site's soil, from the RuneScape Wiki.

Usage: python scripts/build_archaeology_caches.py

Bucket:Infobox_excavation_hotspot holds hotspots and material caches alike; a cache's page names its dig site, or
none for caches out in the world. Caches inside a dig site also give that site's soil, the rest give materials only
(Soil page: "Material caches will only yield soil if they are located within a dig site"). A dig site's soil is the
soil its hotspots give (data/mechanics.json, the hotspot calculator's data). Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "src/rune_advisor/data"
OUT = DATA / "archaeology_caches.json"


SITE = re.compile(r"\|\s*site\s*=\s*([^|\n}]+)")


def _dig_site(site: str, soils: dict[str, str]) -> str | None:
    """The dig site a cache location is in ("Kharid-et Dig Site", "Warforge Dig Site"), or None out in the world."""
    site = re.sub(r"\[\[|\]\]", "", site).split("|")[-1].strip()
    return next((d for d in soils if site == d or site.startswith(d.removesuffix(" Dig Site"))), None)


def main() -> None:
    rows = wiki.bucket_all(CachedClient(), "infobox_excavation_hotspot", ["page_name", "level", "dig_site", "materials"])
    hotspots = json.loads((DATA / "mechanics.json").read_text())["archaeology"]["hotspots"]
    soils: dict[str, str] = {}
    for r in rows:
        h = hotspots.get(r["page_name"])
        for site in r.get("dig_site") or []:
            if h and h.get("soil") and site != "None":
                soils.setdefault(site, h["soil"])
    caches = []
    client = CachedClient()
    for r in rows:
        if not r["page_name"].startswith("Material cache") or not r.get("materials"):
            continue
        # One page per material, a location line per cache: inside a dig site (soil too) or out in the world.
        page = client.get_json(wiki.API, params={"action": "parse", "page": r["page_name"], "prop": "wikitext",
                                                 "redirects": 1, "format": "json"})["parse"]["wikitext"]["*"]
        sites = {m.strip() for m in SITE.findall(page)} or set(r.get("dig_site") or [])
        in_site = sorted({_dig_site(s, soils) for s in sites} - {None})
        outside = any(_dig_site(s, soils) is None for s in sites) or not sites
        base = {"level": int(r.get("level") or 1), "material": r["materials"][0]}
        for site in in_site:
            caches.append({"cache": f"{r['page_name']} at {site}", **base, "dig_site": site, "soil": soils[site]})
        if outside:
            caches.append({"cache": r["page_name"], **base, "dig_site": None, "soil": None})
    OUT.write_text(json.dumps({"source": "RuneScape Wiki, Bucket:Infobox_excavation_hotspot (CC BY-NC-SA 3.0)",
                               "dig_site_soils": dict(sorted(soils.items())),
                               "caches": sorted(caches, key=lambda c: (c["level"], c["cache"]))},
                              indent=1, ensure_ascii=False) + "\n")
    print(f"{len(caches)} caches, {len(soils)} dig sites with soil -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
