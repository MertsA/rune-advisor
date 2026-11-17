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

from ..http import CachedClient

API = "https://runescape.wiki/api.php"

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
    rows = bucket(client, "quest", ["page_name", "official_length", "requirements", "json"], {"page_name": title}, limit=1)
    return parse_quest_row(rows[0]) if rows else None


def fetch_all_quests(client: CachedClient) -> dict[str, QuestInfo]:
    rows = bucket_all(client, "quest", ["page_name", "official_length", "requirements", "json"])
    return {r["page_name"]: parse_quest_row(r) for r in rows}


def fetch_all_achievements(client: CachedClient) -> list[dict[str, Any]]:
    return bucket_all(client, "achievement", ["page_name", "name", "id", "runescore", "maincategory",
                                              "subcategory", "difficulty", "json"])


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
    rows = bucket_all(client, "achievement_requirement", ["page_name", "regular", "ironman"])
    out = {}
    for row in rows:
        raw = (row.get("ironman") if ironman else None) or row.get("regular")
        if raw:
            out[row["page_name"]] = json.loads(raw)
    return out
