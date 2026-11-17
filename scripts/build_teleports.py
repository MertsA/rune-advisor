"""Build data/teleports.json: every teleport destination the wiki has coordinates for.

Usage: uv run python scripts/build_teleports.py

Source: the RuneScape Wiki's Bucket:Coordinates rows of type "teleport destination" (written by the
{{Teleport map}} template on each teleport method's page: spells, jewellery, lodestones, capes, outfits,
locators...). Coordinates are game tiles (x, y, plane) on the wiki's map IDs (28 = the surface). CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/teleports.json"
_FILE = re.compile(r"\[\[(?:File|Image):[^\]]*\]\]")
_LINK = re.compile(r"\[\[([^\]]*)\]\]")
_MARKUP = re.compile(r"'''|<br\s*/?>|&#58;")


def clean(title: str) -> str:
    """"[[Varrock Square]]" -> "Varrock Square"; "[[Troll Invasion|Burthorpe Troll Invasion]]" -> the label;
    members' icons ([[File:Members.png|...]]) dropped."""
    text = _LINK.sub(lambda m: m[1].split("|")[-1], _FILE.sub("", title or ""))
    return " ".join(_MARKUP.sub(" ", text).split())


def kind(page: str) -> str:
    if page.endswith("Lodestone (spell)"):
        return "lodestone"
    if page.endswith(" Teleport") or page.endswith("(spell)") or page.startswith("Teleport "):
        return "spell"
    return "item"  # jewellery, tablets, capes, outfits, locators and the rest


def main() -> None:
    client = CachedClient()
    rows, offset = [], 0
    while True:
        chunk = wiki.bucket(client, "coordinates", ["page_name", "page_name_sub", "type", "title", "x", "y", "plane", "mapid"],
                            {"type": "teleport destination"}, limit=500, offset=offset)
        rows += chunk
        if len(chunk) < 500:
            break
        offset += 500
    out = []
    for r in rows:
        xs, ys = r.get("x") or [], r.get("y") or []
        if not xs or not ys:
            continue
        out.append({"page": r["page_name"], "kind": kind(r["page_name"]), "destination": clean(r.get("title") or ""),
                    "x": int(xs[0]), "y": int(ys[0]), "plane": int(r.get("plane") or 0), "map": int(r.get("mapid") or 0)})
    out.sort(key=lambda t: (t["kind"], t["page"], t["destination"]))
    OUT.write_text(json.dumps({"source": "https://runescape.wiki Bucket:Coordinates (type = teleport destination), CC BY-NC-SA 3.0",
                               "rows": out}, indent=1, ensure_ascii=False))
    print(f"{len(out)} teleport destinations ({sum(t['kind'] == 'lodestone' for t in out)} lodestones) -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
