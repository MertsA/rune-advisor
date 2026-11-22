"""Prerequisites of wiki training methods, from the free-text `requirements` of data/wiki_rates.json.

The extraction kept the guides' wording ("The Chosen Commander quest; 60 Ranged and 60
Strength; enhanced grappling hook"), so this recognises the parts we can check:
  * quest titles (matched against the player's quest list, so any quest the Adventurer's Log knows),
  * skill levels: "60 Ranged", "Slayer 55", "Ranged, Agility and Strength 90".
Items the text names ("protean traps", "ectophial") are matched against the wiki's item names
(`items_named`) so a missing one can be flagged; they're not enforced, because tool-belt and
storage items aren't in the export. Everything else (unlocks, outfits) is left unchecked.
"Partial <quest>" only needs the quest started, which we don't track per step, so it's skipped, as
are clauses marked recommended/optional or "for the high figure".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache

from .models import PlayerState, QuestStatus
from .xp import SKILL_ALIASES, _skills

_SKILL = r"(?:Attack|Strength|Defence|Ranged|Prayer|Magic|Runecrafting|Runecraft|Construction|Dungeoneering|" \
         r"Constitution|Agility|Herblore|Thieving|Crafting|Fletching|Slayer|Hunter|Mining|Smithing|Fishing|" \
         r"Cooking|Firemaking|Woodcutting|Farming|Summoning|Divination|Invention|Archaeology|Necromancy)"
_LEVEL_FIRST = re.compile(rf"\b(\d{{1,3}})\s+({_SKILL})\b")
_OPTIONAL = re.compile(r"recommended|optional|for the high|for higher|for the best", re.I)
_SKILL_LIST_THEN_LEVEL = re.compile(rf"\b({_SKILL}(?:\s*(?:,|and|&)\s*{_SKILL})*)\s+(\d{{1,3}})\b")


@dataclass
class Requirements:
    quests: list[str] = field(default_factory=list)
    skills: dict[str, int] = field(default_factory=dict)
    unparsed: list[str] = field(default_factory=list)


def parse(text: str | None, quest_titles: set[str]) -> Requirements:
    req = Requirements()
    if not text:
        return req
    # Recommended/optional extras and "for the high figure" boosts aren't prerequisites.
    rest = "; ".join(c for c in text.split(";") if not _OPTIONAL.search(c))
    # Longest titles first so "Dimension of Disaster: Curse of Arrav" wins over "Dimension of Disaster".
    for title in sorted(quest_titles, key=len, reverse=True):
        m = re.search(rf"(?<![\w'])(Partial\s+)?{re.escape(title)}(?![\w'])", rest, re.I)
        if m:
            if not m.group(1):
                req.quests.append(title)
            rest = rest[:m.start()] + " " + rest[m.end():]
    for level, skill in _LEVEL_FIRST.findall(rest):
        _add(req, skill, int(level))
    rest = _LEVEL_FIRST.sub(" ", rest)
    for skills, level in _SKILL_LIST_THEN_LEVEL.findall(rest):
        for skill in re.split(r"\s*(?:,|and|&)\s*", skills):
            _add(req, skill, int(level))
    rest = _SKILL_LIST_THEN_LEVEL.sub(" ", rest)
    req.unparsed = [p.strip(" .,") for p in re.split(r"[;,]", rest)
                    if p.strip(" .,") and not re.fullmatch(r"(quest|miniquest|and|or)", p.strip(" .,"), re.I)]
    return req


def _add(req: Requirements, skill: str, level: int) -> None:
    skill = SKILL_ALIASES.get(skill, skill)
    if skill in _skills()["skills"] and 1 <= level <= 120:
        req.skills[skill] = max(level, req.skills.get(skill, 0))


def unmet(text: str | None, player: PlayerState, own_skills: tuple[str, ...] = ()) -> list[str]:
    """The quest and skill prerequisites in `text` that the player doesn't meet.

    Levels in `own_skills` (the skill the method trains) are skipped: the method's level band
    already says when it becomes available, and the player gets there by training."""
    req = parse(text, set(player.quests))
    missing = [q for q in req.quests if not player.quests[q].done
               and player.quests[q].status != QuestStatus.COMPLETED]
    missing += [f"{lvl} {s}" for s, lvl in req.skills.items() if s not in own_skills and player.level(s) < lvl]
    return missing


@cache
def _known_items() -> dict[str, str]:
    """Lower-case item name (and simple singular) -> item name, from the bundled wiki data."""
    import json
    from . import datapkg
    names: set[str] = set()
    for f, key in (("equipment.json", "items"), ("shops.json", "items")):
        path = datapkg.path(f)
        if path.is_file():
            names |= set(json.loads(path.read_text())[key])
    path = datapkg.path("recipe_catalogue.json")
    if path.is_file():
        cat = json.loads(path.read_text())
        names |= {r["output"] for r in cat["recipes"]} | set(cat.get("members_items", []))
    path = datapkg.path("drop_tables.json")
    if path.is_file():
        names |= {d[0] for drops in json.loads(path.read_text())["sources"].values() for d in drops}
    out = {}
    for n in names:
        out[n.lower()] = n
    return out


def items_named(text: str | None) -> list[str]:
    """Items a requirement text names as a whole clause ("enhanced grappling hook", "protean traps")."""
    known, found = _known_items(), []
    for clause in re.split(r";", text or ""):
        if _OPTIONAL.search(clause):
            continue
        parts = re.split(r",|\band\b|\bor\b", re.sub(r"\((.*?)\)", r", \1", clause))
        for part in parts:
            c = part.strip(" .").lower()
            for cand in (c, c.removesuffix("s"), c.removesuffix("es"), re.sub(r"^(a|an|the|full|any) ", "", c)):
                if cand in known and known[cand] not in found:
                    found.append(known[cand])
                    break
    return found


# Treasure Hunter-only supplies (wiki infobox `restriction = th`): ironmen can't play Treasure Hunter, so a
# method that needs them ("Carrion jadinkos (protean traps)") isn't one they can use. Magic notepaper is not one:
# Stanley Limelight sells it for thaler, and enchanted notepaper is made with Invention 97 (2026-11-22).
_TH_ONLY = re.compile(r"^(protean |silverhawk |portable |pulse core|cinder core)", re.I)


def th_only_items(text: str | None) -> list[str]:
    """Treasure Hunter-only items the requirement text names."""
    return [i for i in items_named(text) if _TH_ONLY.match(i)]


# The same supplies named anywhere in free text. The free-to-play guide rows have no requirement text and
# name the supply in the method itself ("Logs, portable brazier").
_TH_MENTION = re.compile(r"\b(protean \w+|silverhawk \w+|portable \w+|pulse cores?|cinder cores?)", re.I)


def th_only_mentions(text: str | None) -> list[str]:
    """Treasure Hunter-only supplies mentioned in `text` (a method name), capitalised like item names."""
    return list(dict.fromkeys(m[1][:1].upper() + m[1][1:].lower() for m in _TH_MENTION.finditer(text or "")))


_DOSE = re.compile(r" \(\d\)$")
# Combat potion tiers, weakest first: a stronger held tier satisfies a weaker named one ("super attack
# potion" is covered by Extreme attack (3)).
_POTION_TIERS = ["", "super ", "grand ", "extreme ", "supreme "]
_MELEE = ("attack", "strength", "defence")


def _holds_potion(holdings: dict[str, int], item: str) -> bool:
    """Does the player hold `item` as any dose, or a stronger tier of the same potion? The guides name
    potions without a dose ("grand ranging potion"), which never matches an item name."""
    held = {_DOSE.sub("", h).lower().removesuffix(" potion") for h in holdings}
    family = item.lower().removesuffix(" potion")
    tier = next((i for i, t in enumerate(_POTION_TIERS) if t and family.startswith(t)), 0)
    base = family[len(_POTION_TIERS[tier]):]
    bases = _MELEE if base == "melee" else (base,)
    return all(any(f"{t}{b}" in held for t in _POTION_TIERS[tier:]) for b in bases)


def missing_items(text: str | None, player: PlayerState) -> list[str]:
    """Items the text names that aren't in the player's exported inventories (maybe on the tool belt).
    In an "X or Y" clause, owning either is enough; a potion counts as held at any dose or a stronger tier."""
    def held(item: str) -> bool:
        return item in player.holdings or (item.lower().endswith(" potion") and _holds_potion(player.holdings, item))
    missing = []
    for clause in re.split(r";", text or ""):
        items = items_named(clause)
        if re.search(r"\bor\b", clause) and any(held(i) for i in items):
            continue
        missing += [i for i in items if not held(i) and i not in missing]
    return missing
