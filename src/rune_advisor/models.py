"""Source-agnostic player state. Each ingest source fills in what it can."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class QuestStatus(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"


@dataclass
class Skill:
    name: str
    level: int
    xp: int
    rank: int | None = None


@dataclass
class Quest:
    title: str
    status: QuestStatus
    quest_points: int
    members: bool = True
    # The Adventurer's Log's "eligible": False means some start requirement isn't met.
    eligible: bool = True
    # The game's internal quest id (the Player Quests API's key), when known.
    quest_id: int | None = None
    # Holiday/seasonal quest (not required for the quest cape). Set from plugin QuestType.type.
    seasonal: bool = False
    # QP already earned although the quest isn't currently finished (a replayable quest being
    # replayed). Counts as done for QP and the quest cape. See rune_advisor.quests.
    qp_earned: bool = False
    # Which source last set the status ("adventurers_log", "events", "plugin", "override").
    source: str = "adventurers_log"
    # False for configs missing from the current quest list (retired quests, some miniquests):
    # kept for requirement and members checks, never suggested as goals.
    listed: bool = True


    @property
    def done(self) -> bool:
        """Finished, or QP already earned (for QP totals and the quest cape)."""
        return self.status == QuestStatus.COMPLETED or self.qp_earned


@dataclass
class Achievement:
    name: str
    runescore: int
    completed: bool
    progress: float | None = None  # 0..1 when the game reports partial progress
    category: str | None = None
    # Requirement tests from the plugin export: dicts with kind (skill/quest/achievement/
    # varTotal/varBit), group, complete, and kind-specific fields. See plugins/rune_advisor_export.
    requirements: list[dict] = field(default_factory=list)
    required_groups: int | None = None  # how many requirement groups must be met
    # Per requirement group, how many of its tests must pass ("any 3 of these 10"); a group not listed
    # needs all of them. From the game configs' requiredSubItems (schema-2 snapshots).
    group_needs: dict[int, int] = field(default_factory=dict)


@dataclass
class PlayerState:
    name: str
    skills: dict[str, Skill] = field(default_factory=dict)
    quests: dict[str, Quest] = field(default_factory=dict)
    achievements: dict[str, Achievement] = field(default_factory=dict)
    # Item name -> count per exported inventory ("BANK", "INV" backpack, "WORN", "METAL_BANK").
    inventories: dict[str, dict[str, int]] = field(default_factory=dict)
    runescore: int | None = None
    ironman: bool = False  # on the ironman hiscores, or in a group; prefer ironman achievement requirements
    group_ironman: bool = False  # a Group Ironman member (the export, else `group` in rune_advisor.toml); implies `ironman`
    hardcore: bool = False  # a Hardcore Ironman, or in a Hardcore Group Ironman group (from the export only)
    members: bool = True   # False plans for free-to-play: no members quests/achievements, F2P skill caps
    members_world: bool | None = None  # world type from the plugin snapshot (None if unknown)
    # The in-game quest journal's QP total (VarPlayer "QP"), when the plugin exported it.
    journal_quest_points: int | None = None
    # Raw exported player vars (see EXPORTED_VARS in the exporter plugin).
    vars: dict[str, int | str | bool] = field(default_factory=dict)
    # The skill of the last strange/golden rock received ({"strange": "Woodcutting"}): its next rock is half as
    # likely until another skill drops one (player.last_rock_skills, from STATUE_LAST_SKILL vars).
    last_rock_skill: dict[str, str] = field(default_factory=dict)
    # Reward currency balances the export carries ({"Slayer points": 610, "Marks of War": 1200}; player.CURRENCY_VARS).
    currencies: dict[str, int] = field(default_factory=dict)
    # Group Ironman: the other members' holdings (member name -> item -> count). Their materials
    # can be moved to this player through group storage or a trade.
    group: dict[str, dict[str, int]] = field(default_factory=dict)
    # Item ids from the plugin export by item name (names can be ambiguous: "Death guard" is any tier).
    item_ids: dict[str, int] = field(default_factory=dict)
    # The in-game collection log: item -> how many were ever obtained (since the log started), from the export.
    item_log: dict[str, int] = field(default_factory=dict)
    # Elite skilling outfit pieces still to craft per skill, from overrides.toml (outfits.py).
    outfit_pieces_left: dict[str, int] = field(default_factory=dict)
    # Outfits whose fragment blueprint is bought (wiki skill names), from overrides.toml where no var shows it.
    outfit_blueprints: set[str] = field(default_factory=set)
    # The hatchet and pickaxe on the tool belt, as the sims label tools ("Crystal", "Primal + 5"): from the export's
    # TOOLBELT_*_LEVEL vars, else overrides.toml ({"hatchet": "Crystal", "pickaxe": ...}), which wins.
    tools: dict[str, str] = field(default_factory=dict)
    # The current Slayer assignment from the export: (creatures, kills left, master), None when none is held
    # or the export doesn't carry it.
    slayer_task: tuple[str, int, str | None] | None = None
    # Boosts the export can't see (perks, familiars, consumables kept up), from overrides.toml
    # [players.<name>.boosts]; see mechanics/profile.py.
    boosts: dict = field(default_factory=dict)
    # War's Retreat campfire tier unlocked at War's Wares ("maple", "magic"...), from overrides.toml until
    # the exporter reads it; None = unknown.
    campfire_tier: str | None = None
    # Banked bonus xp per skill (paid out 1:1 on top of trained xp until used up; members only).
    bonus_xp: dict[str, int] = field(default_factory=dict)
    # Fort Forinthry Town Hall rested xp points, cashed in with Aster for bonus xp in one skill.
    rested_xp: int | None = None
    # Penguin Hide and Seek points (max 250), from overrides.toml: no exported var carries them.
    penguin_points: int = 0
    # Items the user wants made or dropped whatever the planner thinks of them (overrides.toml `wanted`):
    # the gear track costs each one (Deathwarden robe top (tier 80), 2026-11-11).
    wanted_items: list[str] = field(default_factory=list)
    # Items held back from planning (overrides.toml `reserve`), already taken out of `inventories`.
    reserved: dict[str, int] = field(default_factory=dict)
    # Dungeoneering scroll traits a what-if plan assumes read (traits.py: valuing a scroll before buying it).
    assumed_traits: set[str] = field(default_factory=set)
    fetched_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def account(self) -> str:
        """The account mode: "Group ironman", "Ironman" or "Regular", "Hardcore" in front where it applies."""
        mode = "group ironman" if self.group_ironman else "ironman" if self.ironman else None
        if mode is None:
            return "Regular"
        return f"Hardcore {mode}" if self.hardcore else mode.capitalize()

    @property
    def bank(self) -> dict[str, int]:
        return self.inventories.get("BANK", {})

    @property
    def holdings_state(self) -> tuple:
        """Changes whenever any inventory does (a new dict, an item added or a count changed)."""
        return tuple((k, id(v), len(v), sum(v.values())) for k, v in self.inventories.items())

    @property
    def holdings(self) -> dict[str, int]:
        """Everything the player owns across exported inventories (bank + backpack + worn...). A fresh copy
        each call; the merge itself is cached until an inventory changes (planning asks thousands of times)."""
        state = self.holdings_state
        cached = self.__dict__.get("_holdings")
        if cached is None or cached[0] != state:
            total: dict[str, int] = {}
            for items in self.inventories.values():
                for name, count in items.items():
                    total[name] = total.get(name, 0) + count
            cached = self.__dict__["_holdings"] = (state, total)
        return dict(cached[1])

    @property
    def quest_points(self) -> int:
        return sum(q.quest_points for q in self.quests.values() if q.done)

    @property
    def max_quest_points(self) -> int:
        return sum(q.quest_points for q in self.quests.values())

    def level(self, skill: str) -> int:
        s = self.skills.get(skill)
        return s.level if s else 1

    def xp(self, skill: str) -> int:
        s = self.skills.get(skill)
        return s.xp if s else 0
