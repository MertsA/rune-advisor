"""Cost model: estimate hours of play to reach a goal."""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from . import datapkg

from . import requirements
from .xp import xp_at

DEFAULT_QUEST_HOURS = 1.5

COMBAT_SKILLS = ("Attack", "Strength", "Defence", "Ranged", "Magic", "Necromancy")
# Combat gives 50 combat-skill xp and 16.5 Constitution xp per 1,000 enemy life points
# (https://runescape.wiki/w/Experience), so Constitution earns 0.33x the combat skill's xp.
CONSTITUTION_SHARE = 16.5 / 50


def _load_defaults() -> dict:
    return tomllib.loads(datapkg.path("xp_rates.toml").read_text())


def _load_wiki_rates(members: bool = True) -> list[dict]:
    """Rows extracted from the wiki training guides (docs/data-package.md, "Rebuilding the data"), if present:
    the pay-to-play guides for members, the free-to-play guides otherwise."""
    name = "wiki_rates.json" if members else "wiki_rates_f2p.json"
    path = datapkg.path(name)
    rows = json.loads(path.read_text()) if path.is_file() else []
    # New content the guides mention without a rate, derived from its activity pages (members only).
    extra = datapkg.path("content_rates.json")
    if members and extra.is_file():
        rows += json.loads(extra.read_text())["rows"]
    return rows


# Wiki skill names that differ from the hiscores names we key skills by.
WIKI_SKILL_NAMES = {"Runecrafting": "Runecraft", "Constitution": "Hitpoints"}
# A generic "Melee" rate applies to whichever melee style is being trained.
MELEE_SKILLS = ("Attack", "Strength", "Defence")


def row_skills(row: dict) -> list[str]:
    skill = WIKI_SKILL_NAMES.get(row["skill"], row["skill"])
    return list(MELEE_SKILLS) if skill == "Melee" else [skill]

# Skills where an xp/hour figure is the wrong model (Summoning is limited by charms, not time).
NOT_TIME_LIMITED = {"Summoning"}
# Fewer usable wiki rows than this and the skill keeps its hand-written rates.
MIN_WIKI_METHODS = 3
# A level band only uses wiki rates if at least this many methods are available there; otherwise
# the single available method is probably a niche one (e.g. Herblore's tar, when the potion tables
# couldn't be extracted) and the hand-written rate is a better estimate.
MIN_METHODS_PER_LEVEL = 2
# Best-case figures are summarised by their own median only when there are at least this many.
MIN_MAX_ROWS = 3


def usable_wiki_rows(rows: list[dict]) -> list[dict]:
    """Rows a solo player can use as ongoing training (per the review tags, when present)."""
    return [r for r in rows
            if not r.get("boosted")
            and r.get("solo", True)
            and r.get("regular_training", True)
            and not r.get("suspicious", False)
            and r.get("figure", "typical") != "per_task"]


def wiki_rate_bands(rows: list[dict]) -> dict[str, list[tuple[int, float | None, str]]]:
    """Per skill, [(from_level, xp_per_hour, method)] at each level where a method starts.
    xp_per_hour is None for bands with too few methods (use the hand-written rate there).

    Uses the fastest usable method available at that level. If that method's figure is only a
    best case (e.g. Slayer's per-task maximums), use the median of the available methods instead.

    Rates are while doing the activity: methods that consume materials (bought or, for ironmen,
    self-gathered) keep their rate here; the time to get the materials is a separate cost.
    """
    by_skill: dict[str, list[dict]] = {}
    for r in usable_wiki_rows(rows):
        for skill in row_skills(r):
            if skill not in NOT_TIME_LIMITED:
                by_skill.setdefault(skill, []).append(r)
    bands: dict[str, list[tuple[int, float, str]]] = {}
    for skill, methods in by_skill.items():
        if len(methods) < MIN_WIKI_METHODS:
            continue
        out = []
        for level in sorted({m["level_min"] for m in methods}):
            available = [m for m in methods if m["level_min"] <= level
                         and (m.get("level_max") is None or m["level_max"] >= level)]
            if not available:
                continue
            if len(available) < MIN_METHODS_PER_LEVEL:
                out.append((level, None, ""))
                continue
            best = max(available, key=typical_rate)
            if best.get("figure") == "max":
                # Best-case figures (Slayer's per-task maxima): their median, unless a typical-figure
                # method (often simulated) is faster.
                maxed = [m for m in available if m.get("figure") == "max"]
                # With only one or two best-case rows their "median" is a maximum: use every method's.
                ranked = sorted(maxed if len(maxed) >= MIN_MAX_ROWS else available, key=typical_rate)
                best = ranked[len(ranked) // 2]
                label = f"median of {len(ranked)} methods (best-case figures)"
                typical = max((m for m in available if m.get("figure") != "max"), key=typical_rate, default=None)
                if typical is not None and typical_rate(typical) > typical_rate(best):
                    best, label = typical, typical["method"]
            else:
                label = best["method"]
            out.append((level, typical_rate(best), label))
        if out:
            bands[skill] = out
    return bands


def _merge_bands(guess: list[tuple[int, float]], wiki: list[tuple[int, float | None, str]]) -> list[tuple[int, float]]:
    """Wiki rates where a wiki band has a rate; hand-written rates below the first wiki level and in
    bands without enough wiki methods."""
    def at(bands, level):
        current = None
        for start, rate in bands:
            if level >= start:
                current = rate
        return current

    wiki_simple = [(lvl, rate) for lvl, rate, _ in wiki]
    first = wiki[0][0]
    merged = []
    for level in sorted({lvl for lvl, _ in guess} | {lvl for lvl, _, _ in wiki}):
        rate = at(wiki_simple, level) if level >= first else None
        merged.append((level, rate if rate is not None else at(guess, level) or (guess[0][1] if guess else 100_000.0)))
    return merged


def custom_row(m: dict) -> dict:
    """A [[methods]] entry from rune_advisor.toml as a wiki-rate-style row."""
    return {"skill": m["skill"], "method": m["method"], "level_min": int(m.get("level_min", 1)),
            "level_max": m.get("level_max"), "xp_per_hour_low": float(m["xp_per_hour"]), "xp_per_hour_high": None,
            "requirements": m.get("requirements", ""), "yields": dict(m.get("yields", {})),
            "side_xp": dict(m.get("side_xp", {})), "source": m.get("source", "rune_advisor.toml"), "solo": True,
            # "simulated" (mechanics/), "measured" (curated from radv session), else typed in; guide rows are "quoted".
            "basis": m.get("basis", "measured" if m.get("clicks_per_hour") is not None else "custom"),
            "activity": m.get("activity"), "assumptions": list(m.get("assumptions", [])),
            **({"recipe": m["recipe"]} if m.get("recipe") else {}), "boosts": list(m.get("boosts", [])),
            # Drop/pickpocket tables the activity loots (drops.py), and actions per hour when xp per kill isn't
            # the measure (pickpockets, cleanses).
            **({"drop_sources": list(m["drop_sources"])} if m.get("drop_sources") else {}),
            **({"actions_per_hour": float(m["actions_per_hour"])} if m.get("actions_per_hour") else {}),
            # Invention twins (mechanics/item_xp.py) remember the method they ride on.
            **({k: m[k] for k in ("base_skill", "base_method") if m.get(k)}),
            # Measured attention (radv session): game actions per hour and the median gap between inputs.
            "clicks_per_hour": m.get("clicks_per_hour"), "input_gap_s": m.get("input_gap_s"),
            "regular_training": True, "figure": m.get("figure", "typical"), "low_high_meaning": "single"}


def basis(row: dict) -> str:
    """Where a row's rate comes from: "simulated", "measured", "custom" or "quoted" (a training guide)."""
    return row.get("basis") or "quoted"


def replaced_by_sims(rows: list[dict]) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """Drop rows a better basis covers: a simulated "Maple Tree" replaces the guide's "Maple trees" (the sim
    is per player, the guide a quote), and a measured [[methods]] row (curated from radv session) replaces both for its activity.
    Returns (kept, [(skill, dropped method, covering activity)])."""
    from .mechanics import covers

    better = {"quoted": ("simulated", "measured"), "simulated": ("measured",)}
    by_basis: dict[str, dict[str, set[str]]] = {"simulated": {}, "measured": {}}
    for r in rows:
        if basis(r) in by_basis and r.get("activity"):
            for skill in row_skills(r):
                by_basis[basis(r)].setdefault(skill, set()).add(r["activity"])
    kept, replaced = [], []
    for r in rows:
        name = r.get("activity") or r["method"]
        hit = next((a for b in better.get(basis(r), ()) for skill in row_skills(r)
                    for a in by_basis[b].get(skill, ()) if covers(a, name)), None)
        if hit:
            replaced.append((r["skill"], r["method"], hit))
            continue
        kept.append(r)
    return kept, replaced


def typical_rate(row: dict) -> float:
    lo, hi = row.get("xp_per_hour_low"), row.get("xp_per_hour_high")
    if lo is not None and hi is not None:
        # "variants" means two setups (e.g. without/with bought energy): take the plain one.
        return float(lo) if row.get("low_high_meaning") == "variants" else (lo + hi) / 2
    return float(lo if lo is not None else hi)


@dataclass
class CostModel:
    rates: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    quest_length_hours: dict[str, float] = field(default_factory=dict)
    # Skills whose rate the player set explicitly (rune_advisor.toml); these are never derived.
    overridden: set[str] = field(default_factory=set)
    # Where each skill's rates came from: "wiki", "guess" or "override".
    rate_source: dict[str, str] = field(default_factory=dict)
    # Method behind each wiki band, keyed by (skill, from_level), for plan explanations.
    methods: dict[tuple[str, int], str] = field(default_factory=dict)
    # Usable wiki method rows per skill (see usable_wiki_rows), for material-aware planning.
    wiki_rows: dict[str, list[dict]] = field(default_factory=dict)
    # The hand-written "reasonably efficient" rates (data/xp_rates.toml), before wiki/override merging.
    baseline: dict[str, list[tuple[int, float]]] = field(default_factory=dict)
    # Wiki methods left out because the player lacks their quests/skill levels: (skill, method, missing).
    excluded: list[tuple[str, str, list[str]]] = field(default_factory=list)
    # Guide rows a simulation replaced: (skill, guide method, simulated activity).
    replaced: list[tuple[str, str, str]] = field(default_factory=list)
    # Basis of each band's rate ("simulated", "quoted"...), keyed like `methods`.
    band_basis: dict[tuple[str, int], str] = field(default_factory=dict)

    @classmethod
    def default(cls, overrides: dict[str, float] | None = None, use_wiki: bool = True,
                members: bool = True, player=None, extra_methods: list[dict] | None = None) -> "CostModel":
        """`player` (a PlayerState) drops wiki methods whose quest/skill prerequisites they don't meet.

        `extra_methods` are extra method rows ([[methods]] in rune_advisor.toml) for content the wiki guides don't
        cover yet: skill, method, level_min, xp_per_hour, optional level_max, requirements, `yields`
        ({item: per hour}, which makes the method a gathering source) and `side_xp` ({skill: xp per
        hour} in other skills)."""
        raw = _load_defaults()
        rates = {k: [(int(a), float(b)) for a, b in v] for k, v in raw["rates"].items()}
        source = {k: "guess" for k in rates}
        baseline = dict(rates)
        methods = {}
        wiki_rows: dict[str, list[dict]] = {}
        excluded, replaced, band_basis = [], [], {}
        if use_wiki:
            rows = _load_wiki_rates(members) + [custom_row(m) for m in extra_methods or []]
            rows, replaced = replaced_by_sims(rows)
            if player is not None:
                kept = []
                for r in rows:
                    if missing := requirements.unmet(r.get("requirements"), player, tuple(row_skills(r))):
                        excluded.append((r["skill"], r["method"], missing))
                    elif getattr(player, "ironman", False) and (th := requirements.th_only_items(r.get("requirements"))
                                                                or requirements.th_only_mentions(r.get("method"))):
                        # Protean traps and the like only come from Treasure Hunter, which ironmen can't play.
                        # The free-to-play rows name them in the method ("Logs, portable brazier"), not a requirement.
                        excluded.append((r["skill"], r["method"], [f"{i} (Treasure Hunter only)" for i in th]))
                    else:
                        kept.append(r)
                rows = kept
            for r in usable_wiki_rows(rows):
                for skill in row_skills(r):
                    wiki_rows.setdefault(skill, []).append(r)
            for skill, bands in wiki_rate_bands(rows).items():
                if all(rate is None for _, rate, _ in bands):
                    continue  # never enough methods at any level: keep the hand-written rates
                rates[skill] = _merge_bands(rates.get(skill, []), bands)
                source[skill] = "wiki"
                # Fallback bands get an empty label so they don't show the previous band's method.
                methods.update({(skill, lvl): method for lvl, _, method in bands})
            by_method = {r["method"]: basis(r) for r in rows}
            simulated = {sk for r in rows if basis(r) == "simulated" for sk in row_skills(r)}
            band_basis = {k: by_method.get(m, ("median (mixed)" if k[0] in simulated else "quoted")
                                           if m.startswith("median of") else "guess")
                          for k, m in methods.items() if m}
        for skill, flat in (overrides or {}).items():
            rates[skill] = [(1, float(flat))]
            source[skill] = "override"
        return cls(rates=rates, quest_length_hours=raw["quest_length_hours"], overridden=set(overrides or {}),
                   rate_source=source, methods=methods, wiki_rows=wiki_rows, baseline=baseline,
                   excluded=excluded, replaced=replaced, band_basis=band_basis)

    def with_flat_rate(self, skill: str, rate: float) -> "CostModel":
        return CostModel(rates={**self.rates, skill: [(1, rate)]}, quest_length_hours=self.quest_length_hours,
                         overridden=self.overridden, rate_source=self.rate_source, methods=self.methods,
                         wiki_rows=self.wiki_rows, baseline=self.baseline, excluded=self.excluded,
                         replaced=self.replaced, band_basis=self.band_basis)

    def available_methods(self, skill: str, level: int) -> list[dict]:
        """Usable wiki methods for a skill at a level, fastest first (empty when the rate isn't from the wiki)."""
        if self.rate_source.get(skill) != "wiki":
            return []
        rows = [r for r in self.wiki_rows.get(skill, [])
                if r["level_min"] <= level and (r.get("level_max") is None or r["level_max"] >= level)]
        return sorted(rows, key=typical_rate, reverse=True)

    def method(self, skill: str, level: int) -> str | None:
        """Wiki method name behind the rate used at this level, if the rate came from the wiki."""
        starts = [lvl for (s, lvl) in self.methods if s == skill and lvl <= level]
        return (self.methods.get((skill, max(starts))) or None) if starts else None

    def best_combat_source(self, levels: dict[str, int]) -> tuple[str, float]:
        """The combat skill with the highest xp/hr at the player's levels, and that rate."""
        return max(((s, self.rate(s, levels.get(s, 1))) for s in COMBAT_SKILLS), key=lambda sr: sr[1])

    def baseline_rate(self, skill: str, level: int) -> float:
        """The hand-written rate at a level, ignoring wiki bands (a "normal training" yardstick)."""
        return self.rate(skill, level, self.baseline)

    def rate(self, skill: str, level: int, table: dict[str, list[tuple[int, float]]] | None = None) -> float:
        bands = (table if table is not None else self.rates).get(skill) or [(1, 100_000.0)]
        current = bands[0][1]
        for start, r in bands:
            if level >= start:
                current = r
        return current

    def hours_for_xp(self, skill: str, from_xp: int, to_xp: int) -> float:
        """Integrate band rates level by level from from_xp to to_xp."""
        if to_xp <= from_xp:
            return 0.0
        hours, xp, level = 0.0, from_xp, 1
        while level < 150 and xp_at(skill, level + 1) <= xp:
            level += 1
        while xp < to_xp:
            next_xp = min(to_xp, xp_at(skill, level + 1)) if level < 150 else to_xp
            if next_xp <= xp:  # past the end of the table
                next_xp = to_xp
            hours += (next_xp - xp) / self.rate(skill, level)
            xp, level = next_xp, level + 1
        return hours

    def hours_to_level(self, skill: str, current_xp: int, target_level: int) -> float:
        return self.hours_for_xp(skill, current_xp, xp_at(skill, target_level))

    def quest_hours(self, length: str | None) -> float:
        if not length:
            return DEFAULT_QUEST_HOURS
        if length in self.quest_length_hours:
            return self.quest_length_hours[length]
        # Ranges like "Long to Very Long": average the endpoints we recognise.
        parts = [self.quest_length_hours[p.strip()] for p in length.split(" to ") if p.strip() in self.quest_length_hours]
        return sum(parts) / len(parts) if parts else DEFAULT_QUEST_HOURS
