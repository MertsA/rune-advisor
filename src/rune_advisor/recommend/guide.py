"""The guide view of the tracks: per track, short entries with a reason, what's left and why the hours may
be off, instead of `radv recommend`'s raw step lists (which stay as the debug view).

Tracks stay separate (no common currency, the user's call 2026-10-29): each section ranks its own
candidates by value per hour. Entries whose hours are mostly placeholders (DEFAULT_TEST_HOURS, per-unit
tally guesses) sort below the modelled ones, since their rank says little.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..cost import COMBAT_SKILLS
from .base import Candidate, rank

GUESSED_SHARE = 0.5  # an entry whose hours are at least this share guesses is "low confidence"
SHORT_HOURS = 1.0
# Skills that make an entry a combat one, beyond the combat skills proper.
COMBAT_LIKE = set(COMBAT_SKILLS) | {"Hitpoints", "Slayer", "Necromancy"}
# Tracks whose entries are always of one kind.
TRACK_TAGS = {"gear": {"combat"}, "collection_logs": {"combat"}, "quest_points": {"quest"}, "outfits": {"skilling"}, "free_xp": {"short"}}
MOODS = ("combat", "skilling", "quest", "short", "afk")
AFK_SHARE = 0.75  # an AFK method this fast (of the fastest) makes training the skill an AFK option


@dataclass
class GuideEntry:
    track: str
    headline: str
    hours: float
    value: str
    why: str
    remaining: list[str]
    tags: set[str]
    confidence: str  # "modelled", "partly guessed", "mostly guessed"
    unsure: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    # Time at the keyboard; `hours` is the ranking cost, net of earned xp (0: the same as hours).
    play_hours: float = 0.0

    @property
    def time_to_play(self) -> float:
        return self.play_hours or self.hours


def afk_skills(ctx) -> set[str]:
    """Skills with an AFK-flagged method (guide rows' `afk`) within AFK_SHARE of the fastest at the player's level."""
    from ..cost import typical_rate

    out = set()
    for skill in ctx.player.skills:
        methods = ctx.cost.available_methods(skill, ctx.player.level(skill))
        rates = [(typical_rate(m) or 0, bool(m.get("afk"))) for m in methods]
        best = max((r for r, _ in rates), default=0)
        if best and any(afk and r >= AFK_SHARE * best for r, afk in rates):
            out.add(skill)
    return out


def tags_of(c: Candidate, afk: set[str] | None = None, halved: set[str] | None = None) -> set[str]:
    tags = set(TRACK_TAGS.get(c.track, set()))
    if afk is not None and c.skills and c.skills <= afk:
        tags.add("afk")
    if halved and c.skills & halved:
        tags.add("half-rocks")  # trains the skill the last statue rock came from: its next rock is half as likely
    if c.skills & COMBAT_LIKE:
        tags.add("combat")
    if c.skills - COMBAT_LIKE:
        tags.add("skilling")
    if any(n.startswith("quest ") or ": quest " in n for n in c.needs):
        tags.add("quest")
    if c.cost_hours <= SHORT_HOURS:
        tags.add("short")
    return tags


def confidence_of(c: Candidate) -> str:
    if c.guessed_hours >= GUESSED_SHARE * max(c.cost_hours, 1e-9) and c.guessed_hours > 0:
        return "mostly guessed"
    if c.uncertain or c.unknowns:
        return "partly guessed"
    return "modelled"


def entry(c: Candidate, afk: set[str] | None = None, halved: set[str] | None = None) -> GuideEntry:
    why = c.why or next((d for d in c.details if d), "")
    return GuideEntry(track=c.track, headline=c.title, hours=c.cost_hours, value=f"{c.value:g} {c.unit}".strip(),
                      why=why, remaining=list(c.needs), tags=tags_of(c, afk, halved), confidence=confidence_of(c),
                      unsure=list(c.unknowns), steps=list(c.details),
                      play_hours=c.play_hours if c.play_hours is not None else c.cost_hours)


def guide(per_track: dict[str, list[Candidate]], mood: str | None = None,
          max_hours: float | None = None, afk: set[str] | None = None,
          halved: set[str] | None = None) -> dict[str, list[GuideEntry]]:
    """Each track's entries, best first, modelled before mostly guessed; filtered by mood tag and length.
    `afk` (afk_skills) enables the afk tag: every skill the entry trains has a near-best AFK method.
    `halved` (player.rock_halved_skills) tags entries training the skill the last statue rock came from."""
    out = {}
    for track, cands in per_track.items():
        entries = [entry(c, afk, halved) for c in rank(cands)]
        entries = [e for e in entries if (mood is None or mood in e.tags)
                   and (max_hours is None or e.hours <= max_hours)]
        # Stable sort: rank order within each confidence level.
        entries.sort(key=lambda e: e.confidence == "mostly guessed")
        out[track] = entries
    return out
