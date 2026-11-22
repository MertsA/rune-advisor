"""Build data/unfarmable_monsters.json: monsters whose drops can't be farmed at a generic kill rate.

Usage: python scripts/build_unfarmable_monsters.py

The gatherer costs a drop the guides don't cover at an estimated kill rate (drops.estimated_kills_per_hour).
That makes no sense for monsters that only exist inside a minigame or random event (Temple Trekking's nail
beasts, which the Summoning plan once counted on for 3,750 talon beast charms at 300 kills/h), on the
player-owned farm (animals are bred, not killed), in Daemonheim (nothing leaves the dungeon), or made one at a time
at a Creature Creation altar from their own drops (frogeels for cave eels at 300 kills/h). The list
is the members of those wiki categories plus a few hand-written names. Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/unfarmable_monsters.json"
API = "https://runescape.wiki/api.php"
CATEGORIES = ["Temple Trekking", "Random events", "Player-owned farm", "Dungeoneering monsters",
              "Shattered Worlds", "Stealing Creation", "Soul Wars", "Pest Control", "Barbarian Assault",
              "Fist of Guthix", "Castle Wars", "Dominion Tower", "Fight Kiln", "Conquest", "Dimension of Disaster",
              "Burthorpe Games Room", "Trouble Brewing", "Tears of Guthix",
              "Creature Creation"]  # hybrids made one at a time at an altar from their ingredients
# Monsters the categories miss (checked by hand on the wiki).
EXTRA = ["Nightmare"]  # Daemonheim boss: its drops (Steel titan pouches) never leave the dungeon


def members(client: CachedClient, category: str) -> list[str]:
    titles, params = [], {"action": "query", "list": "categorymembers", "cmtitle": f"Category:{category}",
                          "cmlimit": "500", "cmnamespace": "0", "format": "json"}
    while True:
        d = client.get_json(API, params)
        titles += [m["title"] for m in d.get("query", {}).get("categorymembers", [])]
        if "continue" not in d:
            return titles
        params = {**params, **d["continue"]}


def main() -> None:
    client = CachedClient()
    by_category = {c: members(client, c) for c in CATEGORIES}
    names = sorted({t for ts in by_category.values() for t in ts} | set(EXTRA))
    OUT.write_text(json.dumps({"source": "https://runescape.wiki category members (CC BY-NC-SA 3.0): "
                                         + ", ".join(CATEGORIES), "monsters": names}, ensure_ascii=False))
    print(f"{len(names)} names ({', '.join(f'{c}: {len(t)}' for c, t in by_category.items())}) -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
