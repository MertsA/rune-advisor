"""Pickpocketing from the wiki's Thieving chance calculator (Module:Thieving chance calculator and /data).

Effective level = Thieving level + a requirement modifier (15 up to level 30 targets, 20 up to 50,
else 25) + additive boosts (gloves of silence 5, soul-in-a-box 2, Ardougne achievements 10, the
Vitur ring 5, a magpie familiar 3, Thieving auras 2-10). Success = 0.75 + 1% per effective level over
the target's requirement, at most 100%. An attempt takes 1.8 s (1.2 s with Sticky Fingers, which
gives 2/3 xp); a failure adds a 3 s stun. Pickpockets per hour = 3600 / (1.8 + (1 - success) x 3).

Stalls, chests and heists (Vault of Hereditas) aren't in the module: they keep the guide rates.
Coins and loot per pickpocket aren't counted (no drop values here).
"""

from __future__ import annotations

from ..models import PlayerState
from . import bands, data, profile, row

ATTEMPT_SECONDS = 1.8
STICKY_ATTEMPT_SECONDS = 1.2
FAIL_PENALTY_SECONDS = 3
# Transcribed from Module:Thieving chance calculator (multipliers_list): item -> effective levels.
ITEM_BOOSTS = {"Gloves of silence": 5, "Soul-in-a-box": 2, "Vitur": 5}
ARDOUGNE = ("Ardougne achievements", 10)  # elite Ardougne achievements (via Ardougne cloak 4 ownership)
AURAS = {1: 2, 2: 4, 3: 6, 4: 8, 5: 10}
MAGPIE = 3


def _req_modifier(req: int) -> int:
    return 15 if req <= 30 else 20 if req <= 50 else 25


def target_rates(player: PlayerState, level: int | None = None) -> list[dict]:
    level = level or player.level("Thieving")
    boost, notes = 0, []
    for item, lv in ITEM_BOOSTS.items():
        if profile.has(player, item, f"{item}*"):
            boost += lv
            notes.append(item)
    if profile.has(player, "Ardougne cloak 4"):
        boost += ARDOUGNE[1]
        notes.append("Ardougne cloak 4 (elite achievements)")
    if aura := profile.boosts(player).get("thieving_aura"):
        boost += AURAS.get(int(aura), 0)
        notes.append(f"Thieving aura tier {aura}")
    if profile.familiar(player, "Thieving") == "Magpie":
        boost += MAGPIE
        notes.append("Magpie")
    sticky = bool(profile.boosts(player).get("sticky_fingers"))
    attempt = STICKY_ATTEMPT_SECONDS if sticky else ATTEMPT_SECONDS
    xp_profile = profile.profile(player, "Thieving")
    out = []
    for t in data().get("thieving", []):
        req = max(1, int(t["level"] or 1))
        if level < req or (t.get("members") and not player.members):
            continue
        chance = min(1.0, max(0.0, 0.75 + 0.01 * (level + _req_modifier(req) + boost - req)))
        per_hour = 3600 / (attempt + (1 - chance) * FAIL_PENALTY_SECONDS)
        xp = t["xp"] * (2 / 3 if sticky else 1) * (1 + xp_profile.xp_bonus)
        out.append({"target": t["text"], "link": t["link"], "level": req, "chance": chance,
                    "per_hour": per_hour, "xp_per_hour": per_hour * xp,
                    "notes": notes + xp_profile.notes + (["Sticky Fingers"] if sticky else [])})
    return out


def _pickpocket_table(link: str) -> str:
    """The drop source a pickpocket rolls: "<page> (pickpocket)" where the page has kill loot too (a guard: grapes
    are a kill drop, not a pickpocket one), else the page itself (a Master Farmer's table is all pickpocket loot)."""
    from ..drops import tables

    split = f"{link} (pickpocket)"
    return split if split in tables()["sources"] else link


def method_rows(player: PlayerState) -> list[dict]:
    rows = []
    for band in bands(player, "Thieving"):
        for t in target_rates(player, band):
            # The target's pickpocket table (drop_tables.json) makes the row a gathering source: seeds from
            # Master Farmers at the successful picks per hour (drops.kills_per_hour reads actions_per_hour).
            rows.append(row("Thieving", f"Pickpocketing {t['target']} (simulated)", max(band, t["level"]), band + 4,
                            t["xp_per_hour"], assumptions=["no walking between targets; loot not counted"],
                            activity=f"Pickpocketing {t['link']}", boosts=t["notes"],
                            drop_sources=[_pickpocket_table(t["link"])], actions_per_hour=t["per_hour"] * t["chance"]))
    return rows
