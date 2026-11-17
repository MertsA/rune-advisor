"""Build data/mechanics.json: the wiki's mechanics data for the skill simulations in src/rune_advisor/mechanics/.

Usage: python scripts/build_mechanics.py <scratch dir>

Evaluates the RuneScape Wiki's calculator data modules with lua5.4 (scripts/wiki_lua_to_json.lua) and
queries its buckets, keeping what the sims need:
- mining: Module:MS Data's boost tables (Varrock armour, outfits, rings, familiars, perks, potions,
  skillchompas, pockets, spells) and per-rock extras (ore box capacity, stone spirits, Varrock tiers);
- smithing: Module:MS Data's forging constants (strikes, progress, heat, reheats, tick waste);
- fishing / thieving: Module:Fishing chance calculator/data, Module:Thieving chance calculator/data
  (the calculators' boost constants are transcribed in the sims, citing the modules);
- rituals: Module:Necromancy/rituals/data; farming: Module:Farming patch data;
- archaeology: Module:Skill calc/Archaeology/data (hotspots, perks, sprites; not the artefact lists);
- herblore: the boost tables of Module:Skill calc/Herblore/data (brooch, Botanist's amulet...);
- outfits: the outfits table of the Experience-boosting set page (pieces, boost);
- burn_charts: Cooking {{Skilling success chart}} low/high per food page;
- spells: Bucket:Infobox_spell (cooldown ticks, xp, runes); monsters: Bucket:Infobox_monster (life
  points, armour, defence, affinities, combat/slayer xp, attack rate); stackable items: Bucket:Infobox_item.
Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from build_skilling_sim import page_source, plain  # noqa: E402
from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src/rune_advisor/data/mechanics.json"
MODULES = ["Module:MS Data", "Module:Fishing chance calculator/data", "Module:Thieving chance calculator/data",
           "Module:Necromancy/rituals/data", "Module:Farming patch data", "Module:Skill calc/Archaeology/data",
           "Module:Skill calc/Herblore/data", "Module:SkillUtils/SeedData"]
MINING_BOOSTS = ["varrock", "outfit_m", "outfit_s", "rings", "luck", "familiar_m", "honed", "fortune", "wise",
                 "furnace", "impsouled", "careless", "explosive", "cape", "mining_potion", "skillchompa", "pocket", "spells_m", "avatar"]
SMITHING = ["base_progress", "progress_mult", "strike", "heat_loss", "high_heat", "low_heat", "max_heat_inc",
            "reheat_base", "reheats", "tickwaste", "tickwaste_afk", "tickwaste_max", "tw_delay", "tw_interface",
            "tw_itemforge", "tw_turn", "smelt_rate", "smelt_rate_base", "double_bars", "bar_contain", "bar_mult",
            "method_s", "rapid", "tinker", "metals", "smithing_cape", "burial_set", "burial_set_xp",
            "burial_set_mats", "burial_items"]
MONSTER_FIELDS = ["life_points", "armour", "defence_level", "melee_affinity", "ranged_affinity", "magic_affinity",
                  "weakness_affinity", "weakness_by_class", "combat_experience", "slayer_experience", "slayer_level",
                  "attack_rate", "combat_level", "is_members_only", "slayer_category"]


def outfits(client: CachedClient) -> list[dict]:
    """[{skill, set, pieces, boost}] from the Skilling outfits page's standard outfits table."""
    text = page_source(client, "Experience-boosting set")
    section = text.split("===Outfits===", 1)[1].split("\n==", 1)[0]
    out = []
    for row in section.split("\n|-"):
        skill = re.search(r"\{\{Sc\|([A-Za-z]+)\}\}", row)
        pieces = re.search(r"\{\{plinkp multiple\|([^}]*)\}\}", row)
        boost = re.search(r"Up to (\d+(?:\.\d+)?)%", row)
        name = re.search(r"\|\|\s*\[\[([^\]|]+)", row)
        if skill and pieces and boost:
            out.append({"skill": skill.group(1), "set": name.group(1) if name else "",
                        "pieces": [p.strip() for p in pieces.group(1).split("|") if "=" not in p],
                        "boost": float(boost.group(1)) / 100})
    return out


def burn_charts(client: CachedClient) -> dict[str, list[float]]:
    """{food page: [low, high]} from the pages' {{Skilling success chart}} with a Cooking x-axis (the
    first, base, series: success = (interp(low, high, level) + 1) / 256)."""
    titles, cont = [], {}
    while True:
        d = client.get_json(wiki.API, params={"action": "query", "list": "embeddedin", "eititle": "Template:Skilling success chart",
                                              "eilimit": 500, "format": "json", **cont}, ttl=7 * 24 * 3600)
        titles += [p["title"] for p in d["query"]["embeddedin"]]
        if "continue" not in d:
            break
        cont = d["continue"]
    out = {}
    for title in titles:
        for m in re.finditer(r"\{\{Skilling success chart([^}]*)\}\}", page_source(client, title)):
            body = m.group(1)
            if "Cooking" not in body:
                continue
            low, high = re.search(r"\|\s*low1\s*=\s*(-?\d+)", body), re.search(r"\|\s*high1\s*=\s*(-?\d+)", body)
            if low and high:
                out[title] = [int(low.group(1)), int(high.group(1))]
                break
    return out


def spells(client: CachedClient) -> list[dict]:
    out = []
    for r in wiki.bucket_all(client, "infobox_spell", ["page_name", "json", "is_members_only"]):
        d = json.loads(r["json"] or "{}")
        out.append({"name": d.get("name") or r["page_name"], "type": d.get("type"), "level": d.get("level"),
                    "xp": d.get("exp"), "cooldown": d.get("cooldown"), "spellbook": d.get("spellbook"),
                    "runes": [[x["name"], x.get("qty", 1)] for x in d.get("runes") or []],
                    "members": r.get("is_members_only") is not False})
    return out


def monsters(client: CachedClient) -> dict[str, dict]:
    """{page (version): stats}; versions (phases, styles) keep their own row."""
    out = {}
    for r in wiki.bucket_all(client, "infobox_monster", ["page_name", "page_name_sub", "json"]):
        d = json.loads(r.get("json") or "{}")
        if not d.get("life_points"):
            continue
        key = r.get("page_name_sub") or r["page_name"]
        out[key] = {"page": r["page_name"], **{f: d.get(f) for f in MONSTER_FIELDS if d.get(f) not in (None, "")}}
    return out


def main(scratch: Path) -> None:
    client = CachedClient()
    scratch.mkdir(parents=True, exist_ok=True)
    for title in MODULES:
        (scratch / (title.replace(":", "_").replace("/", "_").replace(" ", "_") + ".txt")).write_text(
            page_source(client, title))
    raw = json.loads(subprocess.run(["lua5.4", str(ROOT / "scripts/wiki_lua_to_json.lua"), str(scratch), *MODULES],
                                    capture_output=True, text=True, check=True).stdout)
    ms = raw["Module:MS Data"]
    rock_extras = {plain(r["name"]): {"spirits": bool(r.get("spirits")), "ore_box": r.get("ore_box"),
                                      "varrock": r.get("varrock"), "varrock_no": r.get("varrock_no"),
                                      "urns": bool(r.get("urns"))} for r in ms["rocks"]}
    arch = raw["Module:Skill calc/Archaeology/data"]
    herb = raw["Module:Skill calc/Herblore/data"]
    out = {
        "source": "RuneScape Wiki calculator modules (" + ", ".join(MODULES) + "), Experience-boosting set, "
                  "Bucket:Infobox_spell, Bucket:Infobox_monster; CC BY-NC-SA 3.0",
        "mining": {"boosts": {k: ms.get(k) for k in MINING_BOOSTS}, "rocks": rock_extras},
        "smithing": {k: ms.get(k) for k in SMITHING},
        "fishing": raw["Module:Fishing chance calculator/data"],
        "thieving": raw["Module:Thieving chance calculator/data"],
        "rituals": raw["Module:Necromancy/rituals/data"],
        "farming": raw["Module:Farming patch data"],
        # Per seed, the low/high roll of keeping a harvest life (Module:SkillUtils/SeedData).
        "seed_rolls": {r["seed"]: [r["low"], r["high"]] for r in raw["Module:SkillUtils/SeedData"]},
        "archaeology": {**{k: v for k, v in arch.items() if k not in ("artefacts", "collections", "collectors")},
                        # Restoration xp per damaged artefact (the calculator's total includes restoring).
                        "artefact_xp": {k: v.get("xp") for k, v in (arch.get("artefacts") or {}).items()
                                        if isinstance(v, dict) and v.get("xp")}},
        "herblore": {k: v for k, v in herb.items() if isinstance(v, dict) and "affects" in v},
        "outfits": outfits(client),
        "spells": spells(client),
        "burn_charts": burn_charts(client),
        "monsters": monsters(client),
        # Bucket:Infobox_item sets `stackable` (to an empty value) only on stackable items.
        "stackable": sorted({r["page_name"] for r in wiki.bucket_all(client, "infobox_item", ["page_name", "stackable"])
                             if "stackable" in r}),
    }
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(f"{len(out['fishing'])} fish, {len(out['thieving'])} pickpocket targets, {len(out['farming'])} seeds, "
          f"{len(out['archaeology'].get('hotspots', {}))} hotspots, {len(out['outfits'])} outfits, "
          f"{len(out['spells'])} spells, {len(out['monsters'])} monsters -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
