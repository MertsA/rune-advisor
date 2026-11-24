"""Build data/wiki_tables/: the wiki tables the planner used to download at every run.

Usage: python scripts/build_wiki_tables.py [--quest-list-from NAME]

  quests.json                    Bucket:Quest rows (requirements, length, items required)
  achievements.json              Bucket:Achievement rows (categories, descriptions, members flag)
  achievement_requirements.json  Bucket:Achievement_requirement rows (regular and ironman trees)
  quest_titles.json              the quests that exist today, from NAME's Adventurer's Log (the list is the same
                                 for every player; only the titles are kept). Kept from the last run without
                                 --quest-list-from.
  fetched.json                   {"fetched": day, "quest_titles": day}

The planner reads these from the data package (`sources/wiki.py`), so the web planner needs no wiki access and a
data package release refreshes them. Rows are stored as the wiki returns them. Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import adventurers_log, wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/wiki_tables"


def write(name: str, value) -> None:
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quest-list-from", metavar="NAME", help="a player whose Adventurer's Log lists today's quests")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    client = CachedClient()
    today = datetime.date.today().isoformat()
    tables = {name: sorted(wiki.bucket_all(client, table, fields), key=lambda r: r["page_name"])
              for name, (table, fields) in wiki.TABLES.items()}
    for name, rows in tables.items():
        write(f"{name}.json", rows)
        print(f"{name}: {len(rows)} rows")
    fetched = {"fetched": today}
    try:
        fetched["quest_titles"] = json.loads((OUT / "fetched.json").read_text()).get("quest_titles")
    except (OSError, ValueError):
        pass
    if args.quest_list_from:
        titles = sorted(adventurers_log.parse_quests(adventurers_log.fetch_quests(client, args.quest_list_from)))
        write("quest_titles.json", titles)
        fetched["quest_titles"] = today
        print(f"quest_titles: {len(titles)}")
    write("fetched.json", fetched)
    return 0


if __name__ == "__main__":
    sys.exit(main())
