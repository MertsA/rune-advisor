"""The planner inside a browser page: this module runs under Pyodide in a web worker (packaging/web/worker.js).

The page plays the desktop app's local server: it keeps the job table and sends one planner call at a time to
the worker, which calls `run` here. What differs from the desktop planner:

  * Player data comes only from the export API (the plugin's posts), read with the token the account page
    handed over. A page cannot ask Jagex's hiscores or Adventurer's Log (no cross-origin access), and with a
    plugin export the planner does not need them.
  * The wiki's tables (achievements, their requirements, quests, prices) are not fetched: the site build puts
    them in the HTTP cache (`CachedClient.keep_stale`), where the planner's usual calls find them.
  * Settings and overrides are the two TOML files the desktop planner reads, written into the worker's
    file system by the page before this module is imported.

Nothing here imports Pyodide's `js` module at import time, so the tests can drive `run` natively.
"""

from __future__ import annotations

import json
import os
import tomllib
from typing import Any, Callable

from .. import config
from ..http import CachedClient
from .planner import Planner, normalise

# The quests that exist today (the Adventurer's Log's titles, the same for every player), written by the site
# build. Without it retired quest configs can show as unfinished (player.player_from_snapshot).
QUEST_TITLES = config.CACHE_DIR / "quest_titles.json"


def _quest_titles() -> set[str] | None:
    try:
        titles = json.loads(QUEST_TITLES.read_text())
    except (OSError, ValueError):
        return None
    return set(titles) if isinstance(titles, list) and titles else None


def _settings(group: list[str], player_name: str) -> config.Settings:
    settings = config.load_settings()
    # The page's API and token (the worker puts them in the environment) beat an [api] table in settings
    # pasted from a desktop install; the paths such settings name do not exist here and are skipped.
    if os.environ.get("RUNE_ADVISOR_API_URL") and os.environ.get("RUNE_ADVISOR_API_TOKEN"):
        settings.api_url = os.environ["RUNE_ADVISOR_API_URL"]
        settings.api_tokens = [os.environ["RUNE_ADVISOR_API_TOKEN"]]
    if group:
        settings.group = [player_name, *group]
    return settings


def web_player(player_name: str, settings: config.Settings, plugin_only: bool = False):
    """The player from the export API's snapshot, then overrides: no hiscores, no Adventurer's Log."""
    from .. import player as players

    snap = players.find_snapshot(settings, player_name)
    if snap is None:
        raise RuntimeError(f"no plugin export for {player_name} under this account: install the Rune Advisor "
                           "plugin, give it a token from the account page and log in to the game once")
    p = players.player_from_snapshot(player_name, snap, _quest_titles())
    # Ironman status is on the hiscores, which a page cannot read: it is a setting here.
    p.ironman = settings.ironman or settings.in_group(player_name)
    if not plugin_only and players.OVERRIDES_FILE.exists():
        players.apply_overrides(p, tomllib.loads(players.OVERRIDES_FILE.read_text()))
        players.reconcile_journal_qp(p)
    return p


def web_ctx_factory(player_name: str, flags: dict, group: list[str]):
    """`cli._ctx` for the web."""
    from .. import cli
    from ..sources import wiki

    settings = _settings(group, player_name)
    if settings.coins_per_hour:
        from ..recommend import gathering
        gathering.COINS_PER_HOUR = float(settings.coins_per_hour)
    if settings.play_hours_per_day:
        from ..recommend import gathering
        gathering.PLAY_HOURS_PER_DAY = float(settings.play_hours_per_day)
    members = False if flags.get("f2p") else settings.player(player_name).members
    if members is None:
        members = settings.members
    p = web_player(player_name, settings, bool(flags.get("plugin_only")))
    client = CachedClient()
    quests = wiki.fetch_all_quests(client)  # one prefetched table in place of a request per quest
    return cli.context_for(p, settings, client, members, quest_info=quests.get)


_planner: Planner | None = None


def planner() -> Planner:
    global _planner
    if _planner is None:
        CachedClient.keep_stale = True
        _planner = Planner(web_ctx_factory)
    return _planner


def meta() -> str:
    """What the page needs before any player is open (GET /api/meta in the desktop app, without the prefs)."""
    from ..recommend.guide import MOODS
    from ..recommend.tracks import ALL_TRACKS

    return json.dumps({"tracks": {n: t.description for n, t in ALL_TRACKS.items()}, "moods": list(MOODS)})


def forget() -> None:
    """Drop the loaded players, so the next call reads the export again."""
    planner().forget()


def run(view: str, body: str, refresh: bool, progress: Callable[[int, int, str], Any]) -> str:
    """One planner call: `body` is the request the page would post to /api/jobs, the answer its result, both
    JSON. A bad request or a planner failure comes back as {"error": "..."}: one readable line for the page."""
    try:
        params = normalise(view, json.loads(body))
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if refresh:
        planner().forget()
    try:
        return json.dumps({"result": planner().compute(view, params, lambda d, t, m: progress(d, t, m))})
    except Exception as e:  # shown to the user
        return json.dumps({"error": str(e) or type(e).__name__})
