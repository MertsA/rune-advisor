"""Invention discoveries as xp in hand.

Every blueprint is discovered once, at an inventor's workbench, for a fixed xp (data/invention_blueprints.json,
scripts/build_invention_blueprints.py: a perfect optimisation of the discovery puzzle). Which ones the player has
discovered comes from the game's all-blueprints achievement ("Draught Includer"): it tests one bit of the
INVENT_BLUEPRINT_UNLOCKED_* vars per blueprint, each requirement described by the blueprint's name, and the export
carries those bits. The level a blueprint needs can be boosted: an Extreme invention potion adds a level per
150,000 Invention xp up to +17 (the wiki's elite-skill extreme boost table), so a bank of them turns every
blueprint up to 17 levels ahead into xp for a few minutes at the workbench.

Blueprints the wiki footnotes are gated behind something else (`gate` in the data: a tech tree rank, the Stormguard
or Ancient Invention blueprints, a Daemonheim purchase, Nomad's Elegy, Heart of Gielinor reputation, a shop). Gates
the export can settle are settled: the Ancient Invention and Stormguard blueprints each have an achievement for
studying them, the technology trees one for completing them (a complete tree opens every device; an incomplete one
leaves them unknown), Nomad's Elegy is a quest. The rest stay gated and are listed apart, never counted as ready.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cache
from . import datapkg

from .models import PlayerState

BLUEPRINT_VAR = "INVENT_BLUEPRINT_UNLOCKED"
# The game's requirement text where it differs from the wiki's blueprint name.
ALIASES = {"Ancient gizmos": "Ancient gizmo"}
BOOST_XP_PER_LEVEL = 150_000
BOOST_MAX = 17
# Potions that boost Invention, best first: the extreme one scales with xp (None), the others are flat.
POTIONS = {"Extreme invention": None, "Super invention": 5, "Invention potion": 3}
_DOSE = re.compile(r"^(Extreme invention|Super invention|Invention potion)(?: \((\d)\))?$")


@cache
def blueprints() -> list[dict]:
    path = datapkg.path("invention_blueprints.json")
    return json.loads(path.read_text())["blueprints"] if path.is_file() else []


def discovered(player: PlayerState) -> dict[str, bool] | None:
    """Blueprint name -> discovered, from the all-blueprints achievement's tests; None when no export has them."""
    for a in player.achievements.values():
        tests = [r for r in a.requirements if str((r.get("var") or {}).get("name", "")).startswith(BLUEPRINT_VAR)]
        if tests:
            return {ALIASES.get(str(r.get("description")), str(r.get("description"))): bool(r.get("complete"))
                    for r in tests}
    return None


def potion_boost(player: PlayerState) -> tuple[str | None, int]:
    """(potion, levels) of the best Invention boost among the player's potions, (None, 0) without one."""
    held = set()
    for item in player.holdings:
        if m := _DOSE.match(item):
            held.add(m[1])
    for name, flat in POTIONS.items():
        if name in held:
            return name, flat if flat is not None else min(BOOST_MAX, player.xp("Invention") // BOOST_XP_PER_LEVEL)
    return None, 0


# gate -> (achievement, requirement description) whose completion opens it; "" = the achievement itself.
GATE_ACHIEVEMENTS = {"ancient": ("Ancient Gizmos", ""), "ancient_tools": ("Ancient Tools", ""),
                     "capacitor": ("Ancient XP Capacitor", ""), "stormguard": ("The Prodigal Spender", "Auto-screener v1.080 blueprint"),
                     "dwarven": ("Invention Gives Me the Blues", "Dwarven"), "goblin": ("Invention Gives Me the Blues", "Goblin")}
GATE_QUESTS = {"elegy": "Nomad's Elegy"}
GATE_TEXT = {"ancient": "the 'Ancient gizmos' blueprint", "ancient_tools": "the 'Ancient tools' blueprint",
             "capacitor": "the 'XP Capacitor' blueprint", "stormguard": "the Auto-screener blueprint (Archaeology Guild)",
             "dwarven": "the dwarven technology tree", "goblin": "the cave goblin technology tree",
             "bryll": "a Daemonheim rewards purchase", "elegy": "Nomad's Elegy", "reputation": "Heart of Gielinor reputation",
             "shop": "a shop purchase", "other": "another unlock"}


def gate_open(player: PlayerState, gate: str) -> bool | None:
    """True/False when the export settles the gate, None when it can't (tech tree ranks short of complete, shops)."""
    if gate in GATE_QUESTS:
        q = player.quests.get(GATE_QUESTS[gate])
        return None if q is None else q.status.name == "COMPLETED"
    if gate not in GATE_ACHIEVEMENTS:
        return None
    name, desc = GATE_ACHIEVEMENTS[gate]
    a = player.achievements.get(name)
    if a is None:
        return None
    if not desc:
        return bool(a.completed)
    done = [bool(r.get("complete")) for r in a.requirements if r.get("description") == desc]
    if not done:
        return None
    # A complete tree opens all its devices; short of that the rank isn't exported.
    return True if all(done) else None


@dataclass
class Discoveries:
    level: int  # the Invention level discoveries are made at (boosted)
    potion: str | None
    boost: int
    ready: list[dict] = field(default_factory=list)  # undiscovered, at or under `level`, not gated
    gated: list[dict] = field(default_factory=list)  # undiscovered, at or under `level`, needing an unlock first
    locked: list[dict] = field(default_factory=list)  # undiscovered, above `level`
    unknown: list[str] = field(default_factory=list)  # game blueprint names the data lacks

    @property
    def xp(self) -> float:
        return sum(r["xp"] for r in self.ready)


def discoveries(player: PlayerState) -> Discoveries | None:
    """What the player could discover now, boosted by the best potion in the bank; None without blueprint state."""
    state = discovered(player)
    if state is None:
        return None
    potion, boost = potion_boost(player)
    level = player.level("Invention") + boost
    d = Discoveries(level=level, potion=potion, boost=boost)
    rows = {b["name"]: b for b in blueprints()}
    for name, done in state.items():
        if done:
            continue
        b = rows.get(name)
        if b is None:
            d.unknown.append(name)
        elif b["level"] > level:
            d.locked.append(b)
        elif b.get("gate") and gate_open(player, b["gate"]) is not True:
            d.gated.append(b)
        else:
            d.ready.append(b)
    for lst in (d.ready, d.gated, d.locked):
        lst.sort(key=lambda b: (-b["xp"], b["name"]))
    return d
