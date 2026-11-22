"""Build the tables currencies.py earns reward-shop currencies from, from RuneScape Wiki pages:

  * data/reaper_tasks.json: each Soul Reaper boss's kills, Slayer xp and reaper points (the Reaper task page's
    Bosses table; normal mode, solo, not extended). Reaper points are earned at the best boss the player kills.
  * data/arc_commodities.json: chimes paid per Arc commodity at the Waiko merchants, before the sell price upgrades
    (Template:Chimes per commodity). Chimes are earned by selling whichever commodity is cheapest to gather.

Usage: python scripts/build_currency_tables.py

Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/reaper_tasks.json"
ARC_OUT = OUT.with_name("arc_commodities.json")
API = "https://runescape.wiki/api.php"
NAME = re.compile(r"\{\{plinkt\|([^|}]+)")
SORT = re.compile(r'data-sort-value="([^"]*)"')


def _value(cell: str) -> str:
    """A table cell's value: its sort value when it has one, else the text after the last attribute bar."""
    if m := SORT.search(cell):
        return m.group(1)
    return cell.rsplit("|", 1)[-1].strip()


def _num(text: str) -> float | None:
    nums = [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]
    return sum(nums) / len(nums) if nums else None  # "3–4" -> 3.5


def parse(wikitext: str) -> list[dict]:
    table = wikitext.split("==Bosses==", 1)[1].split("\n|}", 1)[0]
    out = []
    for block in table.split("\n|-\n"):
        lines = [ln for ln in block.split("\n") if ln.startswith("|") and not ln.startswith("|-")]
        if not lines or not (m := NAME.search(lines[0])):
            continue  # a header, or the hard-mode half of a boss's two rows
        # Requirement cells span lines ("* {{Skillreq|Combat|115}}"): keep the cells that start a value.
        cells = [_value(ln[1:]) for ln in lines[1:]]
        if len(cells) < 5:
            continue
        combat, kills, _, xp, points = cells[:5]
        if None in (k := _num(kills), x := _num(xp), p := _num(points)):
            continue
        out.append({"boss": m.group(1).strip(), "combat": int(_num(combat) or 0), "kills": k,
                    "slayer_xp": x, "points": p})
    return out


def parse_commodities(wikitext: str) -> dict[str, float]:
    """{commodity: chimes each} from the template's tables: the first value cell, its base price (a bracketed
    figure is Cyclosis's higher price, not assumed)."""
    out = {}
    for block in wikitext.split("\n|-\n"):
        lines = [ln for ln in block.split("\n") if ln.startswith("|") and not ln.startswith("|-")]
        if len(lines) < 2 or not (m := NAME.search(lines[0])):
            continue
        value = re.match(r"\s*([\d.]+)", lines[1][1:])
        if value and float(value.group(1)) > 0:
            out[m.group(1).strip()] = float(value.group(1))
    return out


def _page(client: CachedClient, title: str) -> str:
    r = client.get_json(API, params={"action": "parse", "page": title, "prop": "wikitext", "redirects": 1,
                                     "format": "json"})
    return r["parse"]["wikitext"]["*"]


def main() -> None:
    client = CachedClient()
    arc = parse_commodities(_page(client, "Template:Chimes per commodity"))
    ARC_OUT.write_text(json.dumps({"source": "https://runescape.wiki/w/Template:Chimes_per_commodity (CC BY-NC-SA 3.0); "
                                             "base prices, no sell price upgrades", "daily_cap": 20000,
                                   "chimes": dict(sorted(arc.items()))}, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(arc)} Arc commodities -> {ARC_OUT}", file=sys.stderr)
    rows = parse(_page(client, "Reaper task"))
    OUT.write_text(json.dumps({"source": "https://runescape.wiki/w/Reaper_task (CC BY-NC-SA 3.0); normal mode, "
                                         "solo, not extended; kills are the range's midpoint",
                               "weekly_cap": 300, "bosses": rows}, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(rows)} reaper bosses -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
