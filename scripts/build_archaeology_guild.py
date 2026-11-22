"""Build data/archaeology_guild.json: the Archaeology Guild shop's one-time unlocks and the research team's general
research (xp an hour by level, chronote cost, what each dig site's research returns), from the RuneScape Wiki.

Usage: python scripts/build_archaeology_guild.py

* Unlocks: the Archaeology Guild Shop page's stock lines sold once ("unique"): soil box, material storage,
  workbench storage and mattock precision upgrades by qualification, the archaeologist's and master archaeologist's
  outfits, the auto-screener blueprint, the energised meteorite shard, the extra relic loadout.
* Research: the Research page's experience table (the Kharid-et column; the other dig sites earn their research
  page's `factor` percent of it) and its cost rule (5 x M chronotes an hour at Kharid-et, times the same factor,
  M = the level rounded down to even). Each "<Dig site> (research)" page gives the level, the factor and the
  materials and artefacts research there returns (amounts aren't published: currencies/archaeology use flagged
  estimates).
Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/archaeology_guild.json"
STORELINE = re.compile(r"\{\{StoreLine\|(.*?)\}\}\s*$", re.M)
SECTION = re.compile(r"^===\s*(\w+)\s*===", re.M)
XP_CELL = re.compile(r"'''(\d+)'''\s*\|\|\s*([\d,.]+)")
DROP = re.compile(r"DropsLineArch\|name=([^|}]+)")
SITES = ["Kharid-et", "Infernal Source", "Everlight", "Stormguard Citadel", "Warforge", "Orthen", "Senntisten",
         "Daemonheim", "Moonrise"]


def _page(client: CachedClient, title: str) -> str:
    r = client.get_json(wiki.API, params={"action": "parse", "page": title, "prop": "wikitext", "redirects": 1,
                                          "format": "json"})
    return r["parse"]["wikitext"]["*"] if "parse" in r else ""


def _fields(line: str) -> dict[str, str]:
    out = {}
    for part in line.split("|"):
        key, sep, value = part.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def unlocks(text: str) -> list[dict]:
    out = []
    marks = [(m.start(), m.group(1)) for m in SECTION.finditer(text)]
    for m in STORELINE.finditer(text):
        f = _fields(m.group(1))
        price = f.get("sell", "").replace(",", "")
        if f.get("stock", "").lower() != "unique" or not price.isdigit() or int(price) == 0:
            continue
        qualification = next((q for pos, q in reversed(marks) if pos < m.start()), "")
        out.append({"name": f.get("displayname") or f.get("name") or f.get("text", ""),
                    "item": f.get("name") or f.get("text", ""), "chronotes": int(price),
                    "qualification": qualification})
    return out


def main() -> None:
    client = CachedClient()
    shop = unlocks(_page(client, "Archaeology Guild Shop"))
    research_text = _page(client, "Research")
    xp = {int(lvl): float(v.replace(",", "")) for lvl, v in XP_CELL.findall(research_text)}
    sites = {}
    for site in SITES:
        text = _page(client, f"{site} Dig Site (research)")
        if not text:
            continue
        level = re.search(r"\|level\s*=\s*(\d+)", text)
        factor = re.search(r"\|factor\s*=\s*(\d+)", text)
        materials = re.search(r"===\s*Materials\s*===(.*?)(===|==)", text, re.S)
        artefacts = re.search(r"===\s*Artefacts\s*===(.*?)(==)", text, re.S)
        sites[f"{site} Dig Site"] = {
            "level": int(level.group(1)) if level else 1, "factor": int(factor.group(1)) if factor else 100,
            "materials": [n.strip() for n in DROP.findall(materials.group(1))] if materials else [],
            "artefacts": [n.strip() for n in DROP.findall(artefacts.group(1))] if artefacts else []}
    OUT.write_text(json.dumps({"source": "RuneScape Wiki: Archaeology Guild Shop, Research and the dig sites' research "
                                         "pages (CC BY-NC-SA 3.0)",
                               "unlocks": shop, "research_xp_per_hour": dict(sorted(xp.items())),
                               "research_cost_per_hour": "5 x factor/100 x (level rounded down to even)",
                               "research_sites": sites}, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(shop)} unlocks, {len(xp)} research levels, {len(sites)} research sites -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
