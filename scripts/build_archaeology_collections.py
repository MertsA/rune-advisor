"""Build data/archaeology_collections.json: each Archaeology collection's artefacts, the chronotes a completion pays
and its other repeat rewards, plus each artefact's chronotes, from the RuneScape Wiki.

Usage: python scripts/build_archaeology_collections.py

Artefacts and their chronotes: Bucket:Infobox_artefact. Collections: Bucket:Infobox_collection names them; each
collection page's Collections table lists the artefacts and the completion bonus, and its "Recurring completion"
drops table the rewards every completion gives (two large Dungeoneering token boxes for Dragonkin V). A completion
pays the artefacts' chronotes plus the bonus (Chronotes page: "a set bonus of 100%"). The planner turns these into
hand-in recipes (archaeology_collections in catalogue.py). Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/archaeology_collections.json"
TABLE = re.compile(r"\{\{Collections table(.*?)\}\}", re.S)
RECURRING = re.compile(r"Recurring completion(.*?)DropsTableBottom", re.S)
DROP = re.compile(r"DropsLine\w*\|name=([^|}]+)\|quantity=([\d,.]+)")


def _num(x) -> float:
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def parse_collection(wikitext: str) -> dict | None:
    m = TABLE.search(wikitext)
    if not m:
        return None
    artefacts, bonus = [], 0.0
    for line in m.group(1).split("\n"):
        line = line.strip()
        if not line.startswith("|"):
            continue
        key, _, value = line[1:].partition("=")
        if value and key.strip() == "collectionCompletionChronotesBonus":
            bonus = _num(value)
        elif not value:
            artefacts.append(line[1:].strip())
    rewards = {}
    if rec := RECURRING.search(wikitext):
        found = [(n.strip(), _num(q)) for n, q in DROP.findall(rec.group(1)) if n.strip() != "Chronotes"]
        # Tetracompass pieces come one a completion, in order (left, right, dial, needle): a quarter of each.
        rotating = "name=tetracompass" in rec.group(1)
        for name, qty in found:
            rewards[name] = qty / len(found) if rotating and name.startswith("Tetracompass piece") else qty
    level = re.search(r"\|archlevel\s*=\s*(\d+)", wikitext)
    members = re.search(r"\|members\s*=\s*(\w+)", wikitext)
    return {"artefacts": artefacts, "bonus_chronotes": bonus, "rewards": rewards,
            "level": int(level.group(1)) if level else 1,
            "members": not (members and members.group(1).lower() == "no")}


def main() -> None:
    client = CachedClient()
    chronotes = {r["page_name"]: _num(r.get("chronotes"))
                 for r in wiki.bucket_all(client, "infobox_artefact", ["page_name", "chronotes"])}
    out = {}
    for r in wiki.bucket_all(client, "infobox_collection", ["page_name"]):
        page = client.get_json(wiki.API, params={"action": "parse", "page": r["page_name"], "prop": "wikitext",
                                                 "redirects": 1, "format": "json"})
        c = parse_collection(page["parse"]["wikitext"]["*"])
        if c and c["artefacts"]:
            c["chronotes"] = sum(chronotes.get(a, 0) for a in c["artefacts"]) + c["bonus_chronotes"]
            out[r["page_name"]] = c
    OUT.write_text(json.dumps({"source": "RuneScape Wiki, Bucket:Infobox_artefact and collection pages (CC BY-NC-SA 3.0)",
                               "artefact_chronotes": dict(sorted(chronotes.items())),
                               "collections": dict(sorted(out.items()))}, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(out)} collections, {len(chronotes)} artefacts -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
