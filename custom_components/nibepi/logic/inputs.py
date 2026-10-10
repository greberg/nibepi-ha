"""Input and output types shared by the controllers.

Ports the input checks of config_node.js:

- ``is_stale`` is ``sensorIsStale``: ``sensor_timeout`` minutes, unset means
  the 60 minute default, 0 disables the check, a missing or non-finite
  timestamp is stale.
- ``is_missing`` is ``isMissingValue`` (None and the 9999 "no value"
  marker) plus non-finite numbers.

Every controller returns a ``Decision``. A missing or stale input gives
offset 0 with ``inputs_ok`` False and the reason stated, so the curve
component falls back to neutral instead of keeping its last value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

DEFAULT_SENSOR_TIMEOUT_MIN = 60.0


def to_float(value: Any) -> Optional[float]:
    """Return a finite float or None (strings like "21.5" are accepted)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def is_missing(value: Any) -> bool:
    """``isMissingValue`` from config_node.js, plus non-finite numbers."""
    number = to_float(value)
    return number is None or number == 9999.0


# Home Assistant state strings that mean "no value".
NO_VALUE_STATES = frozenset({"unavailable", "unknown", "none", ""})


def state_value(state: Any) -> Optional[float]:
    """A Home Assistant state string as a number; unavailable/unknown is None."""
    if isinstance(state, str) and state.strip().lower() in NO_VALUE_STATES:
        return None
    return to_float(state)


def state_timestamp(last_reported: Any, last_updated: Any) -> Optional[datetime]:
    """When a Home Assistant entity last reported its value.

    Cloud sensors such as Netatmo poll every ~10 minutes and often report
    the same temperature; ``last_updated``/``last_changed`` then stand
    still and the sensor would look stale. ``last_reported`` (HA 2024.3+)
    moves on every report, so it is preferred; the newer of the two is used
    in case only ``last_updated`` is set.
    """
    stamps = [t for t in (last_reported, last_updated) if isinstance(t, datetime)]
    return max(stamps) if stamps else None


@dataclass(frozen=True)
class Reading:
    """A measured value and when it was last updated (timezone aware)."""

    value: float
    ts: datetime

    def age(self, now: datetime) -> timedelta:
        return now - self.ts


def is_stale(
    reading: Optional[Reading],
    now: datetime,
    timeout_min: Optional[float] = DEFAULT_SENSOR_TIMEOUT_MIN,
) -> bool:
    """``sensorIsStale``: True when the reading is older than the timeout.

    ``timeout_min`` None means the 60 minute default and 0 disables the
    check, like ``config.home.sensor_timeout``.
    """
    if reading is None:
        return True
    minutes = DEFAULT_SENSOR_TIMEOUT_MIN
    if timeout_min is not None:
        value = to_float(timeout_min)
        if value == 0:
            return False
        if value is not None and value > 0:
            minutes = value
    if reading.ts is None:
        return True
    return now > reading.ts + timedelta(minutes=minutes)


def usable(
    reading: Optional[Reading],
    now: datetime,
    timeout_min: Optional[float] = DEFAULT_SENSOR_TIMEOUT_MIN,
) -> Optional[str]:
    """None when the reading can be used, otherwise why not."""
    if reading is None or is_missing(reading.value):
        return "missing"
    if is_stale(reading, now, timeout_min):
        return "stale"
    return None


@dataclass(frozen=True)
class PriceSlot:
    """One price interval [start, end). ``price`` in the source's unit."""

    start: datetime
    end: datetime
    price: float


@dataclass(frozen=True)
class ForecastPoint:
    """One forecast step. Wind speed in m/s, bearing in degrees."""

    time: datetime
    temperature: Optional[float] = None
    wind_speed: Optional[float] = None
    wind_bearing: Optional[float] = None
    wind_gust: Optional[float] = None
    condition: Optional[str] = None


@dataclass(frozen=True)
class Decision:
    """A controller's result: curve offset contribution and why."""

    offset: float
    reason: str
    inputs_ok: bool
    details: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def zero(cls, reason: str, inputs_ok: bool = False, **details: Any) -> "Decision":
        return cls(0.0, reason, inputs_ok, dict(details))


def js_round(value: float) -> int:
    """JavaScript ``Math.round``: half rounds towards +infinity.

    Python's ``round`` rounds half to even, which would turn -2.5 into -2
    but 2.5 into 2 as well; Math.round gives -2 and 3.
    """
    return int(math.floor(value + 0.5))


def to_fixed(value: float, digits: int = 2) -> float:
    """``Number(x.toFixed(digits))``: half rounds away from zero."""
    scale = 10 ** digits
    magnitude = math.floor(abs(value) * scale + 0.5) / scale
    return math.copysign(magnitude, value) if magnitude else 0.0
