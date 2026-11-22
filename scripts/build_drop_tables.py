"""Build data/drop_tables.json: what each monster/activity drops, and monsters' xp per kill.

Usage: python scripts/build_drop_tables.py

From the wiki's Bucket:Dropsline (one row per item a source drops) and Bucket:Infobox_monster
(https://runescape.wiki, CC BY-NC-SA 3.0). Rarities are parsed to a chance per kill/roll where the
wiki gives a number ("1/480", "45/1,000"); "Always" is 1, and worded rarities ("Rare", "Very rare")
keep their text with no number. The drops module (src/rune_advisor/drops.py) scores them against
what a player needs.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/drop_tables.json"
_FRACTION = re.compile(r"^\s*([\d.,]+)\s*/\s*([\d.,]+)\s*$")


def _num(x):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


def chance(rarity: str | None) -> float | None:
    if not rarity:
        return None
    if rarity.strip().lower() == "always":
        return 1.0
    if m := _FRACTION.match(rarity):
        a, b = _num(m[1]), _num(m[2])
        return a / b if a and b else None
    return None


def quantity(q: str | None) -> float:
    """Average of "35–45" / "35-45" / "10,20" style quantities (1 if missing)."""
    nums = [_num(x) for x in re.split(r"[–\-,;]|\s+", str(q or "1")) if _num(x) is not None]
    return sum(nums) / len(nums) if nums else 1.0


ONCE = re.compile(r"(?:\bonly|can only be \w+|only given|unlocked|be obtained) once\b(?!\s*(?:per|a |every))", re.I)


WEEKLY = re.compile(r"once (?:per|a) week", re.I)


def _notes(d: dict) -> str:
    return " ".join(n.get("content", "") if isinstance(n, dict) else str(n)
                    for n in (d.get("Rarity Notes") or []) + (d.get("Quantity Notes") or []))


def _thieving(d: dict) -> bool:
    t = d.get("Drop type") or []
    return "thieving" in (t if isinstance(t, list) else [t])


def once_only(d: dict) -> bool:
    """A drop line the wiki notes as given once ever ("Can only be looted once", a quest's chest): no source to farm.
    Notes like "once per week" (claims) or "dropped at once" (all together) aren't."""
    return bool(ONCE.search(_notes(d)))


def category_pages(client: CachedClient, category: str, depth: int = 2, seen: set[str] | None = None) -> set[str]:
    """Article titles in a wiki category and its subcategories (up to `depth` levels)."""
    seen = seen if seen is not None else set()
    pages, cont = set(), {}
    while True:
        data = client.get_json(wiki.API, params={"action": "query", "list": "categorymembers", "cmtitle": category,
                                                 "cmlimit": 500, "format": "json", **cont}, ttl=7 * 24 * 3600)
        for m in data["query"]["categorymembers"]:
            if m["ns"] == 14 and depth > 0 and m["title"] not in seen:
                seen.add(m["title"])
                pages |= category_pages(client, m["title"], depth - 1, seen)
            elif m["ns"] == 0:
                pages.add(m["title"])
        if "continue" not in data:
            return pages
        cont = {"cmcontinue": data["continue"]["cmcontinue"]}


def main() -> None:
    client = CachedClient()
    sources: dict[str, list[dict]] = {}
    lines = list(wiki.bucket_all(client, "dropsline", ["page_name", "item_name", "drop_json", "rare_drop_table"]))
    # A page with both kill and pickpocket loot (a guard) keeps its pickpocket lines apart, as "<page> (pickpocket)":
    # the Thieving sim's rows pick from those, kills from the rest.
    types: dict[str, set[bool]] = {}
    for r in lines:
        types.setdefault(r["page_name"], set()).add(_thieving(json.loads(r.get("drop_json") or "{}")))
    for r in lines:
        d = json.loads(r.get("drop_json") or "{}")
        page = r["page_name"]
        if len(types[page]) > 1 and _thieving(d):
            page = f"{page} (pickpocket)"
        rarity = d.get("Rarity")
        if (rarity or "").lower() in ("discontinued", "never") or once_only(d):
            continue
        # Compact rows: [item, chance per kill/roll (or the worded rarity), average quantity, 1 if the
        # item comes from the shared rare drop table].
        c = chance(rarity)
        if c is not None and WEEKLY.search(_notes(d)):
            c /= 7  # a claim once a week: the planner counts NPC hand-outs a visit a day
        sources.setdefault(page, []).append([r["item_name"], c if c is not None else rarity,
                                                      round(quantity(d.get("Drop Quantity")), 2),
                                                      1 if r.get("rare_drop_table") else 0])
    monsters = {}
    for r in wiki.bucket_all(client, "infobox_monster", ["page_name", "combat_experience", "life_points",
                                                         "slayer_level", "slayer_experience", "is_members_only"]):
        xp = _num(r.get("combat_experience"))
        if xp:
            monsters.setdefault(r["page_name"], {"xp": xp, "life_points": _num(r.get("life_points")),
                                                 "slayer_level": _num(r.get("slayer_level")),
                                                 "slayer_xp": _num(r.get("slayer_experience")),
                                                 "members": r.get("is_members_only") != "No"})
    OUT.write_text(json.dumps({"source": "https://runescape.wiki Bucket:Dropsline, Bucket:Infobox_monster "
                                         "(CC BY-NC-SA 3.0)", "sources": sources, "monsters": monsters,
                               # Category:Bosses: fights that need a strategy, so no generic kill-rate estimate.
                               "bosses": sorted(category_pages(client, "Category:Bosses"))},
                              ensure_ascii=False, separators=(",", ":")))
    print(f"{len(sources)} drop sources, {len(monsters)} monsters -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
