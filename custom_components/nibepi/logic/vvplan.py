"""VV-AI plan and control: when to heat hot water, to what temperature.

Ports from config_node.js:

- ``vvAiBuildPlan``: a "day" runs 02:00-02:00 local (a draw just after
  midnight belongs to the evening before). The day's profile sum picks its
  mode: below ``day_small`` (1.0) no heating, below ``day_medium`` (3.0)
  economy (1), below ``day_large`` (6.0) normal (2), else luxury (3). The
  day's largest slot is its peak; the largest other slot of at least
  ``peak_threshold`` (3.0) and 6 h or more away is a second peak, which
  caps luxury at normal. Before each peak a heating window of
  ``max(1, preheat)`` hours (2) ends at the peak. With price control
  (``vv_ai_use_price_enable``) the window moves to the cheapest such window
  starting within ``max(window, price_window)`` hours (7) before the peak,
  ties going to the later one. Inside the window, the cheapest two
  consecutive hours (from at most ``max(2, price_window)`` h before the
  peak) are recorded. The target is the pump's stop temperature for the
  day's mode (47049 economy, 47048 normal, 47047 luxury).
- ``vvAiPlanHeatAllowed``: with price control on, plan heating is allowed
  in a window's two cheapest hours, or in its last two hours before the
  peak once the cheap hours have passed, or anywhere in a window without
  cheap hours (no prices).
- The control part of ``vvAiTick`` (``VvControl``): the min-temp floor
  first (BT7 under ``min_temp`` -> heat to ``min_temp + 4``, at most the
  luxury stop temperature, with hysteresis until reached), then the plan:
  inside a window and allowed, heat while BT7 is under the target; once BT7
  reaches it, the rest of the window (and windows joined to it) is closed.

Deliberate differences:

- Absolute time instead of a 168-slot ring: peaks are local wall-clock
  hours turned into aware instants, windows are real durations. Node-RED
  mapped a 48 h price list onto hour-of-week indexes, where each 15-minute
  price overwrote the hour with its last quarter and DST shifted hours.
- Windows are placed in 15-minute steps on the time-weighted price of the
  real slots (hourly or 15-minute). A window without full price coverage
  is not a candidate, as Node-RED skipped hours without a price.
- Where windows of two days overlap, the higher mode wins (Node-RED: the
  later write to the ring).
- A window whose stop temperature is unknown has no target and does not
  heat (Node-RED fell back to the min-temp floor value there).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, List, Optional, Sequence, Tuple

from .inputs import PriceSlot, to_float
from .vvlearn import SLOTS

UTC = timezone.utc
STEP = timedelta(minutes=15)
MIN_PEAK_GAP_H = 6
FLOOR_MARGIN = 4.0  # °C over min_temp the floor heats to
CHEAP_HOURS = 2
RUN_LIMIT = timedelta(hours=24)

MODE_NAMES = {1: "economy", 2: "normal", 3: "luxury"}


@dataclass(frozen=True)
class PlanSettings:
    day_small: float = 1.0
    day_medium: float = 3.0
    day_large: float = 6.0
    peak_threshold: float = 3.0
    preheat_hours: int = 2
    use_price: bool = False
    price_window_hours: float = 7.0

    @classmethod
    def from_values(
        cls,
        day_small: Any = None,
        day_medium: Any = None,
        day_large: Any = None,
        peak_threshold: Any = None,
        preheat_hours: Any = None,
        use_price: Any = False,
        price_window_hours: Any = None,
    ) -> "PlanSettings":
        """Node-RED's validation: invalid values fall back to the defaults."""
        small = to_float(day_small)
        if small is None or small <= 0:
            small = 1.0
        medium = to_float(day_medium)
        if medium is None or medium <= small:
            medium = 3.0
        large = to_float(day_large)
        if large is None or large <= medium:
            large = 6.0
        peak = to_float(peak_threshold)
        if peak is None or peak <= 0:
            peak = 3.0
        preheat = to_float(preheat_hours)
        if preheat is None:
            preheat = 2.0
        elif preheat < 0:
            preheat = 0.0
        preheat = min(12.0, preheat)
        window = to_float(price_window_hours)
        if window is None or window <= 0:
            window = 7.0
        window = min(24.0, window)
        return cls(small, medium, large, peak, int(preheat), bool(use_price), window)

    @property
    def window_hours(self) -> int:
        return max(1, int(self.preheat_hours))


@dataclass(frozen=True)
class StopTemps:
    """The pump's hot water stop temperatures (47049, 47048, 47047)."""

    economy: Optional[float] = None
    normal: Optional[float] = None
    luxury: Optional[float] = None

    def for_mode(self, day_mode: int) -> Optional[float]:
        return {1: self.economy, 2: self.normal, 3: self.luxury}.get(day_mode)

    def complete(self) -> bool:
        return None not in (self.economy, self.normal, self.luxury)


@dataclass(frozen=True)
class HeatWindow:
    start: datetime  # UTC
    end: datetime
    peak: datetime  # start of the peak hour
    day: date  # the 02:00-02:00 day it belongs to
    day_mode: int  # 1 economy, 2 normal, 3 luxury
    target: Optional[float]  # BT7 stop temperature, None if unknown
    cheap_start: Optional[datetime] = None
    cheap_end: Optional[datetime] = None
    price_placed: bool = False

    def contains(self, when: datetime) -> bool:
        return self.start <= when < self.end

    def compact(self, tz: tzinfo) -> dict:
        def hm(when: Optional[datetime]) -> Optional[str]:
            return when.astimezone(tz).isoformat(timespec="minutes") if when is not None else None

        return {
            "start": hm(self.start),
            "end": hm(self.end),
            "peak": hm(self.peak),
            "mode": MODE_NAMES.get(self.day_mode),
            "target": self.target,
            "cheap_start": hm(self.cheap_start),
        }


# --------------------------------------------------------------------------
# Profile -> days and peaks
# --------------------------------------------------------------------------


def day_sums(profile: Sequence[float]) -> List[float]:
    """Sum per weekday (Monday 0) over its 02:00-02:00 day."""
    sums = [0.0] * 7
    for index in range(SLOTS):
        value = profile[index] if profile[index] > 0 else 0.0
        hour = index % 24
        day = index // 24
        if hour < 2:
            day = (day + 6) % 7
        sums[day] += value
    return sums


def day_mode(total: float, settings: PlanSettings) -> int:
    if total < settings.day_small:
        return 0
    if total < settings.day_medium:
        return 1
    if total < settings.day_large:
        return 2
    return 3


def day_peaks(profile: Sequence[float], weekday: int, settings: PlanSettings) -> Tuple[List[int], bool]:
    """Peak hours (offsets 2..25 from the day's midnight) and whether there
    are two. Empty when the day has no positive slot."""
    base = weekday * 24
    main = -1
    best = 0.0
    candidates: List[Tuple[int, float]] = []
    for hour in range(2, 26):
        value = profile[(base + hour) % SLOTS]
        if value > best:
            best = value
            main = hour
        if value >= settings.peak_threshold:
            candidates.append((hour, value))
    if main < 0:
        return [], False
    second = -1
    second_value = -1.0
    for hour, value in candidates:
        if hour == main or abs(hour - main) < MIN_PEAK_GAP_H:
            continue
        if value > second_value:
            second_value = value
            second = hour
    if second >= 0:
        return sorted([main, second]), True
    return [main], False


def wall_time(day: date, hour_offset: int, tz: tzinfo) -> datetime:
    """Local wall-clock ``day`` 00:00 + ``hour_offset`` hours, as UTC."""
    when = day + timedelta(days=hour_offset // 24)
    local = datetime(when.year, when.month, when.day, hour_offset % 24, tzinfo=tz)
    return local.astimezone(UTC)


# --------------------------------------------------------------------------
# Prices
# --------------------------------------------------------------------------


class PriceLine:
    """Sorted price slots with a time-weighted average over any interval."""

    def __init__(self, slots: Sequence[PriceSlot]) -> None:
        self.slots = sorted(slots, key=lambda s: s.start)
        self._ends = [s.end for s in self.slots]

    def __bool__(self) -> bool:
        return bool(self.slots)

    def average(self, start: datetime, end: datetime) -> Optional[float]:
        """None unless prices cover all of [start, end)."""
        need = (end - start).total_seconds()
        if need <= 0:
            return None
        total = 0.0
        covered = 0.0
        index = bisect.bisect_right(self._ends, start)
        while index < len(self.slots):
            slot = self.slots[index]
            if slot.start >= end:
                break
            low = max(start, slot.start)
            high = min(end, slot.end)
            seconds = (high - low).total_seconds()
            if seconds > 0:
                total += slot.price * seconds
                covered += seconds
            index += 1
        if covered < need - 1:
            return None
        return total / covered

    def cheapest(self, first: datetime, last: datetime, length: timedelta) -> Optional[datetime]:
        """Start of the cheapest window of ``length`` starting in
        [first, last] (15-minute steps); ties go to the later start."""
        best: Optional[datetime] = None
        best_price = math.inf
        start = first
        while start <= last:
            price = self.average(start, start + length)
            if price is not None and price <= best_price + 1e-9:
                best_price = min(price, best_price)
                best = start
            start += STEP
        return best


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


def build_plan(
    profile: Sequence[float],
    now: datetime,
    tz: tzinfo,
    settings: PlanSettings,
    stops: StopTemps,
    prices: Sequence[PriceSlot] = (),
    days_before: int = 1,
    days_after: int = 2,
) -> List[HeatWindow]:
    """Heating windows for the 02:00-02:00 days around ``now``, by start."""
    sums = day_sums(profile)
    line = PriceLine(prices)
    length = timedelta(hours=settings.window_hours)
    lookback = timedelta(hours=max(settings.window_hours, math.floor(settings.price_window_hours)))
    cheap_back = timedelta(hours=max(CHEAP_HOURS, math.floor(settings.price_window_hours)))
    cheap_len = timedelta(hours=CHEAP_HOURS)
    today = now.astimezone(tz).date()
    windows: List[HeatWindow] = []
    for offset in range(-days_before, days_after + 1):
        day = today + timedelta(days=offset)
        weekday = day.weekday()
        mode = day_mode(sums[weekday], settings)
        if mode == 0:
            continue
        peaks, two = day_peaks(profile, weekday, settings)
        if not peaks:
            continue
        if two and mode == 3:
            mode = 2
        for hour in peaks:
            peak = wall_time(day, hour, tz)
            start = peak - length
            placed = False
            if settings.use_price and line:
                best = line.cheapest(peak - lookback, peak - length, length)
                if best is not None:
                    start = best
                    placed = True
            end = start + length
            cheap_start = None
            if line:
                cheap_start = line.cheapest(max(start, peak - cheap_back), end - cheap_len, cheap_len)
            windows.append(
                HeatWindow(
                    start=start,
                    end=end,
                    peak=peak,
                    day=day,
                    day_mode=mode,
                    target=stops.for_mode(mode),
                    cheap_start=cheap_start,
                    cheap_end=cheap_start + cheap_len if cheap_start is not None else None,
                    price_placed=placed,
                )
            )
    windows.sort(key=lambda w: (w.start, w.peak))
    return windows


def windows_at(windows: Sequence[HeatWindow], now: datetime) -> List[HeatWindow]:
    return [w for w in windows if w.contains(now)]


def window_at(windows: Sequence[HeatWindow], now: datetime) -> Optional[HeatWindow]:
    """The window in force at ``now``: the highest mode, then target."""
    found = windows_at(windows, now)
    if not found:
        return None
    return max(found, key=lambda w: (w.day_mode, w.target if w.target is not None else -math.inf))


def heat_allowed(windows: Sequence[HeatWindow], now: datetime) -> bool:
    """``vvAiPlanHeatAllowed`` (only consulted with price control on)."""
    if not windows:
        return True
    allowed = False
    for window in windows_at(windows, now):
        if window.cheap_start is None or window.cheap_end is None:
            return True
        if window.cheap_start <= now < window.cheap_end:
            allowed = True
        elif now >= window.cheap_end and window.peak - now <= timedelta(hours=CHEAP_HOURS):
            allowed = True
    return allowed


def run_end(windows: Sequence[HeatWindow], now: datetime) -> datetime:
    """End of the run of windows (joined where they touch) around ``now``."""
    found = windows_at(windows, now)
    if not found:
        return now
    end = max(w.end for w in found)
    changed = True
    while changed:
        changed = False
        for window in windows:
            if window.start <= end < window.end:
                end = window.end
                changed = True
    return min(end, now + RUN_LIMIT)


def next_heat_start(
    windows: Sequence[HeatWindow], now: datetime, closed_until: Optional[datetime], use_price: bool
) -> Optional[datetime]:
    """When the plan next heats (or started heating, while in a window)."""
    for window in windows:
        if window.target is None or window.end <= now:
            continue
        if closed_until is not None and window.end <= closed_until:
            continue
        if use_price and window.cheap_start is not None:
            return window.cheap_start
        return window.start
    return None


# --------------------------------------------------------------------------
# Control
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlResult:
    heat: bool
    target: Optional[float]
    source: Optional[str]  # "floor", "plan" or None
    reason: str


def _hm(when: datetime, tz: tzinfo) -> str:
    return when.astimezone(tz).strftime("%H:%M")


class VvControl:
    """Min-temp floor and plan heating (state: floor hysteresis, closed run)."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.floor_active = False
        self.floor_target: Optional[float] = None
        self.closed_until: Optional[datetime] = None

    def decide(
        self,
        bt7: float,
        now: datetime,
        tz: tzinfo,
        windows: Sequence[HeatWindow],
        stops: StopTemps,
        floor_enabled: bool,
        floor_temp: Optional[float],
        use_price: bool,
    ) -> ControlResult:
        # 1) Min-temp floor.
        if floor_enabled and floor_temp is not None:
            if not self.floor_active and bt7 < floor_temp:
                target = floor_temp + FLOOR_MARGIN
                if stops.luxury is not None and target > stops.luxury:
                    target = stops.luxury
                self.floor_active = True
                self.floor_target = target
            if self.floor_active and self.floor_target is not None:
                if bt7 < self.floor_target:
                    return ControlResult(
                        True,
                        self.floor_target,
                        "floor",
                        "BT7 %.1f under the floor: heat to %.1f" % (bt7, self.floor_target),
                    )
                self.floor_active = False
                self.floor_target = None
        else:
            self.floor_active = False
            self.floor_target = None
        # 2) The plan.
        window = window_at(windows, now)
        if window is None:
            following = next((w for w in windows if w.start > now), None)
            if following is None:
                return ControlResult(False, None, None, "no heating window planned")
            return ControlResult(
                False, None, None, "no window now, next %s" % _hm(following.start, tz)
            )
        if window.target is None:
            return ControlResult(False, None, None, "stop temperature for the window unknown")
        span = "window %s-%s for %s (%s, %.1f °C)" % (
            _hm(window.start, tz),
            _hm(window.end, tz),
            _hm(window.peak, tz),
            MODE_NAMES.get(window.day_mode),
            window.target,
        )
        if use_price and not heat_allowed(windows, now):
            cheap = (
                " (cheapest %s-%s)" % (_hm(window.cheap_start, tz), _hm(window.cheap_end, tz))
                if window.cheap_start is not None and window.cheap_end is not None
                else ""
            )
            return ControlResult(False, window.target, "plan", "%s: waiting for the cheap hours%s" % (span, cheap))
        if self.closed_until is not None and now < self.closed_until:
            return ControlResult(
                False, window.target, "plan", "%s: done, closed until %s" % (span, _hm(self.closed_until, tz))
            )
        if bt7 < window.target:
            return ControlResult(True, window.target, "plan", "%s: BT7 %.1f, heat" % (span, bt7))
        self.closed_until = run_end(windows, now)
        return ControlResult(
            False, window.target, "plan", "%s: BT7 %.1f reached, closed until %s" % (span, bt7, _hm(self.closed_until, tz))
        )
