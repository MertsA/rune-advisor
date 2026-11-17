"""Official RS3 hiscores. index_lite.json gives true (cap-respecting) levels, xp, and activity scores incl. RuneScore."""

from __future__ import annotations

from typing import Any

import httpx

from ..http import CachedClient
from ..models import Skill

BASE = "https://secure.runescape.com"
# Other boards share the same path: m=hiscore_ironman, m=hiscore_hardcore_ironman.
BOARDS = {"normal": "m=hiscore", "ironman": "m=hiscore_ironman", "hardcore": "m=hiscore_hardcore_ironman"}


def fetch(client: CachedClient, player: str, board: str = "normal") -> dict[str, Any]:
    return client.get_json(f"{BASE}/{BOARDS[board]}/index_lite.json", params={"player": player})


def is_on_board(client: CachedClient, player: str, board: str) -> bool:
    """Whether the player has an entry on a board, e.g. "ironman" (ironmen also appear on "normal")."""
    try:
        fetch(client, player, board)
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:  # not on that board
            return False
        raise


def parse_skills(data: dict[str, Any]) -> dict[str, Skill]:
    return {
        s["name"]: Skill(name=s["name"], level=s["level"], xp=s["xp"], rank=s["rank"] if s["rank"] > 0 else None)
        for s in data["skills"]
        if s["name"] != "Overall"
    }


def parse_activities(data: dict[str, Any]) -> dict[str, int]:
    return {a["name"]: a["score"] for a in data.get("activities", []) if a["score"] >= 0}


def parse_runescore(data: dict[str, Any]) -> int | None:
    return parse_activities(data).get("RuneScore")
