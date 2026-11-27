"""Build data/anachronia_base_camp.json: the Anachronia base camp's buildings (each tier's requirements, materials and
benefits) and the Dream of Iaia's skilling stations, from the RuneScape Wiki.

Usage: python scripts/build_base_camp.py

* Buildings: the "Anachronia base camp/<Building> tiers" templates (Town Hall, Storehouse, Sleeping Quarters, Spa,
  Hunter Lodge, Slayer Lodge, Player Lodge). Requirements are other buildings' tiers and skill levels; materials are
  base camp resources and, at some tier 3s, one item.
* One-off structures (the Anachronia base camp page's tables, hand-written here): bank chest, lodestone, remote totem
  access point, the four deposit boxes.
* Workers: 10 to start, +5/+15/+30 from the Sleeping Quarters' tiers, +5/+5 from Fort Forinthry's Command Centre at
  tiers 2 and 3; 60 resources an hour each. Storage cap: 5,000 before the Storehouse, then its tiers' figures.
* The Dream of Iaia (its page): a station is built at 75 in its skill (unboosted) with 50,000 of every resource and
  then holds 135,000 xp (plus 15,000 paid at once); stocking takes 2 of the station's resource per xp, up to 350,000
  stored; training pays 175 xp every 3 ticks (350,000 an hour), boosted by bonus xp at no cost to the store.
The export names the resources CLAY, LEAVES, PELTS (hides), STONE, VINES, WOOD (DINO_BASE_CAMP_RESOURCE_<NAME>).
Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = ROOT / "src/rune_advisor/data/anachronia_base_camp.json"
BUILDINGS = {"Town Hall": "TOWNHALL", "Storehouse": "STOREHOUSE", "Sleeping Quarters": "SLEEPING_QUARTERS",
             "Spa": "SPA", "Hunter Lodge": "HUNTER_LODGE", "Slayer Lodge": "SLAYER_LODGE", "Player Lodge": "PLAYER_LODGE"}
RESOURCES = {"Wood": "WOOD", "Vines": "VINES", "Leaves": "LEAVES", "Hides": "PELTS", "Stone": "STONE", "Clay": "CLAY"}
AMOUNT = re.compile(r"\{\{plink amount\|([^|}]+)\|(?:txt=([^|}]+)\|)?([\d,]+)\}\}")
BUILDING_REQ = re.compile(r"(Town Hall|Storehouse|Sleeping Quarters) \(tier (\d)\)")
SKILL_REQ = re.compile(r"\{\{Skillreq\|(\w+)\|(\d+)\}\}")
LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
ONE_OFF = {
    "Bank chest": {"var": "DINO_BASE_CAMP_BUILDING_TIER_BANK_CHEST", "needs": {"Town Hall": 1},
                   "resources": {"WOOD": 500, "STONE": 500}, "items": {},
                   "benefits": ["A bank chest at the Town Hall"]},
    "Lodestone": {"var": "DINO_BASE_CAMP_BUILDING_TIER_LODESTONE", "needs": {"Town Hall": 1},
                  "resources": {"STONE": 150, "CLAY": 150}, "items": {}, "benefits": ["The Anachronia lodestone"]},
    "Remote totem access point": {"var": "DINO_BASE_CAMP_BUILDING_TIER_TOTEM", "needs": {"Town Hall": 1},
                                  "resources": {"STONE": 150_000, "VINES": 150_000, "WOOD": 150_000},
                                  "items": {"Remototem": 1},
                                  "benefits": ["Change any totem on Anachronia remotely, from the base camp"]},
    **{f"Deposit box ({where})": {"var": f"DINO_BASE_CAMP_BUILDING_DEPOSIT_BOX_{i}", "needs": {"Town Hall": 1},
                                  "resources": {"WOOD": 30_000, "STONE": 30_000}, "items": {},
                                  "benefits": [f"A deposit box ({where.lower()})"]}
       for i, where in enumerate(("Western", "North-eastern", "South-eastern", "Central"), start=1)},
}
WORKERS = {"start": 10, "Sleeping Quarters": [5, 15, 30], "Command Centre": {"2": 5, "3": 5}, "per_hour": 60}
STOREHOUSE_CAP = [5_000, 25_000, 80_000, 150_000]  # by Storehouse tier 0-3
IAIA = {
    "stations": {"Construction": "STONE", "Crafting": "PELTS", "Fishing": "CLAY", "Fletching": "WOOD",
                 "Herblore": "LEAVES", "Hunter": "VINES"},
    "build_level": 75, "build_resources": 50_000, "build_stores": 135_000, "build_pays": 15_000,
    "resources_per_xp": 2, "max_stored": 350_000, "xp_per_action": 175, "ticks_per_action": 3,
    "quest": "Extinction",
}


def _page(client: CachedClient, title: str) -> str:
    r = client.get_json(wiki.API, params={"action": "parse", "page": title, "prop": "wikitext", "redirects": 1,
                                          "format": "json"})
    return r["parse"]["wikitext"]["*"] if "parse" in r else ""


def tiers(text: str) -> list[dict]:
    out = []
    for row in text.split("|-")[1:]:
        cells = [c.strip() for c in re.split(r"^\|(?!-)", row, flags=re.M)[1:]]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        req, mats, benefits = cells[1], cells[2], cells[3]
        resources, items = {}, {}
        for name, txt, n in AMOUNT.findall(mats):
            label = (txt or name).strip()
            if label in RESOURCES:
                resources[RESOURCES[label]] = int(n.replace(",", ""))
            else:
                items[name.strip()] = int(n.replace(",", ""))
        out.append({
            "tier": int(cells[0]),
            "needs": {b: int(t) for b, t in BUILDING_REQ.findall(req)},
            "levels": {s: int(lv) for s, lv in SKILL_REQ.findall(req)},
            "resources": resources, "items": items,
            "benefits": [re.sub(r"\s+", " ", LINK.sub(r"\1", line.lstrip("* "))).strip()
                         for line in benefits.splitlines() if line.strip().lstrip("*").strip()],
        })
    return out


def main() -> None:
    client = CachedClient()
    buildings = {}
    for name, key in BUILDINGS.items():
        rows = tiers(_page(client, f"Template:Anachronia base camp/{name} tiers"))
        if len(rows) != 3:
            raise SystemExit(f"{name}: expected 3 tiers, read {len(rows)}")
        buildings[name] = {"var": f"DINO_BASE_CAMP_BUILDING_TIER_{key}", "tiers": rows}
    out = {"source": "RuneScape Wiki: Anachronia base camp, its '<Building> tiers' templates, and Dream of Iaia "
                     "(CC BY-NC-SA 3.0)",
           "buildings": buildings, "one_off": ONE_OFF, "workers": WORKERS, "storehouse_cap": STOREHOUSE_CAP,
           "dream_of_iaia": IAIA}
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(buildings)} buildings -> {OUT}")


if __name__ == "__main__":
    main()
