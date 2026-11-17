"""Archive a client's plugin export: copy its PluginData.db and dump one player's snapshot as JSON.

Since 2026-11-01 the exporter is HTTP-only and writes no snapshots to PluginData.db: use `radv pull <player>`
to archive a snapshot from the export API instead. This script still reads older databases (and the dev
plugin's dumps).

    uv run python scripts/archive_snapshot.py YourName [--beta] [--date 2026-10-31]

Writes state/archive/<date>_<player>_<beta|live>/{PluginData.db, snapshot_<player>.json} (plus
config_<kind>.json / smoke_<kind>.json for the dev plugin's dumps when present)
and prints the `snapshot = ...` line for `[players.<player>]` in rune_advisor.toml. The live client can't run dev
plugins, so the real account is planned from the last archived beta export (a fresh save import on the beta);
`find_snapshot` falls back to that file when the live plugin DB has no snapshot for the player.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.config import ARCHIVE_DIR, load_settings  # noqa: E402
from rune_advisor.sources import plugin_db  # noqa: E402



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("player")
    ap.add_argument("--beta", action="store_true", help="archive the plugin beta client's DB (the default is the live one)")
    ap.add_argument("--db", type=Path, help="an explicit PluginData.db path")
    ap.add_argument("--date", default=date.today().isoformat(), help="folder date (default today)")
    args = ap.parse_args()

    settings = load_settings()
    beta_db = load_settings(beta=True).plugin_db
    db = args.db or (beta_db if args.beta else settings.plugin_db)
    snaps = plugin_db.snapshots(plugin_db.read_entries(db, settings.export_plugin_id))
    snap = next((v for k, v in snaps.items() if k.lower() == args.player.lower()), None)
    if snap is None:
        print(f"no snapshot for {args.player} in {db} (players: {', '.join(snaps) or 'none'})", file=sys.stderr)
        return 1
    name = snap.get("player", args.player)

    base = ARCHIVE_DIR / f"{args.date}_{name}_{'beta' if args.beta or db == beta_db else 'live'}"
    out, n = base, 1
    while out.exists():  # a second export the same day gets a numbered folder
        n += 1
        out = base.with_name(f"{base.name}_{n}")
    out.mkdir(parents=True)
    shutil.copy2(db, out / "PluginData.db")
    snap_path = out / f"snapshot_{name}.json"
    snap_path.write_text(json.dumps(snap))
    dumps = plugin_db.dumps(plugin_db.read_entries(db, settings.dev_plugin_id))
    for key, value in dumps.items():
        if key.startswith(("config:", "smoke:")):  # config_quests.json, smoke_vars.json, ...
            (out / (key.replace(":", "_", 1).replace(":", "_") + ".json")).write_text(json.dumps(value))

    skills = snap.get("skills") or {}
    total_xp = sum(s.get("xp", 0) for s in skills.values()) if isinstance(skills, dict) else 0
    invs = snap.get("inventories") or {}
    print(f"archived {name} from {db}")
    print(f"  {snap_path}  (exportedAt={snap.get('exportedAt')}, total xp {total_xp:,}, "
          f"{len(invs)} inventories, {len(snap.get('vars') or {})} vars)")
    for key in sorted(dumps):
        if key.startswith(("config:", "smoke:")):
            print(f"  dump {key}: {len(dumps[key])} entries")
    print(f"\n[players.{name}]\nsnapshot = \"{snap_path.as_posix().replace(str(Path.home()), '~')}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
