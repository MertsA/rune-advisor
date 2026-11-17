"""Build data/elite_outfits.json: the elite skilling outfits made from Invention fragments, from the RuneScape
Wiki's "Elite skilling outfit" page.

Usage: python scripts/build_elite_outfits.py

Per skill: the fragment currency's player var (exported by the plugin as CURRENCY_FRAGMENTS_*), the component
outfits (3, or 4 for Dungeoneering) with their five pieces in slot order, and the combined outfit's pieces in the
same order, plus each fragment page's drop size and interval and any blueprint unlock (FRAGMENT_RULES). Shared
mechanics: from level 70, 1/2,500 per drop of a bonus 3,600; x2 at 99, x3 at 120, x4 at 200m xp; 3,600 per piece;
leftover fragments are destroyed once the combined outfit is made. Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/elite_outfits.json"
API = "https://runescape.wiki/api.php"
# Fragment currency var suffix per skill (CURRENCY_FRAGMENTS_<X> in the plugin's var dump).
FRAGMENT_VARS = {"Divination": "DIVINATION", "Dungeoneering": "DUNGEONEERING", "Farming": "FARMING",
                 "Fishing": "SHARK", "Hunter": "TRAPPER", "Mining": "GEMSTONE", "Runecrafting": "ETHEREAL",
                 "Thieving": "CAMOUFLAGE", "Woodcutting": "WOODCUTTING"}
# From each fragment page (2026-10-29): mean fragments per drop, minutes between drops (None = per dungeon floor),
# and the blueprint that must be bought first ("unlock_var" = the player var that shows it's bought, if found).
FRAGMENT_RULES = {
    "Thieving": {"per_drop": 50, "minutes": 5},
    "Farming": {"per_drop": 20, "minutes": 1},
    "Dungeoneering": {"per_drop": 100, "minutes": None,
                      "unlock": "blueprints from Marmaros (Daemonheim) for 100,000 Dungeoneering tokens"},
    "Hunter": {"per_drop": 100.5, "minutes": 5,
               "unlock": "Trapper outfit piece blueprint from the Hunter Mark Shop (Anachronia) for 100 Hunter marks",
               "unlock_var": "DINO_GAME_SHOP_ELITE_OUTFIT_BLUEPRINT", "unlock_currency_var": "DINO_GAME_HUNTER_MARKS",
               "unlock_cost": 100},
}
DEFAULT_RULE = {"per_drop": 100, "minutes": 5}
OUTFIT = r"\[\[([^\]|]+)\]\][^\n{]*?\{\{Nowrap\|\{\{Plinkp multiple\|([^}]+)\}\}"


def main() -> None:
    data = CachedClient().get_json(API, {"action": "parse", "page": "Elite_skilling_outfit", "prop": "wikitext",
                                         "format": "json"})
    text = data["parse"]["wikitext"]["*"]
    table = text[text.index('{| class="wikitable lighttable'):]
    table = table[:table.index("\n|}")]
    out = {}
    for row in table.split("\n|-")[1:]:
        skill = re.search(r"\{\{Sc\|([^}]+)\}\}", row)
        if not skill or skill[1] not in FRAGMENT_VARS:
            continue
        cells = row.split("\n|")
        # Cells after the skill header: component outfits (bulleted), then the combined outfit.
        components = [{"outfit": o, "pieces": p.split("|")} for o, p in re.findall(r"\*\s*" + OUTFIT, cells[1])]
        combined = re.search(OUTFIT, cells[2])
        if not components or not combined:
            raise SystemExit(f"unexpected table row for {skill[1]}")
        out[skill[1]] = {"fragment_var": f"CURRENCY_FRAGMENTS_{FRAGMENT_VARS[skill[1]]}",
                         **FRAGMENT_RULES.get(skill[1], DEFAULT_RULE),
                         "components": components,
                         "combined": {"outfit": combined[1], "pieces": combined[2].split("|")}}
    OUT.write_text(json.dumps(out, indent=1) + "\n")
    print(f"{len(out)} outfits -> {OUT}")


if __name__ == "__main__":
    main()
