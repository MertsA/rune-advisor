"""Build data/skilling_sim.json: the wiki calculators' data for simulating Woodcutting and Mining.

Usage: python scripts/build_skilling_sim.py <scratch dir>

Downloads the RuneScape Wiki's calculator data modules (Module:SkillUtils/TreeData and HatchetData
for Woodcutting, Module:MS Data and Module:MiningPicks/Data for Mining), evaluates them with lua5.4
(scripts/wiki_lua_to_json.lua) and keeps what src/rune_advisor/sim needs. Rocks the Mining calculator
doesn't have yet (new content such as the Exalted Quarry's essence deposits) are added from
Bucket:Infobox_ore (durability, hardness, xp multiplier). Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src/rune_advisor/data/skilling_sim.json"
MODULES = ["Module:SkillUtils/TreeData", "Module:SkillUtils/HatchetData", "Module:MS Data", "Module:MiningPicks/Data"]
_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")


def page_source(client: CachedClient, title: str) -> str:
    data = client.get_json(wiki.API, params={"action": "query", "prop": "revisions", "rvprop": "content",
                                             "rvslots": "main", "titles": title, "format": "json",
                                             "formatversion": 2}, ttl=7 * 24 * 3600)
    pages = data.get("query", {}).get("pages", [])
    revs = pages[0].get("revisions") if pages else None
    return revs[0]["slots"]["main"]["content"] if revs else ""


def plain(name: str) -> str:
    return _LINK.sub(r"\1", name or "").strip()


def main(scratch: Path) -> None:
    client = CachedClient()
    scratch.mkdir(parents=True, exist_ok=True)
    needed = set(MODULES) | {"Module:SkillUtils"}
    for title in needed:
        (scratch / (title.replace(":", "_").replace("/", "_").replace(" ", "_") + ".txt")).write_text(
            page_source(client, title))
    raw = json.loads(subprocess.run(["lua5.4", str(ROOT / "scripts/wiki_lua_to_json.lua"), str(scratch), *MODULES],
                                    capture_output=True, text=True, check=True).stdout)
    trees = [{k: t.get(k) for k in ("name", "logs", "level", "xp", "chance", "ratio", "fell", "is_menaphos",
                                     "is_bgh_tree", "can_crystallise")}
             for t in raw["Module:SkillUtils/TreeData"]]
    hatchets = [{k: h.get(k) for k in ("label", "level", "power", "proficiency", "augmentable")}
                for h in raw["Module:SkillUtils/HatchetData"]["all_hatchets"].values()] \
        if isinstance(raw["Module:SkillUtils/HatchetData"]["all_hatchets"], dict) \
        else [{k: h.get(k) for k in ("label", "level", "power", "proficiency", "augmentable")}
              for h in raw["Module:SkillUtils/HatchetData"]["all_hatchets"]]
    ms = raw["Module:MS Data"]
    rocks = []
    for r in ms["rocks"]:
        ores = r.get("name_GrandExchange") or [plain(r["name"])]
        rocks.append({"name": plain(r["name"]), "ore": ores[0], "level": r["level"], "durability": r["diff"],
                      "hardness": r["hardness"], "xp": r["xp"], "core": r.get("core") == 1, "drain": r.get("drain") == 1,
                      "ore_qty": r.get("ore", 1),
                      **{k: r.get(k) for k in ("crit_chance_level", "double_ore_chance", "rockertunity_level")}})
    known = {r["ore"] for r in rocks} | {r["name"] for r in rocks}
    for r in wiki.bucket_all(client, "infobox_ore", ["page_name", "level", "ore", "json"]):
        d = json.loads(r.get("json") or "{}")
        ore = (r.get("ore") or [None])[0]
        if not ore or ore in known or r["page_name"] in known or not d.get("hitpoint") or d.get("hardness") is None:
            continue
        mult = float(d.get("multiplier") or 0)
        rocks.append({"name": r["page_name"], "ore": ore, "level": int(r.get("level") or 1), "durability": d["hitpoint"],
                      "hardness": d["hardness"], "xp": mult / 100 if mult > 5 else mult, "core": False, "drain": False,
                      "ore_qty": d.get("ore_qty") or 1, "crit_chance_level": None, "double_ore_chance": None,
                      "rockertunity_level": None, "source": "Bucket:Infobox_ore"})
        known.add(ore)
    picks = [{"label": k, **{f: v.get(f) for f in ("level", "min", "max", "avg", "pen")}}
             for k, v in raw["Module:MiningPicks/Data"]["picks"].items()]
    mining = {k: ms[k] for k in ("base_xp", "swing", "swing_hr", "rck_hr", "rck_loss", "crit_chance_base", "crit_chances",
                                 "crit_bonus_level", "crit_bonus", "double_ore_chance_base", "double_ore_chances",
                                 "rockertunity_mult_base", "rockertunity_mult", "method_m")}
    OUT.write_text(json.dumps({"source": "RuneScape Wiki calculator modules (" + ", ".join(MODULES)
                                         + ") and Bucket:Infobox_ore, CC BY-NC-SA 3.0",
                               "trees": trees, "hatchets": sorted(hatchets, key=lambda h: (h["level"], h["power"])),
                               "rocks": rocks, "picks": sorted(picks, key=lambda p: (p["level"], p["avg"])),
                               "mining": mining}, indent=1))
    print(f"{len(trees)} trees, {len(hatchets)} hatchets, {len(rocks)} rocks, {len(picks)} pickaxes -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
