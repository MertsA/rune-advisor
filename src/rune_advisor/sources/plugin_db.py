"""Read what our in-game plugins wrote: snapshots and inventories from the exporter (plugins/rune_advisor_export),
sessions and config/name dumps from the dev plugin (plugins/rune_advisor_dev).

The client's PersistentDB is a SQLite file next to the game cache:
    <user_folder>/plugins/PluginData.db
    CREATE TABLE DataStore (PluginID TEXT, Key TEXT, Type INTEGER, Value BLOB, PRIMARY KEY(PluginID, Key))
Type follows PersistentDBType: 0 BOOL, 1 INT, 2 FLOAT, 3 STRING, 4 STRUCTURED_DATA.
The exporter stores JSON as STRING entries so we never need to decode the STRUCTURED_DATA blob format.
Rows are keyed by the plugin's manifest *name* ("Rune Advisor Export", "Rune Advisor Dev"), not its folder name, and
the store rejects large strings (somewhere above ~78 KB), hence the chunking.

Beta (dev-plugin) clients use a separate user folder (…/RuneScape-BETA/plugins/PluginData.db).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

SNAPSHOT_PREFIX = "snapshot:"
CHUNK_SEP = "#"
INVENTORY_PREFIX = "inv:"  # inv:<player>:<INV config name>
# Dev plugin dumps: config:quests / config:achievements (static config shapes with var names),
# debug:<kind> (config name lists, var watch logs) and smoke:vars / smoke:state (vars-only export trial).
DUMP_PREFIXES = ("config:", "debug:", "smoke:")

# SkillType enum names (plugin API) -> hiscores skill names.
SKILLTYPE_TO_NAME = {
    "ATTACK": "Attack", "DEFENCE": "Defence", "STRENGTH": "Strength", "HITPOINTS": "Hitpoints",
    "RANGED": "Ranged", "PRAYER": "Prayer", "MAGIC": "Magic", "COOKING": "Cooking",
    "WOODCUTTING": "Woodcutting", "FLETCHING": "Fletching", "FISHING": "Fishing",
    "FIREMAKING": "Firemaking", "CRAFTING": "Crafting", "SMITHING": "Smithing", "MINING": "Mining",
    "HERBLORE": "Herblore", "AGILITY": "Agility", "THIEVING": "Thieving", "SLAYER": "Slayer",
    "FARMING": "Farming", "RUNECRAFTING": "Runecraft", "HUNTER": "Hunter",
    "CONSTRUCTION": "Construction", "SUMMONING": "Summoning", "DUNGEONEERING": "Dungeoneering",
    "DIVINATION": "Divination", "INVENTION": "Invention", "ARCHAEOLOGY": "Archaeology",
    "NECROMANCY": "Necromancy",
}


def read_entries(db_path: Path, plugin_id: str) -> dict[str, Any]:
    """Return {key: decoded value} for one plugin, with chunked values joined.

    Copies the DB first so a running client's lock can't bite.
    """
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "PluginData.db"
        shutil.copy2(db_path, copy)
        for suffix in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                shutil.copy2(side, copy.with_name(copy.name + suffix))
        con = sqlite3.connect(copy)
        try:
            rows = con.execute("SELECT Key, Type, Value FROM DataStore WHERE PluginID = ?", (plugin_id,)).fetchall()
        finally:
            con.close()
    raw = {key: _text(typ, val) for key, typ, val in rows}
    return {key: _decode(val) for key, val in _join_chunks(raw).items()}


def _text(typ: int, val: Any) -> Any:
    if isinstance(val, bytes) and typ == 3:
        val = val.decode("utf-8", errors="replace")
    if isinstance(val, str):
        val = val.rstrip("\x00")  # the client stores strings with a C-style NUL terminator
    return val  # STRUCTURED_DATA (4) and numbers are returned raw; their format is undocumented.


def _join_chunks(raw: dict[str, Any]) -> dict[str, Any]:
    """The exporter splits large values into "<key>#1".."<key>#N" with {"chunks": N} at "<key>"."""
    out = {}
    for key, val in raw.items():
        if CHUNK_SEP in key:
            continue
        header = _decode(val)
        if isinstance(header, dict) and set(header) == {"chunks"}:
            parts = [raw.get(f"{key}{CHUNK_SEP}{i}") for i in range(1, header["chunks"] + 1)]
            if any(p is None for p in parts):
                continue  # mid-write or damaged; skip rather than return half a snapshot
            val = "".join(parts)
        out[key] = val
    return out


def list_plugin_ids(db_path: Path) -> list[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT PluginID FROM DataStore")]
    finally:
        con.close()


def _decode(val: Any) -> Any:
    if isinstance(val, str) and val[:1] in "{[":
        try:
            return json.loads(val)
        except json.JSONDecodeError:
            pass
    return val


def snapshots(entries: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{player_name: snapshot}, with exported inventories under snapshot["inventories"][<INV name>]."""
    out: dict[str, dict[str, Any]] = {}
    for key, val in entries.items():
        if key.startswith(SNAPSHOT_PREFIX) and isinstance(val, dict):
            out[key[len(SNAPSHOT_PREFIX):]] = dict(val)
    for key, val in entries.items():
        if key.startswith(INVENTORY_PREFIX) and isinstance(val, dict):
            player, _, inv_name = key[len(INVENTORY_PREFIX):].rpartition(":")
            out.setdefault(player, {}).setdefault("inventories", {})[inv_name] = val
    return out


def dumps(entries: dict[str, Any]) -> dict[str, Any]:
    """The dev plugin's dumps by key ("config:quests", "debug:varPlayerNames", ...)."""
    return {key: val for key, val in entries.items() if key.startswith(DUMP_PREFIXES)}
