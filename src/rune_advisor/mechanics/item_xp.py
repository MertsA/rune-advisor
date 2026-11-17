"""Invention item xp: augmented gear levels up while the player fights or skills, and a siphon turns that item xp
into Invention xp. Invention is trained almost entirely this way, so every combat and skilling method the player
can run with augmentable gear gets an Invention twin row ("<method> with augmented <gear>") whose xp/h is the
Invention xp the siphons pay and whose side xp is the method's own skill.

Mechanics (https://runescape.wiki/w/Equipment_level, /Augmentation, /Equipment_siphon, Module:Equipment experience
table, read 2026-11-11):
- Combat: an item gets a share of the monster's combat xp (Constitution excluded) per hit, in proportion to damage:
  9% for a two-handed weapon, 4.5% for a main-hand, off-hand, shield, body or legs item. A method's combat-skill xp/h
  is that xp/h, so item xp/h = share x the row's xp/h per augmented slot.
- Skilling: item xp = floor(k x base xp) / 1000 per action: Archaeology k=288, Fishing/Mining/Woodcutting 118,
  Firemaking 65, Smithing 78, Thieving 32 (Runecrafting's k isn't published). Guide/sim xp/h is taken as base xp (it
  includes outfit bonuses, so this overstates a little: flagged).
- Item levels: ITEM_LEVEL_XP; the cap is 5/10/15/20 at Invention 4/27/60/99. Siphoning at item level L pays
  SIPHON_XP[L] x (1 + 0.015 x (tier - 80)) Invention xp and resets the item; disassembling pays the level-10 figure
  at L >= 10 but destroys the item, so ironmen siphon. Level 12 is where the siphon's payout stops growing
  (540,000 at tier 80 for 81,960 item xp: 6.59 Invention xp per item xp); below Invention 60 the cap is 10
  (270,000 for 40,120: 6.73). Each siphon is an Equipment siphon (50 Simple parts, 5 Dextrous, 5 Precious).
- Augmentable: tier 70+ in a weapon slot (Invention 2), shield or body (16), legs (45), tools (22). Tools count as
  tier 70, or 90 with the Crystal tool siphon blueprint.
- Not modelled: divine charge drain (an ironman supply: energy), perks, the augmentor itself (45 Base/Flexible/
  Tensile parts, 8 Enhancing, 7 Powerful components and divine energy,
  AUGMENTOR_ENERGY, made once per item; `radv augment` lists them and chains shortfalls to disassembly), item xp from Slayer rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import PlayerState
from . import row

ITEM_LEVEL_XP = [0, 0, 1_160, 2_607, 5_176, 8_285, 11_760, 15_835, 21_152, 28_761, 40_120, 57_095, 81_960, 117_397,
                 166_496, 232_755, 320_080, 432_785, 575_592, 753_631, 972_440]  # index = item level (1..20)
# Tier-80 payout by item level: disassembly at L, siphon at L+2 (the wiki module's `mult` table, capped at 540,000).
_MULT = [0, 0, 9_000, 27_000, 54_000, 108_000, 144_000, 198_000, 270_000, 378_000, 540_000]
SIPHON_XP = {level: _MULT[min(level - 2, 10)] for level in range(4, 21)}
LEVEL_CAPS = ((99, 20), (60, 15), (27, 10), (0, 5))  # Invention level -> highest item level
SLOT_SHARE = {"2h weapon": 0.09, "main hand weapon": 0.045, "off-hand weapon": 0.045, "off-hand": 0.045,
              "torso": 0.045, "legs": 0.045}
SLOT_LEVEL = {"2h weapon": 2, "main hand weapon": 2, "off-hand weapon": 2, "off-hand": 16, "torso": 16, "legs": 45}
MIN_TIER = 70
SKILL_K = {"Archaeology": 288, "Fishing": 118, "Mining": 118, "Woodcutting": 118, "Firemaking": 65, "Smithing": 78,
           "Thieving": 32}
TOOL_LEVEL = 22
SIPHON_LEVEL = 27  # the Equipment siphon blueprint: the Invention level the twin rows start at
TOOL_TIER, CRYSTAL_SIPHON_TIER = 70, 90
# Augmentable tools per skill (Augmentation page, tools section); the Invention-made ones are makeable at 22.
TOOLS = {
    "Archaeology": ("Dragon mattock", "Crystal mattock", "Imcando mattock", "Mattock of Time and Space",
                    "Guildmaster Tony's mattock"),
    "Firemaking": ("Pyro-matic", "Enhanced pyro-matic", "Crystal tinderbox"),
    "Fishing": ("Fishing rod-o-matic", "Enhanced fishing rod-o-matic", "Dragon harpoon", "Crystal fishing rod",
                "Tavia's fishing rod"),
    "Mining": ("Dragon pickaxe", "Crystal pickaxe", "Imcando pickaxe", "Pickaxe of earth and song",
               "Pickaxe of life and death"),
    "Smithing": ("Hammer-tron", "Enhanced hammer-tron", "Crystal hammer"),
    "Thieving": ("Pickpocketing bag", "Mushroomy pickpocketing bag"),
    "Woodcutting": ("Dragon hatchet", "Crystal hatchet", "Imcando hatchet", "Hatchet of ember and glade",
                    "Hatchet of bloom and blight", "Hawthorn's hatchet"),
}
INVENTION_MADE_TOOLS = {"Fishing": "Fishing rod-o-matic", "Firemaking": "Pyro-matic", "Smithing": "Hammer-tron"}
SIPHON_MATERIALS = {"Simple parts": 50, "Dextrous components": 5, "Precious components": 5}
AUGMENTOR_MATERIALS = {"Base parts": 45, "Flexible parts": 45, "Tensile parts": 45, "Enhancing components": 8,
                       "Powerful components": 7}


# Divine energy one augmentor takes, by energy (Augmentor page): any one type.
AUGMENTOR_ENERGY = {"Ancestral energy": 100, "Incandescent energy": 225, "Luminous energy": 350, "Radiant energy": 400,
                    "Brilliant energy": 450, "Elder energy": 500, "Lustrous energy": 550, "Vibrant energy": 650,
                    "Gleaming energy": 700, "Sparkling energy": 800, "Glowing energy": 850, "Bright energy": 900,
                    "Flickering energy": 1000, "Pale energy": 2000}


def augmentor_energy(holdings: dict[str, int]) -> tuple[str, int, int] | None:
    """(energy, amount per augmentor, augmentors the held stack makes) for the held energy that makes the most
    augmentors; None when no stack makes one."""
    best = max(((holdings.get(e, 0) // n, e) for e, n in AUGMENTOR_ENERGY.items()), default=(0, ""))
    return (best[1], AUGMENTOR_ENERGY[best[1]], best[0]) if best[0] > 0 else None


def tier_factor(tier: int) -> float:
    return 1 + 0.015 * (tier - 80)


def item_level_cap(invention: int) -> int:
    return next(cap for level, cap in LEVEL_CAPS if invention >= level)


def siphon_level(invention: int) -> int | None:
    """The item level to siphon at: 12 (the payout's ceiling) when the cap allows, else the cap; None when siphons
    can't be used yet (the Equipment siphon blueprint is Invention 27)."""
    if invention < 27:
        return None
    return min(12, item_level_cap(invention))


def invention_per_item_xp(invention: int, tier: int) -> float:
    """Invention xp per item xp earned on a tier-`tier` item, siphoning at siphon_level (0 when siphons can't be used)."""
    level = siphon_level(invention)
    if level is None:
        return 0.0
    return SIPHON_XP[level] * tier_factor(tier) / ITEM_LEVEL_XP[level]


@dataclass
class Slot:
    slot: str
    item: str
    tier: int
    share: float
    augmented: bool  # the export holds the augmented version already
    ok: bool  # counts: tier and Invention level allow it
    why: str = ""


@dataclass
class Setup:
    """What a style's or skill's augmented gear earns: item xp/h shares, the Invention xp/h at a method rate."""
    kind: str  # "combat" or "skilling"
    name: str  # style or skill
    slots: list[Slot] = field(default_factory=list)
    tool_assumed: str | None = None  # an Invention-made tool the player would make first

    @property
    def active(self) -> list[Slot]:
        return [s for s in self.slots if s.ok]

    def describe(self) -> str:
        return ", ".join(f"{short_name(s.item)} (t{s.tier})" for s in self.active)

    def invention_rate(self, player: PlayerState, xp_per_hour: float, base_k: int | None = None) -> float:
        """Invention xp/h while a method pays `xp_per_hour` in its own skill (combat: shares of it; skilling: k/1000)."""
        inv = player.level("Invention")
        total = 0.0
        for s in self.active:
            item_xp = xp_per_hour * (s.share if base_k is None else base_k / 1000)
            total += item_xp * invention_per_item_xp(inv, s.tier)
        return total

    def siphons_per_hour(self, player: PlayerState, xp_per_hour: float, base_k: int | None = None) -> float:
        level = siphon_level(player.level("Invention"))
        if level is None:
            return 0.0
        per_cycle = ITEM_LEVEL_XP[level]
        return sum(xp_per_hour * (s.share if base_k is None else base_k / 1000) / per_cycle for s in self.active)


def short_name(item: str) -> str:
    """"Augmented Death guard (tier 90)" -> "Death guard"."""
    return item.removeprefix("Augmented ").split(" (tier")[0]


def _held_augmented(player: PlayerState, item: str) -> bool:
    """The export holds the augmented version (a style-switch family like the Sunspear is one item: any variant counts)."""
    from ..gear import equipment

    names = {item}
    for family in equipment().get("switchable", []):
        if item in family or item.removeprefix("Augmented ") in family:
            names |= set(family)
    bases = {n.removeprefix("Augmented ").split(" (tier")[0] for n in names}
    return any(h.startswith("Augmented ") and h.removeprefix("Augmented ").split(" (tier")[0] in bases
               for h in player.holdings)


def combat_setup(player: PlayerState, style: str) -> Setup:
    """The best owned loadout's augmentable pieces for a combat style."""
    from ..gear import best_loadout

    inv = player.level("Invention")
    setup = Setup("combat", style)
    for slot, item in best_loadout(player, style).slots.items():
        if slot not in SLOT_SHARE:
            continue
        ok, why = True, ""
        if item.tier < MIN_TIER:
            ok, why = False, f"tier {item.tier}: only tier {MIN_TIER}+ can be augmented"
        elif inv < SLOT_LEVEL[slot]:
            ok, why = False, f"needs Invention {SLOT_LEVEL[slot]}"
        setup.slots.append(Slot(slot, item.name, item.tier, SLOT_SHARE[slot], _held_augmented(player, item.name), ok, why))
    return setup


def skilling_setup(player: PlayerState, skill: str) -> Setup | None:
    """The augmentable tool for a skilling skill: one the player holds, else an Invention-made one at 22."""
    from ..invention import discovered

    inv = player.level("Invention")
    setup = Setup("skilling", skill)
    held = [t for t in TOOLS.get(skill, ()) if any(h == t or h == f"Augmented {t}" for h in player.holdings)]
    tool = held[0] if held else INVENTION_MADE_TOOLS.get(skill)
    if tool is None:
        return None
    if not held:
        setup.tool_assumed = tool
    crystal = (discovered(player) or {}).get("Crystal tool siphon") and tool.startswith("Crystal")
    tier = CRYSTAL_SIPHON_TIER if crystal else TOOL_TIER
    ok, why = inv >= TOOL_LEVEL, "" if inv >= TOOL_LEVEL else f"needs Invention {TOOL_LEVEL}"
    setup.slots.append(Slot("tool", tool, tier, SKILL_K[skill] / 1000, _held_augmented(player, tool), ok, why))
    return setup


def method_rows(player: PlayerState, rows: list[dict]) -> list[dict]:
    """Invention twin rows for the player's combat and skilling methods (`rows`: the other sims' and the guides'
    rows): xp/h = the Invention xp the siphons pay while running the method, side xp = the method's own skill."""
    from ..cost import row_skills, typical_rate
    from .combat import GUIDE_STYLE

    if siphon_level(player.level("Invention")) is None:
        return []
    out = []
    setups: dict[str, Setup | None] = {}
    for r in rows:
        skills = row_skills(r)
        skill = r["skill"] if r["skill"] in GUIDE_STYLE else skills[0]
        if r.get("level_min", 1) > player.level(skills[0]):
            continue
        try:
            rate = typical_rate(r) if r.get("xp_per_hour") is None else float(r["xp_per_hour"])
        except (TypeError, ValueError):
            continue
        if not rate:
            continue
        if skill in GUIDE_STYLE:
            # Only fights level gear: rituals, alchemy and teleport rows pay combat-skill xp without a hit.
            if not (r.get("kills_per_hour") or r.get("drop_sources")):
                continue
            key = GUIDE_STYLE[skill]
            if key not in setups:
                setups[key] = combat_setup(player, key)
            setup, k = setups[key], None
        elif skill in SKILL_K:
            if skill not in setups:
                setups[skill] = skilling_setup(player, skill)
            setup, k = setups[skill], SKILL_K[skill]
        else:
            continue
        if setup is None or not setup.active:
            continue
        inv_rate = setup.invention_rate(player, rate, k)
        if inv_rate <= 0:
            continue
        assumptions = [f"augmented: {setup.describe()}",
                       f"siphoned at item level {siphon_level(player.level('Invention'))}: "
                       f"~{setup.siphons_per_hour(player, rate, k):.1f} Equipment siphons an hour "
                       f"(50 Simple parts, 5 Dextrous and 5 Precious components each); divine charge drain not costed"]
        if k is not None:
            assumptions.append("the method's xp/h taken as base xp (outfit bonuses add no item xp: a little high)")
        if setup.tool_assumed:
            assumptions.append(f"a {setup.tool_assumed} to make first (Invention {TOOL_LEVEL})")
        side = {skills[0]: rate}  # a "Melee" row trains one of Attack/Strength/Defence at a time: count it once
        # The twin's band is the Invention level (siphons from 27); the base method's own level was checked above.
        out.append(row("Invention", f"{r['method']} with augmented {setup.describe()}", SIPHON_LEVEL, None, inv_rate, side_xp=side, assumptions=assumptions,
                       activity=r.get("activity") or r["method"], base_method=r["method"], base_skill=skill,
                       **{key: r[key] for key in ("kills_per_hour", "drop_sources", "requirements") if r.get(key)}))
    return out
