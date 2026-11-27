"""Build data/fort_forinthry.json: Fort Forinthry's nine buildings, each tier's Construction level, experience,
materials and benefits, from the RuneScape Wiki's "<Building> tiers" templates, plus the other blueprints on the
Fort Forinthry page (eternal reinforcement, the lodestone, the side gate, the eastern border wall).

Usage: python scripts/build_fort_forinthry.py

Hand-written here, from the Fort Forinthry page's text: each building's quest gates (`QUESTS`: a quest that must be
started for tier 1 and finished for tier 2 up, or finished for every tier), the player variable that holds its tier
(`VARS`, from the dev plugin's var dump; PLAYER_FORT_BUILDING_TOWNHALL read 3 on a fort built to tier 3, beta
2026-11-26), and the typical experience an hour of building each tier, taken from data/wiki_rates.json's "Fort
building" rows (the Construction training guide).
Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = ROOT / "src/rune_advisor/data/fort_forinthry.json"
RATES = ROOT / "src/rune_advisor/data/wiki_rates.json"

BUILDINGS = ["Workshop", "Town Hall", "Chapel", "Command Centre", "Kitchen", "Guardhouse", "Grove",
             "Ranger's Workroom", "Botanist's Workbench"]
VARS = {"Workshop": "PLAYER_FORT_BUILDING_WORKSHOP", "Town Hall": "PLAYER_FORT_BUILDING_TOWNHALL",
        "Chapel": "PLAYER_FORT_BUILDING_CHAPEL", "Command Centre": "PLAYER_FORT_BUILDING_COMMAND_CENTRE",
        "Kitchen": "PLAYER_FORT_BUILDING_KITCHEN", "Guardhouse": "PLAYER_FORT_BUILDING_GUARDHOUSE",
        "Grove": "PLAYER_FORT_BUILDING_GROVE", "Ranger's Workroom": "PLAYER_FORT_BUILDING_RANGERS_WORKROOM",
        "Botanist's Workbench": "PLAYER_FORT_BUILDING_BOTANISTS_WORKBENCH"}
# Quest gates (the Fort Forinthry page): every building but the tier 1 Workshop needs New Foundations nearly done
# (counted as done); "start" quests must be begun for tier 1 and finished from tier 2. The page states no quest
# for the Town Hall, Chapel and Command Centre beyond New Foundations.
QUESTS = {
    "Workshop": {"done": [], "start": ["New Foundations"]},  # tier 1 is built during the quest
    "Town Hall": {"done": ["New Foundations"]},
    "Chapel": {"done": ["New Foundations"]},
    "Command Centre": {"done": ["New Foundations"]},
    "Kitchen": {"done": ["New Foundations"], "start": ["Murder on the Border"]},
    "Guardhouse": {"done": ["New Foundations"], "start": ["Unwelcome Guests"]},
    "Grove": {"done": ["New Foundations", "Unwelcome Guests"]},
    "Ranger's Workroom": {"done": ["New Foundations"], "start": ["Dead and Buried"]},
    "Botanist's Workbench": {"done": ["New Foundations", "Ancient Awakening"]},
}
# The Fort Forinthry page's other blueprints, as its tables give them.
EXTRAS = {
    "Eternal reinforcement": {"level": 105, "xp": 499_200, "materials": {"Eternal frame": 65, "Stone wall segment": 6},
                              "repeatable": True, "note": "needs a building at tier 3; reinforces one at random"},
    "Fort Forinthry lodestone": {"level": 1, "xp": 1_100, "materials": {"Limestone brick": 10}},
    "Side gate": {"level": 50, "xp": 1_100, "materials": {"Stone wall segment": 5}, "note": "needs the Grove at tier 1"},
    "Eastern border wall": {"level": 1, "xp": 3_360, "materials": {"Stone wall segment": 6, "Logs": 6},
                            "note": "needs Unwelcome Guests; before the Grove"},
}
ROW = re.compile(r'\|- data-rowid="Tier (\d)"(.*?)(?=\|- )', re.S)
AMOUNT = re.compile(r"\{\{plink amount\|([^|}]+)\|([\d,]+)")
LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
TEMPLATE = re.compile(r"\{\{(?:[^{}|]*\|)*([^{}|]*)\}\}")


def _page(client: CachedClient, title: str) -> str:
    r = client.get_json(wiki.API, params={"action": "parse", "page": title, "prop": "wikitext", "redirects": 1,
                                          "format": "json"})
    return r["parse"]["wikitext"]["*"] if "parse" in r else ""


def _plain(text: str) -> str:
    text = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", "", text, flags=re.S)
    text = LINK.sub(r"\1", text)
    text = re.sub(r"\{\{(?:No)?[Cc]oins\|([\d,]+)[^}]*\}\}", r"\1 gp", text)  # {{NoCoins|750|c}} -> 750 gp
    for _ in range(3):
        text = TEMPLATE.sub(r"\1", text)
    return re.sub(r"\s+", " ", text.replace("'''", "")).strip()


def tiers(text: str) -> list[dict]:
    out = []
    for m in ROW.finditer(text + "\n|- "):
        cells = [c.strip() for c in re.split(r"^\|(?!-)", m.group(2), flags=re.M)[1:]]
        level, xp, materials, benefits = cells[1], cells[2], cells[3], cells[4]
        out.append({
            "tier": int(m.group(1)), "level": int(level), "xp": int(xp.replace(",", "")),
            "materials": {item.strip(): int(n.replace(",", "")) for item, n in AMOUNT.findall(materials)},
            "benefits": [("  " if line.startswith("**") else "") + _plain(line.lstrip("*"))
                         for line in benefits.splitlines() if line.startswith("*")],
        })
    return out


def main() -> None:
    client = CachedClient()
    rates = {r["method"]: r["xp_per_hour_low"] for r in json.loads(RATES.read_text())
             if r["method"].startswith("Fort building: ")}
    buildings = {}
    for name in BUILDINGS:
        rows = tiers(_page(client, f"Template:{name} tiers"))
        if len(rows) != 3:
            raise SystemExit(f"{name}: expected 3 tiers, read {len(rows)}")
        for row in rows:
            row["xp_per_hour"] = rates.get(f"Fort building: {name} (tier {row['tier']})")
        buildings[name] = {"var": VARS[name], **QUESTS[name], "tiers": rows}
    EXTRAS["Eternal reinforcement"]["xp_per_hour"] = rates.get("Fort building: Eternal reinforcement (tier 3)")
    out = {"source": "RuneScape Wiki: Fort Forinthry and the '<Building> tiers' templates; xp an hour from the "
                     "Construction training guide (CC BY-NC-SA 3.0)",
           "buildings": buildings, "extras": EXTRAS}
    OUT.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(buildings)} buildings -> {OUT}")


if __name__ == "__main__":
    main()
