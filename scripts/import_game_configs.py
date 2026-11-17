"""Import the dev plugin's config dumps (quests, achievements) into the planner's data files.

    uv run python scripts/import_game_configs.py [--beta | --db PATH | --archive DIR]

Reads `config:quests` and `config:achievements` (the "Dump configs" button of plugins/rune_advisor_dev) from the
plugin beta client's PluginData.db (the default), an explicit DB, or an archive folder's config_*.json files
(scripts/archive_snapshot.py), and writes src/rune_advisor/data/game_configs/{quests,achievements}.json.gz.
The published exporter's snapshots carry only state per config id, so re-run this after game updates that
add quests or achievements (the profile notes unknown ids).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.config import load_settings  # noqa: E402
from rune_advisor.sources import plugin_db  # noqa: E402
from rune_advisor.sources.game_configs import DATA_DIR, DUMP_KEYS, GameConfigs  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, help="a PluginData.db to read (default: the plugin beta client's)")
    ap.add_argument("--archive", type=Path, help="an archive folder with config_quests.json / config_achievements.json")
    ap.add_argument("--out", type=Path, default=DATA_DIR)
    args = ap.parse_args()

    if args.archive:
        dumps = {key: json.loads((args.archive / f"config_{kind}.json").read_text()) for kind, key in DUMP_KEYS.items()}
        source = args.archive
    else:
        settings = load_settings()
        source = args.db or load_settings(beta=True).plugin_db
        dumps = plugin_db.dumps(plugin_db.read_entries(source, settings.dev_plugin_id))
    missing = [key for key in DUMP_KEYS.values() if key not in dumps]
    if missing:
        print(f"{source}: no {', '.join(missing)} (press \"Dump configs\" on the dev plugin's panel)", file=sys.stderr)
        return 1
    configs = GameConfigs.from_dumps(dumps)
    configs.write(args.out)
    print(f"wrote {len(configs.quests)} quests and {len(configs.achievements)} achievements from {source} to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
