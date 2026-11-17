"""The compressor's capacity: no price boost the compressor cannot deliver.

A price boost (``heat_cheap_sN`` / ``heat_verycheap_sN``) raises the curve
offset in cheap hours so the house stores heat. That only works while the
compressor has room to go faster. When it already runs at its top
frequency and the supply still lags its target (degree minutes well below
zero), a higher offset gives no more heat: it only raises the target, the
degree minutes fall further, the pump asks for its electric addition
(direct electricity, or nothing when the addition is blocked), and the
deficit is paid back by running at full speed after the cheap hours.

The night of 16-17 Nov: very cheap +2 from 23:00, the compressor at 90 Hz
from 01:00 to 05:00, the supply flat at 34-35 °C, degree minutes -450 to
-1000, 43084 asking 3 kW of a blocked addition, the room flat, and 90 Hz
again 05:40-07:30 to catch up.

So while the compressor is *saturated* the price component's boost is
held at 0 (a lowering in expensive hours still applies):

- saturated: heating priority, the frequency at least ``SHARE_ON`` of the
  highest frequency seen in ``KEEP_DAYS`` days and the degree minutes at
  most ``DM_ON``;
- released: the frequency below ``SHARE_OFF`` of the top, the degree
  minutes above ``DM_OFF``, or the readings missing;
- during hot water the state is kept: the compressor is busy, the
  heating deficit is unchanged.

The top frequency is learned (one maximum per day, kept for ``KEEP_DAYS``)
because it depends on the model and its settings; below ``MIN_TOP_HZ``
nothing is known yet and the guard stays off.

Plain Python, no Home Assistant imports.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Mapping, Optional

from .inputs import Decision
from .texts import msg

SHARE_ON = 0.95
SHARE_OFF = 0.85
DM_ON = -300.0
DM_OFF = -150.0
MIN_TOP_HZ = 50.0
KEEP_DAYS = 14
PRIORITY_HEATING = 30
PRIORITY_HOT_WATER = 20
REG_DEGREE_MINUTES = 43005


class Capacity:
    def __init__(self) -> None:
        self.saturated = False
        self.since: Optional[datetime] = None
        self.daily_top: Dict[str, float] = {}  # ISO date -> highest Hz that day
        self.freq: Optional[float] = None
        self.dm: Optional[float] = None

    @property
    def top(self) -> Optional[float]:
        top = max(self.daily_top.values(), default=None)
        return top if top is not None and top >= MIN_TOP_HZ else None

    def _learn(self, day: date, freq: float) -> None:
        key = day.isoformat()
        if freq > self.daily_top.get(key, 0.0):
            self.daily_top[key] = freq
        keep = sorted(self.daily_top)[-KEEP_DAYS:]
        self.daily_top = {k: self.daily_top[k] for k in keep}

    def update(self, now: datetime, freq: Optional[float], dm: Optional[float], priority: Optional[float]) -> bool:
        """One tick with the current readings (None: missing or stale);
        True when the state changed."""
        before = self.saturated
        self.freq, self.dm = freq, dm
        if freq is not None:
            self._learn(now.date(), float(freq))
        top = self.top
        if freq is None or dm is None or top is None:
            self._set(False, now)
        elif priority is not None and int(priority) == PRIORITY_HOT_WATER:
            pass  # hot water: keep the state
        elif self.saturated:
            if freq < SHARE_OFF * top or dm > DM_OFF:
                self._set(False, now)
        elif (priority is None or int(priority) == PRIORITY_HEATING) and freq >= SHARE_ON * top and dm <= DM_ON:
            self._set(True, now)
        return self.saturated != before

    def _set(self, on: bool, now: datetime) -> None:
        if on != self.saturated:
            self.since = now
        self.saturated = on

    def to_dict(self) -> Dict[str, Any]:
        return {"daily_top": dict(self.daily_top)}

    def load(self, data: Any) -> None:
        if not isinstance(data, Mapping):
            return
        tops = data.get("daily_top")
        if isinstance(tops, Mapping):
            for k, v in tops.items():
                try:
                    date.fromisoformat(str(k))
                except ValueError:
                    continue
                if isinstance(v, (int, float)) and v > 0:
                    self.daily_top[str(k)] = float(v)

    def attributes(self) -> Dict[str, Any]:
        top = self.top
        return {
            "saturated": self.saturated,
            "since": self.since.isoformat() if self.since is not None else None,
            "top_hz": top,
            "frequency_hz": self.freq,
            "degree_minutes": self.dm,
        }


def hold_boost(capacity: Capacity, decision: Decision) -> Decision:
    """The price component's decision, with a boost held at 0 while the
    compressor is saturated."""
    if not capacity.saturated or decision.offset <= 0:
        return decision
    details = dict(decision.details, held_boost=decision.offset)
    reason = msg("price.capacity_held", reason=decision.reason, freq=capacity.freq or 0.0, dm=capacity.dm or 0.0)
    return Decision(0.0, reason, decision.inputs_ok, details)
