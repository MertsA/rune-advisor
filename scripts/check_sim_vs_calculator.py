"""Compare sim.py's Mining and Woodcutting rates with the wiki's own calculators, run live.

Usage: python scripts/check_sim_vs_calculator.py

The wiki renders {{#invoke:Mining Rate Calculator Module|main|...}} for any parameters through the
parse API, so the calculator itself is the reference: for a few setups (level, pickaxe, method,
Varrock armour, Honed/Fortune perks) this prints the calculator's average xp/h and ore/h beside the
sim's (raw ore/h, before bank trips) for every rock both know, and the worst ratios. A mismatch is a
porting bug; a match means differences with the money making guides come from the guides.
Woodcutting runs Template:Woodcutting chance calculator (logs/h and xp/h; the sim's walk to the next
tree after one falls is left out for the comparison, as the calculator has none).
Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor import sim  # noqa: E402
from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.models import PlayerState, Skill  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402
from rune_advisor.xp import xp_for_level  # noqa: E402

SETUPS = [
    {"mining_level": 50, "pickaxe": "Rune", "method": "Semi-AFK + Rockertunities"},
    {"mining_level": 90, "pickaxe": "Crystal", "method": "Semi-AFK + Rockertunities"},
    {"mining_level": 99, "pickaxe": "Elder rune + 5", "method": "4-Ticking + Rockertunities",
     "varrock": "Varrock armour 4", "honed": "Honed 6", "fortune": "Fortune 3"},
]


def calculator(client: CachedClient, setup: dict) -> dict[str, tuple[float, float]]:
    # The calculator's own pickaxe keys drop the spaces around "+" ("Elder rune+5").
    args = "|".join(f"{k}={v.replace(' + ', '+') if k == 'pickaxe' else v}" for k, v in {"str_level": 99, **setup}.items())
    d = client._client.post(wiki.API, data={"action": "parse", "format": "json", "prop": "text", "contentmodel": "wikitext",
                                            "text": "{{#invoke:Mining Rate Calculator Module|main|" + args + "}}"}).json()
    out = {}
    for r in re.findall(r"<tr>(.*?)</tr>", d["parse"]["text"]["*"], re.S):
        cells = [html.unescape(re.sub("<[^>]+>", "", c)).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if len(cells) >= 10 and cells[0].isdigit():
            try:
                out[cells[1]] = tuple(float(cells[i].replace(",", "").split()[0]) for i in (6, 8))  # avg xp/h, ore/h
            except (IndexError, ValueError):
                continue
    return out


WC_SETUPS = [
    {"level": 60, "hatchet": "rune"},
    {"level": 90, "hatchet": "crystal", "ring": "yes", "outfit": "Nature's sentinel"},
    {"level": 99, "hatchet": "dragon", "honed": 4, "juju": "yes", "familiar": "Beaver"},
]


def _render(client: CachedClient, text: str) -> list[list[str]]:
    d = client._client.post(wiki.API, data={"action": "parse", "format": "json", "prop": "text",
                                            "contentmodel": "wikitext", "text": text}).json()
    return [[html.unescape(re.sub("<[^>]+>", "", c)).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            for r in re.findall(r"<tr>(.*?)</tr>", d["parse"]["text"]["*"], re.S)]


def woodcutting(client: CachedClient) -> list[tuple]:
    out = []
    for setup in WC_SETUPS:
        rows = _render(client, "{{Woodcutting chance calculator|" + "|".join(f"{k}={v}" for k, v in setup.items()) + "}}")
        level = setup["level"]
        p = PlayerState(name="calc", members=True, skills={"Woodcutting": Skill("Woodcutting", level, xp_for_level(level))})
        p.inventories["BANK"] = {"Ring of Whispers": 1} if setup.get("ring") else {}
        if setup.get("outfit"):
            p.inventories["BANK"]["Nature's sentinel hat"] = 1
        p.boosts = {"perks": {"hatchet": [f"Honed {setup['honed']}"]} if setup.get("honed") else {},
                    "juju": ["Woodcutting"] if setup.get("juju") else [],
                    "familiar": {"Woodcutting": setup["familiar"]} if setup.get("familiar") else {}}
        hatchet = next(h for h in sim.data()["hatchets"] if h["label"].lower() == setup["hatchet"])
        mine = {r.activity: r for r in sim.woodcutting_rates(p, hatchet=hatchet, fell_walk=False)}
        print(f"== woodcutting {setup}")
        for cells in rows[1:]:
            if len(cells) < 6 or cells[0] not in mine:
                continue
            clph, cxph = (float(cells[i].replace(",", "")) for i in (4, 5))
            r = mine[cells[0]]
            lph = r.raw_items_per_hour or r.items_per_hour
            out.append((abs(lph / clph - 1) + abs(r.xp_per_hour / cxph - 1), cells[0], level, r.xp_per_hour / cxph, lph / clph))
            print(f"  {cells[0]:28} logs {clph:>7,.0f} sim {lph:>7,.0f} ({lph / clph:.2f})   xp {cxph:>9,.0f} sim "
                  f"{r.xp_per_hour:>9,.0f} ({r.xp_per_hour / cxph:.2f})")
    return out


def main() -> None:
    client = CachedClient()
    worst = []
    for setup in SETUPS:
        level = setup["mining_level"]
        p = PlayerState(name="calc", members=True, skills={"Mining": Skill("Mining", level, xp_for_level(level)),
                                                            "Strength": Skill("Strength", 99, xp_for_level(99))})
        p.inventories["BANK"] = {setup["varrock"]: 1} if setup.get("varrock") else {}
        perks = [setup[k] for k in ("honed", "fortune") if setup.get(k)]
        p.boosts = {"perks": {"pickaxe": perks}, "mining_method": setup["method"]} if perks else {"mining_method": setup["method"]}
        p.tools["pickaxe"] = setup["pickaxe"]
        mine = {r.activity: r for r in sim.mining_rates(p)}
        calc = calculator(client, setup)
        print(f"== {setup}")
        for rock, (cxp, core) in calc.items():
            r = mine.get(rock)
            if r is None or not core:
                continue
            xr, orr = r.xp_per_hour / cxp if cxp else 0, (r.raw_items_per_hour or r.items_per_hour) / core
            worst.append((abs(orr - 1) + abs(xr - 1), rock, setup["mining_level"], xr, orr))
            print(f"  {rock:28} xp {cxp:>9,.0f} sim {r.xp_per_hour:>9,.0f} ({xr:.2f})   ore {core:>9,.1f} "
                  f"sim {(r.raw_items_per_hour or r.items_per_hour):>9,.1f} ({orr:.2f})")
    worst += woodcutting(client)
    worst.sort(reverse=True)
    print("worst:", [(w[1], w[2], round(w[3], 2), round(w[4], 2)) for w in worst[:8]])


if __name__ == "__main__":
    main()
