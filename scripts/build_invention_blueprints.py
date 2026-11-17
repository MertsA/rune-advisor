"""Invention blueprints with their discovery xp (data/invention_blueprints.json).

    uv run python scripts/build_invention_blueprints.py

Every blueprint (materials, devices, machines, research) with the Invention level it is discovered at and the xp
a perfect optimisation awards, from the wiki's Blueprints page (its table; "Discovery" gives the optimisation
bands: a perfect arrangement is 100%). Discovering a blueprint is a one-off, so the planner treats undiscovered
ones as xp in hand (track free_xp): the game's "Draught Includer" achievement tests one bit per blueprint, and
its requirement descriptions are the blueprint names used here. Blueprints the table footnotes carry a `gate`:
what has to be unlocked before they can be discovered (`dwarven`/`goblin`: that technology tree's rank, `ancient`:
the 'Ancient gizmos' blueprint, `ancient_tools`, `capacitor`: the other Stormguard blueprints, `bryll`: a
Daemonheim rewards purchase, `elegy`: Nomad's Elegy, `reputation`: Heart of Gielinor reputation, `shop`: an
outfit-piece or shop purchase). The Auto-screener's blueprint is bought at the Archaeology Guild and the table
doesn't footnote it, so it is gated by hand (`stormguard`).

Source: RuneScape Wiki, Blueprints (https://runescape.wiki/w/Blueprints, CC BY-NC-SA 3.0).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.config import USER_AGENT  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src" / "rune_advisor" / "data" / "invention_blueprints.json"
PAGE = "Blueprints"
_ROW = re.compile(r"\|\s*(\w+):\s*\[\[([^\]|]+)(?:\|([^\]]+))?\]\](.*)")
_REF = re.compile(r'<ref group="reqs"(?: name="(\w+)")?\s*/?>(.*?)(?:</ref>|$)', re.S)
# Footnote text -> gate, for the unnamed footnotes.
_GATE_WORDS = [("Heart of Gielinor", "reputation"), ("Ancient tools", "ancient_tools"), ("XP Capacitor", "capacitor"),
               ("Daemonheim", "bryll"), ("Farmer's Market", "shop"), ("Hunter Mark", "shop"), ("Boni", "shop")]
HAND_GATES = {"Auto-screener v1.080": "stormguard"}


def wikitext(title: str) -> str:
    url = "https://runescape.wiki/api.php?" + urllib.parse.urlencode(
        {"action": "parse", "page": title, "prop": "wikitext", "format": "json", "redirects": 1})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=60) as resp:
        return json.load(resp)["parse"]["wikitext"]["*"]


def parse(text: str) -> list[dict]:
    """Rows of the Blueprints table. Level and xp cells span the rows of blueprints discovered together
    (rowspan), so they carry over to the rows that lack them; a row separator may carry stray text ("|-mk.")."""
    rows: list[dict] = []
    level = xp = None
    for block in re.split(r"^\|-.*$", text, flags=re.M):
        lines = [ln for ln in block.splitlines() if ln.startswith("|") and not ln.startswith(("|}", "|+"))]
        if not lines or not (m := _ROW.match(lines[0])):
            continue
        kind, page, shown, _rest = m.groups()
        cells = []
        for ln in lines[1:]:
            ln = re.sub(r'rowspan\s*=\s*"\d+"\s*\|', "", ln)
            cells.append(re.sub(r"\{\{sumf\|([\d.,]+)\}\}", r"\1", ln).strip("| ").strip().replace(",", ""))
        if len(cells) >= 3:
            level, xp = int(cells[0]), float(cells[1])
        elif len(cells) == 2:
            xp = float(cells[0])
        if level is None or xp is None:
            continue
        name = (shown or page).strip()
        gate = HAND_GATES.get(name)
        if (ref := _REF.search(lines[0])) and gate is None:
            gate = ref.group(1) or next((g for word, g in _GATE_WORDS if word in ref.group(2)), "other")
        rows.append({"name": name, "kind": kind, "level": level, "xp": xp, **({"gate": gate} if gate else {})})
    return rows


def main() -> int:
    rows = parse(wikitext(PAGE))
    OUT.write_text(json.dumps({"source": f"https://runescape.wiki/w/{PAGE} (CC BY-NC-SA 3.0); xp at a perfect optimisation",
                               "blueprints": rows}, indent=1) + "\n")
    print(f"{OUT}: {len(rows)} blueprints, {sum(r['xp'] for r in rows):,.1f} xp in all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
