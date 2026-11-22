"""Build data/drop_source_kinds.json: what each drop table source is (a monster, an item to open, scenery, an NPC, a
quest, a Hunter creature), from which RuneScape Wiki infobox or category its page carries.

Usage: python scripts/build_drop_source_kinds.py

The drop tables (Bucket:Dropsline) name a page per source but not its type. The gatherer gives a source with no
kill or catch rate a flagged estimate by kind (drops.assumed_rate): scenery is gathered, an item is opened, an NPC
hands out once a day, a quest or achievement rewards once. A page with none of these infoboxes is "other" (activities, D&Ds).
Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_drop_tables import category_pages  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "src/rune_advisor/data"
OUT = DATA / "drop_source_kinds.json"
# The first infobox a page carries decides (a monster page with an NPC infobox too is a monster).
KINDS = [("monster", "infobox_monster"), ("item", "infobox_item"), ("quest", "infobox_quest"),
         ("achievement", "achievement"),
         ("scenery", "infobox_scenery"), ("npc", "infobox_npc")]


# Random spawns while skilling that the Random events category doesn't hold.
RANDOM_SPAWNS = {"Divine blessing"}


def main() -> None:
    client = CachedClient()
    sources = json.loads((DATA / "drop_tables.json").read_text())["sources"]
    pages: dict[str, set[str]] = {}
    for kind, bucket in KINDS:
        pages[kind] = {r["page_name"] for r in wiki.bucket_all(client, bucket, ["page_name"])}
    # Hunter creatures carry an NPC infobox like shopkeepers do: told apart by their category, caught not handed out.
    creatures = category_pages(client, "Category:Hunter creatures")
    # Random events (a divine blessing, a B.A.N.K. stander) spawn now and then while skilling; D&Ds are daily or weekly.
    randoms = category_pages(client, "Category:Random events")
    dnds = category_pages(client, "Category:Distractions and Diversions", depth=1)
    out = {}
    for src in sources:
        page = src.split("#")[0]
        kind = next((kind for kind, _ in KINDS if page in pages[kind]), "other")
        if page in creatures and kind in ("npc", "other", "monster"):
            kind = "creature"
        elif (page in randoms or page in RANDOM_SPAWNS) and kind not in ("item", "monster"):
            kind = "random"
        elif page in dnds and kind not in ("item", "monster"):
            kind = "other"  # once a day (drops.DAILY_KINDS)
        out[src] = kind
    OUT.write_text(json.dumps({"source": "RuneScape Wiki infobox buckets (CC BY-NC-SA 3.0)",
                               "kinds": dict(sorted(out.items()))}, indent=0, ensure_ascii=False) + "\n")
    counts: dict[str, int] = {}
    for k in out.values():
        counts[k] = counts.get(k, 0) + 1
    print(f"{len(out)} drop sources -> {OUT}: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
