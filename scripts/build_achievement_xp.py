"""Build data/achievement_xp.json: antique lamp rewards of the area task sets, from the RuneScape Wiki's
"Antique lamp" list.

Usage: python scripts/build_achievement_xp.py

Keys are the game's achievement names ("Ardougne Set Tasks - Elite"); values are lamp choices in the same
shape as data/quest_xp.json: {"choice": [{"xp": n, "skills": "any", "min_level": n}, ...]}, one entry per lamp.
Quest lamps are left to data/quest_xp.json. Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/achievement_xp.json"
API = "https://runescape.wiki/api.php"
# "[[Ardougne achievements]] (elite)" in the Obtained column.
TASK_SET = re.compile(r"\[\[(?P<area>[^\]|]+?) achievements\]\]\s*\((?P<tier>easy|medium|hard|elite)\)", re.I)


def number(cell: str) -> int | None:
    m = re.search(r"\d[\d,]*", cell)
    return int(m.group().replace(",", "")) if m else None


def main() -> None:
    data = CachedClient().get_json(API, {"action": "parse", "page": "Antique_lamp", "prop": "wikitext",
                                         "format": "json"})
    text = data["parse"]["wikitext"]["*"]
    table = text[text.index("==List of antique lamps=="):]
    table = table[:table.index("|}")]
    out: dict[str, dict] = {}
    for row in table.split("|-")[1:]:
        # Columns: image || Obtained || Article || XP || Level || Amount || Notes
        cells = [c.strip() for c in row.strip().lstrip("|").split("||")]
        if len(cells) < 6 or not (m := TASK_SET.search(cells[1])):
            continue
        xp, level, amount = number(cells[3]), number(cells[4]) or 0, number(cells[5]) or 1
        if not xp:
            continue
        name = f"{m['area'].strip()} Set Tasks - {m['tier'].capitalize()}"
        lamps = out.setdefault(name, {"set": {}, "choice": []})["choice"]
        lamps += [{"xp": xp, "skills": "any", "min_level": level} for _ in range(amount)]
    OUT.write_text(json.dumps(dict(sorted(out.items())), indent=1) + "\n")
    print(f"{len(out)} task sets -> {OUT}")


if __name__ == "__main__":
    main()
