"""Reality check while the optimiser steers: its forecasts against the room.

The fit's own checks (confidence, pinned parameters, the neutral offset
against history) judge the model on the past. This one judges it on what
it does now: each plan predicts the indoor temperature hours ahead, and
when the room keeps ending up clearly colder than the plan made
``LOOKBACK`` earlier said, the model is wrong in the direction that
matters (too little heat), whatever the reason (a parameter, the weather,
an open window). The optimiser then pauses for ``PAUSE`` and the rules
steer; the gate says why and until when.

Only plans made while the optimiser steered count (a plan that was not
applied predicts nothing about the room), and only a shortfall: a room
warmer than predicted costs a little energy, not comfort, and the
guards that keep the offset near the rules make it warmer than planned
on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

LOOKBACK = timedelta(hours=2)
# The plan compared with is one made between LOOKBACK and LOOKBACK + WINDOW ago.
WINDOW = timedelta(minutes=45)
SHORTFALL = 0.3  # °C colder than predicted ...
SUSTAINED = timedelta(minutes=30)  # ... for this long
PAUSE = timedelta(hours=12)
KEEP = LOOKBACK + WINDOW + timedelta(minutes=15)


@dataclass(frozen=True)
class Pause:
    since: datetime
    until: datetime
    predicted: float
    measured: float

    def as_dict(self) -> Dict[str, Any]:
        return {"since": self.since.isoformat(), "until": self.until.isoformat(),
                "predicted": round(self.predicted, 2), "measured": round(self.measured, 2)}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Pause"]:
        try:
            return cls(datetime.fromisoformat(data["since"]), datetime.fromisoformat(data["until"]),
                       float(data["predicted"]), float(data["measured"]))
        except (KeyError, TypeError, ValueError):
            return None


def trajectory(steps: Sequence[Any]) -> List[Tuple[datetime, float]]:
    """(time, predicted indoor) points of a plan's steps."""
    if not steps:
        return []
    points = [(steps[0].start, float(steps[0].t_in_start))]
    points.extend((s.end, float(s.t_in_end)) for s in steps)
    return points


def predicted_at(points: Sequence[Tuple[datetime, float]], when: datetime) -> Optional[float]:
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if t0 <= when <= t1:
            span = (t1 - t0).total_seconds()
            return v0 if span <= 0 else v0 + (v1 - v0) * (when - t0).total_seconds() / span
    return None


class ForecastWatch:
    """Per system: recent steering plans, the shortfall run, the pause."""

    def __init__(self) -> None:
        self._plans: Dict[str, List[Tuple[datetime, List[Tuple[datetime, float]]]]] = {}
        self._short_since: Dict[str, datetime] = {}
        self.pauses: Dict[str, Pause] = {}
        self.last: Dict[str, Dict[str, Any]] = {}  # system -> the last comparison, for diagnostics

    def record(self, system: str, made_at: datetime, steps: Sequence[Any]) -> None:
        """A plan made while the optimiser steered ``system``."""
        points = trajectory(steps)
        plans = [p for p in self._plans.get(system, []) if made_at - p[0] <= KEEP]
        if points:
            plans.append((made_at, points))
        self._plans[system] = plans

    def paused(self, system: str, now: datetime) -> Optional[Pause]:
        pause = self.pauses.get(system)
        if pause is not None and now >= pause.until:
            del self.pauses[system]
            return None
        return pause

    def check(self, system: str, now: datetime, t_in: Optional[float], steering: bool) -> Optional[Pause]:
        """Compare the room with the plan made ``LOOKBACK`` ago; returns a
        new pause when the shortfall lasted ``SUSTAINED``."""
        if self.paused(system, now) is not None:
            return None
        if not steering or t_in is None:
            self._short_since.pop(system, None)
            return None
        candidates = [p for p in self._plans.get(system, []) if LOOKBACK <= now - p[0] <= LOOKBACK + WINDOW]
        if not candidates:
            self._short_since.pop(system, None)
            return None
        made_at, points = min(candidates, key=lambda p: now - p[0])
        predicted = predicted_at(points, now)
        if predicted is None:
            self._short_since.pop(system, None)
            return None
        self.last[system] = {"at": now.isoformat(), "plan_made": made_at.isoformat(),
                             "predicted": round(predicted, 2), "measured": round(t_in, 2)}
        if t_in > predicted - SHORTFALL:
            self._short_since.pop(system, None)
            return None
        since = self._short_since.setdefault(system, now)
        if now - since < SUSTAINED:
            return None
        self._short_since.pop(system, None)
        pause = Pause(now, now + PAUSE, predicted, t_in)
        self.pauses[system] = pause
        return pause

    def to_dict(self) -> Dict[str, Any]:
        return {system: p.as_dict() for system, p in self.pauses.items()}

    def load(self, data: Any) -> None:
        if isinstance(data, Mapping):
            for system, value in data.items():
                pause = Pause.from_dict(value)
                if pause is not None:
                    self.pauses[str(system)] = pause
