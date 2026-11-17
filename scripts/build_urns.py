"""Build data/urns.json: every urn tier from the RuneScape Wiki's "Urns" page.

Usage: python scripts/build_urns.py

Per tier: skill, Crafting level, the highest resource level it accepts (None = any), the base xp that
fills it and the xp it pays out (20% of that). The rune or essence that activates each skill's urns
comes from the page's creation table. Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from build_skilling_sim import page_source  # noqa: E402
from rune_advisor.http import CachedClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/urns.json"
SKILLS = ["Cooking", "Divination", "Farming", "Fishing", "Hunter", "Mining", "Prayer", "Runecrafting",
          "Smithing", "Woodcutting"]


def number(cell: str) -> float | None:
    m = re.search(r"[\d,]+(?:\.\d+)?", cell)
    return float(m.group().replace(",", "")) if m else None


def main() -> None:
    page = page_source(CachedClient(), "Urns")
    runes = {}
    creation = page[page.index("==Creation=="):page.index("==Usage==")]
    for skill in SKILLS:
        m = re.search(r"\[\[" + skill + r"\]\]\s*\n\|\s*\{\{plink\|([^}|]+)", creation)
        runes[skill] = m.group(1) if m else None
    types = page[page.index("==Types of urns=="):page.index("==Items with similar functions==")]
    urns = []
    for skill in SKILLS:
        start = types.find(f"==={skill}===")
        end = min([i for i in (types.find("===", start + len(skill) + 6),) if i > 0] or [len(types)])
        for row in types[start:end].split("|-")[1:]:
            name = re.search(r"\{\{Plinkt\|([^|}]+)", row)
            cells = [c.strip() for c in re.split(r"\n\|\s*", row)]
            if not name:
                continue
            # Columns after the 5 icon cells (unfired/no gem/no rune/empty/full): members, Crafting level,
            # accepts, xp to fill, teleport xp.
            members_i = next((i for i, c in enumerate(cells) if c.startswith("{{Members")), None)
            if members_i is None or members_i + 4 >= len(cells):
                continue
            accepts = cells[members_i + 2]
            lvl = re.search(r"level (\d+)", accepts)
            urns.append({"name": name.group(1), "skill": skill, "members": "Yes" in cells[members_i],
                         "crafting": int(number(cells[members_i + 1]) or 1),
                         "accepts_level": int(lvl.group(1)) if lvl else None,
                         "fill_xp": number(cells[members_i + 3]), "payout_xp": number(cells[members_i + 4]),
                         "exquisite": name.group(1).lower().startswith("exquisite"),
                         "rune": runes[skill]})
    OUT.write_text(json.dumps({"source": "RuneScape Wiki, Urns (CC BY-NC-SA 3.0)", "urns": urns}, indent=1))
    print(f"{len(urns)} urns -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
