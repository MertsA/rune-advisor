"""A complete Adventurer's Log, stitched from a feed that only ever shows the last 20 events.

The Player Events API (`sources.adventurers_log.fetch_events`) is a fixed window, newest first, with no
cursor. `poll` fetches it and appends whatever is newer than the stored log, found by aligning the window
against the log's tail as a *sequence*: events are not deduplicated individually, because two events can
share a second and batched kill messages repeat verbatim. If nothing in a full window overlaps the stored
tail, more than a window's worth happened between polls and a gap marker is written instead of pretending
the log is complete.

The log is one JSON object per line, oldest first, in `state/events/<player>.jsonl`: the API's event
fields as they came (`date`, `type`, `text`, `details`) or `{"gap": true, "at": <poll time>}`.

Cadence: the API caches responses for up to 10 minutes, so polling faster than that returns the same body
(poll every 10 minutes while the game is open, and once more some 11 minutes after it closes to catch the
last cached events).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import STATE_DIR
from .http import CachedClient
from .sources import adventurers_log

EVENTS_DIR = STATE_DIR / "events"
WINDOW = 20  # events the API returns at most

Event = dict[str, Any]


def log_path(player: str) -> Path:
    return EVENTS_DIR / f"{player.lower().replace(' ', '_')}.jsonl"


def read_log(path: Path) -> list[Event]:
    """Stored events (and gap markers), oldest first."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _exact(e: Event) -> tuple:
    return (e.get("date"), e.get("type"), e.get("text"), e.get("details"))


def _loose(e: Event) -> tuple:
    # Batched events (kill counts) may be rewritten in place; their date and type still line up.
    return (e.get("date"), e.get("type"))


def _overlap(tail: list[Event], window: list[Event], key) -> int:
    """Longest k such that the window's k oldest events equal the tail's k newest (both oldest first)."""
    for k in range(min(len(tail), len(window)), 0, -1):
        if [key(e) for e in window[:k]] == [key(e) for e in tail[-k:]]:
            return k
    return 0


@dataclass
class Merge:
    new: list[Event] = field(default_factory=list)  # oldest first, what to append
    gap: bool = False  # a full window with no overlap: events were missed
    loose: bool = False  # aligned on date+type only (some stored event's text changed)


def merge(stored: list[Event], window_newest_first: list[Event], now: datetime | None = None) -> Merge:
    """What a fresh window adds to the stored log."""
    window = list(reversed(window_newest_first))
    tail = [e for e in stored if not e.get("gap")][-WINDOW:]
    m = Merge()
    if not tail:
        m.new = window
        return m
    k = _overlap(tail, window, _exact)
    if k == 0:
        k = _overlap(tail, window, _loose)
        m.loose = k > 0
    if k == 0 and len(window) >= WINDOW:
        # Nothing lines up: the stored tail scrolled out of the window between polls.
        m.gap = True
        m.new = [{"gap": True, "at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")}, *window]
        return m
    # k == 0 with a short window: the feed holds the player's whole history and we have all of it
    # (or their privacy settings changed); nothing to add beyond what isn't stored.
    m.new = window[k:] if k else [e for e in window if _exact(e) not in {_exact(t) for t in tail}]
    return m


def append(path: Path, events: list[Event]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for e in events:
            f.write(json.dumps(e, sort_keys=True) + "\n")


def poll(client: CachedClient, player: str, path: Path | None = None) -> Merge | None:
    """Fetch the feed once and append what's new. None when the player is free-to-play (no feed)."""
    path = path or log_path(player)
    data = adventurers_log.fetch_events(client, player, ttl=0)
    if data is None:
        return None
    m = merge(read_log(path), data.get("events", []))
    append(path, m.new)
    return m
