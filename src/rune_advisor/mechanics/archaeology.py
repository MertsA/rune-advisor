"""Archaeology excavation from the wiki's hotspot calculator (Module:Archaeology Hotspot Calculator Module,
Module:Skill calc/Archaeology/data).

Hits per hour: the AFK method's 1,000-1,175 (average used), less an eighth for banking without a porter.
Each hit succeeds (a material) with 0.21 + 1.11% per point of the mattock's Focus, times the Honed perk;
it deals the mattock's Precision to the hotspot's hitpoints, and every `hitpoints / precision` hits
finds an artefact. xp/h = misses x fail xp + materials x success xp + artefacts x (artefact xp +
restoration xp), times the xp boosts (Archaeologist's outfit, Wise, clan avatar), as the calculator's
AFK total. Materials split by the hotspot's weights (restoring uses some of them; not netted here).

Time sprites ("High Intensity", a second row per hotspot): hits at SPRITE_ACTIVITY of the calculator's
1,500/h maximum, +5% success (40% sprite focus at 2% a hit), precision x1.1 x SPRITE_PRECISION, and +20% on
the finding/material/fail xp (not restoration), as the calculator's method options.

Left out: the sashbrush, monocle, tea and pylons, and walking between hotspots.
"""

from __future__ import annotations

from ..models import PlayerState
from . import bands, data, profile, row

AFK_HITS = (1000 + 1175) / 2
SPRITE_ACTIVITY = 0.95  # assumption: share of the 1,500 hits/h maximum while chasing sprites
SPRITE_HITS = 1500 * SPRITE_ACTIVITY
SPRITE_SUCCESS = 0.05
SPRITE_PRECISION = 1.05  # the calculator's simple-mode sprite precision (the full one depends on crit bonus)
SPRITE_XP = 1.2
NO_PORTER_SHARE = 7 / 8
EXCLUDED = {"Dragonkin remains (Desperate Measures)", "Orthen rubble (Desperate Measures)", "Fort debris",
            "Forum entrance", "Barricade (Archaeology)", "Orthen rubble", "Dragonkin remains"}


def mattock(player: PlayerState, level: int) -> tuple[dict | None, str, bool]:
    """(mattock data, name, assumed?): overrides.toml `mattock`, an owned one, else the best for the level."""
    table = (data().get("archaeology") or {}).get("mattocks") or {}
    named = (player.tools.get("mattock") or "").lower().removesuffix(" mattock")
    usable = {k: v for k, v in table.items() if v.get("Level", 999) <= level}
    for k, v in usable.items():
        if k.lower() == named or f"{k} mattock".lower() in {h.lower() for h in player.holdings}:
            return v, k, False
    if not usable:
        return None, "", True
    k = max(usable, key=lambda k: (usable[k]["Precision"], usable[k]["Focus"]))
    return usable[k], k, True


def hotspot_rates(player: PlayerState, level: int | None = None, sprites: bool = False) -> list[dict]:
    a = data().get("archaeology") or {}
    level = level or player.level("Archaeology")
    m, mname, assumed = mattock(player, level)
    if m is None:
        return []
    honed = (a.get("honed") or {}).get(f"Honed {profile.perks(player, 'mattock').get('Honed', 0)}", 0)
    success = (0.21 + m.get("Focus", 0) * 0.0111 + (SPRITE_SUCCESS if sprites else 0)) * (1 + honed / 100)
    hits = (SPRITE_HITS if sprites else AFK_HITS) * (1 if profile.porter(player) else NO_PORTER_SHARE)
    precision = m["Precision"] * (1.1 * SPRITE_PRECISION if sprites else 1)
    find_boost = SPRITE_XP if sprites else 1
    boost = 1 + profile.profile(player, "Archaeology", "mattock").xp_bonus
    out = []
    for name, h in (a.get("hotspots") or {}).items():
        if name in EXCLUDED or h.get("level", 999) > level or not h.get("hitpoints"):
            continue
        fail = h.get("failxp") or 10 ** (0.0000965 * h["level"] ** 2)
        succ = h.get("successxp") or fail * 9.3
        art = h.get("artefactxp") or 100 * fail
        artefacts = hits / (h["hitpoints"] / precision)
        mats = hits * success
        restore = [(a.get("artefact_xp") or {}).get(n.removesuffix(" (damaged)")) or
                   (a.get("artefact_xp") or {}).get(n) for n, _ in h.get("artefacts") or []]
        restore = [x for x in restore if x]
        restore_xp = sum(restore) / len(restore) if restore else 0
        xp = ((hits * (1 - success) * fail + mats * succ + artefacts * art) * find_boost + artefacts * restore_xp) * boost
        weights = sum(w for _, w in h.get("materials") or []) or 1
        out.append({"hotspot": name, "level": h["level"], "xp_per_hour": xp, "artefacts_per_hour": artefacts,
                    "materials": {mat: mats * w / weights for mat, w in h.get("materials") or []},
                    "mattock": f"{mname} mattock" + (", assumed" if assumed else "")})
    return out


def method_rows(player: PlayerState) -> list[dict]:
    rows = []
    for band in bands(player, "Archaeology"):
        for sprites in (False, True):
            for h in hotspot_rates(player, band, sprites):
                how = "time sprites, " if sprites else ""
                rows.append(row("Archaeology", f"{h['hotspot']} (simulated, {how}{h['mattock']})", max(band, h["level"]),
                                band + 4, h["xp_per_hour"], yields=h["materials"],
                                assumptions=[f"time sprites at {SPRITE_ACTIVITY:.0%} activity" if sprites else "AFK hit rate"],
                                activity=h["hotspot"], afk=not sprites))
    return rows
