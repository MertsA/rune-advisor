"""Where the player can walk, from their own recorded movement.

The planner's trip times (mechanics/trips.py) are assumptions: nothing tells it how far a bank is from a
rock. The wiki's maps are pictures rendered from the game's data, with no walkability in them, and the
plugin API has no pathfinding call. What the API does give, for the player's own character only, is the
tile it stands on and the route the client computed for its last click (PlayerEntity.coordGrid and
.route). The session recorder logs both, and this module turns them into:

- `tiles`: tiles the character stood on or the client routed through, i.e. known walkable, packed like
  the API's CoordGrid ((level << 28) | (x << 14) | z);
- `steps`: pairs of tiles walked between in one game tick (at most 2 apart: running), the edges a
  pathfinder can use later;
- `jumps`: moves of more than JUMP_DISTANCE tiles or across levels in one tick, labelled with the game
  action clicked just before (a ladder, a door, "Teleport" on a lodestone, an Agility shortcut): the
  candidate transport edges, to be matched to the wiki's teleport destinations (data/teleports.json).

Everything is merged into one store (TILE_STORE, JSON) across sessions, session by session, so the map
grows with play; `radv tiles <player>` merges new sessions and summarises it. Nothing here reads other
players or the game's map files.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from . import datapkg
from pathlib import Path
from typing import Any

from .config import STATE_DIR
from .sessions import Session

TILE_STORE = STATE_DIR / "tiles.json"
JUMP_DISTANCE = 2  # tiles a tick: running; anything further (or a level change) isn't walking
JUMP_WINDOW_S = 10.0  # the action clicked within this long before a jump is taken as its cause
MAP_SQUARE = 64


def pack(level: int, x: int, z: int) -> int:
    return (level << 28) | (x << 14) | z


def unpack(p: int) -> tuple[int, int, int]:
    return p >> 28, (p >> 14) & 0x3FFF, p & 0x3FFF


def line_tiles(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[int, int]]:
    """Tiles from a to b along a straight or diagonal line (how the client walks between waypoints),
    both ends included. Any other segment yields its two ends only: the tiles between are unknown."""
    dx, dz = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dz == 0:
        return [a]
    if dx != 0 and dz != 0 and abs(dx) != abs(dz):
        return [a, b]
    n = max(abs(dx), abs(dz))
    sx, sz = (dx > 0) - (dx < 0), (dz > 0) - (dz < 0)
    return [(a[0] + sx * i, a[1] + sz * i) for i in range(n + 1)]


@dataclass
class Jump:
    src: tuple[int, int, int]  # level, x, z
    dst: tuple[int, int, int]
    action: str | None  # "Climb-up Ladder", "Teleport Lodestone", None if nothing was clicked just before
    at: float


@dataclass
class Trail:
    tiles: set[int] = field(default_factory=set)
    steps: set[tuple[int, int]] = field(default_factory=set)  # packed (from, to), from < to
    jumps: list[Jump] = field(default_factory=list)

    def add_step(self, a: int, b: int) -> None:
        self.tiles.update((a, b))
        if a != b:
            self.steps.add((min(a, b), max(a, b)))


def _action_before(s: Session, t: float) -> str | None:
    best = None
    for at, verb, target in s.clicks_seq:
        if at > t:
            break
        if t - at <= JUMP_WINDOW_S:
            best = f"{verb} {target}".strip()
    return best


def trail(s: Session) -> Trail:
    """Walkable tiles, walked steps and jumps from one session's moves and routes."""
    out = Trail()
    prev = None
    for t, level, x, z in s.moves:
        here = pack(level, x, z)
        if prev is not None:
            pl, px, pz = unpack(prev)
            if pl == level and max(abs(x - px), abs(z - pz)) <= JUMP_DISTANCE:
                out.add_step(prev, here)
            else:
                out.tiles.add(here)
                out.jumps.append(Jump((pl, px, pz), (level, x, z), _action_before(s, t), t))
        else:
            out.tiles.add(here)
        prev = here
    # Routes: from where the character stood when the route was set, through each waypoint.
    moves = s.moves
    for t, level, points in s.routes:
        start = next(((x, z) for mt, ml, x, z in reversed(moves) if mt <= t and ml == level), None)
        path = ([start] if start else []) + list(points)
        for a, b in zip(path, path[1:]):
            tiles = line_tiles(a, b)
            if len(tiles) == 2 and max(abs(b[0] - a[0]), abs(b[1] - a[1])) > 1:
                out.tiles.update(pack(level, *p) for p in tiles)  # not a straight segment: ends only
                continue
            for p, q in zip(tiles, tiles[1:]):
                out.add_step(pack(level, *p), pack(level, *q))
            if len(tiles) == 1:
                out.tiles.add(pack(level, *tiles[0]))
    return out


def load_store(path: Path = TILE_STORE) -> dict[str, Any]:
    if path.is_file():
        d = json.loads(path.read_text())
        d.setdefault("tiles", [])
        d.setdefault("steps", [])
        d.setdefault("jumps", [])
        d.setdefault("sessions", [])
        return d
    return {"tiles": [], "steps": [], "jumps": [], "sessions": []}


def save_store(store: dict[str, Any], path: Path = TILE_STORE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, separators=(",", ":")))


def merge(store: dict[str, Any], sessions: list[Session]) -> tuple[int, int]:
    """Add every finished session not yet in the store; returns (sessions merged, new tiles)."""
    tiles = set(store["tiles"])
    steps = {tuple(s) for s in store["steps"]}
    jumps: dict[tuple, dict] = {(j["from"][0], j["from"][1], j["from"][2], j["to"][0], j["to"][1], j["to"][2], j.get("action")): j
                                for j in store["jumps"]}
    seen = set(store["sessions"])
    merged, before = 0, len(tiles)
    for s in sessions:
        key = f"{s.player}:{s.number}"
        if key in seen or not s.ended or not (s.moves or s.routes):
            continue
        tr = trail(s)
        tiles |= tr.tiles
        steps |= tr.steps
        for j in tr.jumps:
            k = (*j.src, *j.dst, j.action)
            if k in jumps:
                jumps[k]["count"] += 1
            else:
                jumps[k] = {"from": list(j.src), "to": list(j.dst), "action": j.action, "count": 1}
        seen.add(key)
        merged += 1
    store["tiles"] = sorted(tiles)
    store["steps"] = sorted(list(s) for s in steps)
    store["jumps"] = list(jumps.values())
    store["sessions"] = sorted(seen)
    return merged, len(tiles) - before


def summary(store: dict[str, Any]) -> dict[str, Any]:
    """Tiles per level, map squares touched, jumps grouped by action."""
    per_level: Counter = Counter()
    squares: set[tuple[int, int, int]] = set()
    for p in store["tiles"]:
        level, x, z = unpack(p)
        per_level[level] += 1
        squares.add((level, x // MAP_SQUARE, z // MAP_SQUARE))
    by_action: Counter = Counter()
    for j in store["jumps"]:
        by_action[j.get("action") or "(no action clicked)"] += j["count"]
    return {"tiles": len(store["tiles"]), "per_level": dict(sorted(per_level.items())), "map_squares": len(squares),
            "steps": len(store["steps"]), "jumps": sum(by_action.values()), "jumps_by_action": by_action.most_common(),
            "sessions": len(store["sessions"])}


def teleports() -> list[dict[str, Any]]:
    """Teleport destinations from the wiki's coordinates bucket (data/teleports.json, scripts/build_teleports.py):
    page (the spell, item or lodestone), destination, x, y, plane, map. Empty if the file isn't built."""
    path = datapkg.path("teleports.json")
    return json.loads(path.read_text())["rows"] if path.is_file() else []
