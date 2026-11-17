"""List items whose cheapest source for a player is a skill-less catalogue conversion (NPC services, reward
shops, combining items), for spotting odd sources: a conversion should only win when it's how the game
really makes the item.

Usage: uv run python scripts/audit_conversions.py <player> [--all]

Checks every material some recipe or guide method uses. Prints item, the conversion's inputs and its hours
per unit; --all also prints the items no source covers.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.cli import _ctx  # noqa: E402
from rune_advisor.recommend.catalogue import catalogue  # noqa: E402


def main(player: str, show_all: bool) -> None:
    ctx = _ctx(player, use_plugin=True)
    materials = {m for r in catalogue() for m, _ in r["materials"]}
    for rows in ctx.cost.wiki_rows.values():
        for row in rows:
            for v in (row.get("recipe") or {}).get("variants", []):
                materials |= {m for m, _ in v["materials"]}
    g = ctx.gatherer
    converted, missing = [], []
    for item in sorted(materials):
        src = g.source(item)
        if src is None:
            missing.append(item)
        elif src.kind == "craft" and src.recipe and src.recipe.get("conversion"):
            inputs = ", ".join(f"{q:g} {m}" for m, q in src.recipe["materials"])
            converted.append((item, inputs, src.cost_per_unit, src.recipe.get("tools") or []))
    print(f"{len(materials)} materials; {len(converted)} sourced by conversion, {len(missing)} with no source")
    for item, inputs, hours, tools in converted:
        print(f"  {item} <- {inputs} ({hours * 3600:,.1f} s each){' tools: ' + ', '.join(tools) if tools else ''}")
    if show_all:
        print("no source:", ", ".join(missing))


if __name__ == "__main__":
    main(sys.argv[1], "--all" in sys.argv)
