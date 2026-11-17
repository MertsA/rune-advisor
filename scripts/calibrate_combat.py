"""Fit mechanics/combat.py's AVG_ABILITY_MULT to the money making guides' boss kill rates.

Usage: python scripts/calibrate_combat.py

For every solo boss guide (data/boss_rates.json) whose boss has combat stats, the model's damage time
per kill (life points / DPS of a typical setup at the guide's gear tier, level 99, best style) is set
against the guide's time per kill; the multiplier is scaled until the median ratio is 1 (the guides' time also holds phases and walking,
so this is a conservative fit: kill rates won't beat the guides'). Bosses whose ratio is beyond
OUTLIER (life points covering several phases, scripted fights) are left out.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rune_advisor.drops import GROUP_ACTIVITY, boss_rates  # noqa: E402
from rune_advisor.mechanics import combat  # noqa: E402

OUTLIER = 5.0


def ratios() -> list[tuple[float, str]]:
    out = []
    for name, rows in boss_rates().items():
        m = combat.monster(name)
        solo = [r for r in rows if not GROUP_ACTIVITY.search(r["activity"] or "")]
        if not m or not m.get("life_points") or not solo:
            continue
        r = sorted(solo, key=lambda r: r["kph"])[(len(solo) - 1) // 2]
        dps = max(combat.reference_dps(99, r.get("gear_tier") or 80, m, s) for s in combat.DAMAGE_SKILL)
        ratio = (m["life_points"] / dps) / (3600 / r["kph"])
        if 1 / OUTLIER <= ratio <= OUTLIER:
            out.append((ratio, name))
    return out


def main() -> None:
    start = combat.AVG_ABILITY_MULT
    # The outlier window moves with the multiplier: iterate to a fixed point.
    for _ in range(20):
        rs = ratios()
        med = statistics.median(r for r, _ in rs)
        if abs(med - 1) < 0.005:
            break
        combat.AVG_ABILITY_MULT *= med
    print(f"{len(rs)} bosses; median model/guide time per kill {med:.3f}")
    print(f"fitted AVG_ABILITY_MULT = {combat.AVG_ABILITY_MULT:.2f} (currently {start})")


if __name__ == "__main__":
    main()
