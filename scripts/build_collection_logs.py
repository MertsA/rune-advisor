"""Build data/collection_logs.json: each boss collection log's items and per-kill drop chances, from the
RuneScape Wiki's boss collection log calculator (Module:Collection log calculator/Boss/Data, evaluated by
scripts/wiki_lua_to_json.lua), plus the Boss Logs achievement that completing it unlocks.

Usage: python scripts/build_collection_logs.py <scratch dir>

Per collection: items {name: chance per kill, or null where the calculator uses a custom handler (enrage,
group size, hard mode): the planner then falls back to the drop tables}, pets (items with a drop
threshold or droptype "pet"), and the achievement whose wiki description names the collection
("Obtained a collection of unique drops from Araxxi."). Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from build_skilling_sim import page_source  # noqa: E402
from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src/rune_advisor/data/collection_logs.json"
MODULE = "Module:Collection log calculator/Boss/Data"
# Collections whose achievement description words the boss differently.
ACHIEVEMENTS = {"Barrows: Rise of the Six": "Family Affair", "Kril Tsutsaroth": "Whale Food",
                "Rex Matriarchs: Osseous": "Ossy Ossy Ossy!"}


def main(scratch: Path) -> None:
    client = CachedClient()
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / (MODULE.replace(":", "_").replace("/", "_").replace(" ", "_") + ".txt")).write_text(
        page_source(client, MODULE))
    raw = json.loads(subprocess.run(["lua5.4", str(ROOT / "scripts/wiki_lua_to_json.lua"), str(scratch), MODULE],
                                    capture_output=True, text=True, check=True).stdout)
    data = raw[MODULE]["collectionData"]
    descriptions = {}
    for r in wiki.fetch_all_achievements(client):
        if r.get("subcategory") == "Boss Logs":
            descriptions[r["name"]] = json.loads(r.get("json") or "{}").get("description") or ""
    out = {}
    for name, c in sorted(data.items()):
        drops = c.get("drops") or {}
        items, pets = {}, []
        for item, d in drops.items():
            if isinstance(d, str):  # a bare name in the Lua list ('Item 2'): its key is an index
                item, d = d, {}
            items[item] = d.get("droprate")
            if d.get("threshold") or d.get("droptype") == "pet":
                pets.append(item)
        # The achievement naming this collection most specifically ("Barrows Brothers" vs "...: Rise of the Six").
        achievement = ACHIEVEMENTS.get(name)
        if achievement is None:
            named = [a for a, desc in descriptions.items() if f"from {name}." in desc or f"from the {name}." in desc] \
                or [a for a, desc in descriptions.items() if name in desc]
            achievement = min(named, key=lambda a: len(descriptions[a])) if named else None
        out[name] = {"items": items, "pets": sorted(pets), "achievement": achievement}
    OUT.write_text(json.dumps({"source": f"RuneScape Wiki {MODULE}, Boss Logs achievements; CC BY-NC-SA 3.0",
                               "collections": out}, indent=1) + "\n")
    print(f"{len(out)} collections ({sum(1 for c in out.values() if c['achievement'])} with an achievement) -> {OUT}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
