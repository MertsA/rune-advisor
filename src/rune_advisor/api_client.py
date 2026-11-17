"""Fetching snapshots from the export API (rune_advisor.export_api) for the planner.

rune_advisor.toml:
    [api]
    url = "https://api.runeadvisor.com"
    token = "..."          # or RUNE_ADVISOR_API_URL / RUNE_ADVISOR_API_TOKEN in the environment
    tokens = ["...", "..."]  # more read tokens (a group mate's, limited to their character); tried in order

`fetch_snapshot` returns the server's snapshot for a character, cached on disk for CACHE_SECONDS so a run of
planner commands doesn't hit the server every time; the last good copy is used when the server can't be reached
(with a note on stderr). `radv pull <player>` archives a snapshot like scripts/archive_snapshot.py does.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .config import STATE_DIR, Settings
from .sources import game_configs
from .sources.game_configs import GameConfigs

CACHE_DIR = STATE_DIR / "api_cache"
CONFIGS_DIR = STATE_DIR / "api_configs"  # config sets by fingerprint, for snapshots newer than the data package
CACHE_SECONDS = 60


def _get(settings: Settings, path: str, token: str | None = None, player: str | None = None) -> Any:
    base = (settings.api_url or "").rstrip("/")
    headers = {"Authorization": f"Bearer {token or settings.api_token}"}
    if player is not None:
        headers["X-Player"] = quote(player, safe="")  # a header, not the URL: web server logs never see the name
    with httpx.Client(timeout=20) as client:
        r = client.get(f"{base}/{path.lstrip('/')}", headers=headers)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def players(settings: Settings, token: str | None = None) -> list[dict[str, Any]]:
    """Characters the API holds state for under this token (under every configured token, when none is named)."""
    seen: dict[str, dict[str, Any]] = {}
    for t in [token] if token else settings.api_tokens:
        for p in (_get(settings, "v1/players", t) or {}).get("players", []):
            seen.setdefault((p.get("player") or "").lower(), p)
    return list(seen.values())


def cached_configs(fp: int) -> GameConfigs | None:
    """The config set for a fingerprint that `ensure_configs` fetched earlier, if any."""
    return GameConfigs.read(CONFIGS_DIR / f"{fp:08x}")


def ensure_configs(settings: Settings, fp: Any, token: str) -> None:
    """When a snapshot names a config set the local data package lacks (the game updated since the package was
    built), fetch it with the token that served the snapshot, check it hashes to `fp` and cache it by
    fingerprint under the state dir. A failure only leaves the snapshot expanded with the package's configs."""
    local = game_configs.load()
    if not isinstance(fp, int) or (local is not None and local.fingerprint == fp) or cached_configs(fp) is not None:
        return
    try:
        body = _get(settings, f"v1/configs/0x{fp:08x}", token)
    except (httpx.HTTPError, OSError, ValueError) as e:
        print(f"export API: can't fetch config set {fp:08x} ({e}); using the data package's", file=sys.stderr)
        return
    if not isinstance(body, dict):
        return
    fetched = GameConfigs.from_dumps({game_configs.DUMP_KEYS["quests"]: body.get("quests"),
                                      game_configs.DUMP_KEYS["achievements"]: body.get("achievements")})
    if fetched.fingerprint != fp:  # never trust a set that isn't the one the snapshot names
        print(f"export API: config set {fp:08x} came back as {fetched.fingerprint:08x}; ignored", file=sys.stderr)
        return
    fetched.write(CONFIGS_DIR / f"{fp:08x}")


def fetch_snapshot(settings: Settings, name: str, max_age: float = CACHE_SECONDS) -> dict[str, Any] | None:
    """The API's snapshot for `name` (case-insensitive), or None when the server has none for it. Each configured
    token is tried in turn: a group mate's token may hold the one character this account lacks."""
    cache = CACHE_DIR / (name.lower() + ".json")
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age:
        return json.loads(cache.read_text())
    snap, error = None, None
    for token in settings.api_tokens:
        try:
            listing = players(settings, token)
            actual = next((p["player"] for p in listing if (p.get("player") or "").lower() == name.lower()), None)
            if actual is None:
                continue
            snap = _get(settings, "v1/snapshot", token, player=actual)
        except (httpx.HTTPError, OSError, ValueError) as e:
            error = e
            continue
        if snap is not None:
            ensure_configs(settings, snap.get("configs"), token)
            break
    if snap is None and error is not None:
        if cache.exists():
            print(f"export API unreachable ({error}); using the cached snapshot for {name}", file=sys.stderr)
            return json.loads(cache.read_text())
        print(f"export API unreachable ({error}); no cached snapshot for {name}", file=sys.stderr)
        return None
    if snap is None:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(snap))
    return snap
