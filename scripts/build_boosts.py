"""Build data/boosts.json: visible temporary skill boosts from the wiki's "Temporary skill boost" page.

Usage: python scripts/build_boosts.py

Per boost: the skills it raises and its formula (flat levels plus a percentage of the base level, rounded down),
and whether it's static (overloads and their variants, wilder pies: reapplied instead of decaying 1 level a
minute). Random boosts ("up to", spicy stews, pulse cores), reductions and invisible boosts are left out; so are
rows whose level increase isn't a plain formula. God banners (a daily +2 to one non-combat skill for 30 minutes)
and the Extreme invention potion (a level per 150,000 Invention xp, up to +17: the elite-skill extreme boost
table; the row carries the cap, `xp_per_level` says how it scales; unlike other skilling potions it holds the full
boost for 6 minutes a dose and then resets, so it is `static` with `minutes`) are added by hand.

Source: RuneScape Wiki, Temporary skill boost (https://runescape.wiki, CC BY-NC-SA 3.0).
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
from rune_advisor.xp import SKILL_ALIASES  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/boosts.json"
_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
_TARGET = re.compile(r"\[\[([^|\]#]+)")
# "5", "2 + 1%", "12% + 2, rounded down", "15% + 3"
_FORMULA = re.compile(r"^\+?(?:(\d+)\s*\+\s*(\d+(?:\.\d+)?)%|(\d+(?:\.\d+)?)%\s*\+\s*(\d+)|(\d+))(?:,? rounded down)?$")
STATIC = ("overload", "wilder pie")
# God banner powerup (same page): +2 to one of these for 30 minutes, once a day.
BANNER_SKILLS = ["Fishing", "Mining", "Runecrafting", "Crafting", "Woodcutting", "Firemaking", "Fletching",
                 "Construction", "Hunter", "Thieving", "Cooking", "Herblore", "Smithing", "Farming", "Divination",
                 "Archaeology"]


def _skill(name: str) -> str:
    return SKILL_ALIASES.get(name.strip(), name.strip())


def formula(text: str) -> tuple[int, float] | None:
    m = _FORMULA.match(text.strip())
    if not m:
        return None
    if m[1]:
        return int(m[1]), float(m[2])
    if m[3]:
        return int(m[4]), float(m[3])
    return int(m[5]), 0.0


def rows(page: str) -> list[list[str]]:
    out = []
    for table in re.findall(r"\{\|.*?\n\|\}", page, flags=re.S):
        for row in table.split("\n|-")[1:]:
            cells = [c.strip() for c in re.split(r"\n\|\s?", "\n" + row.strip()) if c.strip()]
            if len(cells) >= 5:
                out.append(cells)
    return out


def main() -> None:
    page = page_source(CachedClient(), "Temporary skill boost")
    boosts = []
    for cells in rows(page):
        image, boost, skills, increase, visibility = cells[:5]
        f = formula(_LINK.sub(r"\1", increase))
        target = _TARGET.search(boost)
        if f is None or not target or visibility.lower() != "visible" or "File:" in boost:
            continue
        name = target[1].split("#")[0].strip()
        found = [_skill(s) for s in _LINK.findall(skills)]
        if not found:
            continue
        boosts.append({"name": name, "label": _LINK.sub(r"\1", boost).strip(), "skills": found,
                       "flat": f[0], "percent": f[1],
                       "static": any(k in name.lower() for k in STATIC)})
    boosts.append({"name": "Extreme invention", "label": "Extreme invention (+1 per 150,000 Invention xp, up to +17; held for 6 minutes a dose)",
                   "skills": ["Invention"], "flat": 17, "percent": 0.0, "static": True, "minutes": 6, "xp_per_level": 150000})
    boosts.append({"name": "God banner", "label": "God banner powerup (daily, 30 minutes)",
                   "skills": [_skill(s) for s in BANNER_SKILLS], "flat": 2, "percent": 0.0, "static": True,
                   "daily_minutes": 30})
    OUT.write_text(json.dumps({"source": "https://runescape.wiki/w/Temporary_skill_boost (CC BY-NC-SA 3.0)",
                               "boosts": boosts}, indent=1))
    print(f"wrote {len(boosts)} boosts to {OUT}")


if __name__ == "__main__":
    main()
