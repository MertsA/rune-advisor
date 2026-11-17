"""Invention perk data (data/perks.json) and the wiki's published gizmo odds (tests/fixtures/perk_checks.json).

    uv run python scripts/build_perks.py [--no-pages]

From the wiki perk calculator's data (MediaWiki:Gadget-perkcalc-data.js):
  * `materials`: per material and gizmo type (weapon/armour/tool), the perks it feeds as [perk, base, roll]
    in the calculator's order (the order matters: it breaks ties in the game's sort), and `ancient` for the
    four materials only an ancient gizmo takes;
  * `perks`: per perk its ranks as [threshold, cost, ancient_only] and `doubleslot` (Enhanced Devoted,
    Enhanced Efficient fill the whole gizmo).
From each perk's page (Infobox Perk): `desc`, the game's description of the effect.
`shells`: what each gizmo shell is made of (Module:Perk suggestions' table).

The check fixture is every `{{Perk calclink}}` / `{{Perk setup row}}` / `{{Perk suggestion}}` on the perk pages and
the two "Optimal ... perk setup" guides whose text states a probability: materials, gizmo, level, potion, the
wanted perks and the percentage the wiki's calculator showed. `rune_advisor.mechanics.perks` must reproduce them.

Source: RuneScape Wiki (https://runescape.wiki, CC BY-NC-SA 3.0).
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rune_advisor.config import USER_AGENT  # noqa: E402

OUT = ROOT / "src" / "rune_advisor" / "data" / "perks.json"
CHECKS = ROOT / "tests" / "fixtures" / "perk_checks.json"
GUIDES = ["Optimal PvM perk setup", "Optimal skilling perk setup"]
GIZMOS = ("weapon", "armour", "tool")
SHELLS = {
    "weapon": {"Blade parts": 10, "Crafted parts": 5, "Strong components": 2},
    "armour": {"Deflecting parts": 10, "Crafted parts": 5, "Protective components": 2},
    "tool": {"Head parts": 10, "Crafted parts": 5, "Precise components": 2},
    "ancient weapon": {"Blade parts": 20, "Historic components": 20, "Classic components": 2, "Strong components": 2},
    "ancient armour": {"Deflecting parts": 20, "Historic components": 20, "Classic components": 2,
                       "Protective components": 2},
    "ancient tool": {"Head parts": 20, "Historic components": 20, "Classic components": 2, "Precise components": 2},
}
# Perk pages whose title isn't the perk's name.
PAGE = {"Glow Worm": "Glow Worm", "Planted Feet": "Planted Feet"}


def wikitext(titles: list[str]) -> dict[str, str]:
    """Page title (as asked) -> wikitext, following redirects, 40 titles a request."""
    out: dict[str, str] = {}
    for i in range(0, len(titles), 40):
        chunk = titles[i:i + 40]
        url = "https://runescape.wiki/api.php?" + urllib.parse.urlencode(
            {"action": "query", "prop": "revisions", "rvprop": "content", "rvslots": "main", "redirects": 1,
             "titles": "|".join(chunk), "format": "json", "formatversion": 2})
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=60) as r:
            q = json.load(r)["query"]
        back = {}
        for step in (q.get("normalized") or []) + (q.get("redirects") or []):
            back[step["to"]] = back.pop(step["from"], step["from"])
        for page in q["pages"]:
            if "revisions" in page:
                out[back.get(page["title"], page["title"])] = page["revisions"][0]["slots"]["main"]["content"]
        time.sleep(0.5)
    return out


def calculator_data() -> dict:
    text = wikitext(["MediaWiki:Gadget-perkcalc-data.js"])["MediaWiki:Gadget-perkcalc-data.js"]
    return json.loads(text[text.index("{"):text.rindex("};") + 1])


_DESC = re.compile(r"\|\s*desc\s*=\s*(.*?)\n\s*\|", re.S)


def clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"<ref.*?(</ref>|/>)", "", text, flags=re.S)
    text = re.sub(r"\{\{[^{}]*\}\}", "", text)
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)
    return re.sub(r"\s+", " ", text.replace("'''", "").replace("''", "")).strip()


_TEMPLATE = re.compile(r"\{\{Perk (?:calclink|setup row|suggestion)\s*\|([^{}]*)\}\}", re.I)
_PCT = re.compile(r"Probability:\s*([\d.]+)%")
_WANT = re.compile(r"^(.*?)(?:\[(\d+)(?:\s*[-–]\s*(\d+))?\])?$")


def material_name(short: str, names: dict[str, str]) -> str | None:
    key = re.sub(r"\s*(components?|parts?)$", "", short.strip().lower())
    return names.get(key)


def checks(text: str, names: dict[str, str], perks: dict) -> list[dict]:
    """The templates on a page that state a probability, as calculator inputs."""
    perk_names = {p.lower(): p for p in perks}
    out = []
    for m in _TEMPLATE.finditer(text):
        args = [a.strip() for a in m[1].split("|")]
        named = dict(a.split("=", 1) for a in args if "=" in a)
        named = {k.strip(): v.strip() for k, v in named.items()}
        pct = _PCT.search(named.get("text", ""))
        mats = [material_name(a, names) for a in args if "=" not in a and a]
        if not pct or not mats or None in mats or "perks" not in named or "gizmo" not in named:
            continue
        want = []
        for part in re.sub(r"\s*\([^)]*\)", "", named["perks"]).split(","):
            w = _WANT.match(part.strip())
            perk = perk_names.get(w[1].strip().lower())
            if perk is None:
                want = None
                break
            # No rank given: any rank. One rank: exactly it. A range: within it.
            lo = int(w[2]) if w[2] else 1
            hi = int(w[3]) if w[3] else (int(w[2]) if w[2] else 99)
            want.append([perk, lo, hi])
        if not want:
            continue
        ancient = named.get("ancient", "").lower() in ("true", "yes", "1") or len(mats) > 5 \
            or any(names_ancient(mat) for mat in mats)
        out.append({"materials": mats, "gizmo": named["gizmo"].lower(), "ancient": ancient,
                    "level": int(named.get("level") or 120), "potion": named.get("potion", "none").lower(),
                    "want": want, "percent": float(pct[1])})
    return out


ANCIENT: set[str] = set()


def names_ancient(material: str) -> bool:
    return material in ANCIENT


def main() -> None:
    raw = calculator_data()
    materials = {}
    for name, comp in raw["comps"].items():
        if not any(comp[g] for g in GIZMOS):
            continue  # junk
        materials[name] = {g: [[e["perk"], e["base"], e["roll"]] for e in comp[g]] for g in GIZMOS}
        if comp.get("ancient"):
            materials[name]["ancient"] = True
            ANCIENT.add(name)
    perks = {}
    for name, perk in raw["perks"].items():
        if name == "No effect":
            continue
        perks[name] = {"ranks": [[r["threshold"], r["cost"], int(r["ancientOnly"])] for r in perk["ranks"]]}
        if perk.get("doubleslot"):
            perks[name]["doubleslot"] = True
    fixture: list[dict] = []
    if "--no-pages" not in sys.argv:
        short = {re.sub(r"\s*(components?|parts?)$", "", m.lower()): m for m in materials}
        # A perk sharing its name with something else lives at "<name> (perk)"; ask for both, keep the infobox one.
        titles = [t for p in perks for t in (PAGE.get(p, p), f"{PAGE.get(p, p)} (perk)")]
        pages = wikitext(titles + GUIDES)
        for name in perks:
            for title in (f"{PAGE.get(name, name)} (perk)", PAGE.get(name, name)):
                text = pages.get(title, "")
                if "Infobox Perk" in text:
                    if m := _DESC.search(text):
                        perks[name]["desc"] = clean(m[1])
                    for c in checks(text, short, perks):
                        fixture.append({"page": title, **c})
                    break
            else:
                print(f"no perk page found for {name}", file=sys.stderr)
        for guide in GUIDES:
            for c in checks(pages.get(guide, ""), short, perks):
                fixture.append({"page": guide, **c})
        seen, unique = set(), []
        for c in fixture:
            key = json.dumps({k: v for k, v in c.items() if k != "page"}, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique.append(c)
        CHECKS.write_text(json.dumps({"source": "https://runescape.wiki perk pages (CC BY-NC-SA 3.0)",
                                      "checks": unique}, indent=1))
        print(f"wrote {len(unique)} published probabilities to {CHECKS}")
    elif OUT.is_file():
        old = json.loads(OUT.read_text())["perks"]
        for name, perk in perks.items():
            if "desc" in old.get(name, {}):
                perk["desc"] = old[name]["desc"]
    OUT.write_text(json.dumps({"source": "https://runescape.wiki MediaWiki:Gadget-perkcalc-data.js, perk pages "
                                         "(CC BY-NC-SA 3.0)",
                               "shells": SHELLS, "materials": materials, "perks": perks},
                              separators=(",", ":"), sort_keys=True))
    print(f"wrote {len(materials)} materials, {len(perks)} perks to {OUT} "
          f"({sum(1 for p in perks.values() if 'desc' in p)} with a description)")


if __name__ == "__main__":
    main()
