"""Build data/boss_rates.json: kill rates, requirements and drop rates from the wiki's money making guides.

Usage: python scripts/build_boss_rates.py

Every "Money making guide/..." page whose {{Mmgtable}} gives kills per hour (kph) becomes one row:
the monster (the Activity's first link), kph, required and recommended skill levels, required quests,
the gear the guide assumes ("Tier 80+ weapon"), and each output with its per-kill amount (the guides
write rates as arithmetic like "1/768 + 3*0.00064"; anything else, such as GE price templates, is
skipped). These are an experienced player's rates with the guide's gear: drops.boss_kills_per_hour
scales them down for weaker gear. Data: RuneScape Wiki, CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import ast
import json
import operator
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from build_skilling_sim import page_source  # noqa: E402
from rune_advisor.http import CachedClient  # noqa: E402
from rune_advisor.sources import wiki  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "src/rune_advisor/data/boss_rates.json"
_LINK = re.compile(r"\[\[([^|\]#]+)")
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def arithmetic(text: str) -> float | None:
    """Evaluate plain arithmetic ("1/768 + 3*0.00064"); None for anything else."""
    text = re.sub(r"<!--.*?-->", "", text or "").strip().replace(",", "")
    if not text:
        return None
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError:
        return None

    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        raise ValueError(n)

    try:
        return ev(tree)
    except (ValueError, ZeroDivisionError):
        return None


def template_params(text: str, name: str) -> dict[str, str] | None:
    """Top-level |key = value parameters of the first {{name ...}} in text (nested templates kept)."""
    start = text.find("{{" + name)
    if start < 0:
        return None
    depth, i, parts, cur = 0, start, [], start + 2 + len(name)
    while i < len(text):
        two = text[i:i + 2]
        if two in ("{{", "[["):
            depth += 1
            i += 2
            continue
        if two in ("}}", "]]"):
            depth -= 1
            i += 2
            if depth == 0:
                parts.append(text[cur:i - 2])
                break
            continue
        if text[i] == "|" and depth == 1:
            parts.append(text[cur:i])
            cur = i + 1
        i += 1
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.strip()] = v.strip()
    return params


def requirements(block: str) -> tuple[dict[str, int], dict[str, int], list[str]]:
    """(required skills, recommended skills, other required lines) from a Skill/Quest list."""
    required, recommended, other = {}, {}, []
    for line in block.splitlines():
        line = line.strip().lstrip("*").strip()
        if not line:
            continue
        rec = "required=recommended" in line
        if line.startswith("{{mmgreq|or") and not rec:
            continue  # alternatives: can't say which one is needed
        for skill, lvl in re.findall(r"\{\{mmgreq\|([A-Z][a-z]+)\|(\d+)", line):
            (recommended if rec or "{{mmgreq|or" in line else required)[skill] = int(lvl)
        if not re.search(r"\{\{mmgreq\|[A-Z][a-z]+\|\d+", line) and not rec:
            other.append(line)
    return required, recommended, other


def main() -> None:
    client = CachedClient()
    titles, cont = [], {}
    while True:
        d = client.get_json(wiki.API, params={"action": "query", "list": "allpages", "apprefix": "Money making guide/",
                                              "aplimit": 500, "format": "json", **cont}, ttl=7 * 24 * 3600)
        titles += [p["title"] for p in d["query"]["allpages"]]
        if "continue" not in d:
            break
        cont = d["continue"]
    rows = []
    for title in titles:
        params = template_params(page_source(client, title), "Mmgtable")
        if not params or not arithmetic(params.get("kph", "")):
            continue
        monster = _LINK.search(params.get("Activity", ""))
        skills, rec_skills, _ = requirements(params.get("Skill", ""))
        _, _, quest_lines = requirements(params.get("Quest", ""))
        quests = [q for line in quest_lines for q in re.findall(r"\{\{mmgreq\|([^|}]+)", line)]
        gear = [t for t in re.findall(r"txt=([^|}]+)", params.get("Item", "")) if re.search(r"[Tt]ier \d+", t)]
        drops = {}
        for k, v in params.items():
            m = re.fullmatch(r"Output(\d+)", k)
            if m:
                n = arithmetic(params.get(f"Output{m.group(1)}num", ""))
                if n:
                    drops[v] = n
        tier = max((int(t) for g in gear for t in re.findall(r"[Tt]ier (\d+)", g)), default=None)
        rows.append({"guide": title, "activity": re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", params.get("Activity", "")),
                     "monster": monster.group(1).strip() if monster else None, "kph": arithmetic(params["kph"]),
                     "per_kill": params.get("isperkill", "").lower().startswith("y"),
                     "skills": skills, "recommended_skills": rec_skills,
                     "quests": quests, "partial_quests": [q for line in quest_lines if "artial" in line
                                                          for q in re.findall(r"\{\{mmgreq\|([^|}]+)", line)],
                     "gear_tier": tier, "drops": drops})
    OUT.write_text(json.dumps({"source": "RuneScape Wiki money making guides (CC BY-NC-SA 3.0)", "rows": rows}, indent=1))
    print(f"{len(rows)} guides with kills/hour (of {len(titles)} pages) -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
