"""Passive workforces: people who gather for the player whether or not they play, split across categories the player
chooses. Managing Miscellania's subjects (kingdom.py) and the Anachronia base camp's workers (basecamp.py) are the two.

What they share, and this module holds:
  * worker-days: a plan books some of the workforce on a category for the days its window lasts (the window: the plan's
    hours played, plus earlier goals' in a joint plan, at `play_hours_per_day`); at most `per_category` workers a
    category and `workers` in all at any one time;
  * output per worker-day per (category, setting): what a booked worker-day brings in; items a category brings in
    together (tuna with swordfish) share the worker-days booked for either;
  * pay per worker-day, with what is already paid (`prepaid`: the kingdom's coffers) used first;
  * a stock of the workforce's own items already gathered (the base camp's store, and compacted resources a station
    drops), drawn before any worker-day is booked.
A plan works on a copy (`Ledger.copy`) and a kept plan's bookings are adopted (`Ledger.adopt`), so a joint plan's
goals can't spend the same workers or stock twice. Booking a category is the plan choosing where the workers go: the
step names the move.
"""

from __future__ import annotations

from .models import PlayerState


class Workforce:
    """One workforce's rules. Subclasses say how many workers, how many a category may take, and what they bring in."""

    name = ""
    worker = "worker"  # what one is called in a step ("subject" for the kingdom)
    cost_per_worker_day = 0.0  # coins

    def workers(self, player: PlayerState) -> int:
        raise NotImplementedError

    def per_category(self, player: PlayerState) -> int:
        return self.workers(player)

    def output(self, player: PlayerState, category: str, setting: str) -> dict[str, float]:
        """Items one worker-day brings in."""
        raise NotImplementedError

    def prepaid(self, player: PlayerState) -> float:
        """Worker-days already paid for."""
        return 0.0

    def stock(self, player: PlayerState, ctx=None) -> dict[str, float]:
        """The workforce's items already in store that plans may draw on."""
        return {}


class Ledger:
    """One plan's (or a joint plan's) use of a workforce over a window of `days`: worker-days per (category, setting),
    what each item has drawn from them, and the stock left."""

    def __init__(self, workforce: Workforce, player: PlayerState, days: float, ctx=None):
        self.workforce, self.player, self.days = workforce, player, days
        self.alloc: dict[tuple[str, str], float] = {}
        self.passive: dict[str, float] = {}  # other passive supplies (weekly claims, kept-planted patches): units drawn
        self.drawn: dict[tuple[str, str, str], float] = {}
        self.prepaid = workforce.prepaid(player)
        self.stock: dict[str, float] = dict(workforce.stock(player, ctx))

    def _blank(self) -> "Ledger":
        out = object.__new__(type(self))
        out.workforce, out.player, out.days = self.workforce, self.player, self.days
        return out

    def copy(self) -> "Ledger":
        out = self._blank()
        out.alloc, out.drawn, out.passive = dict(self.alloc), dict(self.drawn), dict(self.passive)
        out.prepaid, out.stock = self.prepaid, dict(self.stock)
        return out

    def adopt(self, other: "Ledger") -> None:
        """Take on another ledger's bookings (a plan's working copy, once the plan is kept)."""
        self.alloc, self.drawn, self.passive = dict(other.alloc), dict(other.drawn), dict(other.passive)
        self.prepaid, self.stock = other.prepaid, dict(other.stock)

    def per_worker_day(self, item: str, category: str, setting: str) -> float:
        return self.workforce.output(self.player, category, setting)[item]

    def capacity(self, item: str, category: str, setting: str, days: float | None = None) -> float:
        """How many of `item` this setting can still bring in: partner worker-days already booked, then free workers
        (the stock is separate: `take_stock`)."""
        per = self.per_worker_day(item, category, setting)
        key = (category, setting)
        spare = max(0.0, self.alloc.get(key, 0.0) * per - self.drawn.get((*key, item), 0.0))
        return spare + self._room(category, days) * per

    def _room(self, category: str, days: float | None = None) -> float:
        days = self.days if days is None else days
        cat = sum(v for (c, _), v in self.alloc.items() if c == category)
        total = sum(self.alloc.values())
        wf, p = self.workforce, self.player
        return max(0.0, min(wf.per_category(p) * days - cat, wf.workers(p) * days - total))

    def take(self, item: str, n: float, category: str, setting: str, days: float | None = None) -> tuple[float, float, float]:
        """Draw up to `n` of `item`: (units from worker-days already booked, units from new ones, new worker-days)."""
        per = self.per_worker_day(item, category, setting)
        key = (category, setting)
        made = self.alloc.get(key, 0.0) * per
        spare = max(0.0, made - self.drawn.get((*key, item), 0.0))
        free = min(n, spare)
        new_days = min((n - free) / per, self._room(category, days))
        self.alloc[key] = self.alloc.get(key, 0.0) + new_days
        self.drawn[(*key, item)] = self.drawn.get((*key, item), 0.0) + free + new_days * per
        return free, new_days * per, new_days

    def pay(self, days: float) -> tuple[float, float]:
        """(new coins, prepaid coins) for `days` of new worker-days: what is already paid first."""
        cost = self.workforce.cost_per_worker_day
        from_prepaid = min(days, self.prepaid)
        self.prepaid -= from_prepaid
        return (days - from_prepaid) * cost, from_prepaid * cost

    def take_stock(self, item: str, n: float) -> float:
        got = min(n, self.stock.get(item, 0.0))
        if got:
            self.stock[item] -= got
        return got

    def add_stock(self, item: str, n: float) -> None:
        self.stock[item] = self.stock.get(item, 0.0) + n
