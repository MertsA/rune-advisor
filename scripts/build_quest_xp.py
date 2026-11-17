"""Build data/quest_xp.json: xp rewards per (mini)quest from the RuneScape Wiki's "Quests/Experience rewards".

Usage: python scripts/build_quest_xp.py

Per quest:
  set:    {skill: xp} rewards with no choice; a lamp size ("small"...) where the table names a lamp
  choice: [{"xp": n | "<size> lamp", "skills": [...] | "any", "min_level": n}] lamps/choices
Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/quest_xp.json"
API = "https://runescape.wiki/api.php"
SKILLS = ["Agility", "Archaeology", "Attack", "Constitution", "Construction", "Cooking", "Crafting", "Defence",
          "Divination", "Dungeoneering", "Farming", "Firemaking", "Fishing", "Fletching", "Herblore", "Hunter",
          "Invention", "Magic", "Mining", "Necromancy", "Prayer", "Ranged", "Runecrafting", "Slayer", "Smithing",
          "Strength", "Summoning", "Thieving", "Woodcutting"]
LAMP = re.compile(r"\b(small|medium|large|huge)\b", re.I)
COMBAT = ["Attack", "Strength", "Defence", "Constitution", "Ranged", "Magic", "Prayer", "Summoning", "Necromancy"]


def availability(text: str) -> list[str] | str:
    """Skills a choice reward can go to: "any", or a list. Understands "Combat (no Prayer, Summoning)",
    "Combat (including Slayer, excluding Necromancy)", "Any (no Dungeoneering)", "Agility OR Thieving"."""
    low = text.lower()
    base = SKILLS if low.startswith("any") else COMBAT if low.startswith("combat") else []
    paren = re.search(r"\((.*)\)", text)
    inner = paren.group(1).lower() if paren else ""
    named = [s for s in SKILLS if s.lower() in (inner if base else low)]
    if not base:
        return named
    if not inner:
        # "Combat, Slayer or Divination": skills named beside "Combat" are added to it.
        return "any" if base is SKILLS else list(base) + [s for s in SKILLS if s.lower() in low and s not in base]
    # "(Attack, Defence & Constitution, Strength)": a plain list narrows the base to those skills.
    if not re.search(r"\b(no|excluding|excludes|including|includes)\b", inner):
        return named
    out = list(base)
    for clause in re.split(r",\s*(?=(?:no|excluding|excludes|including|includes)\b)", inner):
        skills = [s for s in SKILLS if s.lower() in clause]
        if clause.startswith(("no", "excluding", "excludes")):
            out = [s for s in out if s not in skills]
        else:
            out += [s for s in skills if s not in out]
    return out


class _Tables(HTMLParser):
    """Top-level tables in page order, each with the last h2/h3 heading before it. Cells keep only their
    own text: the quest cells nest a small "Including prerequisites" table, which is skipped."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.heading: str | None = None
        self.in_heading = False
        self.tables: list[tuple[str | None, list[list[str]]]] = []
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "h3") and self.depth == 0:
            self.in_heading, self.heading = True, ""
        elif tag == "table":
            self.depth += 1
            if self.depth == 1:
                self.tables.append((self.heading, []))
        elif self.depth == 1 and tag == "tr":
            self.tables[-1][1].append([])
        elif self.depth == 1 and tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("h2", "h3") and self.in_heading:
            self.in_heading = False
            self.heading = self.heading.replace("[edit | edit source]", "").strip()
        elif tag == "table":
            self.depth -= 1
        elif self.depth == 1 and tag in ("td", "th") and self.cell is not None:
            self.tables[-1][1][-1].append(" ".join("".join(self.cell).split()))
            self.cell = None

    def handle_data(self, data):
        if self.in_heading:
            self.heading += data
        elif self.depth == 1 and self.cell is not None:
            self.cell.append(data.replace("\xa0", " "))


def amount(cell: str) -> float | str | None:
    """A number of xp, or "<size> lamp" for a lamp-sized reward ("Medium", "Small XP lamp")."""
    num = re.search(r"\d[\d,]*", cell)
    if num and not LAMP.search(cell):
        return float(num.group().replace(",", ""))
    if m := LAMP.search(cell):
        return f"{m.group(1).lower()} lamp"
    return None


def main() -> None:
    data = CachedClient().get_json(API, {"action": "parse", "page": "Quests/Experience_rewards",
                                         "prop": "text", "format": "json"})
    page = data["parse"]["text"]["*"]
    parser = _Tables()
    parser.feed(page)
    out: dict[str, dict] = {}
    for section, table in parser.tables:
        if not table or not table[0]:
            continue
        head = [h.lower() for h in table[0]]
        if section in SKILLS and head[0] == "quest":
            for r in table[1:]:
                if len(r) < 3 or not r[0]:
                    continue
                xp = amount(r[2])
                if xp is not None:
                    out.setdefault(r[0], {"set": {}, "choice": []})["set"][section] = xp
        elif section == "Skill choice" and head[0] == "experience":
            for r in table[1:]:
                if len(r) < 5 or not r[4]:
                    continue
                xp = amount(r[0])
                if xp is None:
                    continue
                skills = availability(r[1])
                level = re.search(r"\d+", r[2] or "")
                out.setdefault(r[4], {"set": {}, "choice": []})["choice"].append(
                    {"xp": xp, "skills": skills, "min_level": int(level.group()) if level else 0})
    OUT.write_text(json.dumps(dict(sorted(out.items())), indent=1) + "\n")
    print(f"{len(out)} quests -> {OUT}")


if __name__ == "__main__":
    main()
