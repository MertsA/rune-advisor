"""Item id -> name map for the export API (data/item_names.json).

    uv run python scripts/build_item_names.py [--db PATH ...] [--lookup]

The wire protocol sends inventories as item ids only (names are static), so the API server names them from
this file when it builds a snapshot for the planner. Sources: every archived snapshot's inventories
(state/archive/*/snapshot_*.json) and the inv:* exports in the live and beta PluginData.db
(plus any --db given). Ids the file lacks show up as "Unknown item <id>" in API snapshots; re-run after an
export that held new items. `--lookup` names the ids still unknown from the wiki (Special:Lookup redirects an
item id to its page: "Antique lamp (Elite Ardougne Tasks)", "Super attack (2)"); those rows carry no config
name and use the wiki's page title, which can be longer than the in-game name.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.config import ARCHIVE_DIR, USER_AGENT, load_settings  # noqa: E402
from rune_advisor.sources import plugin_db  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src" / "rune_advisor" / "data" / "item_names.json"
WIKI_LOOKUP = "https://runescape.wiki/w/Special:Lookup"


def wiki_item_name(item_id: int) -> str | None:
    """The wiki page an item id redirects to (Special:Lookup), as a name: underscores to spaces and the
    "#(2)" anchor of a dose/charge variant appended as " (2)"."""
    req = urllib.request.Request(f"{WIKI_LOOKUP}?type=item&id={item_id}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            url = resp.geturl()
    except (urllib.error.URLError, OSError):
        return None
    if "/w/" not in url or "Special:Lookup" in url:
        return None
    title = urllib.parse.unquote(url.rsplit("/w/", 1)[1]).replace("_", " ")
    page, _, anchor = title.partition("#")
    return f"{page} {anchor}" if anchor else page


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, action="append", default=[])
    ap.add_argument("--lookup", action="store_true", help="name the ids still unknown from the wiki")
    args = ap.parse_args()
    names: dict[int, dict[str, str]] = {}
    if OUT.exists():
        names = {int(k): v for k, v in json.loads(OUT.read_text()).get("items", {}).items()
                 if not str(v.get("name", "")).startswith("Unknown item")}

    def take(items):
        for it in items:
            # API snapshots carry "Unknown item <id>" for ids this file lacked: not a name.
            if isinstance(it.get("id"), int) and it.get("name") and not str(it["name"]).startswith("Unknown item"):
                names[it["id"]] = {"name": it["name"], "config": it.get("config") or ""}

    snapshots = list(ARCHIVE_DIR.glob("*/snapshot_*.json")) if ARCHIVE_DIR.exists() else []
    unknown: set[int] = set()
    for path in snapshots:
        for inv in (json.loads(path.read_text()).get("inventories") or {}).values():
            take(inv.get("items", []))
            unknown |= {it["id"] for it in inv.get("items", [])
                        if isinstance(it.get("id"), int) and str(it.get("name", "")).startswith("Unknown item")}
    if args.lookup:
        for item_id in sorted(unknown - set(names)):
            if name := wiki_item_name(item_id):
                names[item_id] = {"name": name, "config": ""}
                print(f"{item_id}: {name} (wiki)")
            else:
                print(f"{item_id}: not on the wiki", file=sys.stderr)
    settings = load_settings()
    for db in [settings.plugin_db, load_settings(beta=True).plugin_db, *args.db]:
        if not db.exists():
            continue
        for snap in plugin_db.snapshots(plugin_db.read_entries(db, settings.export_plugin_id)).values():
            for inv in (snap.get("inventories") or {}).values():
                take(inv.get("items", []))
    OUT.write_text(json.dumps({"source": "the player's own plugin exports (item names as the client shows them)",
                               "items": {str(k): names[k] for k in sorted(names)}}, indent=0) + "\n")
    print(f"{OUT}: {len(names)} items from {len(snapshots)} archived snapshots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
