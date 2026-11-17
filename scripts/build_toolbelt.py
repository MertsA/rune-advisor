"""Build data/toolbelt.json: the tool belt's items and how each is unlocked, from the RuneScape Wiki's "Tool belt"
page.

Usage: python scripts/build_toolbelt.py

Per item: the wiki's note as plain text, and what the planner can cost from it: reward-shop purchases
("for 2,500 [[pilfer points]]"), level requirements ("requiring level 41 [[Dungeoneering]]") and quests
("upon completion of the [[One Small Favour]] quest", "Obtained during [[Dwarf Cannon]]"). Which items a
player has comes from their TOOLBELT_* vars at plan time (recommend/tracks/toolbelt.py). Data: RuneScape Wiki,
CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/toolbelt.json"
API = "https://runescape.wiki/api.php"
PURCHASE = re.compile(r"for ([\d,]+) \[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
LEVEL = re.compile(r"level (\d+) \[\[([^\]|]+)(?:\|[^\]]+)?\]\]", re.I)
QUEST = re.compile(r"(?:completion of|during|Reward from) (?:the )?\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


def plain(text: str) -> str:
    text = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", "", text, flags=re.S)
    text = re.sub(r"\{\{(?:yes|no|\(m\))\}\}", "", text, flags=re.I)
    text = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\{\{[^}]*\}\}|'''?|^\s*\*\s*", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


def main() -> None:
    text = CachedClient().get_json(API, {"action": "parse", "page": "Tool belt", "prop": "wikitext",
                                         "format": "json"})["parse"]["wikitext"]["*"]
    out = {}
    for row in text.split("|- data-rowid=")[1:]:
        name = row.split("\n")[0].strip().strip('"').strip("'")
        cells = [c.strip() for c in row.split("\n|")[1:]]
        note = "\n".join(cells[1:-1]) if len(cells) > 2 else ""
        purchases = [{"cost": int(n.replace(",", "")), "currency": (label or cur).strip()}
                     for n, cur, label in PURCHASE.findall(note)]
        out[name] = {"note": plain(note), "purchases": purchases,
                     "levels": {s: int(lvl) for lvl, s in LEVEL.findall(note)},
                     "quests": sorted(set(QUEST.findall(note)))}
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"{len(out)} tool belt items ({sum(1 for v in out.values() if v['purchases'])} bought) -> {OUT}")


if __name__ == "__main__":
    main()
