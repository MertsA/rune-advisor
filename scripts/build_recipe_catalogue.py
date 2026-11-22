"""Build data/recipe_catalogue.json: every wiki recipe for the production skills, one step each.

Usage: python scripts/build_recipe_catalogue.py

Magic gets one entry per teleport spell (Bucket:Infobox_spell): casting it "uses" its runes. The
elemental runes are left out, assuming an elemental staff supplies them.

The planner uses this to train from what's in the bank (recipes/catalogue.py): pouches from the
charms and shards you hold, bows from your logs, and so on. Recipes stay single-step so chains
form while planning (logs -> unstrung bow -> bow, each step giving xp).

Source: the wiki's Bucket:Recipe and Bucket:Infobox_spell (https://runescape.wiki, CC BY-NC-SA 3.0). Dungeoneering
(Daemonheim) recipes are dropped: their items never leave the dungeon.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402
from rune_advisor.xp import SKILL_ALIASES  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/recipe_catalogue.json"
SKILLS = {"Summoning", "Fletching", "Crafting", "Smithing", "Cooking", "Herblore", "Firemaking", "Construction", "Necromancy",
          "Prayer", "Runecraft", "Archaeology"}  # Archaeology: artefact restoration (damaged artefact + materials)
ELEMENTAL_RUNES = {"Air rune", "Water rune", "Earth rune", "Fire rune"}
# Conversions an item does once a day (Molten glass page: Desert amulets convert 50-150 soda ash daily).
DAILY_TOOLS = ("Desert amulet",)
_PLAIN = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")


def plain(text: str) -> str:
    return _PLAIN.sub(r"\1", text or "")


# Spells cast every `cooldown` ticks (Bucket:Infobox_spell); 4 when a teleport has none listed.
TELEPORT_TICKS = 4
# Skilling spells whose target is any item (alchemy): runes are the only modelled material.
ANY_ITEM_SPELLS = {"High Level Alchemy", "Low Level Alchemy"}
_QUOTED = re.compile(r"'([^']+)'")
_LINK = re.compile(r"\[\[([^\]|#]+)")


def _num(x, default=None):
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _skill(name: str) -> str:
    name = (name or "").strip().capitalize()
    return SKILL_ALIASES.get(name, name)


def entries(d: dict) -> list[dict]:
    names = [m["name"] for m in d.get("materials", [])] + [o["name"] for o in d.get("outputs", [])]
    # Daemonheim recipes whose items carry the dungeon in their names are kept, tagged: they never feed recipes
    # outside (the names don't match), and catalogue users that train skills leave them out.
    daemonheim = any("(Dungeoneering)" in n or "(Daemonheim)" in n for n in names)
    skills = [(_skill(s.get("name")), _num(s.get("level"), 1), _num(s.get("experience"), 0)) for s in d.get("skills", [])]
    # Skills whose level the wiki marks boostable for this recipe (unmarked: not assumed boostable).
    boostable = sorted({_skill(s.get("name")) for s in d.get("skills", []) if s.get("boostable") is True})
    materials = [[m["name"], _num(m.get("quantity"), 1)] for m in d.get("materials", [])]
    if not materials or not d.get("outputs"):
        return []
    out = d["outputs"][0]
    if not skills and d.get("skill") is False:
        # Skill-less steps (robust glass machine, NPC services) as conversions for gathering chains. A tool
        # (an amulet that converts) must be owned; DAILY_TOOLS' conversions are capped per day.
        tools = [t.strip() for t in re.split(r",|<br\s*/?>", plain(d.get("tool") or "")) if t.strip()]
        return [{**({"daemonheim": True} if daemonheim else {}), "skill": "", "output": out["name"], "output_qty": _num(out.get("quantity"), 1) or 1, "level": 1,
                 "xp": 0.0, "ticks": _num(d.get("ticks")), "materials": materials,
                 "members": str(d.get("members", "Yes")).lower() != "no",
                 "quests": _LINK.findall(d.get("quest") or ""), "other_skills": [],
                 "facility": d.get("facility") or "", "conversion": True, "tools": tools,
                 **({"daily": True} if any(t.startswith(DAILY_TOOLS) for t in tools) else {})}]
    found = []
    for skill, level, xp in skills:
        # 0-xp steps (exalting essence chunks at the colossus) are kept as conversions: the gathering
        # planner can use them in a chain, but they're never training.
        if not xp and len(skills) == 1:
            found.append({**({"daemonheim": True} if daemonheim else {}), "skill": skill, "output": out["name"], "output_qty": _num(out.get("quantity"), 1) or 1,
                          "level": int(level), "xp": 0.0, "ticks": _num(d.get("ticks")), "materials": materials,
                          "members": str(d.get("members", "Yes")).lower() != "no",
                          "quests": _LINK.findall(d.get("quest") or ""), "other_skills": [],
                          "facility": d.get("facility") or "", "conversion": True})
            continue
        if skill not in SKILLS or not xp:
            continue
        found.append({**({"daemonheim": True} if daemonheim else {}),
            "skill": skill, "output": out["name"], "output_qty": _num(out.get("quantity"), 1) or 1,
            "level": int(level), "xp": xp, "ticks": _num(d.get("ticks")),
            "materials": materials,
            "members": str(d.get("members", "Yes")).lower() != "no",
            "quests": _LINK.findall(d.get("quest") or ""),
            # Other skills the recipe needs (e.g. a Crafting level for a Fletching item).
            "other_skills": [[s, int(lvl)] for s, lvl, _ in skills if s != skill and lvl],
            "facility": d.get("facility") or "",
            # Cooking: the level that stops burning (with gauntlets when listed), for burn chances.
            **({"noburn": d.get("noburnlevel"), "gauntlets": d.get("gauntlets")} if skill == "Cooking" else {}),
            **({"boostable": boostable} if boostable else {}),
        })
    return found


def spell_entries(client: CachedClient) -> list[dict]:
    """Teleport spells and alchemy as Magic "recipes", cast every `cooldown` ticks (lodestones are free;
    other skilling spells need a target item the recipe can't name)."""
    out = []
    for r in wiki.bucket_all(client, "infobox_spell", ["page_name", "json", "is_members_only"]):
        d = json.loads(r["json"] or "{}")
        runes = [[x["name"], _num(x.get("qty"), 1)] for x in d.get("runes") or [] if x["name"] not in ELEMENTAL_RUNES]
        name = d.get("name") or r["page_name"]
        if (d.get("type") != "teleport" and name not in ANY_ITEM_SPELLS) or not runes or not _num(d.get("exp")):
            continue
        out.append({"skill": "Magic", "output": f"cast {name}", "output_qty": 1,
                    "level": int(_num(d.get("level"), 1)), "xp": _num(d.get("exp")),
                    "ticks": _num(d.get("cooldown")) or TELEPORT_TICKS,
                    "materials": runes, "members": r.get("is_members_only") is not False,
                    "quests": _QUOTED.findall(d.get("unlock") or ""), "other_skills": [],
                    "facility": d.get("spellbook") or ""})
    return out


# Gathering-skill "recipes" with no materials: the item a gathering action yields and its xp.
GATHERING = {"Woodcutting"}


def main() -> None:
    catalogue, seen, gathered = [], set(), {}
    client = CachedClient()
    for r in wiki.bucket_all(client, "recipe", ["page_name", "production_json"]):
        d = json.loads(r["production_json"] or "{}")
        skills = d.get("skills", [])
        if not d.get("materials") and d.get("outputs") and len(skills) == 1 and _skill(skills[0].get("name")) in GATHERING:
            xp, out = _num(skills[0].get("experience"), 0), d["outputs"][0]["name"]
            if xp:
                gathered.setdefault(out, {"skill": _skill(skills[0]["name"]), "output": out,
                                          "level": int(_num(skills[0].get("level"), 1)), "xp": xp,
                                          "members": str(d.get("members", "Yes")).lower() != "no"})
        for e in entries(d):
            key = json.dumps(e, sort_keys=True)
            if key not in seen:
                seen.add(key)
                catalogue.append(e)
    catalogue += spell_entries(client)
    catalogue.sort(key=lambda e: (e["skill"], e["level"], e["output"]))
    # Farming: what each seed grows (Bucket:Infobox_seed), for planning herbs etc. from patches.
    seeds = []
    for r in wiki.bucket_all(client, "infobox_seed", ["page_name", "level", "patch", "crop_json", "plant_experience",
                                                     "harvest_experience", "cycle", "cycle_length", "harvest_life",
                                                     "max_harvest_action", "yield_per_harvest", "check_experience"]):
        try:
            crops = [c["name"] for c in json.loads(r.get("crop_json") or "[]") if c.get("name")]
        except json.JSONDecodeError:
            crops = []
        if crops and r.get("cycle") and r.get("cycle_length"):
            seeds.append({"seed": r["page_name"], "level": int(_num(r.get("level"), 1)), "patch": r.get("patch") or [],
                          "crops": crops, "plant_xp": _num(r.get("plant_experience"), 0),
                          "harvest_xp": _num(r.get("harvest_experience"), 0),
                          "grow_minutes": int(r["cycle"]) * int(r["cycle_length"]),
                          # Yield: harvest lives (herbs, allotments) or a fixed number of harvests (trees, bushes).
                          "harvest_life": _num(r.get("harvest_life")), "max_harvests": _num(r.get("max_harvest_action")),
                          "yield_per_harvest": _num(r.get("yield_per_harvest")),
                          "check_xp": _num(r.get("check_experience"), 0)})
    # Bucket:Infobox_item sets is_members_only (to an empty value) only on members' items.
    members_items = sorted({r["page_name"] for r in wiki.bucket_all(client, "infobox_item",
                                                                    ["page_name", "is_members_only"])
                            if "is_members_only" in r})
    OUT.write_text(json.dumps({"source": "https://runescape.wiki Bucket:Recipe (CC BY-NC-SA 3.0)",
                               "recipes": catalogue, "members_items": members_items, "seeds": seeds,
                               "gathered": sorted(gathered.values(), key=lambda g: (g["skill"], g["level"]))},
                              ensure_ascii=False))
    print(f"{len(catalogue)} recipes -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
