"""Replay the exporter's recorded wire posts and check they rebuild the player's state.

Only databases written before 2026-11-01 have recorded posts (the local stand-in transport is gone; the
exporter posts to the API, which keeps the state).

    uv run python scripts/check_wire.py [player] [--db PATH]

Reads wire:<player>:post:<n> from the plugin beta client's PluginData.db (the mock transport's records),
replays them as the server would (rune_advisor.wire: layout and hash checks, deltas applied in order) and
compares the result with the exporter's own snapshot: every achievement test's pass/fail derived from the
wire state vs the snapshot's, and the inventory sets vs the inv:* exports.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor import wire  # noqa: E402
from rune_advisor.config import load_settings  # noqa: E402
from rune_advisor.sources import plugin_db  # noqa: E402
from rune_advisor.sources.game_configs import _as_list, derive, load  # noqa: E402
from rune_advisor.sources.plugin_db import SKILLTYPE_TO_NAME  # noqa: E402
from rune_advisor.xp import level_at_xp  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("player")
    ap.add_argument("--db", type=Path)
    args = ap.parse_args()
    settings = load_settings()
    db = args.db or load_settings(beta=True).plugin_db
    entries = plugin_db.read_entries(db, settings.export_plugin_id)
    configs = load()
    if configs is None:
        print("no game configs: run scripts/import_game_configs.py", file=sys.stderr)
        return 1
    r = wire.replay(entries, configs, args.player)
    if not r.posts:
        print(f"{db}: no wire posts for {args.player}", file=sys.stderr)
        return 1
    for p in r.posts:
        print(f"post {p.number}: {p.kind} {p.bytes} bytes, {p.verdict}" + ("" if (p.verdict == "accepted") == p.accepted_by_plugin
                                                                       else " (DISAGREES with the plugin's transport)"))
    print(f"layout {r.layout.hash:#010x}, {len(r.layout.slots)} slots, {r.layout.total_bits} bits; consistent: {r.consistent}")
    if r.state is None:
        return 1
    fields, named, skills, members = wire.values(r.layout, r.state)
    snaps = plugin_db.snapshots(entries)
    snap = snaps.get(args.player)
    if snap and snap.get("schema") == 2:
        levels = {name: level_at_xp(SKILLTYPE_TO_NAME.get(name, name), xp) for name, xp in skills.items()}
        d = derive(configs, fields, levels)
        agree = disagree = 0
        for a in _as_list(snap.get("achievements")):
            states = _as_list(a.get("tests"))
            derived = d.tests.get(a["id"])
            if not states or derived is None or len(states) != len(derived):
                continue
            for st, dv in zip(states, derived):
                if dv is None:
                    continue
                if bool(st[0]) == dv:
                    agree += 1
                else:
                    disagree += 1
        print(f"achievement tests derived from the wire state vs the snapshot: {agree} agree, {disagree} disagree")
        qa = qd = 0
        for q in _as_list(snap.get("quests")):
            s = d.quests.get(q["id"])
            if s is None:
                continue
            api = "finished" if q.get("finished") else "started" if q.get("started") else "not started"
            qa, qd = qa + (s == api), qd + (s != api)
        print(f"quests: {qa} agree, {qd} disagree")
        invs = snap.get("inventories") or {}
        same = diff = 0
        for name, items in r.state.invs.items():
            exported = {i["id"]: 0 for i in (invs.get(name) or {}).get("items", [])}
            for i in (invs.get(name) or {}).get("items", []):
                if i.get("count", 0) > 0:
                    exported[i["id"]] = exported.get(i["id"], 0) + i["count"]
            exported = {k: v for k, v in exported.items() if v > 0}
            if name in invs:
                same, diff = same + (exported == items), diff + (exported != items)
        unknown = [n for n in r.layout.inventories if n not in r.state.invs]
        print(f"inventory sets vs inv:* exports: {same} identical, {diff} differ (the bank differs unless it was open at both times); "
              f"{len(unknown)} never loaded this session (server keeps its last copy): {', '.join(unknown)}")
        print(f"skills: {len(skills)} slots, Attack xp {skills.get('ATTACK')}; members {members}; named vars {len(named)}")
    return 0 if r.consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
