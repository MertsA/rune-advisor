"""Build data/equipment.json: combat stats of every equippable item, for the gear picker.

Usage: python scripts/build_equipment.py

From the wiki's Bucket:Infobox_bonuses (https://runescape.wiki, CC BY-NC-SA 3.0):
  {name: [slot, class, tier, ability damage (magic weapons only list it there), accuracy, damage bonus, armour, life points,
          {skill: level requirement}, attack style, attack range]}
"damage bonus" is the style's bonus (strength for melee, ranged for ranged...), which the wiki
stores per style; we keep the item's own class's value.

`switchable` lists item families whose variants can be swapped for free (e.g. the Sunspear's
"Switch style" option), so owning one variant means owning all: names ending in (melee)/(ranged)/
(magic) whose wiki page offers "Switch style".
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/equipment.json"
_REQ = re.compile(r'data-skill="(\w+)" data-level="(\d+)"')
_STYLE_SUFFIX = re.compile(r"^(.*) \((melee|ranged|magic)\)$")
DAMAGE_BONUS = {"melee": "strength", "ranged": "ranged", "magic": "magic", "necromancy": "necromancy"}


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def page_source(client: CachedClient, title: str) -> str:
    data = client.get_json(wiki.API, params={"action": "query", "prop": "revisions", "rvprop": "content",
                                             "rvslots": "main", "titles": title, "format": "json",
                                             "formatversion": 2, "redirects": 1}, ttl=7 * 24 * 3600)
    pages = data.get("query", {}).get("pages", [])
    revs = pages[0].get("revisions") if pages else None
    return revs[0]["slots"]["main"]["content"] if revs else ""


def main() -> None:
    client = CachedClient()
    items = {}
    for r in wiki.bucket_all(client, "infobox_bonuses", ["page_name", "json"]):
        try:
            b = json.loads(r["json"])
        except (TypeError, json.JSONDecodeError):
            continue
        cls = b.get("class") or "none"
        bonus = max((_num(b.get(k)) for k in DAMAGE_BONUS.values()), default=0.0) if cls in ("hybrid", "all") \
            else _num(b.get(DAMAGE_BONUS.get(cls, "strength")))
        reqs = {s: int(lvl) for s, lvl in _REQ.findall(b.get("requirements") or "")}
        tier = b.get("tier")
        items.setdefault(r["page_name"], [b.get("slot"), cls, int(tier) if isinstance(tier, (int, float)) else 0,
                                          _num(b.get("ability_damage")) or _num(b.get("damage")),
                                          _num(b.get("accuracy")), bonus,
                                          _num(b.get("armour")), _num(b.get("lp")), reqs, b.get("style"),
                                          int(_num(b.get("attack_range")))])
    families: dict[str, set[str]] = {}
    for name in items:
        if m := _STYLE_SUFFIX.match(name):
            families.setdefault(m[1], set()).add(name)
    switchable = []
    for base, names in sorted(families.items()):
        if len(names) < 2:
            continue
        if "Switch style" in page_source(client, sorted(names)[0]):
            switchable.append(sorted(names))
    # In-game names drop the wiki's page qualifiers ("Death guard" is "Death guard (tier 90)"), so the
    # exporter's item ids are the reliable link: Bucket:Infobox_item id -> page.
    ids = {}
    for r in wiki.bucket_all(client, "infobox_item", ["page_name", "item_id"]):
        if r["page_name"] in items:
            for i in r.get("item_id") or []:
                ids[str(i)] = r["page_name"]
    OUT.write_text(json.dumps({"source": "https://runescape.wiki Bucket:Infobox_bonuses (CC BY-NC-SA 3.0)",
                               "items": items, "switchable": switchable, "ids": ids},
                              ensure_ascii=False, separators=(",", ":")))
    print(f"{len(items)} items ({len(ids)} ids), {len(switchable)} switchable families -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
