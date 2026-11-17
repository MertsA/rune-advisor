"""Binds the JSON API's requests to the planner: validates options, builds (and reuses) a Context, calls a view."""

from __future__ import annotations

import re
from typing import Callable

from .. import views
from ..recommend.guide import MOODS
from ..recommend.tracks import ALL_TRACKS

PLAYER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,11}$")  # RuneScape display names
VIEWS = ("profile", "guide", "next", "focus")
CONTEXT_FLAGS = ("f2p", "plugin_only", "beta")


def _num(options: dict, name: str, default: float | None, lo: float, hi: float) -> float | None:
    v = options.get(name, default)
    if v in (None, ""):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not lo <= v <= hi:
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
    return v


def normalise(view: str, params: dict) -> dict:
    """The canonical, validated form of a request (also the cache key). Raises ValueError for a bad one."""
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}")
    player = str(params.get("player") or "").strip()
    if not PLAYER.match(player):
        raise ValueError("player must be a RuneScape name (letters, digits, spaces, - and _; up to 12)")
    o = params.get("options") or {}
    if not isinstance(o, dict):
        raise ValueError("options must be an object")
    group = o.get("group") or []
    if not isinstance(group, list) or not all(isinstance(g, str) and PLAYER.match(g.strip()) for g in group):
        raise ValueError("group must be a list of player names")
    out = {"player": player, "options": {**{f: bool(o.get(f)) for f in CONTEXT_FLAGS},
                                         "group": sorted(g.strip() for g in group)}}
    mood = o.get("mood") or None
    if mood is not None and (mood not in MOODS or view not in ("guide", "next")):
        raise ValueError(f"mood is one of {', '.join(MOODS)}")
    if view == "guide":
        tracks = o.get("tracks") or []
        if not isinstance(tracks, list) or any(t not in ALL_TRACKS for t in tracks):
            raise ValueError(f"tracks are among {', '.join(ALL_TRACKS)}")
        out["options"].update(tracks=[t for t in ALL_TRACKS if t in tracks], mood=mood,
                              session=_num(o, "session", None, 0.1, 1000), top=int(_num(o, "top", 10, 1, 100)))
    elif view == "next":
        out["options"].update(hours=_num(o, "hours", 2.0, 0.1, 1000), mood=mood, replan=bool(o.get("replan")),
                              max_replans=None if o.get("max_replans") in (None, "") else int(_num(o, "max_replans", None, 0, 100)))
    elif view == "focus":
        goal = str(o.get("goal") or "").strip()
        if not goal or len(goal) > 200:
            raise ValueError("goal is required (e.g. \"Thieving outfit\", \"Thieving:99\", \"item:Dragon pickaxe\")")
        out["options"]["goal"] = goal
    return out


def default_ctx_factory(player: str, flags: dict, group: list[str]):
    """The CLI's own context builder, so the app plans exactly what `radv` does."""
    import typer

    from .. import cli
    try:
        return cli._ctx(player, True, beta=flags["beta"], plugin_only=flags["plugin_only"], f2p=flags["f2p"],
                        group=group or None)
    except typer.Exit as e:  # _ctx reports a missing snapshot this way
        raise RuntimeError(str(e.exit_code)) from None


class Planner:
    """compute(view, params, progress) for a JobManager. Contexts are kept per (player, context options): building
    one fetches and merges every source, and the views reuse it; `forget` drops them (an explicit refresh)."""

    def __init__(self, ctx_factory: Callable = default_ctx_factory):
        self._factory = ctx_factory
        self._ctxs: dict[tuple, object] = {}

    def forget(self) -> None:
        self._ctxs.clear()

    def context(self, params: dict, progress, reuse: bool = True):
        o = params["options"]
        key = (params["player"].lower(), o["f2p"], o["plugin_only"], o["beta"], tuple(o["group"]))
        if not reuse or key not in self._ctxs:
            progress(0, 0, "loading player data")
            self._ctxs[key] = self._factory(params["player"], {f: o[f] for f in CONTEXT_FLAGS}, o["group"])
        return self._ctxs[key]

    def compute(self, view: str, params: dict, progress) -> dict:
        ctx = self.context(params, progress)
        progress(0, 0, f"building {view}")
        o = params["options"]
        if view == "profile":
            return views.profile_view(ctx)
        if view == "guide":
            return views.guide_view(ctx, o["tracks"] or None, o["mood"], o["session"], o["top"], progress)
        if view == "next":
            return views.next_view(ctx, o["hours"], o["mood"], o["replan"], o["max_replans"], progress)
        return views.focus_view(ctx, o["goal"], progress)
