"""Build data/alch_values.json: {item: High Level Alchemy value} from the RuneScape Wiki's Bucket:Infobox_item.

Usage: python scripts/build_alch_values.py

Used for ironman coins: drops from combat training are alched to pay for shop purchases
(recommend/base.py, Context.alch_income). Items with versions keep the highest value. Data: RuneScape Wiki,
CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/alch_values.json"


def main() -> None:
    out: dict[str, int] = {}
    for r in wiki.bucket_all(CachedClient(), "infobox_item", ["page_name", "item_name", "high_alchemy_value"]):
        v = r.get("high_alchemy_value")
        if not isinstance(v, int) or v <= 0:
            continue
        for name in {r.get("item_name"), r.get("page_name")} - {None, ""}:
            out[name] = max(out.get(name, 0), v)
    OUT.write_text(json.dumps({"source": "RuneScape Wiki Bucket:Infobox_item; CC BY-NC-SA 3.0",
                               "values": dict(sorted(out.items()))}) + "\n")
    print(f"{len(out)} alchable items -> {OUT}")


if __name__ == "__main__":
    main()
