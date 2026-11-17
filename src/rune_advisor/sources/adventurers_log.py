"""Adventurer's Log module of the RuneScape Platform API (replaces RuneMetrics, retired 2026-11-10).

Docs: https://developer.runescape.com/platform/modules/adventurers-log/ (launched 2026-11-04).
Always answers JSON; a 404 carries {"error": "PLAYER_NOT_FOUND" | "PLAYER_NOT_MEMBER"}.
Responses are cached server-side for up to 10 minutes; quests sync on logout, world hop and at
intervals during play.

- playerQuests/NAME: every quest, subquest and miniquest keyed by the game's quest id, plus the
  top-level `qp` as the game itself counts it (Dimension of Disaster replays and Sheep Shearer
  included), so it doubles as the journal total when no plugin export gives one.
- playerEvents/NAME: the 20 most recent Adventurer's Log events (members only, and only the event
  types the player has enabled in-game). We only read quest completions out of it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from ..http import CachedClient, error_code
from ..models import Quest, QuestStatus

BASE = "https://secure.runescape.com/m=adventurers-log/api/v1"

QUEST_EVENT = "rs_plog_quest"
_QUEST_DONE = re.compile(r"^Quest complete: (?P<title>.+)$")


def fetch_quests(client: CachedClient, player: str) -> dict[str, Any]:
    return client.get_json(f"{BASE}/playerQuests/{player}")


def fetch_events(client: CachedClient, player: str, ttl: float | None = None) -> dict[str, Any] | None:
    """The recent events feed, or None for a free-to-play player (the feed is members only)."""
    try:
        return client.get_json(f"{BASE}/playerEvents/{player}", ttl=ttl)
    except httpx.HTTPStatusError as e:
        if error_code(e) == "PLAYER_NOT_MEMBER":
            return None
        raise


def parse_quests(data: dict[str, Any]) -> dict[str, Quest]:
    return {
        q["title"]: Quest(
            title=q["title"],
            status=QuestStatus(q["status"]),
            quest_points=q["qp"],
            members=q["members"],
            eligible=q["eligible"],
            quest_id=int(quest_id),
            source="adventurers_log",
        )
        for quest_id, q in data["quests"].items()
    }


def parse_journal_qp(data: dict[str, Any]) -> int | None:
    """The player's QP as the game counts them (the quest journal's total)."""
    qp = data.get("qp")
    return qp if isinstance(qp, int) else None


def parse_event_quest_completions(events: dict[str, Any]) -> dict[str, datetime]:
    """Quest titles completed according to the (recent-only) events feed."""
    done: dict[str, datetime] = {}
    for e in events.get("events", []):
        if e.get("type") != QUEST_EVENT:
            continue
        if m := _QUEST_DONE.match(e["text"]):
            done[m["title"].strip()] = datetime.fromisoformat(e["date"])
    return done
