"""`radv` command line."""

from __future__ import annotations

import json
from datetime import datetime
from functools import cache
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .config import ARCHIVE_DIR, load_settings
from .cost import CostModel
from .http import CachedClient
from .models import QuestStatus
from .player import current_quest_titles, find_snapshot, load_group, load_player, player_from_snapshot
from .resources import load_pool
from .recommend import Context, combine, rank
from .recommend.guide import MOODS
from .recommend.tracks import ALL_TRACKS
from .sources import plugin_db, wiki
from .views import charge_steps, guide_sections, next_picks, session_prep

app = typer.Typer(no_args_is_help=True, help="RuneScape 3 planning helper")
console = Console()


IRONMAN_TRACK_WEIGHTS = {"gear": 1.5, "milestones": 0.5}
MAX_GUIDE_NEEDS = 4  # "left:" items shown per guide entry

BETA_OPT = typer.Option(False, "--beta", help="Read plugin data from the plugin beta client")
PLUGIN_ONLY_OPT = typer.Option(False, "--plugin-only",
                               help="Use only the plugin snapshot (no hiscores/Adventurer's Log/overrides), e.g. for a reset beta character")
SNAPSHOT_OPT = typer.Option(None, "--snapshot", help="Plan from an archived snapshot JSON file (implies --plugin-only)")
F2P_OPT = typer.Option(False, "--f2p", help="Plan for a free-to-play account (also: members = false in rune_advisor.toml)")
GROUP_OPT = typer.Option(None, "--group", "-g",
                         help="Group Ironman member (repeatable; also: group = [...] in rune_advisor.toml)")


def _ctx(player_name: str, use_plugin: bool, beta: bool = False, plugin_only: bool = False,
         snapshot: Path | None = None, f2p: bool = False, group: list[str] | None = None) -> Context:
    settings = load_settings(beta=beta)
    if settings.coins_per_hour:
        from .recommend import gathering
        gathering.COINS_PER_HOUR = float(settings.coins_per_hour)
    if group:
        settings.group = [player_name, *group]
    members = False if f2p else settings.player(player_name).members
    if members is None:
        members = settings.members
    client = CachedClient()
    if snapshot or plugin_only:
        snap = json.loads(snapshot.read_text()) if snapshot else find_snapshot(settings, player_name)
        if snap is None:
            raise typer.Exit(f"no plugin snapshot for {player_name} in {settings.plugin_db}")
        player = player_from_snapshot(player_name, snap, current_quest_titles(client, player_name))
        player.ironman = settings.ironman or settings.in_group(player_name)
    else:
        player = load_player(client, player_name, settings, use_plugin=use_plugin)
    return context_for(player, settings, client, members)


def context_for(player, settings, client: CachedClient, members: bool | None = None, quest_info=None) -> Context:
    """The planning Context for a loaded player: group mates, membership, cost model and the wiki's tables.
    `quest_info` replaces the per-quest wiki lookup (the web planner answers it from one prefetched table)."""
    player_name = player.name
    if settings.in_group(player_name):
        load_group(player, settings)
        mates = ", ".join(m for m in settings.group if m.lower() != player_name.lower())
        player.notes.append(f"group ironman with {mates}: their items can be moved over via group storage or trade")
    from . import claims
    if weekly := claims.available(player):
        player.notes.append("weekly claims unlocked (task set rewards, counted as sources): "
                            + ", ".join(f"{c.qty:,} {c.item} from {c.npc}" for c in weekly))
    # Explicit setting wins; otherwise a snapshot taken on a free world means plan for F2P.
    if members is not None:
        player.members = members
    elif player.members_world is not None:
        player.members = player.members_world
    if not player.members:
        player.notes.append("free-to-play: members' content excluded; rates from the free-to-play guides")
    # Simulated methods (mechanics/: Woodcutting, Mining, Fishing, Thieving...) replace the guide rows they
    # cover, for this player's levels, tools and boosts.
    from . import mechanics
    cost = CostModel.default(settings.xp_rates, members=player.members, player=player,
                             extra_methods=settings.methods + mechanics.all_rows(player))
    # The export has no achievement categories; the wiki's main/subcategory fills them ("Area Tasks / Elite Ardougne").
    achievement_rows = wiki.fetch_all_achievements(client)
    categories = {r["name"]: " / ".join(c for c in (r.get("maincategory"), r.get("subcategory")) if c)
                  for r in achievement_rows if r.get("name")}
    for a in player.achievements.values():
        a.category = a.category or categories.get(a.name) or None
    if cost.excluded:
        player.notes.append(f"{len(cost.excluded)} wiki training methods skipped for unmet quests/levels "
                            f"(e.g. {cost.excluded[0][1]}: needs {', '.join(cost.excluded[0][2])})")
    return Context(
        player=player,
        cost=cost,
        quest_info=quest_info or cache(lambda t: wiki.fetch_quest(client, t)),
        resources=load_pool(player, settings.resources),
        achievement_reqs=wiki.fetch_achievement_requirements(client, ironman=player.ironman),
        farming=settings.farming,
        skill_weights=settings.skill_weights,
        achievement_descriptions={r["name"]: json.loads(r["json"] or "{}").get("description") or ""
                                  for r in achievement_rows},
        members_only_achievements=set() if player.members else wiki.members_only_achievements(client),
        prices=None if player.ironman else _prices(client),
        unique_value_hours=settings.unique_value_hours,
        prefer_exquisite_urns=settings.prefer_exquisite_urns,
    )


def _prices(client: CachedClient):
    """GE prices for a normal account's supplies; None if the wiki can't be reached (supplies then cost no time)."""
    from .prices import Prices
    try:
        return Prices(client)
    except Exception as e:  # network or format trouble shouldn't stop planning
        console.print(f"[dim]note: no GE prices ({e}); bought supplies aren't costed[/dim]")
        return None


@app.command("ui")
def ui_cmd(no_window: bool = typer.Option(False, "--no-window", help="Serve and print the URL (with its secret) "
                                          "instead of opening the window; for development"),
           port: int = typer.Option(0, help="Port for --no-window (default: random)")):
    """The planner window (needs the `ui` extra: pywebview). Closing the window ends the process."""
    from .app import run
    raise typer.Exit(run(no_window=no_window, port=port))


@app.command()
def profile(player: str, plugin: bool = typer.Option(True, help="Merge in-game plugin snapshot"),
            beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT, snapshot: Path = SNAPSHOT_OPT,
            f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """Skills, quest points and RuneScore."""
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    p = ctx.player
    journal = f" (journal: {p.journal_quest_points})" if p.journal_quest_points is not None else ""
    t = Table(title=f"{p.name}  QP {p.quest_points}/{p.max_quest_points}{journal}  RuneScore {p.runescore}")
    cols = ("Skill", "Level", "XP", "Rank") + (("Bonus XP",) if p.bonus_xp else ())
    for col in cols:
        t.add_column(col, justify="right" if col != "Skill" else "left")
    for s in sorted(p.skills.values(), key=lambda s: s.xp):
        bonus = (f"{p.bonus_xp[s.name]:,}" if p.bonus_xp.get(s.name) else "-",) if p.bonus_xp else ()
        t.add_row(s.name, str(s.level), f"{s.xp:,}", f"{s.rank:,}" if s.rank else "-", *bonus)
    console.print(t)
    if p.rested_xp is not None:
        console.print(f"rested xp: {p.rested_xp:,} (cash in with Aster at Fort Forinthry)")
    if not p.bonus_xp and p.rested_xp is None:
        console.print("[dim]bonus xp / rested xp unknown: set bonus_xp / rested_xp in overrides.toml[/dim]")
    for n in p.notes:
        console.print(f"[dim]note: {n}[/dim]")


@app.command()
def pull(player: str, archive: bool = typer.Option(True, help="Save the snapshot under state/archive")):
    """Fetch a character's snapshot from the export API ([api] in rune_advisor.toml) and archive it."""
    from datetime import date

    from . import api_client

    settings = load_settings()
    if not settings.api_url or not settings.api_token:
        raise typer.Exit("set [api] url and token in rune_advisor.toml first")
    snap = api_client.fetch_snapshot(settings, player, max_age=0)
    if snap is None:
        raise typer.Exit(f"the API has no state for {player} (characters: "
                         f"{', '.join(p['player'] for p in api_client.players(settings)) or 'none'})")
    for note in snap.get("notes") or []:
        typer.echo(f"note: {note}")
    skills = snap.get("skills") or {}
    total = sum(int(v.get("xp") or 0) for v in skills.values())
    typer.echo(f"{snap.get('player')}: {len(skills)} skills, total xp {total:,}, {len(snap.get('inventories') or {})} inventories, "
               f"updated {snap.get('exportedAt')}")
    if archive:
        out = ARCHIVE_DIR / f"{date.today().isoformat()}_{snap.get('player')}_api"
        n = 1
        while out.exists():
            n += 1
            out = out.with_name(f"{date.today().isoformat()}_{snap.get('player')}_api_{n}")
        out.mkdir(parents=True)
        path = out / f"snapshot_{snap.get('player')}.json"
        path.write_text(json.dumps(snap))
        typer.echo(f"archived to {path}")


@app.command()
def events(players: list[str] = typer.Argument(None, help="Players to poll (default: the group's members accounts)"),
           fetch: bool = typer.Option(True, help="Poll the Adventurer's Log before showing the log"),
           tail: int = typer.Option(5, help="How many of the latest events to show")):
    """Poll the Adventurer's Log events feed and append what's new to state/events/<player>.jsonl.

    The feed only shows the last 20 events, so a complete log needs polling while the player is online
    (every 10 minutes is enough: the API caches that long)."""
    from . import events as ev

    settings = load_settings()
    if not players:
        players = [m for m in settings.group if settings.player(m).members is not False]
        if not players:
            raise typer.Exit("name a player, or set group = [...] in rune_advisor.toml")
    client = CachedClient()
    for name in players:
        path = ev.log_path(name)
        if fetch:
            try:
                m = ev.poll(client, name, path)
            except httpx.HTTPError as e:
                typer.echo(f"{name}: poll failed ({e})")
                continue
            if m is None:
                typer.echo(f"{name}: free-to-play, no events feed")
                continue
            new = [e for e in m.new if not e.get("gap")]
            flags = (" (GAP: more than 20 events since the last poll, some are missing)" if m.gap else "") + \
                    (" (aligned on date+type: a stored event's text changed)" if m.loose else "")
            typer.echo(f"{name}: {len(new)} new event{'s' if len(new) != 1 else ''}{flags}")
        log = ev.read_log(path)
        typer.echo(f"{name}: {sum(1 for e in log if not e.get('gap'))} events in {path}"
                   f"{', ' + str(sum(1 for e in log if e.get('gap'))) + ' gap(s)' if any(e.get('gap') for e in log) else ''}")
        for e in log[-tail:]:
            typer.echo(f"  {e['at'] + '  -- gap --' if e.get('gap') else e['date'] + '  ' + e['text']}")


@app.command()
def quests(player: str, all: bool = typer.Option(False, "--all", help="Include completed quests"),
           plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
           snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT):
    """Quest status after merging all sources."""
    p = _ctx(player, plugin, beta, plugin_only, snapshot, f2p).player
    journal = f" (journal: {p.journal_quest_points})" if p.journal_quest_points is not None else ""
    t = Table(title=f"{p.name} quests  QP {p.quest_points}/{p.max_quest_points}{journal}")
    for col in ("Quest", "Status", "QP", "Eligible", "Source"):
        t.add_column(col)
    for q in sorted(p.quests.values(), key=lambda q: q.title):
        if all or not q.done or q.qp_earned:
            status = f"{q.status.value} (replay, QP earned)" if q.qp_earned else q.status.value
            t.add_row(q.title, status, str(q.quest_points), "yes" if q.eligible else "no", q.source)
    console.print(t)


@app.command()
def recommend(player: str,
              track: list[str] = typer.Option(None, "--track", "-t", help=f"One of {', '.join(ALL_TRACKS)}"),
              top: int = typer.Option(8, "-n"),
              plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
              snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """Rank goals per track by value per estimated hour, then overall."""
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    names = track or list(ALL_TRACKS)
    per_track = {n: rank(ALL_TRACKS[n].candidates(ctx)) for n in names}
    for n, cands in per_track.items():
        t = Table(title=f"{n}: {ALL_TRACKS[n].description}")
        for col in ("#", "Goal", "Value", "Hours", "Value/h", "Notes"):
            t.add_column(col)
        if not cands:
            t.add_row("-", "[dim]no candidates (missing data?)[/dim]", "", "", "", "")
        for i, c in enumerate(cands[:top], 1):
            t.add_row(str(i), c.title + (" [yellow](est. incomplete)[/yellow]" if c.uncertain else ""),
                      f"{c.value:g} {c.unit}", f"{c.cost_hours:.1f}", f"{c.efficiency:.2f}", "; ".join(c.details))
        console.print(t)
    if len(per_track) > 1:
        t = Table(title="overall (each track normalised to its best = 1.0)")
        for col in ("#", "Score", "Track", "Goal", "Hours"):
            t.add_column(col)
        # Ironmen: favour the classic gear grinds over skilling milestones unless rune_advisor.toml says otherwise.
        weights = {**(IRONMAN_TRACK_WEIGHTS if ctx.player.ironman else {}), **load_settings().track_weights}
        for i, (score, c) in enumerate(combine(per_track, weights)[:top], 1):
            t.add_row(str(i), f"{score:.2f}", c.track, c.title, f"{c.cost_hours:.1f}")
        console.print(t)
    for n in ctx.player.notes:
        console.print(f"[dim]note: {n}[/dim]")


@app.command()
def guide(player: str,
          track: list[str] = typer.Option(None, "--track", "-t", help=f"One of {', '.join(ALL_TRACKS)}"),
          mood: str = typer.Option(None, "--mood", help=f"Only entries tagged {', '.join(MOODS)}"),
          session: float = typer.Option(None, "--session", help="Only entries that fit in this many hours"),
          top: int = typer.Option(5, "-n"), verbose: bool = typer.Option(False, "-v", help="Show every step"),
          plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
          snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """Short per-track suggestions: what to do, why, what's left. `recommend` shows the full steps."""
    if mood and mood not in MOODS:
        raise typer.BadParameter(f"mood is one of {', '.join(MOODS)}")
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    names = track or list(ALL_TRACKS)
    sections = guide_sections(ctx, names, mood, session)
    for n, entries in sections.items():
        console.rule(f"[bold]{n}[/bold]: {ALL_TRACKS[n].description}", align="left")
        if not entries:
            console.print("  [dim]nothing here (missing data, or filtered out)[/dim]")
        for i, e in enumerate(entries[:top], 1):
            conf = "" if e.confidence == "modelled" else f" [yellow]({e.confidence})[/yellow]"
            console.print(f"{i:>2}. [bold]{e.headline}[/bold]  ~{e.hours:.1f}h  {e.value}  "
                          f"[dim]{', '.join(sorted(e.tags))}[/dim]{conf}", markup=True, highlight=False)
            lines = [("why", e.why)] if e.why else []
            if e.remaining:
                lines.append(("left", "; ".join(e.remaining[:MAX_GUIDE_NEEDS])
                              + (f" (+{len(e.remaining) - MAX_GUIDE_NEEDS} more)" if len(e.remaining) > MAX_GUIDE_NEEDS else "")))
            if e.unsure:
                lines.append(("unsure", "; ".join(e.unsure[:MAX_GUIDE_NEEDS])))
            for label, text in lines:
                console.print(f"      [dim]{label}:[/dim] {text}", markup=True, highlight=False)
            for step in charge_steps(ctx, e.time_to_play if "combat" in e.tags else 0.0, session):
                console.print(f"      [dim]prep:[/dim] {step}", markup=False, highlight=False)
            if verbose:
                for step in e.steps:
                    console.print(f"        - {step}", markup=False, highlight=False)
    for n in ctx.player.notes:
        console.print(f"[dim]note: {n}[/dim]")


@app.command("next")
def next_session(player: str, hours: float = typer.Option(2.0, "--hours", help="Session length"),
                 mood: str = typer.Option(None, "--mood", help=f"Only entries tagged {', '.join(MOODS)}"),
                 replan: bool = typer.Option(False, "--replan", help="Re-plan every track after each pick that "
                                             "reaches a level or finishes a quest (slow: one pass per such pick)"),
                 max_replans: int = typer.Option(None, "--max-replans", help="Cap the re-plans (default: no cap)"),
                 plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
                 snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """Pack a session: cash-ins first, then each track's best entry that fits, in turn."""
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    picks = next_picks(ctx, hours, mood, replan, max_replans)
    used = sum(p.extra_hours for p in picks)
    console.rule(f"[bold]next {hours:g}h for {player}[/bold] (~{used:.1f}h planned)", align="left")
    if not picks:
        console.print("  [dim]nothing fits: try more hours or another mood[/dim]")
    for i, p in enumerate(picks, 1):
        e = p.entry
        conf = "" if e.confidence == "modelled" else f" [yellow]({e.confidence})[/yellow]"
        play = e.time_to_play
        extra = "" if p.extra_hours == play else " (done on the way)"
        net = f" [dim](~{e.hours:.1f}h net of the xp it earns)[/dim]" if abs(play - e.hours) >= 0.05 else ""
        console.print(f"{i:>2}. [bold]{e.headline}[/bold]  ~{play:.1f}h{extra}{net}  [dim]{e.track}[/dim]{conf}", highlight=False)
        if e.remaining:
            console.print("      [dim]left:[/dim] " + "; ".join(e.remaining[:MAX_GUIDE_NEEDS]), highlight=False)
        if p.shares:
            console.print("      [dim]shared:[/dim] " + "; ".join(p.shares), highlight=False)
    # One prep for the session's combat: the picks share the charge pack.
    for step in session_prep(ctx, picks, hours):
        console.print(f"  [dim]prep:[/dim] {step}", markup=False, highlight=False)


@app.command()
def focus(player: str, goal: str = typer.Argument(..., help='"Thieving outfit", "Thieving:99", an achievement, or "item:<name>"'),
          top: int = typer.Option(8, "-n"),
          plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
          snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """Pick one goal: the activities that advance it, ranked by what else each one advances."""
    from .recommend.focus import focus as make_focus, parse_goal
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    try:
        g = parse_goal(ctx, goal)
    except ValueError as e:
        raise typer.BadParameter(str(e))

    def show(f, indent=""):
        console.rule(f"[bold]{f.goal}[/bold]", align="left")
        console.print(f"{indent}{f.summary}", markup=False, highlight=False)
        for n in f.other_needs:
            console.print(f"{indent}  [dim]needs:[/dim] {n}", highlight=False)
        for n in f.route:
            console.print(f"{indent}  [dim]route:[/dim] {n}", highlight=False)
        for n in f.prep:
            console.print(f"{indent}  [dim]prep:[/dim] {n}", markup=False, highlight=False)
        if f.any_method:
            console.print(f"{indent}  [dim]any method also:[/dim] " + "; ".join(f.any_method), highlight=False)
        for i, o in enumerate(f.options[:top], 1):
            hours = f"  ~{o.hours:.1f}h" if o.hours is not None else ""
            console.print(f"{indent}{i:>2}. [bold]{o.activity}[/bold]  {o.rate}{hours}", highlight=False)
            if o.extras:
                console.print(f"{indent}      [dim]also:[/dim] " + "; ".join(o.extras), highlight=False)
        for sub in f.sections:
            show(sub, indent)

    show(make_focus(ctx, g))


@app.command()
def gaps(player: str, items: list[str] = typer.Argument(None, help="Items to plan (default: every track's goals)"),
         top: int = typer.Option(25, "-n"),
         plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
         snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """Items plans need that nothing known makes, sells or drops (their hours are missing from those plans),
    by how many goals each one blocks: the data gaps worth filling first."""
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    if items:
        for item in items:
            ctx.gatherer.plan({item: 1})
    else:
        for t in ALL_TRACKS.values():
            t.candidates(ctx)
    found = sorted(ctx.gatherer.gaps.items(), key=lambda kv: (-len(kv[1][1]), -kv[1][0]))
    if not found:
        console.print("no unsourced inputs")
    for item, (units, goals) in found[:top]:
        some = ", ".join(sorted(goals)[:3]) + (f" (+{len(goals) - 3} more)" if len(goals) > 3 else "")
        console.print(f"[bold]{item}[/bold]  {units:,} needed  [dim]for {len(goals)}: {some}[/dim]", highlight=False)


@app.command()
def rates(level: int = typer.Option(None, help="Show rates at this level instead of each skill's own level"),
          player: str = typer.Option(None, help="Use this player's levels"), f2p: bool = F2P_OPT):
    """XP/hour the cost model uses per skill, and where each rate comes from. With --player, the
    player's simulated methods (mechanics/) join and replace the guide rows they cover."""
    if player:
        ctx = _ctx(player, True, f2p=f2p)
        cost, levels = ctx.cost, ctx.player.skills
    else:
        cost, levels = CostModel.default(load_settings().xp_rates, members=not f2p), {}
    t = Table(title="xp rates (method rows: simulated = mechanics, quoted = training guide; guess = hand-written; "
                    "override = rune_advisor.toml)")
    for col in ("Skill", "Level", "XP/h", "Basis", "Method"):
        t.add_column(col)
    for skill in sorted(cost.rate_source):
        lvl = level or (levels[skill].level if skill in levels else 99)
        band = max((lv for (s, lv) in cost.methods if s == skill and lv <= lvl), default=None)
        src = cost.rate_source[skill]
        how = cost.band_basis.get((skill, band), src) if src == "wiki" and band is not None else src
        t.add_row(skill, str(lvl), f"{cost.rate(skill, lvl):,.0f}", how, cost.method(skill, lvl) or "")
    console.print(t)
    if cost.replaced:
        console.print(f"[dim]{len(cost.replaced)} guide rows replaced by simulations "
                      f"(e.g. {cost.replaced[0][1]} -> {cost.replaced[0][2]}).[/dim]")
    console.print("[dim]Hitpoints is derived from the best combat skill (0.33x its xp), unless overridden.[/dim]")


def _skill(name: str) -> str:
    """A skill as typed ("runecrafting", "Constitution") -> the planner's name ("Runecraft", "Hitpoints")."""
    from .xp import SKILL_ALIASES

    name = name.strip().title()
    return SKILL_ALIASES.get(name, name)


@app.command()
def methods(player: str, skill: str, level: int = typer.Option(None, help="Plan at this level instead of the player's"),
            plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
            snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """Training methods for a skill with the drops that matter to this player (needed items, charms,
    uniques they don't own)."""
    skill = _skill(skill)
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    level = level or ctx.player.level(skill)
    t = Table(title=f"{skill} methods at {level} for {ctx.player.name}")
    for col in ("Method", "XP/h", "Kills/h", "Drops that matter"):
        t.add_column(col)
    from .drops import kills_per_hour
    from .requirements import missing_items
    for m, rate, drops in ctx.method_drops(skill, level):
        kph = kills_per_hour(m, skill, rate)
        achs = {a for src in m.get("drop_sources") or [] for a in ctx.activities.achievements_at(src)}
        notes = [d.describe() for d in drops[:5]] + ([f"[yellow]{m['byproducts']}[/yellow]"] if m.get("byproducts") else [])
        if absent := missing_items(m.get("requirements"), ctx.player):
            notes.append(f"[yellow]needs {', '.join(absent)} (not in your bank export; maybe on the tool belt)[/yellow]")
        if achs:
            notes.append(f"[green]advances {len(achs)} achievements ({sum(rs for _, rs in achs)} RS): "
                         + ", ".join(n for n, _ in sorted(achs)[:4]) + ("..." if len(achs) > 4 else "") + "[/green]")
        t.add_row(m["method"], f"{rate:,.0f}", f"{kph:,.0f}" if kph else "-",
                  "\n".join(notes) or ("[dim]no drop data[/dim]" if not m.get("drop_sources") else ""))
    console.print(t)


@app.command()
def gp(player: str, skill: str, level: int = typer.Option(None, help="At this level instead of the player's"),
       plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
       snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """GP/h of each method's yields at GE prices (untradeables via their best processing): a sanity
    check on rates, e.g. a level-105 node that makes less than lower ones is suspect."""
    skill = _skill(skill)
    from .cost import typical_rate
    from .prices import Prices

    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    level = level or ctx.player.level(skill)
    prices = Prices()
    t = Table(title=f"{skill} methods at {level} for {ctx.player.name}: yields at GE prices ({prices.updated})")
    for col in ("Method", "From", "XP/h", "GP/h", "Yields"):
        t.add_column(col)
    rows = []
    for m in ctx.cost.available_methods(skill, level):
        if m.get("yields"):
            total, notes = prices.gp_per_hour(m["yields"])
            rows.append((total, m, notes))
    for total, m, notes in sorted(rows, key=lambda r: -r[0]):
        t.add_row(m["method"], str(m["level_min"]), f"{typical_rate(m):,.0f}", f"{total:,.0f}", "\n".join(notes))
    console.print(t if rows else f"no {skill} methods with yields at {level}")


@app.command()
def price(item: str):
    """GE price of an item (untradeables valued by their best processing chain) and its daily volume."""
    from .prices import Prices

    v = Prices().value(item)
    if v is None:
        raise typer.Exit(f"no GE price for {item}, nor a processing chain to one")
    vol = f", {v.volume:,} traded/day" if v.volume is not None else ""
    console.print(f"{item}: {v.gp:,.0f} gp ({v.how}{vol})")


@app.command()
def drops(item: str, player: str = typer.Option(None, help="Estimate hours per drop for this player")):
    """Where an item drops (wiki drop tables), most likely first, with monsters' xp per kill and, with
    --player, the expected hours to get one (guide kill rates, else estimated from their gear)."""
    from .drops import bosses, estimated_kills_per_hour, kills_per_hour, sources_of, tables
    monsters = tables()["monsters"]
    ctx = _ctx(player, True) if player else None
    guide_kph = {}
    if ctx:
        from .cost import typical_rate
        for skill, rows in ctx.cost.wiki_rows.items():
            for r in ctx.cost.available_methods(skill, ctx.player.level(skill)):
                if kph := kills_per_hour(r, skill, typical_rate(r)):
                    for src in r.get("drop_sources") or []:
                        guide_kph[src] = max(guide_kph.get(src, 0), kph)
    rows = sorted(sources_of(item), key=lambda r: -(r[1] if isinstance(r[1], (int, float)) else 0))
    t = Table(title=f"{item}: {len(rows)} sources")
    for col in ("Source", "Chance", "Qty", "Combat xp/kill") + (("Kills/h", "Hours per drop"),) * bool(ctx):
        if isinstance(col, tuple):
            for c in col:
                t.add_column(c)
        else:
            t.add_column(col)
    for src, chance, qty in rows:
        numeric = isinstance(chance, (int, float)) and chance
        rate = f"1/{1 / chance:,.0f}" if numeric else str(chance or "?")
        xp = monsters.get(src, {}).get("xp")
        cells = [src, rate, f"{qty:g}", f"{xp:,.0f}" if xp else "-"]
        if ctx:
            kph, how = guide_kph.get(src), "guide"
            if not kph and (est := estimated_kills_per_hour(ctx.player, src, ctx.combat_style)):
                kph, how = est[0], "est."
            cells += [f"{kph:,.0f} ({how})" if kph else ("boss" if src in bosses() else "-"),
                      f"{1 / (kph * chance * qty):,.1f}" if kph and numeric else "-"]
        t.add_row(*cells)
    console.print(t)


@app.command()
def gear(player: str, style: str = typer.Argument(None, help="melee, ranged, magic or necromancy (default: all)"),
         plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT, snapshot: Path = SNAPSHOT_OPT):
    """Best owned loadout per combat style (wiki equipment stats; switchable variants count as owned)."""
    from .gear import STYLES, best_loadout, gear_factor
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot)
    for st in ([style] if style else list(STYLES)):
        lo = best_loadout(ctx.player, st)
        t = Table(title=f"{st}: ability damage {lo.ability_damage:,.0f} (tier {lo.weapon_tier}), damage bonus "
                        f"{lo.damage_bonus:.1f}, armour {lo.armour:,.0f}; gear factor {gear_factor(ctx.player, st):.2f}")
        for col in ("Slot", "Item", "Tier"):
            t.add_column(col)
        for slot, i in lo.slots.items():
            t.add_row(slot, i.name, str(i.tier))
        console.print(t)


@app.command()
def joint(player: str, goals: list[str] = typer.Argument(None, help='Goals like "Summoning:99" (default: top milestones)'),
          top: int = typer.Option(5, "-n", help="With no goals given, plan this many top milestone goals"),
          verbose: bool = typer.Option(False, "-v", help="Show each goal's steps"),
          plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
          snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """Plan several level goals together: shared bank/lamps are used once, and side xp from one goal
    (e.g. Slayer xp while gathering pure essence) shortens the others."""
    from .recommend.joint import joint_plan
    from .recommend.tracks import MilestoneTrack
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    if goals:
        parsed = [(g.rsplit(":", 1)[0], int(g.rsplit(":", 1)[1])) for g in goals]
    else:
        cands = rank(MilestoneTrack.candidates(ctx))
        parsed = [(c.title.split(" ", 1)[1], int(c.title.split(" ", 1)[0])) for c in cands
                  if c.title.split(" ", 1)[0].isdigit()][:top]
    plan = joint_plan(ctx, parsed)
    t = Table(title=f"{ctx.player.name}: {len(parsed)} goals together ({plan.hours:.1f}h, "
                    f"{plan.hours_alone:.1f}h if planned separately)")
    for col in ("#", "Goal", "Alone", "Together", "Side xp already in"):
        t.add_column(col)
    for i, st in enumerate(plan.steps, 1):
        t.add_row(str(i), f"{st.target} {st.skill}", f"{st.hours_alone:.1f}h", f"{st.plan.hours:.1f}h",
                  f"{st.credited_xp:,.0f}" if st.credited_xp else "")
    console.print(t)
    if verbose:
        for st in plan.steps:
            console.print(f"[bold]{st.target} {st.skill}[/bold]")
            for line in st.plan.steps:
                console.print(f"  {line}")


@app.command()
def roadmap(player: str, target: str = typer.Argument("Completionist Cape", help="Achievement to plan for"),
            verbose: bool = typer.Option(False, "-v", help="Show every activity and skill step"),
            plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
            snapshot: Path = SNAPSHOT_OPT):
    """Everything left for a big achievement, with shared needs merged: skills planned together,
    quests once, then the remaining activities by cost."""
    from .recommend.roadmap import roadmap as build
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot)
    rm = build(ctx, target)
    console.print(f"[bold]{target}[/bold] for {ctx.player.name}: {len(rm.achievements)} unfinished achievements, "
                  f"~{rm.hours:,.0f}h ({rm.uncertain_hours:,.0f}h of it rough)")
    console.print(f"  skills ~{rm.skill_hours:,.0f}h, quests ~{rm.quest_hours:,.0f}h, other activities ~{rm.activity_hours:,.0f}h")
    if rm.calendar_hours:
        console.print(f"  plus ~{rm.calendar_hours / 24:,.0f} days of waiting on farming growth and Managing Miscellania "
                      "(runs in the background while you do the rest)")
    if rm.joint:
        t = Table(title="Skills (planned together)")
        for col in ("Goal", "From", "Hours", "Side xp in", "Notes"):
            t.add_column(col)
        for st in rm.joint.steps:
            notes = "; ".join(n for n in (
                "materials partly unmodelled" if st.plan.materials_unmodelled or st.plan.materials_short else "",
                f"{st.plan.calendar_hours / 24:,.0f} days of waiting" if st.plan.calendar_hours else "",
                f"{st.plan.gather_coins / 1e6:,.1f}m gp" if st.plan.gather_coins >= 1e6 else "") if n)
            t.add_row(f"{st.target} {st.skill}", str(ctx.player.level(st.skill)), f"{st.plan.hours:,.1f}",
                      f"{st.credited_xp:,.0f}" if st.credited_xp else "", notes)
        console.print(t)
    if rm.quest_plan:
        console.print(f"Quests (~{rm.quest_hours:.1f}h): " + ", ".join(sorted(rm.quests)))
    t = Table(title="Other activities, cheapest first")
    for col in ("Achievement", "Hours", "", "What"):
        t.add_column(col)
    for a in sorted(rm.activities, key=lambda a: a.plan.hours):
        steps = a.plan.steps if verbose else a.plan.steps[:2]
        t.add_row(a.achievement, f"{a.plan.hours:,.1f}", "~" if a.plan.uncertain else "",
                  "; ".join(steps)[:160] + ("..." if not verbose and len(a.plan.steps) > 2 else ""))
    console.print(t)


@app.command("sim")
def simulate(player: str, skill: str = typer.Argument("woodcutting", help="woodcutting, mining or rituals"),
             level: int = typer.Option(None, help="Simulate at this level instead of the player's"),
             method: str = typer.Option(None, help='Mining play style, e.g. "AFK", "Semi-AFK", "4-Ticking + Rockertunities"'),
             top: int = typer.Option(15, "-n"), plugin: bool = True, beta: bool = BETA_OPT,
             plugin_only: bool = PLUGIN_ONLY_OPT, snapshot: Path = SNAPSHOT_OPT):
    """Woodcutting/Mining/Necromancy ritual rates from the wiki's mechanics for this player's levels and gear."""
    from . import sim
    p = _ctx(player, plugin, beta, plugin_only, snapshot).player
    if skill.lower().startswith("rit"):
        _simulate_rituals(p, level, top)
        return
    rates = sim.woodcutting_rates(p, level) if skill.lower().startswith("wood") else \
        sim.mining_rates(p, level, method or sim.DEFAULT_MINING_METHOD)
    rates = sorted(rates, key=lambda r: -r.xp_per_hour)[:top]
    if not rates:
        console.print("nothing to simulate at this level")
        return
    r0 = rates[0]
    t = Table(title=f"{skill} for {p.name} at {r0.level}: {r0.tool}{' (assumed: set it in overrides.toml)' if r0.assumed_tool else ''}"
                    f"{'; ' + r0.notes if r0.notes else ''}")
    for col in ("Tree/rock", "Item", "Success", "Items/h", "XP/h"):
        t.add_column(col)
    for r in rates:
        t.add_row(r.activity, r.item, f"{r.chance:.1%}" if r.chance is not None else "-", f"{r.items_per_hour:,.0f}",
                  f"{r.xp_per_hour:,.0f}")
    console.print(t)


@app.command("discoveries")
def discoveries_cmd(player: str, plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
                    snapshot: Path = SNAPSHOT_OPT):
    """Invention blueprints still to discover: ready now (boosted by the best potion in the bank), gated, and above
    the boosted level, each with its discovery xp."""
    from .invention import GATE_TEXT, discoveries
    p = _ctx(player, plugin, beta, plugin_only, snapshot).player
    d = discoveries(p)
    if d is None:
        console.print("no blueprint state in the export (the all-blueprints achievement's tests are missing)")
        return
    boost = f" with {'an' if d.potion[0] in 'AEIOU' else 'a'} {d.potion} (+{d.boost})" if d.potion else " (no Invention potion in the bank)"
    console.print(f"{p.name}: Invention {p.level('Invention')}, discovering at {d.level}{boost}")
    t = Table(title=f"ready now: {len(d.ready)} blueprints, {d.xp:,.0f} xp")
    for col in ("Blueprint", "Level", "XP"):
        t.add_column(col)
    for b in d.ready:
        t.add_row(b["name"], str(b["level"]), f"{b['xp']:,.0f}")
    console.print(t)
    if d.gated:
        t = Table(title=f"at your level but gated: {len(d.gated)} blueprints, {sum(b['xp'] for b in d.gated):,.0f} xp")
        for col in ("Blueprint", "Level", "XP", "Needs"):
            t.add_column(col)
        for b in d.gated:
            t.add_row(b["name"], str(b["level"]), f"{b['xp']:,.0f}", GATE_TEXT.get(b.get("gate", "other"), "another unlock"))
        console.print(t)
    if d.locked:
        t = Table(title=f"above {d.level}: {len(d.locked)} blueprints, {sum(b['xp'] for b in d.locked):,.0f} xp")
        for col in ("Blueprint", "Level", "XP", "Needs"):
            t.add_column(col)
        for b in sorted(d.locked, key=lambda b: (b["level"], b["name"])):
            t.add_row(b["name"], str(b["level"]), f"{b['xp']:,.0f}", GATE_TEXT.get(b["gate"], "") if b.get("gate") else "")
        console.print(t)
    if d.unknown:
        console.print(f"blueprints the data lacks: {', '.join(d.unknown)}")


@app.command("augment")
def augment_cmd(player: str, top: int = typer.Option(8, "-n", help="Invention methods to list"),
                plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
                snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """What to augment: the best owned gear per combat style and per skilling tool, the Invention xp/h each earns
    at the player's methods (siphoned), and the materials the augmentors and siphons need."""
    from .cost import typical_rate
    from .mechanics import item_xp
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    p = ctx.player
    inv = p.level("Invention")
    level = item_xp.siphon_level(inv)
    console.print(f"{p.name}: Invention {inv}, item level cap {item_xp.item_level_cap(inv)}"
                  + (f", siphon at item level {level} ({item_xp.invention_per_item_xp(inv, 80):.2f} Invention xp per item xp at tier 80)"
                     if level else ": siphons need Invention 27"))
    rows = sorted(ctx.cost.wiki_rows.get("Invention", []), key=typical_rate, reverse=True)
    best_for: dict[str, dict] = {}
    for r in rows:
        key = r.get("base_skill") or ""
        if key and key not in best_for:
            best_for[key] = r
    t = Table(title="gear to augment (best owned loadout per style)")
    for col in ("Style", "Slot", "Item", "Tier", "Status"):
        t.add_column(col)
    to_augment: list[tuple[float, str]] = []  # (the style's best Invention xp/h, item)
    from .mechanics.combat import GUIDE_STYLE
    style_rate = {}
    for skill, r in best_for.items():
        style = GUIDE_STYLE.get(skill)
        if style:
            style_rate[style] = max(style_rate.get(style, 0.0), typical_rate(r))
    for style in ("melee", "ranged", "magic", "necromancy"):
        s = item_xp.combat_setup(p, style)
        for slot in s.slots:
            status = "augmented" if slot.augmented else ("augment it" if slot.ok else slot.why)
            if slot.ok and not slot.augmented:
                to_augment.append((style_rate.get(style, 0.0), item_xp.short_name(slot.item)))
            t.add_row(style, slot.slot, item_xp.short_name(slot.item), str(slot.tier), status)
    for skill in item_xp.SKILL_K:
        s = item_xp.skilling_setup(p, skill)
        if s is None:
            t.add_row(skill, "tool", "none held (tool belt tools aren't read)", "", "")
            continue
        for slot in s.slots:
            status = ("make and augment it" if s.tool_assumed else "augmented" if slot.augmented
                      else "augment it" if slot.ok else slot.why)
            t.add_row(skill, "tool", item_xp.short_name(slot.item), str(slot.tier), status)
    console.print(t)
    t = Table(title=f"Invention xp/h by method (top {top}; the method's own xp comes on the side)")
    for col in ("Method", "Invention xp/h", "Side xp/h", "Siphons/h"):
        t.add_column(col)
    for r in rows[:top]:
        side = ", ".join(f"{k} {v:,.0f}" for k, v in (r.get("side_xp") or {}).items())
        siph = next((a for a in r.get("assumptions", []) if "siphons an hour" in a), "")
        n = siph.split("~")[1].split(" ")[0] if "~" in siph else ""
        t.add_row(r["method"], f"{typical_rate(r):,.0f}", side, n)
    console.print(t)
    store = p.inventories.get("MATERIALS_STORAGE", {})
    aug = min((store.get(m, 0) // n for m, n in item_xp.AUGMENTOR_MATERIALS.items()), default=0)
    siph = min((store.get(m, 0) // n for m, n in item_xp.SIPHON_MATERIALS.items()), default=0)
    console.print(f"materials: {aug} augmentors' worth in storage (each 45 Base/Flexible/Tensile parts, 8 Enhancing, "
                  f"7 Powerful components + divine energy), {siph} siphons' worth (50 Simple parts, 5 Dextrous, 5 Precious each)")
    energy = item_xp.augmentor_energy(p.holdings)
    if energy:
        console.print(f"energy: {energy[1]:,} {energy[0]} an augmentor ({p.holdings.get(energy[0], 0):,} held: "
                      f"enough for {energy[2]:,}); any one divine energy works, the higher tiers take fewer")
    else:
        console.print("energy: no held divine energy makes an augmentor (" + ", ".join(
            f"{n:,} {e.removesuffix(' energy')}" for e, n in item_xp.AUGMENTOR_ENERGY.items()) + ")")
    ordered = list(dict.fromkeys(item for _, item in sorted(to_augment, key=lambda t: -t[0])))
    if ordered:
        console.print(f"augment next (by the style's Invention xp/h): {', '.join(ordered)}")
        # One augmentor and a first siphon per piece: what the storage lacks is chained to disassembly.
        need: dict[str, int] = {}
        for mats in (item_xp.AUGMENTOR_MATERIALS, item_xp.SIPHON_MATERIALS):
            for m, n in mats.items():
                need[m] = need.get(m, 0) + n * len(ordered)
        _print_material_chain(ctx, need, f"an augmentor and a siphon for each of those {len(ordered)}")
    from . import charge
    have = charge.pack_charge(p)
    console.print("charge pack: " + (f"{have:,.0f} charge" if have is not None else "not in this export")
                  + f" (a Divine charge adds {charge.DIVINE_CHARGE:,}: {charge.CHARGE_PARTS} Simple parts + one energy)")
    for style in ("melee", "ranged", "magic", "necromancy"):
        now, items = charge.style_drain(p, style)
        full, every = charge.style_drain(p, style, planned=True)
        if not full:
            continue
        def lasts(rate):
            return f", ~{have / rate * 60:,.0f} min of combat" if have is not None and rate else ""
        line = f"  {style}: " + (f"{now:,.0f} charge/h in augmented {', '.join(items)}{lasts(now)}" if now
                               else "nothing augmented yet")
        if full != now:
            line += f"; {full:,.0f}/h with {', '.join(every)} all augmented{lasts(full)}"
        console.print(line, highlight=False)
    main = charge.main_style(p)
    for planned in (False, True):
        trip = charge.prep(ctx, charge.SESSION_HOURS, main, planned=planned)
        if trip.needed and trip.steps:
            console.print(f"for a {charge.SESSION_HOURS:g} h {main} session"
                          + (" once those are augmented" if planned else "") + ":")
            for step in trip.steps:
                console.print(f"  {step}", highlight=False)
            break
    console.print("[dim]drain assumes continuous combat and the charge drain research your level allows; "
                  "perks: radv perks; tools on the tool belt aren't read[/dim]")


def _print_material_chain(ctx, need: dict[str, int], what: str) -> None:
    """Invention materials `need` beyond the storage, with the gatherer's route to them (disassembly chains)."""
    from .recommend.gathering import Gatherer
    store = ctx.player.inventories.get("MATERIALS_STORAGE", {})
    short = {m: n - store.get(m, 0) for m, n in need.items() if n > store.get(m, 0)}
    if not short:
        console.print(f"[green]materials for {what}: all in storage[/green]")
        return
    console.print(f"short for {what}: " + ", ".join(f"{n:,} {m}" for m, n in sorted(short.items())))
    g = Gatherer(ctx).plan(short)
    for step in g.steps:
        console.print(f"  {step}")
    if g.short:
        console.print("  [yellow]no source known for: " + ", ".join(f"{n:,} {m}" for m, n in g.short.items()) + "[/yellow]")
    console.print(f"  ~{g.hours:.1f} h in all")


@app.command("perks")
def perks_cmd(player: str,
              style: list[str] = typer.Option(None, "--style", help="Combat style to plan (repeatable; default: all)"),
              skill: list[str] = typer.Option(None, "--skill", help="Skilling tool to plan (repeatable; default: held tools)"),
              level: int = typer.Option(None, "--level", help="Invention level to make the gizmos at (default: yours, with the best potion held)"),
              ancient: bool = typer.Option(None, "--ancient/--no-ancient", help="Ancient (9-slot) gizmos (default: when the blueprint is discovered)"),
              others: bool = typer.Option(False, "--others", help="Also list perks the planner doesn't value, with a gizmo that makes each"),
              plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
              snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """Suggested perks: the gizmos worth making for your augmentable gear and tools, with exact odds at your
    Invention level, which outcomes to keep, and the disassembly route to any materials you lack."""
    from .mechanics import perks as mech
    from .recommend import perks as rp
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    p = ctx.player
    styles = [s.lower() for s in style] if style else (None if not skill else [])
    skills = [s.title() for s in skill] if skill else (None if not style else [])
    s, plans = rp.suggest(ctx, styles, skills, level, ancient)
    boost = f" ({s.base_level} + {s.potion})" if s.potion and level is None and s.level != s.base_level else ""
    console.print(f"{p.name}: gizmos made at Invention {s.level}{boost}, {'ancient (9 slots)' if s.ancient else 'regular (5 slots)'}")
    if not s.ancient and ancient is None and any("Ancient gizmos" in h for h in p.holdings):
        console.print("[yellow]you hold the 'Ancient gizmos' blueprint: once it is discovered, ancient gizmos take 9 "
                      "materials and reach higher ranks (preview with --ancient)[/yellow]")
    picks = []
    for plan in plans:
        console.print(f"[bold]{plan.owner}[/bold] ({'combat gear' if plan.kind == 'combat' else 'tool'}; "
                      f"measured against {plan.hours:,.0f} h of planned training)")
        for pk in plan.picks:
            counts: dict[str, int] = {}
            for m in pk.materials:
                counts[m] = counts.get(m, 0) + 1
            mats = ", ".join(f"{n} x {m}" for m, n in counts.items())
            shown = [(o, pr) for o, pr in pk.keep if pr >= 0.0005][:rp.MAX_KEEP_SHOWN] or pk.keep[:1]
            keep = ", ".join(f"{mech.describe(o)} ({pr:.1%})" for o, pr in shown)
            if len(pk.keep) > len(shown):
                keep += f", +{len(pk.keep) - len(shown)} rarer"
            console.print(f"  [cyan]{pk.item}[/cyan]: {'ancient ' if pk.ancient else ''}{pk.gizmo} gizmo, {mats}")
            console.print(f"    keep: {keep}")
            console.print(f"    {pk.chance:.1%} of gizmos are keepers (~{pk.gizmos} to make); {pk.gain}: worth "
                          f"{pk.hours:.1f} h, materials to gather {pk.cost_hours:.1f} h")
        if not plan.picks:
            console.print("  no gizmo worth making from what you can get")
        for note in plan.notes:
            console.print(f"  [dim]{note}[/dim]")
        for line in dict.fromkeys(e for pk in plan.picks for e in pk.extras):
            console.print(f"  [dim]not valued: {line}[/dim]")
        picks += plan.picks
    if others:
        for gizmo in mech.GIZMOS:
            if not any(pk.gizmo == gizmo for pk in picks) and (style or skill):
                continue
            rows = rp.others(s, gizmo, set())
            if not rows:
                continue
            t = Table(title=f"other {gizmo} perks you can make (not valued)")
            for col in ("Perk", "Chance", "Materials", "Effect"):
                t.add_column(col)
            for o in rows:
                counts = {}
                for m in o.materials:
                    counts[m] = counts.get(m, 0) + 1
                name = o.perk if len(mech.perks()[o.perk]["ranks"]) == 1 else f"{o.perk} {o.rank}+"
                t.add_row(name, f"{o.chance:.0%}", ", ".join(f"{n} x {m}" for m, n in counts.items()), o.desc)
            console.print(t)
    if picks:
        need: dict[str, int] = {}
        for pk in picks:
            for m, n in pk.needs.items():
                need[m] = need.get(m, 0) + n
        _print_material_chain(ctx, need, f"the {sum(pk.gizmos for pk in picks)} gizmos above (shells included)")
    console.print("[dim]odds are exact (the wiki calculator's mechanics); gizmo counts are the expected number made "
                  "before a keeper; a failed attempt (no perk) costs nothing[/dim]")


@app.command("gizmo")
def gizmo_cmd(level: int, gizmo: str, materials: list[str],
              ancient: bool = typer.Option(False, "--ancient", help="An ancient gizmo (9 slots)"),
              top: int = typer.Option(15, "-n", help="Outcomes to list"),
              player: str = typer.Option(None, "--player", help="Cost the materials against this player's storage"),
              make: int = typer.Option(1, "--make", help="Gizmos to cost with --player (shells included)")):
    """Exact odds of one gizmo: `radv gizmo 88 weapon "5 Precise components"` (materials in slot order: middle,
    top, left, right, bottom; a leading count repeats one)."""
    from .mechanics import perks as mech
    names = {m.lower(): m for m in mech.materials()}
    short = {m.lower().rsplit(" ", 1)[0]: m for m in mech.materials()}
    mats: list[str] = []
    for arg in materials:
        count, _, name = arg.partition(" ") if arg.split(" ")[0].rstrip("x").isdigit() else ("1", "", arg)
        found = names.get(name.strip().lower()) or short.get(name.strip().lower())
        if found is None:
            raise typer.Exit(f"unknown material {name!r}")
        mats += [found] * int(count.rstrip("x"))
    if gizmo not in mech.GIZMOS:
        raise typer.Exit(f"gizmo must be one of {', '.join(mech.GIZMOS)}")
    if len(mats) > (mech.ANCIENT_SLOTS if ancient else mech.SLOTS):
        raise typer.Exit(f"too many materials for {'an ancient' if ancient else 'a regular'} gizmo")
    odds = mech.gizmo_odds(level, gizmo, mats, ancient)
    if odds is None:
        raise typer.Exit("too many perk combinations in play to enumerate")
    made = mech.per_gizmo(odds)
    t = Table(title=f"{'ancient ' if ancient else ''}{gizmo} gizmo at Invention {level}: {', '.join(mats)}")
    t.add_column("Perks")
    t.add_column("Per gizmo made", justify="right")
    for o, pr in sorted(made.items(), key=lambda kv: -kv[1])[:top]:
        t.add_row(mech.describe(o), f"{pr:.2%}")
    console.print(t)
    console.print(f"no perk (nothing consumed, try again): {odds.get((), 0.0):.2%} of attempts")
    if player:
        from .recommend.perks import recipe_units
        ctx = _ctx(player, True)
        need = {m: n * make for m, n in recipe_units(tuple(mats), gizmo, ancient).items()}
        console.print(f"{make} gizmo{'s' if make != 1 else ''} with shell{'s' if make != 1 else ''}: "
                      + ", ".join(f"{n:,} {m}" for m, n in need.items()))
        _print_material_chain(ctx, need, f"{make} of these")


def _simulate_rituals(p, level: int | None, top: int) -> None:
    """Every ritual at its best xp/h alteration loadout, beside the bare ritual, with inks per hour."""
    from .mechanics import rituals
    lvl = level or p.level("Necromancy")
    best = {r["ritual"]: r for r in rituals.ritual_rates(p, lvl)}
    bare = {r["ritual"]: r for r in rituals.ritual_rates(p, lvl, alterations=False)}
    if not best:
        console.print("nothing to simulate at this level")
        return
    g = rituals.gear(p, lvl)
    t = Table(title=f"Necromancy rituals for {p.name} at {lvl}: {g.describe()}; disturbances handled at "
                    f"{rituals.EFFORT} speed, Um")
    for col in ("Ritual", "Glyphs", "Rituals/h", "XP each", "XP/h", "bare XP/h", "Inks and focus per hour"):
        t.add_column(col)
    for name, r in sorted(best.items(), key=lambda kv: -kv[1]["xp_per_hour"])[:top]:
        mats = ", ".join(f"{q * r['per_hour']:,.0f} {k}" for k, q in sorted(r["materials"].items(), key=lambda kv: -kv[1]))
        t.add_row(name, r["glyphs"], f"{r['per_hour']:.1f}", f"{r['xp']:,.0f}", f"{r['xp_per_hour']:,.0f}",
                  f"{bare[name]['xp_per_hour']:,.0f}", mats)
    console.print(t)


def _session_entries(settings, db: Path | None) -> dict:
    """Recorded sessions live under the dev plugin's id; sessions recorded before the 2026-10-31 split sit under
    the exporter's id, so both are read (the dev plugin's rows win on a key clash)."""
    path = db or settings.plugin_db
    entries = {k: v for k, v in plugin_db.read_entries(path, settings.export_plugin_id).items()
               if k.startswith(("session:", "sessions:"))}
    entries.update(plugin_db.read_entries(path, settings.dev_plugin_id))
    return entries


@app.command("session")
def session(player: str, number: int = typer.Argument(None, help="Session number (omit to list them)"),
            skill: str = typer.Option(None, help="Main skill for the [[methods]] row (default: most xp)"),
            name: str = typer.Option(None, "--name", help="Method name for the [[methods]] row (default: the label)"),
            db: Path = typer.Option(None, help="Path to PluginData.db"), beta: bool = BETA_OPT,
            chat: bool = typer.Option(False, "--chat", help="Also print every logged game message with its time")):
    """Rates measured by the dev plugin's Start/Stop session recorder, and a [[methods]] row from them."""
    from . import sessions as sess
    settings = load_settings(beta=beta)
    found = sess.sessions(_session_entries(settings, db), player)
    if not found:
        raise typer.Exit(f"no recorded sessions for {player} (Start/Stop on the Rune Advisor Dev panel)")
    if number is None:
        t = Table(title=f"Recorded sessions for {player}")
        for col in ("#", "Label", "Time", "Main skill", "XP/h"):
            t.add_column(col)
        for s in found:
            main = s.main_skill
            t.add_row(str(s.number), (s.label or "-") + ("" if s.ended else " (running)"), f"{s.seconds / 60:.0f} min",
                      main or "-", f"{s.per_hour(s.xp).get(main, 0):,.0f}" if main else "-")
        console.print(t)
        return
    s = next((s for s in found if s.number == number), None)
    if s is None:
        raise typer.Exit(f"no session {number} for {player}")
    console.print(f"[bold]{s.label or f'Session {s.number}'}[/bold]: {s.seconds / 60:.1f} min"
                  + ("" if s.ended else " (not stopped: data ends at the last autosave, up to a minute missing)"))
    if s.hours < 0.25:
        console.print("[yellow]under 15 minutes: rates are rough[/yellow]")
    t = Table(title="XP")
    for col in ("Skill", "XP", "XP/h", "Level at start"):
        t.add_column(col)
    for k, v in sorted(s.xp.items(), key=lambda kv: -kv[1]):
        t.add_row(k, f"{v:,.0f}", f"{v / s.hours:,.0f}", str(s.start_levels.get(k, "-")))
    console.print(t)
    t = Table(title="Items (net of banking)")
    for col in ("Item", "Gained", "Used", "Net/h"):
        t.add_column(col)
    for i in sorted(set(s.gained) | set(s.used), key=lambda i: -(s.gained.get(i, 0) - s.used.get(i, 0))):
        g, u = s.gained.get(i, 0), s.used.get(i, 0)
        t.add_row(i, f"{g:,.0f}", f"{u:,.0f}", f"{(g - u) / s.hours:,.0f}")
    console.print(t)
    if s.input_times:
        console.print(f"Attention: {s.clicks / s.hours:,.0f} actions/h, {s.keys / s.hours:,.0f} key presses/h; "
                      f"median {s.median_input_gap:.0f}s between inputs, {s.idle_share:.0%} of the time idle "
                      f"(gaps of {sess.IDLE_GAP_S}s+)")
        top = ", ".join(f"{v} {sess._TAG.sub('', tgt)} x{n}" for (v, tgt), n in s.actions.most_common(5))
        console.print(f"Most used actions: {top}")
    rows = sess.lifetime_summary(s)
    if rows:
        t = Table(title="Objects: how long each stayed on its tile before changing")
        for col in ("Object (config name)", "Samples", "Median"):
            t.add_column(col)
        for loc, n, med in rows[:15]:
            t.add_row(loc, str(n), f"{med:.0f}s")
        console.print(t)
    changes = sess.target_changes(s)
    if changes:
        t = Table(title="Clicked objects that changed name (state changes, e.g. a node cracking)")
        for col in ("At", "From", "To"):
            t.add_column(col)
        for at, old_name, new_name in changes[:20]:
            t.add_row(f"{at:.1f}s", old_name, new_name)
        console.print(t)
    cadence = sess.message_cadence(s)
    if cadence:
        t = Table(title="Game messages (numbers as #): how often, and the usual gap between two")
        for col in ("Message", "Count", "Per hour", "Median gap", "First at"):
            t.add_column(col)
        for m in cadence[:20]:
            t.add_row(m.pattern, str(m.count), f"{m.per_hour:,.0f}", f"{m.median_gap:.1f}s" if m.median_gap is not None
                      else "-", f"{m.first:.0f}s")
        console.print(t)
        if chat:
            for t_, kind, text in s.chat:
                console.print(f"{t_:8.1f}s  {kind:16} {text}", markup=False, highlight=False)
    sw = sess.swings(s)
    if sw:
        console.print(f"Swings: \"{sw.message}\" every {sw.interval_ticks} ticks; {sw.successes} in {sw.swings} swings: "
                      f"{sw.chance:.1%} per swing outside stalls ({sw.chance_all:.1%} overall)", markup=False)
        if sw.stalls:
            t = Table(title="Stalls: runs of misses too long to be chance")
            for col in ("From", "To", "Swings", "First thing in the gap"):
                t.add_column(col)
            for st in sw.stalls:
                t.add_row(f"{st.start:.1f}s", f"{st.end:.1f}s", str(st.swings), st.after)
            console.print(t)
    elif not s.chat:
        console.print("[dim]No game messages logged (grant the plugin's chatAccess permission to record them).[/dim]")
    console.print("\nA [[methods]] row, if this is worth curating into rune_advisor.toml (the planner never reads sessions):\n", markup=False)
    console.print(sess.to_toml(s.method(skill, name)), markup=False, highlight=False)


@app.command("tiles")
def tiles(player: str = typer.Argument(None, help="Only this player's sessions (default: every recorded player)"),
          db: Path = typer.Option(None, help="Path to PluginData.db"), beta: bool = BETA_OPT,
          store: Path = typer.Option(None, help="Tile store to update (default: state/tiles.json)"),
          jumps: int = typer.Option(15, "--jumps", help="How many jump kinds to list")):
    """Merge recorded sessions' own movement into the walkable-tile store and summarise it."""
    from . import movement
    from . import sessions as sess

    settings = load_settings(beta=beta)
    path = store or movement.TILE_STORE
    found = sess.sessions(_session_entries(settings, db), player)
    data = movement.load_store(path)
    merged, new_tiles = movement.merge(data, found)
    if merged:
        movement.save_store(data, path)
    console.print(f"{merged} new session(s) merged, {new_tiles:,} new tiles -> {path}")
    s = movement.summary(data)
    if not s["tiles"]:
        console.print("[dim]No movement recorded yet: record a session with the plugin's Start/Stop panel while walking about.[/dim]")
        return
    console.print(f"{s['tiles']:,} walkable tiles over {s['map_squares']} map squares (per level: "
                  + ", ".join(f"{lv}: {n:,}" for lv, n in s["per_level"].items())
                  + f"), {s['steps']:,} walked steps, {s['jumps']} jumps, from {s['sessions']} session(s)")
    if s["jumps_by_action"]:
        t = Table(title="Jumps (moves that weren't walking) by the action clicked just before")
        for col in ("Action", "Times"):
            t.add_column(col)
        for action, n in s["jumps_by_action"][:jumps]:
            t.add_row(action, str(n))
        console.print(t)
    tele = movement.teleports()
    if tele:
        console.print(f"[dim]{len(tele):,} wiki teleport destinations on file (data/teleports.json), not yet matched to jumps.[/dim]")


@app.command("wiki-quest")
def wiki_quest(title: str):
    """Show what the wiki knows about a quest's requirements."""
    info = wiki.fetch_quest(CachedClient(), title)
    if not info:
        raise typer.Exit(f"no wiki quest named {title!r}")
    console.print(info)


@app.command("plugin-data")
def plugin_data(db: Path = typer.Option(None, help="Path to PluginData.db"), beta: bool = BETA_OPT):
    """List what our plugins (the exporter and the dev plugin) have written to the game's PersistentDB."""
    settings = load_settings(beta=beta)
    path = db or settings.plugin_db
    console.print(f"DB: {path}")
    if path.exists():
        # Plugins have no clock (no `os` in the sandbox), so the file's mtime is our timestamp.
        console.print(f"last written: {datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d %H:%M:%S}")
    console.print(f"plugin ids present: {plugin_db.list_plugin_ids(path)}")
    snaps = plugin_db.snapshots(plugin_db.read_entries(path, settings.export_plugin_id))
    if not snaps:
        console.print(f"[yellow]no snapshots for plugin id {settings.export_plugin_id!r} yet[/yellow]")
    for name, s in snaps.items():
        console.print(f"{name}: skills={len(s.get('skills') or {})} quests={len(s.get('quests') or [])} "
                      f"achievements={len(s.get('achievements') or [])}")
        for inv, contents in sorted((s.get("inventories") or {}).items()):
            items = contents.get("items", [])
            placeholders = sum(1 for i in items if i.get("placeholder"))
            console.print(f"  {inv}: {len(items)} slots" + (f" ({placeholders} placeholders)" if placeholders else ""))
    dev = plugin_db.read_entries(path, settings.dev_plugin_id)
    if dev:
        from . import sessions as sess
        by_player: dict[str, int] = {}
        for s in sess.sessions(dev):
            by_player[s.player] = by_player.get(s.player, 0) + 1
        console.print(f"{settings.dev_plugin_id}: sessions " + (", ".join(f"{p} x{n}" for p, n in by_player.items()) or "none")
                      + "; dumps: " + (", ".join(f"{k} ({len(v) if isinstance(v, (list, dict)) else '?'})"
                                                 for k, v in sorted(plugin_db.dumps(dev).items())) or "none"))


@app.command("dnd")
def dnd_cmd(player: str, today: str = typer.Option(None, "--today", help="Plan from this date (YYYY-MM-DD; default: today)"),
            plugin: bool = True, beta: bool = BETA_OPT, plugin_only: bool = PLUGIN_ONLY_OPT,
            snapshot: Path = SNAPSHOT_OPT, f2p: bool = F2P_OPT, group: list[str] = GROUP_OPT):
    """D&D reset tokens: what one more completion of every daily, weekly and monthly Distraction & Diversion is worth
    to the player (training it replaces toward an unlock, less the D&D's own time) and where the held tokens go in
    the reset periods left before their removal."""
    from datetime import date
    from .dnd import CADENCES, rules
    from .recommend.tracks.dnd_tokens import periods, plan
    ctx = _ctx(player, plugin, beta, plugin_only, snapshot, f2p, group)
    day = date.fromisoformat(today) if today else date.today()
    valued, plans, removal = plan(ctx, day)
    data = rules()
    for c in CADENCES:
        t = Table(title=f"{c} D&Ds: one reset's worth to {ctx.player.name}")
        for col in ("D&D", "Saves", "Takes", "Net", "What"):
            t.add_column(col, justify="right" if col in ("Saves", "Takes", "Net") else "left")
        for v in valued[c]:
            tag = f" [not yet: {', '.join(v.reward.blocked)}]" if v.reward.blocked else ""
            tag += f" [{v.reward.players}+ players]" if v.reward.players > 1 else ""
            t.add_row(v.reward.dnd + tag, f"{v.hours * 60:.0f} min", f"{v.reward.minutes:.0f} min", f"{v.net * 60:+.0f} min",
                      "; ".join(v.lines) or "nothing your plans value")
        for n_, why in data.get("not_valued", {}).items():
            if n_ in data["dnds"].get(c, []):
                t.add_row(n_, "-", "-", "-", f"not valued: {why}")
        console.print(t)
        s = plans[c]
        n_periods = len(periods(c, day, removal))
        console.print(f"{s.tokens} x {data['tokens'][c]['item']}; {n_periods} reset period{'s' if n_periods != 1 else ''} before {removal:%-d %b}")
        by_period: dict[str, list[str]] = {}
        for slot in s.slots:
            by_period.setdefault(slot.period, []).append(f"{slot.valued.reward.dnd} ({slot.net * 60:+.0f} min)")
        for label, ds in by_period.items():
            console.print(f"  {label}: {', '.join(ds)}")
        if s.unused:
            console.print(f"  {s.unused} tokens have no worthwhile slot before the removal")
        console.print()
    for line in data.get("rules", []):
        console.print(f"- {line}")
    if data.get("removal_note"):
        console.print(f"- {data['removal_note']}")


data_app = typer.Typer(no_args_is_help=True, help="Data package: status, check, apply, rollback")
app.add_typer(data_app, name="data")


@data_app.command("status")
def data_status() -> None:
    """Which data the planner reads (installed package or the bundled data) and what is kept."""
    from . import datapkg
    from .config import DATA_HOME
    a, st = datapkg.active(), datapkg.read_state()
    console.print(f"data format {datapkg.DATA_FORMAT}; reading: {a.source} ({datapkg.version()}) from {a.root}")
    console.print(f"installed: current {st['current'] or '-'}, previous {st['previous'] or '-'}; folder {DATA_HOME}")
    for n in a.notes:
        console.print(f"[yellow]{n}[/yellow]")
    s = load_settings()
    console.print(f"updates: {'check' if s.check_updates else 'off'}, url {s.data_url or '(none: never calls out)'}")


@data_app.command("check")
def data_check() -> None:
    """Ask the update server whether a newer package of this app's format exists (installs nothing)."""
    from . import datapkg
    s = load_settings()
    if not s.data_url:
        console.print("no data URL configured (url under the data table in rune_advisor.toml, or RUNE_ADVISOR_DATA_URL): not calling out")
        raise typer.Exit(0)
    try:
        offer = datapkg.check(s.data_url)
    except (datapkg.PackageError, httpx.HTTPError) as e:
        console.print(f"[red]check failed: {e}[/red]")
        raise typer.Exit(1)
    console.print(f"newest for format {datapkg.DATA_FORMAT}: {offer.version} "
                  f"({'newer: run `radv data apply`' if offer.newer else 'already installed'})")


@data_app.command("apply")
def data_apply(file: Path = typer.Argument(None, help="a local package (its .sig must sit beside it); else download the newest"),
               force: bool = typer.Option(False, "--force", help="allow installing an older version")) -> None:
    """Install a data package; it is used from the next start."""
    from . import datapkg
    try:
        if file is not None:
            ver = datapkg.install_file(file, allow_older=force)
        else:
            s = load_settings()
            if not s.data_url:
                console.print("no data URL configured: give a package file, or set the data url")
                raise typer.Exit(1)
            ver = datapkg.update(s.data_url)
            if ver is None:
                console.print("already up to date")
                raise typer.Exit(0)
    except (datapkg.PackageError, httpx.HTTPError, OSError) as e:
        console.print(f"[red]refused: {e}[/red]")
        raise typer.Exit(1)
    console.print(f"installed data package {ver}; used from the next start")


@data_app.command("rollback")
def data_rollback() -> None:
    """Go back to the previous package (or the bundled data when there is none)."""
    from . import datapkg
    try:
        ver = datapkg.rollback()
    except datapkg.PackageError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"rolled back to {ver}; used from the next start")


if __name__ == "__main__":
    app()
