"""Legionella guard: make sure the tank reaches a legionella temperature.

The F1255 has its own periodic hot water increase: 47050 Periodic HW on/off,
47051 the interval in days (1..90) and 47046 its stop temperature
(55..70 °C). This guard does not replace it; it watches for it and steps in
only when a cycle is overdue (periodic increase off in the pump, or a cycle
that did not reach the temperature). Verified by the user on the real pump:
a one-time increase (48132 = 4) heats the tank to the luxury stop
temperature 47047. Believed, not verified: holding the hot water period
47134 at 0 does not block the pump's own periodic increase.

**Detection.** ``last_done`` is the last time BT7 (40013, hot water top)
held at least ``temperature - 0.5 °C`` for ``hold_minutes`` (default 10),
whoever heated it: the pump's periodic increase, a luxury charge or this
guard. A missing or stale BT7 breaks the hold. On the first start,
``last_done_from_history`` finds the last such hold in the recorder's BT7
history; without one the guard counts from the start of that history (or
from now, without any history).

**Parameters.** The temperature is 47046 when it can be read, else the
option (default 60 °C). The interval is 47051 days when the pump's periodic
increase is on (47050 = 1), else the option (default 14 days).

**When.** With the pump's periodic increase on, the pump is expected to do
it: the guard is due one day after the pump's own run should have happened
(``last_done + interval + 1 day``) and has one more day to find a cheap
window. With it off (or unknown), the guard owns the cycle: it is due one
day before the interval ends and must have run by ``last_done + interval``.

**Window.** The cheapest ``window_hours`` (default 2 h) window that starts
at or after the due time and ends by the deadline, among the known price
slots (time-weighted average, 15-minute steps); on a price tie the night
(22:00-06:00) wins, then the earlier start. Without prices: the first
``start_hour`` (default 02:00) in that span, else at once. A deadline that
is too close or has passed: run now.

**Cycle** (hot water mode ``active`` only; in ``shadow`` the plan is shown
and nothing runs). The hot water controller leases 47047 (luxury stop
temperature) = the legionella temperature and 48132 = 4 (one-time
increase), and releases 47134 (hot water period) so the pump can charge.
It ends when BT7 has held the temperature for ``hold_minutes`` (done; the
gateway restores 47047 to its baseline and 48132 to 0), after
``max_hours`` (default 4 h; aborted, retried after 12 h), or at once when
BT7 goes missing or stale (aborted). It has priority over every other hot
water decision, Away included.

Pure Python, no Home Assistant imports, Python 3.9-compatible.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Optional, Sequence, Tuple

from .inputs import PriceSlot, to_float
from .vvplan import PriceLine

REG_PERIODIC_STOP = 47046  # stop temperature periodic HW, °C
REG_LUX_STOP = 47047  # stop temperature HW luxury, °C (leased during a cycle)
REG_PERIODIC = 47050  # periodic HW on/off
REG_PERIODIC_INTERVAL = 47051  # days

INPUT_REGISTERS = (REG_PERIODIC_STOP, REG_PERIODIC, REG_PERIODIC_INTERVAL)

TOLERANCE = 0.5  # °C under the temperature that still counts
MARGIN = timedelta(days=1)
STEP = timedelta(minutes=15)
RETRY_AFTER = timedelta(hours=12)
DONE_SHOWN = timedelta(hours=6)  # "done" state after a cycle that HA ran
LUX_RESTORE_GRACE = timedelta(minutes=15)  # 47047 may still read the cycle's value
NIGHT_HOURS = frozenset({22, 23, 0, 1, 2, 3, 4, 5})
TEMP_LIMITS = (45.0, 70.0)  # the gateway's lease limits for 47047
INTERVAL_LIMITS = (1, 90)

DEFAULT_TEMPERATURE = 60.0
DEFAULT_INTERVAL_DAYS = 14
DEFAULT_HOLD_MINUTES = 10
DEFAULT_WINDOW_HOURS = 2.0
DEFAULT_MAX_HOURS = 4.0
DEFAULT_START_HOUR = 2

STATES = ("off", "disabled", "idle", "planned", "running", "done", "aborted")

RESULT_DONE = "done"
RESULT_TIMEOUT = "aborted: timeout"
RESULT_BT7 = "aborted: BT7 missing or stale"
RESULT_CANCELLED = "cancelled"


def _clamp(value: Any, default: float, low: float, high: float) -> float:
    number = to_float(value)
    if number is None:
        return default
    return max(low, min(high, number))


@dataclass(frozen=True)
class LegionellaSettings:
    enabled: bool = True
    temperature: float = DEFAULT_TEMPERATURE
    interval_days: float = DEFAULT_INTERVAL_DAYS
    hold_minutes: float = DEFAULT_HOLD_MINUTES
    window_hours: float = DEFAULT_WINDOW_HOURS
    max_hours: float = DEFAULT_MAX_HOURS
    start_hour: int = DEFAULT_START_HOUR

    @classmethod
    def from_values(
        cls,
        enabled: Any = True,
        temperature: Any = None,
        interval_days: Any = None,
        hold_minutes: Any = None,
        window_hours: Any = None,
        max_hours: Any = None,
        start_hour: Any = None,
    ) -> "LegionellaSettings":
        window = _clamp(window_hours, DEFAULT_WINDOW_HOURS, 0.5, 12.0)
        return cls(
            enabled=bool(enabled) if enabled is not None else True,
            temperature=_clamp(temperature, DEFAULT_TEMPERATURE, *TEMP_LIMITS),
            interval_days=_clamp(interval_days, DEFAULT_INTERVAL_DAYS, *INTERVAL_LIMITS),
            hold_minutes=_clamp(hold_minutes, DEFAULT_HOLD_MINUTES, 1.0, 120.0),
            window_hours=window,
            max_hours=max(window, _clamp(max_hours, DEFAULT_MAX_HOURS, 1.0, 12.0)),
            start_hour=int(_clamp(start_hour, DEFAULT_START_HOUR, 0, 23)),
        )


def _iso(when: Optional[datetime]) -> Optional[str]:
    return when.isoformat() if when is not None else None


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


@dataclass
class LegionellaState:
    """What is persisted (in the hot water store, key ``legionella``)."""

    last_done: Optional[datetime] = None
    last_done_by: Optional[str] = None  # "ha", "pump" (seen on BT7) or "history"
    # Counted from here while last_done is unknown.
    since: Optional[datetime] = None
    history_checked: bool = False
    running_since: Optional[datetime] = None
    run_temperature: Optional[float] = None
    lux_before: Optional[float] = None  # 47047 before the cycle took it
    ended_at: Optional[datetime] = None
    last_result: Optional[str] = None
    retry_after: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_done": _iso(self.last_done),
            "last_done_by": self.last_done_by,
            "since": _iso(self.since),
            "history_checked": self.history_checked,
            "running_since": _iso(self.running_since),
            "run_temperature": self.run_temperature,
            "lux_before": self.lux_before,
            "ended_at": _iso(self.ended_at),
            "last_result": self.last_result,
            "retry_after": _iso(self.retry_after),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "LegionellaState":
        """Tolerant: anything malformed is left unknown."""
        if not isinstance(data, dict):
            return cls()
        by = data.get("last_done_by")
        result = data.get("last_result")
        return cls(
            last_done=_parse(data.get("last_done")),
            last_done_by=by if isinstance(by, str) else None,
            since=_parse(data.get("since")),
            history_checked=data.get("history_checked") is True,
            running_since=_parse(data.get("running_since")),
            run_temperature=to_float(data.get("run_temperature")),
            lux_before=to_float(data.get("lux_before")),
            ended_at=_parse(data.get("ended_at")),
            last_result=result if isinstance(result, str) else None,
            retry_after=_parse(data.get("retry_after")),
        )


@dataclass(frozen=True)
class Parameters:
    temperature: float
    temperature_source: str  # "pump" (47046) or "option"
    interval: timedelta
    interval_source: str  # "pump" (47051) or "option"
    pump_periodic: bool  # 47050 = 1: the pump runs its own periodic increase


def parameters(
    settings: LegionellaSettings,
    periodic: Optional[float],
    interval_days: Optional[float],
    stop: Optional[float],
) -> Parameters:
    """Temperature and interval from the pump when it can be read."""
    if stop is not None and TEMP_LIMITS[0] <= stop <= TEMP_LIMITS[1]:
        temperature, temp_source = float(stop), "pump"
    else:
        temperature, temp_source = settings.temperature, "option"
    pump_periodic = periodic is not None and int(periodic) == 1
    if pump_periodic and interval_days is not None and INTERVAL_LIMITS[0] <= interval_days <= INTERVAL_LIMITS[1]:
        days, interval_source = float(interval_days), "pump"
    else:
        days, interval_source = float(settings.interval_days), "option"
    return Parameters(temperature, temp_source, timedelta(days=days), interval_source, pump_periodic)


def due_times(reference: datetime, params: Parameters) -> Tuple[datetime, datetime]:
    """(due, deadline) counted from ``reference`` (the last cycle)."""
    if params.pump_periodic:
        # The pump does it itself; step in one day after it should have.
        due = reference + params.interval + MARGIN
        return due, due + MARGIN
    deadline = reference + params.interval
    return deadline - MARGIN, deadline


def plan_window(
    now: datetime,
    first: datetime,
    deadline: datetime,
    length: timedelta,
    prices: Sequence[PriceSlot],
    tz: tzinfo,
    start_hour: int = DEFAULT_START_HOUR,
) -> Tuple[datetime, str]:
    """(start, how) of the window: "price", "default hour", "now" or "deadline"."""
    first = max(first, now)
    last = deadline - length
    if last <= now:
        return now, "deadline"
    if first > last:
        return last, "deadline"
    # Candidates on the quarter hour (the first one may be off it).
    starts = [first]
    step_s = STEP.total_seconds()
    t = datetime.fromtimestamp(math.ceil(first.timestamp() / step_s) * step_s, timezone.utc)
    if t <= first:
        t += STEP
    while t <= last:
        starts.append(t)
        t += STEP
    line = PriceLine(prices)
    best: Optional[Tuple[float, int, datetime]] = None
    if line:
        for start in starts:
            price = line.average(start, start + length)
            if price is None:
                continue
            night = 0 if start.astimezone(tz).hour in NIGHT_HOURS else 1
            key = (round(price, 6), night, start)
            if best is None or key < best:
                best = key
    if best is not None:
        return best[2], "price"
    for start in starts:
        local = start.astimezone(tz)
        if local.hour == start_hour and local.minute == 0:
            return start, "default hour"
    return first, "now"


def last_done_from_history(
    points: Sequence[Tuple[float, Optional[float]]],
    threshold: float,
    hold_s: float,
    end: float,
) -> Optional[float]:
    """When the last hold of ``threshold`` for ``hold_s`` completed.

    ``points`` are (epoch, value) state changes; a value holds until the
    next point (or ``end``), None breaks a hold. Returns the epoch at which
    the latest qualifying hold reached ``hold_s``, or None.
    """
    ordered = sorted(points, key=lambda p: p[0])
    found: Optional[float] = None
    run_start: Optional[float] = None
    for index, (ts, value) in enumerate(ordered):
        nxt = ordered[index + 1][0] if index + 1 < len(ordered) else end
        if value is not None and value >= threshold:
            if run_start is None:
                run_start = ts
            if nxt - run_start >= hold_s:
                found = run_start + hold_s
        else:
            run_start = None
    return found


@dataclass
class LegionellaStatus:
    """The guard's view for one tick (entities, cards, the controller)."""

    state: str
    run: bool = False
    temperature: Optional[float] = None
    reason: str = ""
    info: Dict[str, Any] = field(default_factory=dict)


class LegionellaGuard:
    """Detects cycles, plans a window and runs one when due."""

    def __init__(self, state: Optional[LegionellaState] = None) -> None:
        self.state = state or LegionellaState()
        self.hold_since: Optional[datetime] = None
        self.requested = False
        self.dirty = False  # persisted state changed: save
        self.status: Optional[LegionellaStatus] = None
        # Set by step for the cycle that just ended (for a repair issue).
        self.ended: Optional[str] = None

    # -- detection --------------------------------------------------------

    def observe(self, now: datetime, bt7: Optional[float], temperature: float, hold_minutes: float) -> bool:
        """Track the BT7 hold; True when it completed a cycle now."""
        if bt7 is None or bt7 < temperature - TOLERANCE:
            self.hold_since = None
            return False
        if self.hold_since is None:
            self.hold_since = now
        if now - self.hold_since < timedelta(minutes=hold_minutes):
            return False
        last = self.state.last_done
        if last is not None and last >= self.hold_since:
            return False  # this hold was already counted
        self.state.last_done = now
        self.state.last_done_by = "ha" if self.state.running_since is not None else "pump"
        self.dirty = True
        return True

    def apply_history(self, done_at: Optional[datetime], covered_from: Optional[datetime], now: datetime) -> None:
        """The first start's recorder search (done once)."""
        state = self.state
        if state.history_checked:
            return
        state.history_checked = True
        if done_at is not None and (state.last_done is None or done_at > state.last_done):
            state.last_done = done_at
            state.last_done_by = "history"
        elif state.last_done is None:
            state.since = min(covered_from or now, state.since or now)
        self.dirty = True

    def reference(self, now: datetime) -> datetime:
        """The last cycle, or when counting started."""
        if self.state.last_done is not None:
            return self.state.last_done
        if self.state.since is None:
            self.state.since = now
            self.dirty = True
        return self.state.since

    def lux_stop(self, now: datetime, reading: Optional[float], held: bool) -> Optional[float]:
        """The luxury stop temperature to plan with: while (or just after)
        a cycle holds 47047, the value from before the cycle."""
        state = self.state
        if state.lux_before is None:
            return reading
        if held or state.running_since is not None:
            return state.lux_before
        if state.ended_at is not None and now - state.ended_at < LUX_RESTORE_GRACE:
            return state.lux_before
        return reading

    # -- one tick ---------------------------------------------------------

    def _end(self, now: datetime, result: str) -> None:
        state = self.state
        state.running_since = None
        state.run_temperature = None
        state.ended_at = now
        state.last_result = result
        state.retry_after = None if result in (RESULT_DONE, RESULT_CANCELLED) else now + RETRY_AFTER
        self.ended = result
        self.dirty = True

    def step(
        self,
        now: datetime,
        tz: tzinfo,
        mode: str,
        settings: LegionellaSettings,
        bt7: Optional[float],
        params: Parameters,
        prices: Sequence[PriceSlot] = (),
        lux_reading: Optional[float] = None,
    ) -> LegionellaStatus:
        """``mode``: the hot water mode ("off", "shadow", "active")."""
        state = self.state
        self.ended = None
        reference = self.reference(now)
        due, deadline = due_times(reference, params)
        info: Dict[str, Any] = {
            "temperature": params.temperature,
            "temperature_source": params.temperature_source,
            "interval_days": round(params.interval.total_seconds() / 86400.0, 2),
            "interval_source": params.interval_source,
            "pump_periodic": params.pump_periodic,
            "hold_minutes": settings.hold_minutes,
            "last_done": state.last_done,
            "last_done_by": state.last_done_by,
            "counting_since": state.since if state.last_done is None else None,
            "due": due,
            "deadline": deadline,
            "planned_start": None,
            "planned_end": None,
            "placed_by": None,
            "running_since": state.running_since,
            "hold_since": self.hold_since,
            "last_result": state.last_result,
            "last_result_at": state.ended_at,
            "retry_after": state.retry_after,
        }

        def status(name: str, reason: str, run: bool = False, temp: Optional[float] = None) -> LegionellaStatus:
            info["running_since"] = state.running_since
            info["last_result"] = state.last_result
            info["last_result_at"] = state.ended_at
            info["retry_after"] = state.retry_after
            info["last_done"] = state.last_done
            info["last_done_by"] = state.last_done_by
            self.status = LegionellaStatus(name, run, temp, reason, info)
            return self.status

        active = mode == "active"
        if state.running_since is not None:
            if not active or not settings.enabled:
                self._end(now, RESULT_CANCELLED)
                self.requested = False
            elif state.last_done is not None and state.last_done >= state.running_since:
                temp = state.run_temperature or params.temperature
                self._end(now, RESULT_DONE)
                return status("done", "legionella cycle done: BT7 held %.1f °C" % temp)
            elif bt7 is None:
                self._end(now, RESULT_BT7)
                return status("aborted", "BT7 missing or stale: legionella cycle aborted")
            elif now - state.running_since >= timedelta(hours=settings.max_hours):
                self._end(now, RESULT_TIMEOUT)
                return status(
                    "aborted",
                    "BT7 did not hold %.1f °C within %g h: legionella cycle aborted"
                    % (state.run_temperature or params.temperature, settings.max_hours),
                )
            else:
                temp = state.run_temperature or params.temperature
                held = (
                    " (held %d min)" % ((now - self.hold_since).total_seconds() // 60)
                    if self.hold_since is not None
                    else ""
                )
                info["planned_end"] = state.running_since + timedelta(hours=settings.max_hours)
                return status(
                    "running",
                    "legionella cycle: heat to %.1f °C, BT7 %s%s" % (temp, "%.1f" % bt7, held),
                    True,
                    temp,
                )

        if mode == "off":
            self.requested = False
            return status("off", "hot water mode off")
        if not settings.enabled:
            self.requested = False
            return status("disabled", "legionella guard off")

        if self.requested and active:
            return self._start(now, params, lux_reading, info, status, "run now (requested)")
        self.requested = False

        first = due
        if state.retry_after is not None and state.retry_after > first:
            first = state.retry_after
        if now < due:
            name = "idle"
            if state.ended_at is not None and now - state.ended_at < DONE_SHOWN and state.last_result:
                name = "done" if state.last_result == RESULT_DONE else (
                    "aborted" if state.last_result.startswith("aborted") else "idle"
                )
            who = "the pump's periodic increase" if params.pump_periodic else "the legionella guard"
            return status(name, "next due %s (%s)" % (_hm(due, tz), who))

        length = timedelta(hours=settings.window_hours)
        start, how = plan_window(now, first, max(deadline, first + length), length, prices, tz, settings.start_hour)
        info["planned_start"] = start
        info["planned_end"] = start + length
        info["placed_by"] = how
        if state.retry_after is not None and now < state.retry_after:
            return status(
                "aborted",
                "%s; retry from %s" % (state.last_result or "aborted", _hm(state.retry_after, tz)),
            )
        if now < start:
            return status("planned", "due: planned %s-%s (%s)" % (_hm(start, tz), _hm(start + length, tz), how))
        if not active:
            return status("planned", "due: would run now (hot water mode %s)" % mode)
        return self._start(now, params, lux_reading, info, status, "due (%s)" % how)

    def _start(self, now, params, lux_reading, info, status, why) -> LegionellaStatus:
        state = self.state
        self.requested = False
        state.running_since = now
        state.run_temperature = params.temperature
        if lux_reading is not None and lux_reading != params.temperature:
            state.lux_before = lux_reading
        state.retry_after = None
        self.dirty = True
        info["planned_start"] = info["planned_start"] or now
        return status(
            "running", "legionella cycle started, %s: heat to %.1f °C" % (why, params.temperature), True,
            params.temperature,
        )


def _hm(when: datetime, tz: tzinfo) -> str:
    return when.astimezone(tz).strftime("%Y-%m-%d %H:%M")
