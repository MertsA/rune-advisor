"""Settle how STATUE_LAST_SKILL / ELFCITY_STATUE_LAST_SKILL encode the last rock's skill, from archived snapshots.

    uv run python scripts/check_rock_vars.py <player>

For each archive (oldest first) prints both vars' raw values, the rocks in the statue collection bag, and what each
reading of the value says: (a) a skill id (Attack 0 ... Necromancy 28), (b) a 1-based index into the bag's bit
order (two rocks per skill, the order of player.STRANGE_ROCK_SKILLS / GOLDEN_ROCK_SKILLS). Between two archives,
the rocks that appeared in the bag are listed: the reading that names the newest rock's skill wins. Export again
after the next rock (and open the bag once, so the *_CLIENT copies refresh) and rerun.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.config import ARCHIVE_DIR  # noqa: E402
from rune_advisor.player import GOLDEN_ROCK_SKILLS, ROCK_LAST_SKILL_VARS, SKILL_IDS, STRANGE_ROCK_SKILLS, statue_bag  # noqa: E402

ORDERS = {"strange": STRANGE_ROCK_SKILLS, "golden": GOLDEN_ROCK_SKILLS}


def readings(kind: str, value: int) -> str:
    skill_id = SKILL_IDS[value] if 0 <= value < len(SKILL_IDS) else "?"
    order = ORDERS[kind]
    index = f"{order[(value - 1) // 2]} {(value - 1) % 2 + 1}" if 1 <= value <= 2 * len(order) else "?"
    return f"(a) skill id -> {skill_id}; (b) 1-based rock index -> {index}"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.splitlines()[2].strip(), file=sys.stderr)
        return 2
    player = sys.argv[1]
    previous: set[str] = set()
    found = False
    for folder in sorted(ARCHIVE_DIR.glob(f"*_{player}_*")):
        path = folder / f"snapshot_{player}.json"
        if not path.exists():
            continue
        variables = json.loads(path.read_text()).get("vars") or {}
        if not any(v in variables for v in ROCK_LAST_SKILL_VARS.values()):
            continue
        found = True
        rocks = set(statue_bag(variables))
        print(f"\n{folder.name}")
        for kind, var_name in ROCK_LAST_SKILL_VARS.items():
            value = variables.get(var_name)
            if isinstance(value, int):
                print(f"  {var_name} = {value}: {readings(kind, value)}")
        print(f"  bag: {', '.join(sorted(rocks)) or 'empty / not refreshed'}")
        if new := sorted(rocks - previous):
            print(f"  new since the previous archive: {', '.join(new)}")
        previous = rocks
    if not found:
        print(f"no archive with the rock vars for {player} under {ARCHIVE_DIR}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
