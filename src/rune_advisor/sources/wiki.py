"""RuneScape Wiki via the MediaWiki API and its Bucket extension (api.php?action=bucket).

Bucket tables are listed at https://runescape.wiki/w/Special:AllPages?namespace=9592
(e.g. Bucket:Quest, Bucket:Achievement, Bucket:Achievement_requirement, Bucket:Infobox_item).
Queries are Lua-ish: bucket('quest').select('page_name','json').where('page_name','X').run()
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .. import datapkg
from ..http import CachedClient

API = "https://runescape.wiki/api.php"

# The whole tables the planner reads, shipped in the data package as data/wiki_tables/<name>.json
# (scripts/build_wiki_tables.py). When the package has a table it is used as is and the wiki is not asked: the
# web planner cannot reach the wiki, and a data release is what refreshes them.
TABLES = {
    "quests": ("quest", ["page_name", "official_length", "requirements", "json"]),
    "achievements": ("achievement", ["page_name", "name", "id", "runescore", "maincategory", "subcategory",
                                     "difficulty", "json"]),
    "achievement_requirements": ("achievement_requirement", ["page_name", "regular", "ironman"]),
}


def _rows(client: CachedClient, name: str) -> list[dict[str, Any]]:
    """A whole table: the data package's copy, else one download from the wiki."""
    if datapkg.exists(f"wiki_tables/{name}.json"):
        return datapkg.load_json(f"wiki_tables/{name}.json")
    table, fields = TABLES[name]
    return bucket_all(client, table, fields)


def tables_fetched() -> str | None:
    """The day the packaged tables were fetched from the wiki, None when the package has none."""
    if not datapkg.exists("wiki_tables/fetched.json"):
        return None
    return datapkg.load_json("wiki_tables/fetched.json").get("fetched")


def packaged_quest_titles() -> set[str] | None:
    """The quests that existed when the package was built (from an Adventurer's Log; the same for everyone)."""
    if not datapkg.exists("wiki_tables/quest_titles.json"):
        return None
    titles = datapkg.load_json("wiki_tables/quest_titles.json")
    return set(titles) if isinstance(titles, list) and titles else None

_SKILLREQ = re.compile(r'data-skill="(?P<skill>[^"]+)" data-level="(?P<level>\d+)"')
_LINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


def _quote(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def bucket(client: CachedClient, table: str, fields: list[str], where: dict[str, str] | None = None,
           limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
    q = f"bucket({_quote(table)}).select({','.join(map(_quote, fields))})"
    for k, v in (where or {}).items():
        q += f".where({_quote(k)},{_quote(v)})"
    q += f".limit({limit}).offset({offset}).run()"
    data = client.get_json(API, params={"action": "bucket", "format": "json", "query": q}, ttl=24 * 3600)
    if "error" in data:
        raise RuntimeError(f"wiki bucket error: {data['error']} (query: {q})")
    return data["bucket"]


def bucket_all(client: CachedClient, table: str, fields: list[str], page: int = 500) -> list[dict[str, Any]]:
    rows, offset = [], 0
    while True:
        chunk = bucket(client, table, fields, limit=page, offset=offset)
        rows += chunk
        if len(chunk) < page:
            return rows
        offset += page


@dataclass
class QuestInfo:
    name: str
    length: str | None
    skill_reqs: dict[str, int] = field(default_factory=dict)
    # Linked pages in the requirements text; filter against known quest titles to get prereq quests.
    linked_pages: list[str] = field(default_factory=list)
    # Items the quest needs (the infobox's "items required", consumed or not): {item: quantity}. A player
    # without one has to get it first (an ironman's Wandering Ga'al needs a ring of stone: a rare drop).
    items: dict[str, int] = field(default_factory=dict)


# One "items required" bullet: an optional count ("4", "A", "an"), then the item's link. Only the first link
# on a line is the item; later ones are asides ("(Sliske's gift will not work)"). Lines without a link
# ("Combat gear") are advice, not items.
_ITEM_LINE = re.compile(r"^\*+\s*(?:(?P<qty>\d[\d,]*)|an?\b)?\s*\[\[(?P<page>[^\]|#]+)(?:[|#][^\]]*)?\]\](?P<tail>.*)$",
                        re.I | re.M)


def parse_quest_items(text: str) -> dict[str, int]:
    """{item: quantity} from a quest infobox's "items required" wikitext (see `_ITEM_LINE`)."""
    items: dict[str, int] = {}
    for m in _ITEM_LINE.finditer(html.unescape(text or "")):
        page = m["page"].strip()
        if page.startswith(("File:", "Category:")):
            continue
        name = page[:1].upper() + page[1:]  # link targets are case-insensitive in their first letter
        qty = int(m["qty"].replace(",", "")) if m["qty"] else 1
        items[name] = items.get(name, 0) + qty
    return items


def parse_quest_row(row: dict[str, Any]) -> QuestInfo:
    extra = json.loads(row.get("json") or "{}")
    req_text = html.unescape(row.get("requirements") or extra.get("requirements") or "")
    skills: dict[str, int] = {}
    for m in _SKILLREQ.finditer(req_text):
        skills[m["skill"]] = max(skills.get(m["skill"], 0), int(m["level"]))
    links = [l.strip() for l in _LINK.findall(req_text) if not l.startswith(("File:", "Category:"))]
    return QuestInfo(
        name=row["page_name"],
        length=row.get("official_length") or extra.get("length"),
        skill_reqs=skills,
        linked_pages=list(dict.fromkeys(links)),
        items=parse_quest_items(extra.get("items") or ""),
    )


def fetch_quest(client: CachedClient, title: str) -> QuestInfo | None:
    if datapkg.exists("wiki_tables/quests.json"):
        return _packaged_quests().get(title)
    rows = bucket(client, "quest", TABLES["quests"][1], {"page_name": title}, limit=1)
    return parse_quest_row(rows[0]) if rows else None


def _packaged_quests() -> dict[str, QuestInfo]:
    global _quests
    if _quests is None:
        _quests = {r["page_name"]: parse_quest_row(r) for r in datapkg.load_json("wiki_tables/quests.json")}
    return _quests


_quests: dict[str, QuestInfo] | None = None


def fetch_all_quests(client: CachedClient) -> dict[str, QuestInfo]:
    if datapkg.exists("wiki_tables/quests.json"):
        return dict(_packaged_quests())
    return {r["page_name"]: parse_quest_row(r) for r in _rows(client, "quests")}


def fetch_all_achievements(client: CachedClient) -> list[dict[str, Any]]:
    return _rows(client, "achievements")


def members_only_achievements(client: CachedClient) -> set[str]:
    """Names of members-only achievements. The bucket's is_members_only column is empty, so read
    the "members" field ("Yes"/"No") from each row's json."""
    out = set()
    for row in fetch_all_achievements(client):
        extra = json.loads(row.get("json") or "{}")
        if str(extra.get("members", "")).strip().lower() == "yes":
            # Page names can carry a disambiguator, e.g. "X (miniquest) (achievement)"; the game uses "X (miniquest)".
            out.add(row["page_name"].removesuffix(" (achievement)"))
            if row.get("name"):
                out.add(row["name"])
    return out


def fetch_achievement_requirements(client: CachedClient, ironman: bool = False) -> dict[str, dict[str, Any]]:
    """{achievement name: requirement tree} from Bucket:Achievement_requirement (one cached download).

    A tree node is {"type": "and"|"or", "units": [...]} or a leaf: {"type": "skill", "name", "level"},
    {"type": "quest"|"achievement", "name", "partial"} or {"type": "intangible", "content"}.
    Ironman accounts use the "ironman" variant when the wiki has one.
    """
    rows = _rows(client, "achievement_requirements")
    out = {}
    for row in rows:
        raw = (row.get("ironman") if ironman else None) or row.get("regular")
        if raw:
            out[row["page_name"]] = json.loads(raw)
    return out
