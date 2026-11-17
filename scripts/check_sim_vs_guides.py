"""Compare the Woodcutting/Mining simulation (sim.py) with the wiki's money making guides.

Usage: python scripts/check_sim_vs_guides.py [--all] [--plain]

Each "Money making guide/..." page whose {{Mmgtable}} gives Mining or Woodcutting xp is one real
player's rate: items per hour per output at the guide's recommended level and gear. The sim is run at
that level with the best tool for it and the guide's own setup (the items it lists count as owned;
its perks, familiar, potions, juju and porters are applied; --plain skips that), and its best rate
for each output item is printed beside the guide's, with the ratio and GE gp/h. Ratios far from 1, or an
output the sim gives as another item (granite sizes), point at sim data to fix. Prints only
mismatches (ratio outside 0.5-2) unless --all. Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from build_boss_rates import arithmetic, template_params  # noqa: E402
from build_skilling_sim import page_source  # noqa: E402
from rune_advisor import sim  # noqa: E402
from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.models import PlayerState, Skill  # noqa: E402
from rune_advisor.prices import Prices  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402
from rune_advisor.xp import xp_for_level  # noqa: E402

SKILLS = {"Mining": sim.mining_rates, "Woodcutting": sim.woodcutting_rates}


def guide_titles(client: CachedClient) -> list[str]:
    titles, cont = [], {}
    while True:
        d = client.get_json(wiki.API, params={"action": "query", "list": "allpages", "apprefix": "Money making guide/",
                                              "aplimit": 500, "format": "json", **cont}, ttl=7 * 24 * 3600)
        titles += [p["title"] for p in d["query"]["allpages"]]
        if "continue" not in d:
            return titles
        cont = d["continue"]


def guide_setup(player: PlayerState, skill: str, params: dict[str, str]) -> None:
    """Give the sim the guide's own setup: the items it lists (as owned), its perks, familiar, potions,
    juju and porters, so a mismatch points at the mechanics rather than the gear."""
    text = params.get("Item", "") + params.get("Other", "")
    items = {m.strip() for m in re.findall(r"\{\{mmgreq\|([^|}]+)", text)}
    player.inventories["BANK"] = {i: 1 for i in items}
    tool = "pickaxe" if skill == "Mining" else "hatchet"
    perks = [f"{n} {r}" for n, r in re.findall(r"\{\{perk link\|([A-Za-z ]+)\|(\d)", text)]
    boosts: dict = {"perks": {tool: perks}} if perks else {}
    familiars = ((sim.mechanics_data().get("mining") or {}).get("boosts") or {}).get("familiar_m") or {}
    if fam := next((f for f in familiars if f != "None" and f.lower() in text.lower()), None):
        boosts["familiar"] = {skill: fam}
    if pot := next((i for i in items if i.lower().endswith(f"{skill.lower()} potion") or "gatherer" in i.lower()), None):
        boosts["potions"] = {skill: pot}
    if "juju" in text.lower():
        boosts["juju"] = [skill]
    if "porter" in text.lower() or "Grace of the elves" in items:
        boosts["porters"] = True
    if "stone spirit" in text.lower():
        boosts["stone_spirits"] = True
    details = (params.get("Details", "") + params.get("Other", "")).lower()
    if skill == "Mining" and ("4-tick" in details or "full stamina" in details):
        boosts["mining_method"] = "4-Ticking + Rockertunities"
    player.boosts = boosts


def main() -> None:
    show_all = "--all" in sys.argv
    client, prices = CachedClient(), Prices()
    rows = []
    for title in guide_titles(client):
        if not re.search(r"Mining|Cutting|Chopping|Woodcutting|logs|ore", title):
            continue  # skip fetching unrelated guides (the cache keeps them a week anyway)
        params = template_params(page_source(client, title), "Mmgtable")
        if not params or params.get("isperkill", "").lower().startswith("y"):
            continue
        skill = next((params[k] for k in params if re.fullmatch(r"Experience\d+", k) and params[k] in SKILLS), None)
        if skill is None:
            continue
        level = max((int(lvl) for s, lvl in re.findall(r"\{\{mmgreq\|([A-Z][a-z]+)\|(\d+)", params.get("Skill", ""))
                     if s == skill), default=1)
        player = PlayerState(name="guide", members=True, skills={
            skill: Skill(skill, level, xp_for_level(level)), "Strength": Skill("Strength", 99, xp_for_level(99))})
        if "--plain" not in sys.argv:
            guide_setup(player, skill, params)
            # Guides state a minimum level but list gear that needs more (a pickaxe of earth and song for
            # banite at "80"): rate it at the level the listed tool needs.
            tools = sim.data()["picks" if skill == "Mining" else "hatchets"]
            kind = "pickaxe" if skill == "Mining" else "hatchet"
            need = max((t["level"] for t in tools if sim.owns_tool(player, t["label"], kind)), default=level)
            if need > level:
                level = need
                player.skills[skill] = Skill(skill, level, xp_for_level(level))
        rates = SKILLS[skill](player)
        xp_key = next(k for k in params if re.fullmatch(r"Experience\d+", k) and params[k] == skill)
        guide_xp = arithmetic(params.get(f"{xp_key}num", "")) or 0
        for k, item in params.items():
            m = re.fullmatch(r"Output(\d+)", k)
            if not m or not item:
                continue
            per_hour = arithmetic(params.get(f"Output{m.group(1)}num", ""))
            if not per_hour:
                continue
            sim_rate = max((r for r in rates if r.yields().get(item)), key=lambda r: r.yields()[item], default=None)
            v = prices.value(item)
            rows.append((title.removeprefix("Money making guide/"), level, item, per_hour,
                         sim_rate.yields()[item] if sim_rate else None, sim_rate.activity if sim_rate else "",
                         v.gp if v else None, guide_xp, sim_rate.xp_per_hour if sim_rate else 0))
    print(f"{'guide':50} {'lvl':>3} {'item':24} {'guide/h':>8} {'sim/h':>8} {'ratio':>6} {'guide xp':>9} {'sim xp':>9} "
          f"{'guide gp/h':>11}  sim rock")
    for title, level, item, g, s, rock, gp, gxp, sxp in rows:
        ratio = s / g if s else None
        if not show_all and ratio is not None and 0.5 <= ratio <= 2:
            continue
        gph = f"{g * gp:,.0f}" if gp else "-"
        print(f"{title[:50]:50} {level:>3} {item[:24]:24} {g:>8,.0f} {s or 0:>8,.0f} "
              f"{(f'{ratio:.2f}' if ratio else '-'):>6} {gxp:>9,.0f} {sxp:>9,.0f} {gph:>11}  {rock}")


if __name__ == "__main__":
    main()
