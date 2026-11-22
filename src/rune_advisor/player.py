"""Assemble a PlayerState from all sources, most-trusted last.

Order: hiscores (skills, RuneScore) -> Adventurer's Log quest list -> Adventurer's Log events feed
-> in-game plugin snapshot -> manual overrides (overrides.toml).
"""

from __future__ import annotations

import json
import re
import tomllib
from datetime import datetime
from pathlib import Path
from functools import cache
from typing import Any

import httpx

from . import config
from .config import ARCHIVE_DIR, Settings
from .drops import resolve_item_log
from . import datapkg
from .http import CachedClient
from .models import Achievement, PlayerState, Quest, QuestStatus, Skill
from .quests import REPLAYABLE_QUESTS, reconcile_journal_qp
from .sources import adventurers_log, hiscores, plugin_db, game_configs
from .xp import SKILL_ALIASES

OVERRIDES_FILE = config.user_file("overrides.toml")


def current_quest_titles(client: CachedClient, name: str) -> set[str] | None:
    """Titles of quests that exist today, from the Adventurer's Log (the list is the same for every player)."""
    try:
        return set(adventurers_log.parse_quests(adventurers_log.fetch_quests(client, name)))
    except (httpx.HTTPError, KeyError):
        return None


def archived_snapshots(name: str, archive_dir: Path = ARCHIVE_DIR) -> list[tuple[str, Path]]:
    """This player's archived snapshots (state/archive/<date>_<player>_<beta|live|api>/snapshot_<player>.json),
    newest first, each with its folder's label ("2026-11-01 beta")."""
    found: list[tuple[str, Path]] = []
    if not archive_dir.is_dir():
        return found
    for folder in archive_dir.iterdir():
        parts = folder.name.split("_")
        if len(parts) < 3 or parts[1].lower() != name.lower():
            continue
        path = next((f for f in folder.glob("snapshot_*.json") if f.stem.split("_", 1)[1].lower() == name.lower()), None)
        if path is not None:
            found.append((f"{parts[0]} {' '.join(parts[2:])}", path))
    return sorted(found, key=lambda t: t[0], reverse=True)


def fill_stale_inventories(snap: dict[str, Any], name: str, archive_dir: Path = ARCHIVE_DIR,
                           fallback: Path | None = None) -> dict[str, Any]:
    """Inventories the snapshot doesn't carry are taken as what they held the last time they were exported.

    The exporter sends an inventory only once the client has loaded it (the metal bank when it is opened, the
    soil box, a ritual's inventories), and a server that never saw this character before has nothing to carry
    forward, so a fresh export can lack containers an older one had. The user's rule (2026-11-09): a stale
    inventory is whatever it held when last seen, not empty and not unknown. Each missing inventory is copied
    from the newest archived snapshot (or the configured fallback file) that has it, and the snapshot's notes
    say which, with the archive's date."""
    have = snap.get("inventories") or {}
    candidates = archived_snapshots(name, archive_dir)
    if fallback is not None and fallback.exists() and fallback.resolve() not in {p.resolve() for _, p in candidates}:
        candidates.append((f"{fallback.name} (configured snapshot)", fallback))
    filled: dict[str, dict[str, Any]] = {}
    for label, path in candidates:
        try:
            old = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if old.get("exportedAt") == snap.get("exportedAt") and old.get("inventories") == have:
            continue  # an archive of this very export
        for inv_name, inv in (old.get("inventories") or {}).items():
            if inv_name not in have and inv_name not in filled and isinstance(inv, dict):
                filled[inv_name] = {**inv, "staleFrom": label}
    if not filled:
        return snap
    by_label: dict[str, list[str]] = {}
    for inv_name, inv in filled.items():
        by_label.setdefault(inv["staleFrom"], []).append(inv_name)
    notes = list(snap.get("notes") or [])
    for label, names in by_label.items():
        notes.append(f"inventories not loaded since this export began, taken as last exported ({label}): "
                     + ", ".join(sorted(names)))
    return {**snap, "inventories": {**have, **filled}, "notes": notes}


def find_snapshot(settings: Settings, name: str) -> dict[str, Any] | None:
    """This player's plugin snapshot: from the plugin DB, else the export API (when rune_advisor.toml has an
    [api] table and the server has state for the name), else their configured snapshot file. Inventories the
    snapshot lacks come from the last archive that had them (`fill_stale_inventories`)."""
    fallback = settings.player(name).snapshot
    snap = None
    try:
        snaps = plugin_db.snapshots(plugin_db.read_entries(settings.plugin_db, settings.export_plugin_id))
        snap = next((v for k, v in snaps.items() if k.lower() == name.lower()), None)
    except FileNotFoundError:
        pass
    if snap is None and settings.api_url and settings.api_token:
        from . import api_client

        snap = api_client.fetch_snapshot(settings, name)
    if snap is None and fallback and fallback.exists():
        snap = json.loads(fallback.read_text())
    return fill_stale_inventories(snap, name, settings.archive_dir, fallback=fallback) if snap is not None else None


def load_group(p: PlayerState, settings: Settings, overrides_file: Path = OVERRIDES_FILE) -> None:
    """Fill in the other group members' holdings from their plugin snapshots, where we have one."""
    for member in settings.group:
        if member.lower() == p.name.lower():
            continue
        snap = find_snapshot(settings, member)
        if snap is None:
            p.notes.append(f"group: no plugin snapshot for {member}, so their items aren't counted")
            continue
        mate = PlayerState(name=member)
        apply_plugin_snapshot(mate, snap)
        if overrides_file.exists():
            apply_reserve(mate, tomllib.loads(overrides_file.read_text()))
        p.group[member] = mate.holdings


def load_player(client: CachedClient, name: str, settings: Settings, use_plugin: bool = True,
                overrides_file: Path = OVERRIDES_FILE) -> PlayerState:
    p = PlayerState(name=name, fetched_at=datetime.now())

    hs = hiscores.fetch(client, name)
    p.skills = hiscores.parse_skills(hs)
    p.runescore = hiscores.parse_runescore(hs)
    # Group ironmen aren't on the (solo) ironman hiscores, but follow ironman requirements.
    p.ironman = settings.in_group(name) or hiscores.is_on_board(client, name, "ironman")

    try:
        quests = adventurers_log.fetch_quests(client, name)
        p.quests = adventurers_log.parse_quests(quests)
        # The game's own QP count: the journal total until a plugin export supplies one.
        p.journal_quest_points = adventurers_log.parse_journal_qp(quests)
        if (events := adventurers_log.fetch_events(client, name)) is not None:
            apply_activity_feed(p, events)
        else:
            # The events feed is members only, so its refusal is the one web source of membership.
            p.members = False
            p.notes.append("free-to-play per the Adventurer's Log (its events feed is members only)")
    except (httpx.HTTPError, KeyError) as e:
        p.notes.append(f"Adventurer's Log unavailable ({e.__class__.__name__}); quests unknown unless plugin data exists")

    if use_plugin:
        if snap := find_snapshot(settings, name):
            apply_plugin_snapshot(p, snap)
        else:
            p.notes.append(f"no plugin snapshot for {name} (bank and in-game state unknown)")

    if overrides_file.exists():
        apply_overrides(p, tomllib.loads(overrides_file.read_text()))
    reconcile_journal_qp(p)
    if p.journal_quest_points is not None and p.journal_quest_points != p.quest_points:
        p.notes.append(f"quest journal says {p.journal_quest_points} QP, per-quest data adds up to "
                       f"{p.quest_points}; some quest's status is wrong in our data")
    return p


def player_from_snapshot(name: str, snap: dict[str, Any], current_quests: set[str] | None = None) -> PlayerState:
    """A player built only from a plugin snapshot: no player state from the web, no overrides.

    Use this for characters whose state differs from the live hiscores, e.g. a reset or
    freshly imported plugin-beta character, or an archived snapshot.

    `current_quests` is the catalogue of quests that exist today (any player's Adventurer's Log quest
    titles). The game still carries retired quest configs (e.g. 0-QP "Romeo and Juliet"); those
    become `listed=False` so they're never suggested, while still answering requirement and
    members checks (some miniquests that achievements require aren't in the catalogue either).
    """
    p = PlayerState(name=name, fetched_at=datetime.now())
    by_name: dict[str, list[dict]] = {}
    # Quest names are in the expanded form only (an export API snapshot holds state by config id).
    for q in expanded(snap)[0].get("quests") or []:
        by_name.setdefault(q["name"], []).append(q)
    if current_quests is None:
        p.notes.append("no quest catalogue: retired quest configs may show as unfinished")
    for title, copies in by_name.items():
        # Retired pre-rework configs share names; the live one is the one with QP or a real state.
        main = max(copies, key=lambda q: (q.get("questPoints", 0), q.get("finished", False), q.get("started", False)))
        p.quests[title] = Quest(title=title, status=QuestStatus.NOT_STARTED, quest_points=main.get("questPoints", 0),
                                members=main.get("members", True), eligible=bool(main.get("canStart", True)),
                                source="plugin", listed=current_quests is None or title in current_quests)
    apply_plugin_snapshot(p, snap, prefer_snapshot_skills=True)
    p.runescore = sum(a.runescore for a in p.achievements.values() if a.completed)
    if current_quests is not None:
        # Quest points only count quests that exist today (retired configs can carry stale QP).
        for q in p.quests.values():
            if not q.listed:
                q.quest_points = 0
    reconcile_journal_qp(p)
    return p


def apply_activity_feed(p: PlayerState, events: dict[str, Any]) -> None:
    """Recent "Quest complete" events beat the quest list, which can lag it by a sync."""
    for title in adventurers_log.parse_event_quest_completions(events):
        if (q := p.quests.get(title)) and q.status != QuestStatus.COMPLETED:
            q.status, q.source = QuestStatus.COMPLETED, "events"


# Containers whose contents are one var per item: var prefix -> (inventory name, item name from the
# var suffix, suffixes that are flags rather than items).
VAR_CONTAINERS = {
    # The herb bag only holds grimy herbs. DINO_HERB_BAG_1..5 are the variables the counts are packed in (four
    # herbs each); exports up to plugin 0.4.2 carry them too, and they are skipped.
    "DINO_HERB_BAG_": ("HERB_BAG", lambda s: "Grimy " + s.replace("_", " ").lower(),
                       {"1", "2", "3", "4", "5", "GREY_OUT", "UNLOCKED", "UPGRADED"}),
    "ARCH_SOIL_BOX_": ("SOIL_BOX", lambda s: s.replace("_", " ").capitalize(), {"UPGRADE"}),
    # Invention materials storage: INVENT_MATERIAL_BASE_PARTS -> "Base parts", ..._JUNK -> "Junk" (the wiki's
    # material names, as data/disassembly.json spells them).
    "INVENT_MATERIAL_": ("MATERIALS_STORAGE", lambda s: s.replace("_", " ").capitalize(), set()),
    # The cramulet: CRAMULET_HAS_<AMULET> = 1 while that amulet is absorbed into it (dismantling returns the
    # separate amulets). Its base amulets (Monkeyspeak, Ghostspeak) have no flag: they are the cramulet itself.
    "CRAMULET_HAS_": ("CRAMULET", lambda s: CRAMULET_AMULETS.get(s, s.replace("_", " ").capitalize()), set()),
}
CRAMULET_AMULETS = {"CAMULET": "Camulet", "CATSPEAK": "Catspeak amulet", "CATSPEAK_ENCHANTED": "Catspeak amulet (e)",
                    "CROCSPEAK": "Crocspeak amulet", "CABBAGESPEAK": "Cabbagespeak amulet", "CRABULET": "Crabulet"}

# Skill ids in the game's order (SkillType), for vars holding a skill number.
SKILL_IDS = ["Attack", "Defence", "Strength", "Hitpoints", "Ranged", "Prayer", "Magic", "Cooking", "Woodcutting",
             "Fletching", "Fishing", "Firemaking", "Crafting", "Smithing", "Mining", "Herblore", "Agility", "Thieving",
             "Slayer", "Farming", "Runecrafting", "Hunter", "Construction", "Summoning", "Dungeoneering", "Divination",
             "Invention", "Archaeology", "Necromancy"]
# The skill of the last strange / golden rock received: the next rock in that skill is half as likely (the wiki's
# "50% less likely to get the same type") until a rock from another skill drops, so switching skills after a rock
# is the faster way to fill the bag. The value is NOT a SkillType id: observed 9 for Herblore (both vars), 8 for
# Agility and 10 for Thieving (2026-10-31), i.e. 8 + the skill's index in the strange rock item-id order
# (STRANGE_ROCK_SKILLS); the 21 seen earlier would be Construction. Golden-only skills (Melee, Magic...) have codes
# not seen yet. Which rock a skill drops next is decided from the rocks the player holds (a cheat-given rock 1 was
# followed by rock 2), so there is no received-rocks var to read: held_rocks is the whole picture.
ROCK_LAST_SKILL_VARS = {"strange": "STATUE_LAST_SKILL", "golden": "ELFCITY_STATUE_LAST_SKILL"}
ROCK_SKILL_CODE_BASE = 8
ROCK_SKILLS_TRAINED = {"Melee": {"Attack", "Strength", "Defence"}}  # golden rock types that aren't one skill

# Reward currency balances held in player vars (found in the 2026-10-31 var-name dump).
CURRENCY_VARS = {"Slayer points": "SMKI_SLAYER_POINTS", "Marks of War": "PVM_HUB_CURRENCY",
                 "Goebie reputation": "RAID1_GOEBIE_REP"}
# War's Retreat campfire: PVM_HUB_CAMPFIRE_STATE assumed to count upgrades bought (0 regular ... 7 eternal magic),
# the order of drops.CAMPFIRE_TIERS. Unverified until compared with the tier shown at War's Wares.
CAMPFIRE_STATE_VAR = "PVM_HUB_CAMPFIRE_STATE"
# Banked bonus xp per skill: BONUSXP_<SKILL> holds the pool in TENTHS of an xp (a small prismatic fallen star at
# Smithing 99 took it 0 -> 106,465 while its table value is 10,646.5; beta, 2026-11-11). BONUSXP_<SKILL>_STORED
# didn't move then (role unknown); both exported, the first one used.
BONUS_XP_PREFIX = "BONUSXP_"
# Penguin Hide and Seek spy points: the VarBit PENG_SPY_POINTS, sent by itself since plugin 0.4.3. Earlier exports
# sent the whole variable it lives in, PENG_SPY_PERM1, where it is bits 16-23 (184 read as 0x47b80186; buying a
# 1-point lamp on the beta took it to 0x47b70186, 2026-11-11). Bits 0-15 are PENG_SPY_TOTAL_PENG, the lifetime
# penguins-spotted count the shop shows; 24-31 are Spy Club unlocks and rework flags.
PENGUIN_POINTS_VAR = "PENG_SPY_POINTS"
PENGUIN_POINTS_PACKED_VAR = "PENG_SPY_PERM1"


def penguin_points_from_var(variables: dict) -> int | None:
    value = variables.get(PENGUIN_POINTS_VAR)
    if isinstance(value, int) and value >= 0:
        return value
    value = variables.get(PENGUIN_POINTS_PACKED_VAR)
    return (value >> 16) & 0xFF if isinstance(value, int) and value >= 0 else None
BONUS_XP_UNIT = 10  # var units per xp


@cache
def _var_values() -> dict[str, dict[str, str]]:
    """What the values of some variables stand for (data/var_values.json, from the wiki's Vars Project)."""
    try:
        return json.loads(datapkg.path("var_values.json").read_text())
    except (OSError, ValueError):
        return {}


def belt_tools(variables: dict) -> dict[str, str]:
    """The pickaxe and hatchet on the tool belt, as the sims label tools: TOOLBELT_PICKAXE_LEVEL 53 is a
    "Primal pickaxe + 5" -> "Primal + 5", "Hatchet of ember and glade" -> "Ember and glade". A gilded pickaxe
    mines like the plain one. Everyone's belt has a tool (bronze at worst), so a value is always a tool."""
    found = {}
    for tool, var in (("pickaxe", "TOOLBELT_PICKAXE_LEVEL"), ("hatchet", "TOOLBELT_HATCHET_LEVEL")):
        value = variables.get(var)
        name = (_var_values().get(var) or {}).get(str(value)) if isinstance(value, int) else None
        if name:
            label = re.sub(rf"(?i)^{tool} of |\b{tool}\b|^gilded ", "", name)
            found[tool] = " ".join(label.split()).capitalize() if label.strip() else name
    return found


def slayer_task_from_vars(variables: dict) -> tuple[str, int, str | None] | None:
    """(creatures, kills left, master) of the current Slayer assignment: SLAYER_TARGET names the creatures (0 =
    none), SLAYER_COUNT the kills left, SLAYER_MASTER who gave it."""
    target, left = variables.get("SLAYER_TARGET"), variables.get("SLAYER_COUNT")
    if not isinstance(target, int) or target <= 0 or not isinstance(left, int) or left <= 0:
        return None
    values = _var_values()
    creatures = (values.get("SLAYER_TARGET") or {}).get(str(target)) or f"assignment {target}"
    return creatures, left, (values.get("SLAYER_MASTER") or {}).get(str(variables.get("SLAYER_MASTER")))


def campfire_tier_from_var(variables: dict) -> str | None:
    from .drops import CAMPFIRE_TIERS
    value = variables.get(CAMPFIRE_STATE_VAR)
    if isinstance(value, int) and 0 <= value < len(CAMPFIRE_TIERS):
        return CAMPFIRE_TIERS[value][0]
    return None
# The gem bag's counts: RAND_REWARD_GEM_BAG_UPGRADE_<GEM>_100 (the upgraded bag's layout, incl. opal/jade/topaz;
# the plain <GEM> vars read the same counts). 2 uncut sapphires showed in both on 2026-10-29.
GEM_BAG = {"SAPPHIRE": "Uncut sapphire", "EMERALD": "Uncut emerald", "RUBY": "Uncut ruby", "DIAMOND": "Uncut diamond",
           "DRAGON": "Uncut dragonstone", "OPAL": "Uncut opal", "JADE": "Uncut jade", "TOPAZ": "Uncut red topaz"}


# Statue collection bag: one bit per rock held, bit n = the rock's item id minus the first rock's id (two rocks
# per skill, in item-id order). Strange rocks are the bag item's obj var, mirrored in STATUE_BAG_CLIENT.
# Golden rocks are the flags ELFCITY_ROCK_<SKILL><1|2>_STORED, all packed in the player var ELFCITY_PERM6: it lost
# each pair as it went into the Statue of Rhiannon (session 11), while STATUE_BAG_GOLDEN_CLIENT is a copy that only
# refreshes with the bag. Verified 2026-10-29. Exports up to plugin 0.4.2 carry ELFCITY_PERM6 itself too; from
# 0.4.3 only the flags (the variable packs unrelated fields).
# The strange rock bag's obj var (mirrored in STATUE_BAG_CLIENT while the bag is open): bit n = rock item id minus
# STATUE_ROCK_AGILITY1's, i.e. the item-id order below (the bag interface shows them row by row in that order).
# Confirmed 2026-10-31: legacyvarobj=13 with Agility 1 and both Herblore rocks held.
STRANGE_ROCK_SKILLS = ["Agility", "Herblore", "Thieving", "Crafting", "Runecrafting", "Mining", "Smithing", "Fishing",
                       "Cooking", "Firemaking", "Woodcutting", "Fletching", "Farming", "Construction", "Hunter"]
# ELFCITY_PERM6's bits follow the skills alphabetically (Farming before Herblore), unlike the golden rock item ids
# (ELFCITY_ROCK_HERBLORE1 precedes ELFCITY_ROCK_FARMING1): a Herblore golden rock set bit 12, Melee1 is bit 16 and
# Prayer1 bit 20 (2026-10-31 wire session, matching the ELFCITY_ROCK_*_STORED flags).
GOLDEN_ROCK_SKILLS = ["Agility", "Construction", "Crafting", "Divination", "Dungeoneering", "Farming", "Herblore",
                      "Magic", "Melee", "Mining", "Prayer", "Ranged", "Slayer", "Smithing", "Summoning", "Woodcutting"]


def golden_rocks_var(variables: dict) -> int | None:
    """The golden rocks held as ELFCITY_PERM6's bitmap (bit 2 x skill index + rock - 1, GOLDEN_ROCK_SKILLS order),
    rebuilt from the ELFCITY_ROCK_*_STORED flags when the export has them, else ELFCITY_PERM6 as exported."""
    value, found = 0, False
    for i, skill in enumerate(GOLDEN_ROCK_SKILLS):
        for n in (1, 2):
            flag = variables.get(f"ELFCITY_ROCK_{skill.upper()}{n}_STORED")
            if isinstance(flag, int):
                found = True
                if flag:
                    value |= 1 << (2 * i + n - 1)
    if found:
        return value
    packed = variables.get("ELFCITY_PERM6")
    return packed if isinstance(packed, int) else None


def statue_bag(variables: dict) -> dict[str, int]:
    """Rocks in the statue collection bag: {"Strange rock (Agility 1)": 1, "Golden rock (Melee 2)": 1, ...}."""
    out = {}
    for value, kind, skills in ((variables.get("STATUE_BAG_CLIENT"), "Strange", STRANGE_ROCK_SKILLS),
                                (golden_rocks_var(variables), "Golden", GOLDEN_ROCK_SKILLS)):
        if not isinstance(value, int) or value == -1:
            continue
        for bit in range(2 * len(skills)):
            if value >> bit & 1:
                out[f"{kind} rock ({skills[bit // 2]} {bit % 2 + 1})"] = 1
    return out


def held_rocks(p: "PlayerState") -> dict[str, dict[str, int]]:
    """Statue rocks the player holds, by kind and skill: {"Strange": {"Herblore": 2}, "Golden": {"Melee": 1}}.
    Counts the collection bag (statue_bag: golden rocks from the ELFCITY_ROCK_* flags, strange rocks from the bag's client
    mirror while the bag is open) plus rock items in any exported inventory ("Strange rock (Herblore)" in the bank
    or backpack: where a rock lands when the bag isn't carried). Two of a skill make a pair for its statue."""
    out: dict[str, dict[str, int]] = {"Strange": {}, "Golden": {}}
    for rock in statue_bag(p.vars):
        kind, rest = rock.split(" rock (", 1)
        skill = rest.rsplit(" ", 1)[0]
        out[kind][skill] = out[kind].get(skill, 0) + 1
    for items in p.inventories.values():
        for name, count in items.items():
            for kind in ("Strange", "Golden"):
                if name.startswith(f"{kind} rock (") and name.endswith(")"):
                    skill = name[len(kind) + 7:-1]
                    out[kind][skill] = out[kind].get(skill, 0) + count
    return out


def last_rock_skills(variables: dict) -> dict[str, str]:
    """{"strange": "Agility", "golden": "Herblore"}: the skill of the last rock of each kind, where exported, decoded
    as ROCK_SKILL_CODE_BASE + an index into STRANGE_ROCK_SKILLS (observed: Agility 8, Herblore 9); other codes
    (golden-only skills) are kept as "code N"."""
    out = {}
    for kind, var_name in ROCK_LAST_SKILL_VARS.items():
        value = variables.get(var_name)
        if isinstance(value, int) and value >= 0:
            index = value - ROCK_SKILL_CODE_BASE
            out[kind] = STRANGE_ROCK_SKILLS[index] if 0 <= index < len(STRANGE_ROCK_SKILLS) else f"code {value}"
    return out


def rock_halved_skills(p: PlayerState) -> set[str]:
    """Skills whose next strange/golden rock is half as likely right now (the last rock of that kind came from
    them), unless the bag already holds both rocks of that skill. Training another rock skill lifts it."""
    rocks = statue_bag(p.vars)
    out: set[str] = set()
    for kind, skill in p.last_rock_skill.items():
        if skill.startswith("code "):
            continue  # an undecoded value: no skill to halve
        label = "Strange" if kind == "strange" else "Golden"
        if f"{label} rock ({skill} 1)" in rocks and f"{label} rock ({skill} 2)" in rocks:
            continue
        out |= ROCK_SKILLS_TRAINED.get(skill, {skill})
    return out


def var_containers(variables: dict) -> dict[str, dict[str, int]]:
    """Holdings of the var-backed containers in VAR_CONTAINERS: {inventory: {item: count}}."""
    out: dict[str, dict[str, int]] = {}
    for prefix, (inv_name, item_name, flags) in VAR_CONTAINERS.items():
        for key, value in variables.items():
            suffix = key.removeprefix(prefix)
            if suffix != key and suffix not in flags and isinstance(value, int) and value > 0:
                out.setdefault(inv_name, {})[item_name(suffix)] = value
    # Enchanting the catspeak amulet inside the cramulet keeps both flags set (2026-10-31 export): one amulet.
    if "Catspeak amulet (e)" in out.get("CRAMULET", {}):
        out["CRAMULET"].pop("Catspeak amulet", None)
    for gem, item in GEM_BAG.items():
        value = variables.get(f"RAND_REWARD_GEM_BAG_UPGRADE_{gem}_100")
        if isinstance(value, int) and value > 0:
            out.setdefault("GEM_BAG", {})[item] = value
    if rocks := statue_bag(variables):
        out["STATUE_BAG"] = rocks
    return out


def expanded(snap: dict[str, Any], configs: "game_configs.GameConfigs | None" = None
             ) -> tuple[dict[str, Any], "game_configs.GameConfigs | None", list[str]]:
    """(snapshot in the shape the planner reads, the configs used, notes). A schema-2 snapshot holds state by
    config id and is joined with the game configs for names and tests; an older one is returned as it is."""
    configs = configs or game_configs.load()
    if (snap.get("schema") or 1) < 2:
        return snap, configs, []
    if configs is None:
        return ({**snap, "quests": [], "achievements": []}, None,
                ["snapshot (schema 2) needs the game configs data: run scripts/import_game_configs.py; "
                 "quests and achievements skipped"])
    fp = snap.get("configs")  # the set the client exported with: newer than the package after a game update
    if isinstance(fp, int) and configs.fingerprint != fp:
        from . import api_client

        configs = api_client.cached_configs(fp) or configs
    snap, notes = game_configs.expand_snapshot(snap, configs)
    return snap, configs, notes


def apply_plugin_snapshot(p: PlayerState, snap: dict[str, Any], prefer_snapshot_skills: bool = False,
                          configs: "game_configs.GameConfigs | None" = None) -> None:
    exported = snap.get("exportedAt")
    p.notes.append(f"Plugin snapshot applied (exportedAt={exported})")
    p.notes.extend(str(n) for n in (snap.get("notes") or []))  # the server's (unknown item ids) and stale fills
    snap, configs, notes = expanded(snap, configs)
    p.notes.extend(notes)
    for enum_name, s in (snap.get("skills") or {}).items():
        name = plugin_db.SKILLTYPE_TO_NAME.get(enum_name, enum_name)
        old = p.skills.get(name)
        # Normally keep the higher xp (the snapshot may be older than the hiscores); a snapshot-only
        # player (e.g. a reset beta character) must take the snapshot's values as they are.
        if prefer_snapshot_skills or old is None or s["xp"] >= old.xp:
            p.skills[name] = Skill(name=name, level=s["level"], xp=s["xp"], rank=old.rank if old else None)
    # Banked bonus xp and rested xp, once the exporter knows which vars hold them (see
    # BONUS_XP_VARS in the exporter).
    for enum_name, amount in (snap.get("bonusXp") or {}).items():
        p.bonus_xp[plugin_db.SKILLTYPE_TO_NAME.get(enum_name, enum_name)] = int(amount)
    if isinstance(snap.get("restedXp"), int):
        p.rested_xp = snap["restedXp"]
    # The game keeps retired pre-rework quest configs under the same names (e.g. two "Rune
    # Mysteries"), so merge by name: done if any copy is done.
    by_name: dict[str, list[dict]] = {}
    # Quest names are in the expanded form only (an export API snapshot holds state by config id).
    for q in expanded(snap)[0].get("quests") or []:
        by_name.setdefault(q["name"], []).append(q)
    # Quests never un-complete (replays show as unfinished in the Adventurer's Log too), so a quest another
    # source has as completed but the snapshot doesn't was finished after the snapshot was taken.
    newer: list[Quest] = []
    for name, copies in by_name.items():
        if (known := p.quests.get(name)) is None:
            continue  # retired configs the Adventurer's Log doesn't list; names otherwise match exactly
        known.seasonal = any(q.get("type") == "SEASONAL" for q in copies)
        status = (QuestStatus.COMPLETED if any(q.get("finished") for q in copies)
                  else QuestStatus.STARTED if any(q.get("started") for q in copies)
                  else QuestStatus.NOT_STARTED)
        if known.status == QuestStatus.COMPLETED and status != QuestStatus.COMPLETED:
            newer.append(known)
            continue
        known.status, known.source = status, "plugin"
    if newer:
        p.notes.append("plugin snapshot is older than the Adventurer's Log for: " + ", ".join(q.title for q in newer))
    for a in snap.get("achievements") or []:
        group_needs = a.get("groupNeeds")
        if group_needs is None and configs is not None and (cfg := configs.achievements.get(a.get("id"))):
            # Older (schema 1) snapshots carry the tests but not how many per group must pass.
            group_needs = {g: n for g, n in enumerate(game_configs._as_list(cfg.get("requiredSubItems")))
                           if isinstance(n, int)}
        p.achievements[a["name"]] = Achievement(
            name=a["name"],
            runescore=a.get("runeScore", 0),
            completed=bool(a.get("completed")),
            progress=(a["progress"] / 10_000) if a.get("progress", 0) > 0 else None,
            category=a.get("category"),
            requirements=a.get("requirements") or [],  # an empty Lua table encodes as {}
            required_groups=a.get("requiredGroups"),
            group_needs={int(g): n for g, n in (group_needs or {}).items()},
        )
    if isinstance(snap.get("membersWorld"), bool):
        p.members_world = snap["membersWorld"]
    p.vars = dict(snap.get("vars") or {})
    p.item_log = resolve_item_log({k: int(v) for k, v in (snap.get("itemLog") or {}).items()
                                   if isinstance(v, (int, float))})
    if isinstance(p.vars.get("QP"), int):
        # The journal total then misses the QP of those quests (replays were already counted).
        p.journal_quest_points = p.vars["QP"] + sum(q.quest_points for q in newer if q.title not in REPLAYABLE_QUESTS)
    # Ore box contents live in vars: MINING_OREBOX_<ORE> (ore count), ..._STONE_SPIRIT_VAR (stored
    # stone spirits) and ..._UPGRADED (capacity upgrade, not contents).
    ore_box: dict[str, int] = {}
    for key, value in p.vars.items():
        if not key.startswith("MINING_OREBOX_") or not isinstance(value, int) or value <= 0:
            continue
        ore = key.removeprefix("MINING_OREBOX_")
        if ore.endswith("_UPGRADED"):
            continue
        if ore.endswith("_STONE_SPIRIT_VAR"):
            ore_box[ore.removesuffix("_STONE_SPIRIT_VAR").replace("_", " ").capitalize() + " stone spirit"] = value
        else:
            ore_box[ore.replace("_", " ").title()] = value
    if ore_box:
        p.inventories["ORE_BOX"] = ore_box
    # Other var-backed containers: one count var per item. Flags (unlocks, upgrades, slots) are skipped.
    for inv_name, contents in var_containers(p.vars).items():
        p.inventories[inv_name] = contents
    for currency, var_name in CURRENCY_VARS.items():
        if isinstance(p.vars.get(var_name), int) and p.vars[var_name] >= 0:
            p.currencies[currency] = p.vars[var_name]
    sync_currency_inventory(p)
    for tool, label in belt_tools(p.vars).items():
        p.tools[tool] = label  # overrides.toml's  /  still win
    if (task := slayer_task_from_vars(p.vars)) is not None:
        p.slayer_task = task
        creatures, left, master = task
        p.notes.append(f"Slayer task: {left:,} {creatures} left" + (f" (from {master})" if master else ""))
    if (tier := campfire_tier_from_var(p.vars)) is not None:
        p.campfire_tier = tier  # overrides.toml's `campfire` still wins (applied after the snapshot)
    if (points := penguin_points_from_var(p.vars)) is not None:
        p.penguin_points = points  # overrides.toml's `penguin_points` still wins
    # Banked bonus xp from BONUSXP_<SKILL> vars, unless the snapshot's bonusXp table already set it.
    stored_differs = []
    for enum_name, name in plugin_db.SKILLTYPE_TO_NAME.items():
        value = p.vars.get(f"{BONUS_XP_PREFIX}{enum_name}")
        if isinstance(value, int) and value > 0 and name not in p.bonus_xp:
            p.bonus_xp[name] = value // BONUS_XP_UNIT
        stored = p.vars.get(f"{BONUS_XP_PREFIX}{enum_name}_STORED")
        if isinstance(stored, int) and stored != (value or 0) and (stored > 0 or (value or 0) > 0):
            stored_differs.append(f"{name} {value or 0:,}/{stored:,}")
    if any(k.startswith(BONUS_XP_PREFIX) for k in p.vars) and p.bonus_xp:
        p.notes.append("bonus xp read from BONUSXP_* vars (tenths of an xp"
                       + (f"; the _STORED vars differ for {', '.join(stored_differs)}" if stored_differs else "") + ")")
    p.last_rock_skill = last_rock_skills(p.vars)
    if halved := rock_halved_skills(p):
        last = ", ".join(f"{k} rock: {s}" for k, s in sorted(p.last_rock_skill.items()))
        p.notes.append(f"statue rocks: the next rock is half as likely in {', '.join(sorted(halved))} "
                       f"(last {last}) until another skill drops one; only some codes of the vars are decoded")

    for inv_name, inv in (snap.get("inventories") or {}).items():
        # Placeholders (count 0) use a bank slot but hold nothing; keep only real stock.
        stock: dict[str, int] = {}
        for item in inv.get("items", []):
            if item["count"] > 0:
                name = item["name"]
                # The server names items from its own table; this planner's may know an id that one lacks.
                if name.startswith("Unknown item ") and isinstance(item.get("id"), int):
                    name = _item_names().get(item["id"], name)
                stock[name] = stock.get(name, 0) + item["count"]
                if isinstance(item.get("id"), int):
                    p.item_ids[name] = item["id"]
        p.inventories[inv_name] = stock
    tier_names(p)


@cache
def _item_names() -> dict[int, str]:
    """Item id -> name from data/item_names.json (scripts/build_item_names.py)."""
    try:
        raw = json.loads(datapkg.path("item_names.json").read_text())
    except (OSError, ValueError):
        return {}
    return {int(k): v["name"] for k, v in raw.get("items", {}).items() if v.get("name")}


def tier_names(p: PlayerState) -> None:
    """Rename held items to the wiki's tiered page name when the export's id says so: the game calls every
    Deathdealer robe top "Deathdealer robe top", the wiki (and every recipe) "Deathdealer robe top (tier 70)".
    Only names that are the held name plus a "(tier N)" qualifier change, so a chain like the tier 80 upgrade
    sees the tier 70 piece as held instead of building one from scratch (2026-11-11)."""
    from .gear import equipment

    ids = equipment().get("ids", {})
    renames = {}
    for held, item_id in list(p.item_ids.items()):
        page = ids.get(str(item_id))
        if page and page != held and page.startswith(held + " (tier "):
            renames[held] = page
    if not renames:
        return
    for inv in p.inventories.values():
        for old, new in renames.items():
            if old in inv:
                inv[new] = inv.get(new, 0) + inv.pop(old)
    for old, new in renames.items():
        p.item_ids[new] = p.item_ids.pop(old)


def _player_overrides(p: PlayerState, raw: dict[str, Any]) -> dict[str, Any] | None:
    return next((v for k, v in raw.get("players", {}).items() if k.lower() == p.name.lower()), None)


def apply_reserve(p: PlayerState, raw: dict[str, Any]) -> None:
    """Hold back `reserve = { "Item" = N }` from the player's items (bank first), so no plan spends them:
    materials saved for a long-term goal the planner doesn't model (e.g. a Blessed flask)."""
    mine = _player_overrides(p, raw) or {}
    for item, n in mine.get("reserve", {}).items():
        want = int(n)
        left = want
        for inv in sorted(p.inventories, key=lambda k: k != "BANK"):
            take = min(left, p.inventories[inv].get(item, 0))
            if take:
                p.inventories[inv][item] -= take
                left -= take
        p.reserved[item] = want - left
        short = f" (only {want - left:,} held)" if left else ""
        p.notes.append(f"reserved {want:,} {item}{short}: plans don't use them")


def apply_group_storage(p: PlayerState, raw: dict[str, Any]) -> None:
    """Top-level `group_storage = { "Item" = N }`: the Group Ironman shared storage, which no export reads yet.
    Every member can plan with it, so it lands in a GROUP_STORAGE inventory of whoever is being planned for
    (an item only there is noted, so a plan's "from the bank" may mean a trip to the storage chest)."""
    items = raw.get("group_storage") or {}
    if not items:
        return
    inv = p.inventories.setdefault("GROUP_STORAGE", {})
    for item, n in items.items():
        inv[item] = inv.get(item, 0) + int(n)
    p.notes.append(f"group storage (overrides.toml): {len(items)} item kinds counted as held")


# Currency balances the reward shops take, as the gatherer's stock (inventory CURRENCIES): a shop recipe's price comes
# out of the balance before any earning is costed, and a joint plan's goals use it up in turn (2026-11-22). Goebie
# reputation is left out: the shops name several reputations alike.
SHOP_CURRENCIES = {"Dungeoneering tokens", "Slayer points", "Marks of War"}


def sync_currency_inventory(p: PlayerState) -> None:
    held = {c: n for c, n in p.currencies.items() if c in SHOP_CURRENCIES and n > 0}
    if held:
        p.inventories["CURRENCIES"] = held
    else:
        p.inventories.pop("CURRENCIES", None)


def apply_overrides(p: PlayerState, raw: dict[str, Any]) -> None:
    apply_group_storage(p, raw)
    mine = _player_overrides(p, raw)
    if not mine:
        return
    apply_reserve(p, raw)
    # bonus_xp = { Attack = 25000 } (as shown when hovering the skill), rested_xp = 1500
    for skill, amount in mine.get("bonus_xp", {}).items():
        p.bonus_xp[SKILL_ALIASES.get(skill, skill)] = int(amount)
    if "rested_xp" in mine:
        p.rested_xp = int(mine["rested_xp"])
    # currencies = { "Dungeoneering tokens" = 250000 }: balances the export lacks (an exported one is overridden).
    for currency, amount in mine.get("currencies", {}).items():
        p.currencies[currency] = int(amount)
    sync_currency_inventory(p)
    if "penguin_points" in mine:  # penguin_points = 184: only needed while the export lacks PENG_SPY_PERM1
        exported = penguin_points_from_var(p.vars)
        p.penguin_points = int(mine["penguin_points"])
        if exported is not None and exported != p.penguin_points:
            p.notes.append(f"penguin points: overrides.toml says {p.penguin_points}, the export {exported}; the override wins")
    for tool in ("hatchet", "pickaxe", "mattock"):
        if tool in mine:
            p.tools[tool] = str(mine[tool])
    # wanted = ["Deathwarden robe top (tier 80)"]: items to cost as goals (wiki page names, tiers included).
    p.wanted_items = [str(w) for w in mine.get("wanted", []) if str(w) not in p.holdings]
    if isinstance(mine.get("boosts"), dict):
        p.boosts = dict(mine["boosts"])
    if "campfire" in mine:
        p.campfire_tier = str(mine["campfire"]).lower()
        if (exported := campfire_tier_from_var(p.vars)) and exported != p.campfire_tier:
            p.notes.append(f"campfire: overrides.toml says {p.campfire_tier}, the export's {CAMPFIRE_STATE_VAR}="
                           f"{p.vars[CAMPFIRE_STATE_VAR]} reads as {exported} (the var's encoding is a guess)")
    for skill, n in mine.get("outfit_pieces_left", {}).items():
        p.outfit_pieces_left[SKILL_ALIASES.get(skill, skill)] = int(n)
    p.outfit_blueprints |= set(mine.get("outfit_blueprints", []))
    for title in mine.get("quests_completed", []):
        if q := p.quests.get(title):
            q.status, q.source = QuestStatus.COMPLETED, "override"
        else:
            p.notes.append(f"override: unknown quest {title!r}")
