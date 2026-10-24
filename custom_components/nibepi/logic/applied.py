"""When the optimiser's plans actually steered the pump.

Expected savings and the plan-vs-measured comparison only mean something
for the time a plan was applied: in shadow mode, with the gate closed
(low model confidence, missing inputs) or with a hot water plan blocked by
the pump's settings, the pump follows the rules and the plan's savings
never happen. The runtime marks each tick per key (a heating system, or
``hot_water``) with whether that plan was applied; the span between two
marks counts as applied when the earlier mark said so. A gap longer than
``max_gap`` (HA stopped, ticks failing) counts as not applied.

Kept in memory only: after a restart nothing before it counts, which
errs on the side of not claiming savings.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

Span = Tuple[datetime, datetime]

MAX_GAP = timedelta(minutes=5)
HOT_WATER = "hot_water"


class AppliedLog:
    def __init__(self, max_gap: timedelta = MAX_GAP) -> None:
        self.max_gap = max_gap
        self._last: Dict[str, Tuple[datetime, bool]] = {}
        self._spans: Dict[str, List[Span]] = {}

    def mark(self, key: str, now: datetime, applied: bool) -> None:
        last = self._last.get(key)
        if last is not None and last[1] and timedelta(0) < now - last[0] <= self.max_gap:
            spans = self._spans.setdefault(key, [])
            if spans and spans[-1][1] == last[0]:
                spans[-1] = (spans[-1][0], now)
            else:
                spans.append((last[0], now))
        self._last[key] = (now, bool(applied))

    def applied_now(self, key: str) -> bool:
        last = self._last.get(key)
        return bool(last and last[1])

    def spans(self, key: str, start: datetime, end: datetime) -> List[Span]:
        """The applied spans of ``key`` clipped to [start, end)."""
        out: List[Span] = []
        for s0, s1 in self._spans.get(key, ()):
            a, b = max(s0, start), min(s1, end)
            if b > a:
                out.append((a, b))
        return out

    def seconds(self, key: str, start: datetime, end: datetime) -> float:
        return sum((b - a).total_seconds() for a, b in self.spans(key, start, end))

    def share(self, keys: Iterable[str], start: datetime, end: datetime) -> Optional[float]:
        """The smallest applied share of [start, end) over ``keys``; None without keys."""
        total = (end - start).total_seconds()
        shares = [self.seconds(key, start, end) / total for key in keys] if total > 0 else []
        return min(shares) if shares else None

    def prune(self, before: datetime) -> None:
        for key, spans in self._spans.items():
            self._spans[key] = [(max(a, before), b) for a, b in spans if b > before]
