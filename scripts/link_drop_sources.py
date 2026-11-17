"""Store each training method's drop sources on its wiki rate row (`drop_sources`).

Usage: python scripts/link_drop_sources.py <mapping.json>

mapping.json comes from the method -> drop source pass (a Haiku subagent picked, per method, the
drop-table pages from data/drop_tables.json whose loot the method gives, and a
person reviewed the result): [{"file": "wiki_rates.json", "i": 12, "sources": [...]}, ...].
Names not in the drop tables are rejected, so a typo can't link a method to nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "src/rune_advisor/data"


def main(mapping_file: Path) -> None:
    known = set(json.loads((DATA / "drop_tables.json").read_text())["sources"])
    mapping = json.loads(mapping_file.read_text())
    files = {m["file"] for m in mapping}
    rows = {f: json.loads((DATA / f).read_text()) for f in files}
    linked = rejected = 0
    for m in mapping:
        row = rows[m["file"]][m["i"]]
        row.pop("drop_sources", None)
        good = [s for s in m.get("sources") or [] if s in known]
        for bad in set(m.get("sources") or []) - set(good):
            rejected += 1
            print(f"unknown source {bad!r} for {row['method']!r}", file=sys.stderr)
        if good:
            row["drop_sources"] = good
            linked += 1
    for f, data in rows.items():
        (DATA / f).write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print(f"linked {linked} methods ({rejected} unknown names dropped)", file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
