"""Divine charge: how long the charge pack lasts in augmented gear, and the prep to cover a planned trip.

Augmented gear drains the charge pack every second of combat (Charge pack page):

    D = (T - 60) / 8 x S x R x L x E   charge a second, per augmented item

  T  the item's tier (67 for anything below 70)
  S  slot: two-handed 1.5, main hand / body / legs 1, off-hand or shield 0.5, tool 0.25
  R  charge drain reduction research: the highest "Charge drain reduction N" blueprint the export shows
     discovered (they can be researched on a potion boost, above the plain level), else what the Invention level
     allows (RESEARCH)
  L  item level: 1-4 x1, 5-13 x0.9, 14-17 x0.875, 18+ x0.85; taken as the time-weighted average of a siphon
     cycle (item level 1 to the siphon level and back), since item levels aren't exported
Checked in game 2026-11-12: a freshly augmented Death guard and Skull lantern (tier 90 main and off-hand,
research 6, item level 1) read 4.95 a second, the formula's (3.75 + 1.875) x 0.88.
  E  Efficient perks: not read from the export, so 1

Combat is taken as continuous (the game drains in 6-tick steps on any hit dealt or taken), which is the upper
bound: banking and walking drain nothing. The pack's charge is the export's `INVENT_CHARGE` divided by 3,000
(CHARGE_UNIT): a reading of 23,984,562 on 2026-11-12 is 7,995 charge, 26.9 minutes at the 4.95 a second the two weapons
read, and the game projected about 26 (the user's reading; the unit is theirs too, not a documented one).
A Divine charge adds 3,000: 20 Simple parts and one divine
energy (ENERGY: the same amounts as an augmentor).

`prep(ctx, hours)`: for a combat trip of `hours` in the player's strongest style, the charge it drains from the
pieces the export holds augmented, what the pack is short, and the steps to cover it: held Divine charges first,
then charges to make from the held energy that makes the most, Simple parts beyond the storage chained to the
gatherer's disassembly route. A plan is sized to its expected time, or to a session when that is shorter
(`trip_hours`): a drop like the dragon pickaxe can take far longer than expected, but nobody preps for the tail.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .mechanics import item_xp
from .models import PlayerState

CHARGE_VAR = "INVENT_CHARGE"
CHARGE_UNIT = 3000  # the var counts 1/3000ths of a charge (fitted to the game's time-left projection)
CHARGE_CAP = 500_000
DIVINE_CHARGE = 3000  # charge one Divine charge adds
CHARGE_PARTS = 20  # Simple parts per Divine charge
ENERGY = item_xp.AUGMENTOR_ENERGY  # a Divine charge takes the same amount of one energy as an augmentor
SESSION_HOURS = 2.0  # a reasonable session, when the caller names none
SLOT_FACTOR = {"2h weapon": 1.5, "main hand weapon": 1.0, "off-hand weapon": 0.5, "off-hand": 0.5, "torso": 1.0,
               "legs": 1.0, "tool": 0.25}
# (Invention level, drain multiplier) of the charge drain reduction researches.
RESEARCH = ((105, 0.80), (95, 0.83), (91, 0.86), (83, 0.88), (78, 0.91), (69, 0.93), (64, 0.95), (49, 0.97), (34, 0.99))
LEVEL_FACTOR = ((18, 0.85), (14, 0.875), (5, 0.9), (1, 1.0))  # item level -> drain multiplier


def pack_charge(player: PlayerState) -> float | None:
    """Charge in the pack, None when the export doesn't carry it."""
    raw = player.vars.get(CHARGE_VAR)
    return None if raw is None else raw / CHARGE_UNIT


def research_factor(player: PlayerState) -> float:
    """The drain multiplier of the best charge drain reduction researched: from the export's discovered
    blueprints, else every tier the Invention level allows."""
    from .invention import discovered

    found = discovered(player)
    by_tier = [f for _, f in reversed(RESEARCH)]  # tier 1..9
    if found is not None and any(k.startswith("Charge drain reduction") for k in found):
        tier = max((n for n in range(1, len(by_tier) + 1) if found.get(f"Charge drain reduction {n}")), default=0)
        return by_tier[tier - 1] if tier else 1.0
    return next((f for level, f in RESEARCH if player.level("Invention") >= level), 1.0)


def level_factor(invention: int) -> float:
    """The item-level multiplier averaged over a siphon cycle (item xp earned at each item level)."""
    top = item_xp.siphon_level(invention) or item_xp.item_level_cap(invention)
    xp = item_xp.ITEM_LEVEL_XP
    if top < 2 or xp[top] <= 0:
        return 1.0
    total = 0.0
    for level in range(1, top):
        span = xp[level + 1] - xp[level]
        total += span * next(f for at, f in LEVEL_FACTOR if level >= at)
    return total / xp[top]


def drain_per_second(player: PlayerState, slot: str, tier: int, item_level: int | None = None) -> float:
    """One augmented item's drain while in combat (or skilling with the tool): at `item_level`, else averaged
    over a siphon cycle."""
    t = tier if tier >= 70 else 67
    level = (next(f for at, f in LEVEL_FACTOR if item_level >= at) if item_level is not None
             else level_factor(player.level("Invention")))
    return (t - 60) / 8 * SLOT_FACTOR.get(slot, 1.0) * research_factor(player) * level


def main_style(player: PlayerState) -> str:
    """The player's strongest combat style by their best owned loadout."""
    from .gear import best_loadout, damage_score

    return max(("melee", "ranged", "magic", "necromancy"), key=lambda s: damage_score(player, best_loadout(player, s)))


def style_drain(player: PlayerState, style: str, planned: bool = False) -> tuple[float, list[str]]:
    """(charge per hour of combat, the pieces draining it) for the style's best loadout: the pieces the export
    holds augmented, or with `planned` every piece that can be augmented."""
    setup = item_xp.combat_setup(player, style)
    slots = [s for s in setup.active if planned or s.augmented]
    per_second = sum(drain_per_second(player, s.slot, s.tier) for s in slots)
    return per_second * 3600, [item_xp.short_name(s.item) for s in slots]


def trip_hours(expected: float, session: float | None = None) -> float:
    """The combat time to prep for: the plan's expected time, or a session when that is shorter."""
    return min(expected, session or SESSION_HOURS)


def best_energy(holdings: dict[str, int]) -> tuple[str, int, int] | None:
    """(energy, amount per Divine charge, charges the held stack makes): the held energy that makes the most."""
    return item_xp.augmentor_energy(holdings)


@dataclass
class Prep:
    style: str
    items: list[str]  # augmented pieces draining charge
    per_hour: float
    hours: float  # the combat time prepped for
    charge: float | None  # in the pack now
    short: float = 0.0  # charge the trip needs beyond the pack
    steps: list[str] = field(default_factory=list)
    gather_hours: float = 0.0

    @property
    def minutes_left(self) -> float | None:
        return None if self.charge is None or self.per_hour <= 0 else self.charge / self.per_hour * 60

    @property
    def needed(self) -> bool:
        return self.per_hour > 0 and (self.charge is None or self.short > 0)

    def headline(self) -> str:
        gear = ", ".join(self.items)
        if self.charge is None:
            return (f"augmented {gear} drains ~{self.per_hour:,.0f} charge an hour of combat; this export has no "
                    f"charge pack level: {self.hours:.1f} h needs {self.per_hour * self.hours:,.0f}")
        return (f"charge pack: {self.charge:,.0f} charge lasts ~{self.minutes_left:,.0f} min of combat in augmented "
                f"{gear} (~{self.per_hour:,.0f} an hour); {self.hours:.1f} h needs {self.per_hour * self.hours:,.0f}")


def prep(ctx, hours: float, style: str | None = None, planned: bool = False) -> Prep:
    """The charge a combat trip of `hours` drains, and the steps to have it in the pack before leaving."""
    p = ctx.player
    style = style or main_style(p)
    per_hour, items = style_drain(p, style, planned)
    out = Prep(style, items, per_hour, hours, pack_charge(p))
    if per_hour <= 0 or out.charge is None:
        return out
    out.short = max(0.0, per_hour * hours - out.charge)
    if out.short <= 0:
        return out
    charges = math.ceil(out.short / DIVINE_CHARGE)
    out.steps.append(out.headline())
    held = min(charges, p.holdings.get("Divine charge", 0))
    if held:
        out.steps.append(f"add {held:,} Divine charge{'s' if held != 1 else ''} from the bank to the pack")
    make = charges - held
    if make <= 0:
        return out
    energy = best_energy(p.holdings)
    parts = make * CHARGE_PARTS
    if energy is None:
        out.steps.append(f"make {make:,} Divine charges ({CHARGE_PARTS} Simple parts and divine energy each: "
                         "no held energy makes one; gather some with Divination)")
    else:
        name, per, enough = energy
        out.steps.append(f"make {make:,} Divine charge{'s' if make != 1 else ''} at an Inventor's workbench "
                         f"({parts:,} Simple parts and {make * per:,} {name}: {CHARGE_PARTS} and {per:,} each) "
                         "and add them to the pack")
        if enough < make:
            out.steps.append(f"  your {name} makes {enough:,}: the other {make - enough:,} need more energy (Divination)")
    store = p.inventories.get("MATERIALS_STORAGE", {})
    lacking = parts - store.get("Simple parts", 0)
    if lacking > 0:
        g = ctx.gatherer.plan({"Simple parts": lacking})
        out.gather_hours = g.hours
        out.steps.append(f"  short {lacking:,} Simple parts ({store.get('Simple parts', 0):,} in storage):")
        out.steps += [f"    {step}" for step in g.steps]
    return out
