"""The planning Context for a loaded player: what the command line, the desktop app and the web planner share.

Kept apart from `cli` so that the web planner, which runs it in a browser, does not have to download the command
line's libraries (typer, rich, click) with it.
"""

from __future__ import annotations

import json
from functools import cache

from .cost import CostModel
from .http import CachedClient
from .player import load_group
from .recommend import Context
from .resources import load_pool
from .sources import wiki


def apply_rates(settings) -> None:
    """The module-level rates rune_advisor.toml may set (coins earned an hour, hours played a day)."""
    if settings.coins_per_hour or settings.play_hours_per_day:
        from .recommend import gathering
        if settings.coins_per_hour:
            gathering.COINS_PER_HOUR = float(settings.coins_per_hour)
        if settings.play_hours_per_day:
            gathering.PLAY_HOURS_PER_DAY = float(settings.play_hours_per_day)


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
    def build_cost(p):
        return CostModel.default(settings.xp_rates, members=p.members, player=p,
                                 extra_methods=settings.methods + mechanics.all_rows(p))

    cost = build_cost(player)
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
        bonus_xp_skill=settings.bonus_xp_skill,
        achievement_descriptions={r["name"]: json.loads(r["json"] or "{}").get("description") or ""
                                  for r in achievement_rows},
        members_only_achievements=set() if player.members else wiki.members_only_achievements(client),
        prices=None if player.ironman else _prices(client, player),
        unique_value_hours=settings.unique_value_hours,
        prefer_exquisite_urns=settings.prefer_exquisite_urns,
        build_cost=build_cost,
    )


def _prices(client: CachedClient, player):
    """GE prices for a normal account's supplies; None if the wiki can't be reached (supplies then cost no time)."""
    from .prices import Prices
    try:
        return Prices(client)
    except Exception as e:  # network or format trouble shouldn't stop planning
        player.notes.append(f"no GE prices ({e}): bought supplies aren't costed")
        return None
