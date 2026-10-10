"""Planning horizon: price slots joined with outdoor forecast and solar proxy.

- Starts at ``now`` (the current slot is cut to its remaining part) and
  runs over the known prices until the first gap or ``max_hours``.
- Keeps the source's slot length (15 min or 1 h, whatever ``price.py``
  produced) unless ``step_minutes`` asks for coarser steps, which average
  the price by time. Steps are aligned on UTC multiples, so DST days just
  have 92 or 100 quarter-hours and nothing else changes.
- Outdoor temperature: the forecast interpolated at each slot's middle,
  shifted by (measured now - forecast now) with the shift fading out over
  ``bias_hours``; beyond the forecast the last value holds; without a
  forecast the current measurement holds for the whole horizon.
- Solar proxy: from latitude/longitude when given, with the cloud cover of
  the nearest forecast condition (within 3 h).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence

from ..inputs import ForecastPoint, PriceSlot
from .solar import DEFAULT_CLOUD, cloud_from_condition, solar_proxy

DEFAULT_MAX_HOURS = 36.0
DEFAULT_BIAS_HOURS = 12.0
DEFAULT_OUTDOOR = 0.0
_CONDITION_REACH = 3 * 3600.0


@dataclass(frozen=True)
class HorizonSlot:
    start: datetime
    end: datetime
    price: float
    t_out: float
    solar: float = 0.0
    t_brine: Optional[float] = None

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


def _usable_prices(prices: Sequence[PriceSlot], now: datetime, max_hours: float) -> List[PriceSlot]:
    limit = now + timedelta(hours=max_hours)
    out: List[PriceSlot] = []
    for slot in sorted(prices, key=lambda s: s.start.timestamp()):
        if slot.end <= now or slot.start >= limit:
            continue
        if out and slot.start > out[-1].end + timedelta(seconds=1):
            break  # gap in the price data
        if out and slot.start < out[-1].end:
            continue  # overlap / duplicate
        start = max(slot.start, now) if not out else slot.start
        end = min(slot.end, limit)
        if (end - start).total_seconds() < 60:
            continue
        out.append(PriceSlot(start, end, slot.price))
    if out and out[0].start > now + timedelta(seconds=1):
        return []  # nothing covers now
    return out


def _resample(slots: List[PriceSlot], step_minutes: int) -> List[PriceSlot]:
    step = step_minutes * 60
    out: List[PriceSlot] = []
    bucket_key = None
    acc = 0.0
    dur = 0.0
    b_start = None
    b_end = None
    for slot in slots:
        t0, t1 = slot.start.timestamp(), slot.end.timestamp()
        while t0 < t1:
            key = int(t0 // step) * step
            seg_end = min(t1, key + step)
            if key != bucket_key:
                if bucket_key is not None and dur > 0:
                    out.append(PriceSlot(b_start, b_end, acc / dur))
                bucket_key, acc, dur = key, 0.0, 0.0
                b_start = datetime.fromtimestamp(t0, timezone.utc)
            acc += slot.price * (seg_end - t0)
            dur += seg_end - t0
            b_end = datetime.fromtimestamp(seg_end, timezone.utc)
            t0 = seg_end
    if bucket_key is not None and dur > 0:
        out.append(PriceSlot(b_start, b_end, acc / dur))
    return out


def _interp(points: Sequence[ForecastPoint], ts: float) -> Optional[float]:
    if not points:
        return None
    if ts <= points[0].time.timestamp():
        return float(points[0].temperature)  # type: ignore[arg-type]
    for p, q in zip(points, points[1:]):
        t0, t1 = p.time.timestamp(), q.time.timestamp()
        if t0 <= ts <= t1:
            f = (ts - t0) / (t1 - t0) if t1 > t0 else 0.0
            return float(p.temperature) + f * (float(q.temperature) - float(p.temperature))  # type: ignore[arg-type]
    return float(points[-1].temperature)  # type: ignore[arg-type]


def _nearest_condition(points: Sequence[ForecastPoint], ts: float) -> Optional[str]:
    best = None
    gap = None
    for p in points:
        g = abs(p.time.timestamp() - ts)
        if p.condition and (gap is None or g < gap):
            best, gap = p.condition, g
    if gap is None or gap > _CONDITION_REACH:
        return None
    return best


def build_horizon(
    prices: Sequence[PriceSlot],
    now: datetime,
    t_out_now: Optional[float],
    forecast: Optional[Sequence[ForecastPoint]] = None,
    max_hours: float = DEFAULT_MAX_HOURS,
    step_minutes: Optional[int] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    bias_hours: float = DEFAULT_BIAS_HOURS,
    t_brine: Optional[float] = None,
) -> List[HorizonSlot]:
    slots = _usable_prices(prices, now, max_hours)
    if step_minutes:
        slots = _resample(slots, step_minutes)
    points = sorted(
        (p for p in (forecast or []) if p.temperature is not None),
        key=lambda p: p.time.timestamp(),
    )
    now_ts = now.timestamp()
    forecast_now = _interp(points, now_ts)
    if t_out_now is None:
        t_out_now = forecast_now if forecast_now is not None else DEFAULT_OUTDOOR
    bias = (t_out_now - forecast_now) if forecast_now is not None else 0.0
    out: List[HorizonSlot] = []
    for slot in slots:
        mid = 0.5 * (slot.start.timestamp() + slot.end.timestamp())
        value = _interp(points, mid)
        if value is None:
            t_out = t_out_now
        else:
            ahead_h = (mid - now_ts) / 3600.0
            fade = max(0.0, 1.0 - ahead_h / bias_hours) if bias_hours > 0 else 0.0
            t_out = value + bias * fade
        solar = 0.0
        if latitude is not None and longitude is not None:
            condition = _nearest_condition(points, mid)
            cloud = cloud_from_condition(condition) if condition else DEFAULT_CLOUD
            solar = solar_proxy(datetime.fromtimestamp(mid, timezone.utc), latitude, longitude, cloud)
        out.append(HorizonSlot(slot.start, slot.end, slot.price, t_out, solar, t_brine))
    return out
