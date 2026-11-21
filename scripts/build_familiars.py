"""Build data/familiars.json: every Summoning familiar's pouch, level and duration, from the RuneScape Wiki's
Bucket:Infobox_familiar.

Usage: python scripts/build_familiars.py

The planner costs a familiar kept up while skilling (consumables.py) as pouches per hour: 60 / duration.
Each row: the familiar's name, the item summoned from (a pouch, or a binding contract), the Summoning level,
the Summoning points a summon costs and the duration in minutes. Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources.wiki import bucket_all  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/familiars.json"


def main() -> None:
    rows = bucket_all(CachedClient(), "infobox_familiar",
                      ["page_name", "name", "level", "summoning_cost", "duration", "pouch"])
    out = {}
    for r in rows:
        name, pouch, minutes = r.get("name"), r.get("pouch"), r.get("duration")
        if not name or not pouch or not minutes:
            continue
        # A page can carry several versions (Pack yak variants): the first row per name wins.
        out.setdefault(name, {"pouch": pouch, "level": int(r.get("level") or 1),
                              "points": int(r.get("summoning_cost") or 0), "minutes": int(minutes)})
    OUT.write_text(json.dumps({"source": "RuneScape Wiki, Bucket:Infobox_familiar (CC BY-NC-SA 3.0)",
                               "familiars": dict(sorted(out.items()))}, indent=1) + "\n")
    print(f"{len(out)} familiars -> {OUT}")


if __name__ == "__main__":
    main()
