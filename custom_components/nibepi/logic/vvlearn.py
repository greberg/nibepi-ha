"""VV learning: a weekly hot water draw profile learned from BT6.

Ports from config_node.js (VV-AI):

- The profile: 168 hour slots of local wall-clock time, Monday 00:00 = 0
  (``getWeekHourIndex``). Each value is an exponential moving average of
  the BT6 drop drawn in that hour (``registerVvDraw``): ``old * (1 - a) +
  drop * a`` with ``a = rate / 100`` (``getVvAlpha``, rate 1..10, default
  10), rounded to 3 decimals. Drops of 0.1 °C or less are ignored.
- Daily decay (``vvAiApplyDailyDecayIfNeeded``): when the local date
  changes, the 24 slots of the weekday that ended are multiplied by
  ``1 - a`` unless a draw was learned in them that day ("seen"); every
  calendar day skipped in between (system down) is decayed as unseen, at
  most 32.
- Draw detection (the BT6 part of ``vvAiTick`` and ``vvCloseDrawEvent``),
  one step per minute, with Node-RED's constants (see below). The drop is
  booked on the hour in which most degrees were drawn
  (``vvSteepestHourTs``).

Deliberate differences:

- Slots are computed from aware datetimes in the Home Assistant time zone,
  so DST days map to the right wall-clock hour (02:00-03:00 occurs twice
  in autumn: both go to slot 2; in spring it never occurs).
- ``register_draw`` decays by the tick's own date. Node-RED used the date
  of the booked hour, so a draw booked before midnight but closed after it
  could decay a day twice and move the "seen" day backwards.
- A decay date earlier than the stored one (clock set back) does nothing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .inputs import to_fixed, to_float

SLOTS = 168
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

DEFAULT_RATE = 10
DEFAULT_MIN_DROP = 3.0  # °C
MAX_SKIPPED_DAYS = 32

# Draw detection (config_node.js VV_DRAW_* / VV_CHARGE_SETTLE_MS).
DRAW_START_DELTA = 1.0  # °C under the running peak opens an event
DRAW_IDLE_CLOSE = timedelta(minutes=15)  # no new low this long: closed
DRAW_RECOVER_DELTA = 0.5  # °C over the trough: recharging, closed
DRAW_MIN_RATE = 1.2  # °C/h; slower is standby/circulation loss
DRAW_REBASE_RATE = 0.9  # °C/h
DRAW_REBASE = timedelta(minutes=30)
CHARGE_SETTLE = timedelta(minutes=10)
DRAW_TRAIL = timedelta(minutes=60)
DRAW_MAX = timedelta(minutes=360)

PRIO_HOT_WATER = 20  # 43086: 10 off, 20 hot water, 30 heat, ...


def learning_alpha(rate: Any) -> float:
    """``getVvAlpha``: rate 1..10 (invalid or <= 0: 10) -> rate / 100."""
    value = to_float(rate)
    if value is None or value <= 0:
        value = DEFAULT_RATE
    value = max(1.0, min(10.0, value))
    return value / 100.0


def min_drop(value: Any) -> float:
    """``getVvMinDrop``: invalid or <= 0 means 3 °C."""
    number = to_float(value)
    if number is None or number <= 0:
        return DEFAULT_MIN_DROP
    return number


def week_slot(when: datetime, tz: tzinfo) -> int:
    """Hour of the week in local wall-clock time, Monday 00:00 = 0."""
    local = when.astimezone(tz)
    return local.weekday() * 24 + local.hour


def slot_name(slot: int) -> str:
    return "%s %02d:00" % (DAYS[(slot // 24) % 7], slot % 24)


def is_charging(prio: Any) -> bool:
    """43086 says the pump is charging hot water (20, or a text saying so)."""
    number = to_float(prio)
    if number is not None:
        return int(number) == PRIO_HOT_WATER
    if isinstance(prio, str):
        text = prio.lower()
        return "varmvatten" in text or "hot water" in text
    return False


def _parse_day(text: Any) -> Optional[date]:
    if not isinstance(text, str):
        return None
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        return None


@dataclass
class VvProfile:
    """The learned profile and its decay bookkeeping (what is persisted)."""

    profile: List[float] = field(default_factory=lambda: [0.0] * SLOTS)
    seen_day: Optional[str] = None  # local date whose draws ``seen`` holds
    seen: List[bool] = field(default_factory=lambda: [False] * SLOTS)
    last_decay_day: Optional[str] = None
    last_learn: Optional[str] = None  # ISO timestamp of the booked hour
    last_drop: Optional[float] = None
    last_slot: Optional[int] = None
    learned: int = 0  # draws learned since this store was created
    imported_from: Optional[str] = None

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": list(self.profile),
            "seen_day": self.seen_day,
            "seen": list(self.seen),
            "last_decay_day": self.last_decay_day,
            "last_learn": self.last_learn,
            "last_drop": self.last_drop,
            "last_slot": self.last_slot,
            "learned": self.learned,
            "imported_from": self.imported_from,
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "VvProfile":
        """Tolerant load: anything malformed falls back to an empty part."""
        result = cls()
        if not isinstance(data, Mapping):
            return result
        result.profile = clean_profile(data.get("profile")) or [0.0] * SLOTS
        if _parse_day(data.get("seen_day")) is not None:
            result.seen_day = str(data["seen_day"]).strip()
            seen = data.get("seen")
            if isinstance(seen, list) and len(seen) == SLOTS:
                result.seen = [v is True for v in seen]
        if _parse_day(data.get("last_decay_day")) is not None:
            result.last_decay_day = str(data["last_decay_day"]).strip()
        if isinstance(data.get("last_learn"), str):
            result.last_learn = data["last_learn"]
        result.last_drop = to_float(data.get("last_drop"))
        slot = to_float(data.get("last_slot"))
        result.last_slot = int(slot) if slot is not None and 0 <= slot < SLOTS else None
        learned = to_float(data.get("learned"))
        result.learned = int(learned) if learned is not None and learned > 0 else 0
        if isinstance(data.get("imported_from"), str):
            result.imported_from = data["imported_from"]
        return result

    # -- decay ------------------------------------------------------------

    def _decay_weekday(self, day: date, factor: float, seen: Optional[List[bool]]) -> None:
        start = day.weekday() * 24
        for index in range(start, start + 24):
            if seen is not None and seen[index]:
                continue
            self.profile[index] = to_fixed(self.profile[index] * factor, 3)

    def apply_daily_decay(self, day: date, alpha: float) -> bool:
        """Decay on a new local day; True when anything changed."""
        if self.seen_day is None:
            self.seen_day = day.isoformat()
            self.seen = [False] * SLOTS
            return True
        old = _parse_day(self.seen_day)
        if old is not None and day <= old:
            return False
        factor = 1.0 - alpha
        if old is None:
            # Unreadable date: Node-RED's fallback decays every unseen slot.
            for index in range(SLOTS):
                if not self.seen[index]:
                    self.profile[index] = to_fixed(self.profile[index] * factor, 3)
        else:
            self._decay_weekday(old, factor, self.seen)
            skipped = old + timedelta(days=1)
            count = 0
            while skipped < day and count < MAX_SKIPPED_DAYS:
                self._decay_weekday(skipped, factor, None)
                skipped += timedelta(days=1)
                count += 1
        self.seen_day = day.isoformat()
        self.seen = [False] * SLOTS
        self.last_decay_day = day.isoformat()
        return True

    # -- learning ---------------------------------------------------------

    def register_draw(
        self, drop: float, booked: datetime, now: datetime, tz: tzinfo, alpha: float
    ) -> bool:
        """Blend a learned drop into the slot of ``booked``."""
        if not isinstance(drop, (int, float)) or not math.isfinite(drop) or drop <= 0.1:
            return False
        slot = week_slot(booked, tz)
        self.apply_daily_decay(now.astimezone(tz).date(), alpha)
        self.seen[slot] = True
        old = self.profile[slot]
        self.profile[slot] = to_fixed(old * (1.0 - alpha) + drop * alpha, 3)
        self.last_learn = booked.isoformat()
        self.last_drop = round(float(drop), 2)
        self.last_slot = slot
        self.learned += 1
        return True

    # -- summaries ----------------------------------------------------------

    def is_empty(self) -> bool:
        return not any(v > 0 for v in self.profile)


def clean_profile(values: Any) -> Optional[List[float]]:
    """168 numbers; non-finite or negative entries become 0. Else None."""
    if not isinstance(values, (list, tuple)) or len(values) != SLOTS:
        return None
    result = []
    for value in values:
        number = to_float(value)
        result.append(number if number is not None and number > 0 else 0.0)
    return result


@dataclass(frozen=True)
class ExpectedDraw:
    when: datetime  # start of the hour (UTC)
    slot: int
    drop: float  # the slot's learned value, °C
    confidence: float  # the value relative to the largest slot, 0..1


def next_draw(
    profile: List[float], now: datetime, tz: tzinfo, threshold: float
) -> Optional[ExpectedDraw]:
    """The next hour within a week whose learned value reaches ``threshold``.

    Hours are stepped in real time from the start of the current local
    hour, so a DST night has 23 or 25 of them.
    """
    largest = max(profile) if profile else 0.0
    if largest <= 0:
        return None
    local = now.astimezone(tz)
    # UTC: adding hours to a zoneinfo datetime is wall-clock arithmetic.
    hour = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    for step in range(1, SLOTS + 2):
        when = hour + timedelta(hours=step)
        slot = week_slot(when, tz)
        value = profile[slot]
        if value > 0 and value >= threshold:
            return ExpectedDraw(when, slot, value, round(min(1.0, value / largest), 2))
    return None


def top_slots(profile: List[float], count: int = 5) -> List[str]:
    """The largest learned slots, e.g. ``["Tue 07:00 4.2", ...]``."""
    ranked = sorted((v, i) for i, v in enumerate(profile) if v > 0)[::-1][:count]
    return ["%s %.1f" % (slot_name(i), v) for v, i in ranked]


@dataclass(frozen=True)
class DrawEvent:
    """A closed draw event; ``learned`` when the drop counts."""

    learned: bool
    drop: float  # what is learned (0 when too slow)
    span: float  # peak - trough
    rate: float  # °C/h from the peak
    minutes: int
    booked: datetime  # the hour the drop is booked on
    reason: str  # why the event closed
    verdict: str  # "learned", "too slow", "below min drop"


class DrawDetector:
    """BT6 draw detection, one ``step`` per tick (Node-RED: per minute).

    Not persisted: an event in progress at a restart is lost, as in
    Node-RED.
    """

    def __init__(self) -> None:
        self.reset()
        self.charge_active = False
        self.settle_until: Optional[datetime] = None

    def reset(self) -> None:
        self.last: Optional[float] = None
        self.in_event = False
        self.start_ts: Optional[datetime] = None
        self.peak: Optional[float] = None
        self.peak_ts: Optional[datetime] = None
        self.trough: Optional[float] = None
        self.trough_ts: Optional[datetime] = None
        self.hour_deg: Dict[int, Tuple[float, datetime]] = {}
        self.history: List[Tuple[datetime, float]] = []

    def _steepest(self, fallback: datetime) -> datetime:
        best: Optional[Tuple[float, datetime]] = None
        for deg, ts in self.hour_deg.values():
            if best is None or deg > best[0]:
                best = (deg, ts)
        return best[1] if best is not None else fallback

    def _close(self, current: float, now: datetime, why: str, drop_min: float) -> DrawEvent:
        assert self.peak is not None and self.trough is not None
        span = self.peak - self.trough
        origin = self.peak_ts or self.start_ts or now
        fall_h = (now - origin).total_seconds() / 3600.0
        rate = span / fall_h if fall_h > 0 else math.inf
        too_slow = rate < DRAW_MIN_RATE
        drop = 0.0 if too_slow else span
        minutes = int(round((now - (self.start_ts or now)).total_seconds() / 60.0))
        learned = drop >= drop_min
        booked = self._steepest(self.trough_ts or now) if learned else (self.trough_ts or now)
        verdict = "learned" if learned else ("too slow" if too_slow else "below min drop")
        event = DrawEvent(learned, drop, span, rate, minutes, booked, why, verdict)
        self.in_event = False
        self.start_ts = None
        self.trough = None
        self.trough_ts = None
        self.hour_deg = {}
        self.history = []
        self.peak = current
        self.peak_ts = now
        return event

    def step(
        self,
        bt6: Optional[float],
        charging: bool,
        now: datetime,
        tz: tzinfo,
        drop_min: float = DEFAULT_MIN_DROP,
    ) -> Optional[DrawEvent]:
        """Feed one BT6 value (None: missing/stale). Returns a closed event."""
        if bt6 is None:
            self.reset()
            return None
        current = float(bt6)
        event: Optional[DrawEvent] = None
        settling = self.settle_until is not None and self.settle_until > now
        if charging or settling:
            if charging and not self.charge_active:
                self.charge_active = True
                # The fall before a charge is the draw that triggered it:
                # evaluate it rather than drop it.
                if self.in_event:
                    event = self._close(current, now, "charging started", drop_min)
            if charging:
                self.settle_until = now + CHARGE_SETTLE
            # BT6 measures the charging circuit now: start over afterwards.
            self.reset()
            return event
        self.charge_active = False
        if self.last is None or self.peak is None:
            self.reset()
            self.last = current
            self.peak = current
            self.peak_ts = now
            return None
        if not self.in_event:
            if current >= self.peak:
                self.peak = current
                self.peak_ts = now
            elif self.peak_ts is not None and now - self.peak_ts >= DRAW_REBASE:
                hours = (now - self.peak_ts).total_seconds() / 3600.0
                if (self.peak - current) / hours < DRAW_REBASE_RATE:
                    # Slow cooling between draws: follow it down.
                    self.peak = current
                    self.peak_ts = now
            if self.peak - current >= DRAW_START_DELTA:
                self.in_event = True
                self.start_ts = now
                self.trough = current
                self.trough_ts = now
                self.hour_deg = {}
                self.history = [(now, current)]
            self.last = current
            return None
        # In an event: follow the trough.
        if self.trough is None or current < self.trough:
            self.trough = current
            self.trough_ts = now
        if self.last is not None and current < self.last:
            slot = week_slot(now, tz)
            deg, first = self.hour_deg.get(slot, (0.0, now))
            self.hour_deg[slot] = (deg + (self.last - current), first)
        start = self.start_ts or now
        origin = self.peak_ts or start
        fall_h = (now - origin).total_seconds() / 3600.0
        assert self.peak is not None and self.trough is not None
        rate = (self.peak - self.trough) / fall_h if fall_h > 0 else math.inf
        since_low = now - (self.trough_ts or start)
        recovered = current - self.trough >= DRAW_RECOVER_DELTA
        too_long = now - start >= DRAW_MAX
        self.history.append((now, current))
        while len(self.history) > 1 and self.history[1][0] <= now - DRAW_TRAIL:
            self.history.pop(0)
        draw_over = False
        oldest = self.history[0]
        if now - start >= DRAW_TRAIL and oldest[0] <= now - DRAW_TRAIL:
            hours = (now - oldest[0]).total_seconds() / 3600.0
            draw_over = (oldest[1] - current) / hours < DRAW_REBASE_RATE
        if not recovered and now - start >= DRAW_REBASE and rate < DRAW_REBASE_RATE:
            # Crawling for half an hour: cooling, not a draw. Re-base.
            self.start_ts = now
            self.peak = current
            self.peak_ts = now
            self.trough = current
            self.trough_ts = now
            self.hour_deg = {}
            self.history = [(now, current)]
        elif recovered or since_low >= DRAW_IDLE_CLOSE or too_long or draw_over:
            if recovered:
                why = "recovered"
            elif since_low >= DRAW_IDLE_CLOSE:
                why = "no new low for 15 min"
            elif draw_over:
                why = "draw over"
            else:
                why = "time limit 360 min"
            event = self._close(current, now, why, drop_min)
        self.last = current
        return event
