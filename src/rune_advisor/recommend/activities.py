"""Achievements advanced by doing an activity: kill counts and item drops.

Many achievements are tallies of one activity ("Cleanse the Gate of Elidinis. (X/25)") or need one
drop ("After the Flood": find Elidinis' memoir). Instead of a flat guess per tally unit, these are
costed from how fast the player can do the activity:
  * an item requirement (the test's description names a dropped item): expected kills to get it;
  * a kill count (the wiki description names a drop source or monster): one kill per unit.
Kill rates come from the guide methods the player can use (`drop_sources`, `actions_per_hour`),
else from the gear-based estimate for regular monsters (drops.estimated_kills_per_hour).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..cost import row_skills, typical_rate
from ..drops import estimated_kills_per_hour, kills_per_hour, sources_of, tables

if TYPE_CHECKING:
    from .base import Context

MIN_SOURCE_NAME = 6
# Single-word names shorter than this ("Shadow", "Guard") are too generic to match in prose.
MIN_SINGLE_WORD_NAME = 8
MAX_NAME_WORDS = 6
_WORD = re.compile(r"[\w'\-.]+")
_KILL_VERBS = re.compile(r"\b(kill|defeat|slay|cleanse|catch|pickpocket|loot|destroy|complete \d+ (?:runs|kills))", re.I)


@dataclass
class Rate:
    source: str
    per_hour: float  # kills/actions per hour
    how: str  # the method, or "estimated"
    # xp the activity trains while doing it: {skill: xp per hour} (the guide method's skills, or the
    # combat skill of an estimated kill rate). Earned on the way, so it lowers what the activity costs.
    xp_per_hour: dict[str, float] = field(default_factory=dict)


class Activities:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._guide: dict[str, Rate] | None = None
        self._names: dict[str, list[str]] | None = None
        self._by_source: dict[str, list[tuple[str, int]]] | None = None

    def _guide_rates(self) -> dict[str, Rate]:
        if self._guide is None:
            rates: dict[str, Rate] = {}
            p = self.ctx.player
            for skill in sorted({s for rows in self.ctx.cost.wiki_rows.values() for r in rows for s in row_skills(r)}):
                for row in self.ctx.cost.available_methods(skill, p.level(skill)):
                    kph = kills_per_hour(row, skill, typical_rate(row))
                    xp = {skill: typical_rate(row), **{s: float(v) for s, v in (row.get("side_xp") or {}).items()}}
                    for src in row.get("drop_sources") or []:
                        if kph and (src not in rates or kph > rates[src].per_hour):
                            rates[src] = Rate(src, kph, row["method"], xp)
            self._guide = rates
        return self._guide

    def rate(self, source: str) -> Rate | None:
        if r := self._guide_rates().get(source):
            return r
        if est := estimated_kills_per_hour(self.ctx.player, source, self.ctx.combat_style):
            return Rate(source, est[0], "estimated kill rate", {est[1]: est[2]})
        return None

    def item_hours(self, item: str) -> tuple[float, Rate] | None:
        """Expected hours to get one `item` as a drop, at the best source the player can do."""
        best = None
        for src, chance, qty in sources_of(item):
            if not isinstance(chance, (int, float)) or not chance or not (r := self.rate(src)):
                continue
            hours = 1 / (r.per_hour * min(1.0, chance * qty))
            if best is None or hours < best[0]:
                best = (hours, r)
        return best

    def _name_index(self) -> dict[str, list[str]]:
        """Lower-case base name (no "(...)" suffix, no leading "The") -> source/monster names."""
        if self._names is None:
            index: dict[str, list[str]] = {}
            for n in set(tables()["sources"]) | set(tables()["monsters"]):
                base = re.sub(r"\s*\(.*\)$", "", n).removeprefix("The ").lower()
                if len(base) >= MIN_SOURCE_NAME and (" " in base or len(base) >= MIN_SINGLE_WORD_NAME):
                    index.setdefault(base, []).append(n)
            self._names = index
        return self._names

    def named_sources(self, text: str, need_verb: bool = True) -> set[str]:
        """Every source/monster named in an achievement description about killing, cleansing,
        catching... (matched as whole word sequences, singular or plural). `need_verb=False` matches any
        text (a training method's name: "Pickpocketing Vyrewatch tither")."""
        if need_verb and not _KILL_VERBS.search(text):
            return set()
        index = self._name_index()
        words = [w.strip(".-'") for w in _WORD.findall(text.lower())]
        found = set()
        for i in range(len(words)):
            for j in range(i + 1, min(len(words), i + MAX_NAME_WORDS) + 1):
                phrase = " ".join(words[i:j])
                for cand in (phrase, phrase.removesuffix("s"), phrase.removesuffix("es")):
                    found.update(index.get(cand, []))
        return found

    def source_in(self, text: str) -> Rate | None:
        """The fastest doable source/monster named in an achievement description about killing."""
        rates = [r for n in self.named_sources(text) if (r := self.rate(n))]
        return max(rates, key=lambda r: r.per_hour, default=None)

    def achievements_at(self, source: str) -> list[tuple[str, int]]:
        """Unfinished achievements (name, RuneScore) that killing/looting `source` advances: kill counts
        that name it, or item requirements it drops."""
        if self._by_source is None:
            index: dict[str, list[tuple[str, int]]] = {}
            for a in self.ctx.player.achievements.values():
                if a.completed or a.runescore <= 0:
                    continue
                srcs = set()
                if any((t.get("totalValue") or 0) > 1 for t in a.requirements):
                    srcs |= self.named_sources(self.ctx.achievement_descriptions.get(a.name, ""))
                for t in a.requirements:
                    if not t.get("complete") and t.get("description"):
                        srcs |= {src for src, _, _ in sources_of(t["description"])}
                for src in srcs:
                    index.setdefault(src, []).append((a.name, a.runescore))
            self._by_source = index
        return self._by_source.get(source, [])
