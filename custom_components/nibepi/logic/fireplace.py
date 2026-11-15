"""The fireplace: a fire near the indoor thermometer is not a warm house.

The room sensor sits in the room with the fireplace. While a fire burns
it reads 1-3 °C warmer than the rest of the house, and afterwards the
warm room takes hours to come back. Taken at face value:

- the rules' indoor component (and the RMU40 feed, which the pump uses
  the same way) lower the heat, and the rooms away from the fire cool;
- the optimiser plans from a room that looks warm;
- the house model learns the fire as sun or internal gains.

So while a fire burns, and in its *afterglow* until the room is back
near where it was, the indoor temperature for control is held at the
value from before the fire (``baseline``; a room colder than that is
still seen as it is), and the fire's hours are left out of the model's
history.

A fire starts with the Fireplace switch, or is detected when the room
rises ``DETECT_RISE`` within ``DETECT_WINDOW``: faster than the heat pump
moves the room (floor heating takes hours for a few tenths). The
baseline is then the lowest reading in that window, from before the rise.

- Switched on: burning until switched off or ``BURN_HOURS`` (auto off).
- Detected: burning until the room has not risen for ``PEAK_QUIET``.
- Afterglow: until every room is back within ``END_MARGIN`` of its
  baseline, at most ``AFTERGLOW_MAX``.

The same detector runs over the logged history before a fit
(``detect_spans``), so fires from before the switch existed are left out
too.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

OFF = "off"
BURNING = "burning"
AFTERGLOW = "afterglow"
SWITCH = "switch"
DETECTED = "detected"

DETECT_RISE = 0.5  # °C ...
DETECT_WINDOW = timedelta(minutes=30)  # ... within this
PEAK_QUIET = timedelta(minutes=60)
BURN_HOURS = 4.0
END_MARGIN = 0.2
AFTERGLOW_MAX = timedelta(hours=8)
KEEP_SPANS = timedelta(days=40)
# History bins before a detected start that the rise already affected.
SPAN_LEAD = timedelta(minutes=10)

Span = Tuple[datetime, datetime]


def _iso(when: Optional[datetime]) -> Optional[str]:
    return when.isoformat() if when is not None else None


def _dt(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


class Fireplace:
    def __init__(self) -> None:
        self.phase = OFF
        self.source: Optional[str] = None
        self.started: Optional[datetime] = None
        self.burn_until: Optional[datetime] = None  # switched on: auto off
        self.glow_since: Optional[datetime] = None
        self.baseline: Dict[str, float] = {}
        self.peak: Dict[str, Tuple[datetime, float]] = {}
        self.spans: List[Span] = []
        self._recent: Dict[str, List[Tuple[datetime, float]]] = {}

    # -- state ------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.phase != OFF

    @property
    def burning(self) -> bool:
        return self.phase == BURNING

    def effective(self, system: str, value: Optional[float]) -> Optional[float]:
        """The indoor temperature for control: the baseline while a fire
        counts, unless the room is colder than that."""
        if value is None or not self.active:
            return value
        base = self.baseline.get(system)
        return value if base is None else min(value, base)

    def spans_until(self, now: datetime) -> List[Span]:
        out = list(self.spans)
        if self.active and self.started is not None:
            out.append((self.started, now))
        return out

    # -- events -----------------------------------------------------------

    def turn_on(self, now: datetime, hours: float = BURN_HOURS) -> None:
        """The switch: a fire was lit (or one detected is confirmed)."""
        if self.phase == OFF:
            self.started = now
            # Lit a while ago: the lowest reading of the last window.
            self.baseline = {s: min(v for _, v in pts) for s, pts in self._recent.items() if pts}
        self.phase = BURNING
        self.source = SWITCH
        self.glow_since = None
        self.burn_until = now + timedelta(hours=hours)

    def turn_off(self, now: datetime) -> None:
        """The switch off: the fire is out; the afterglow runs."""
        if self.phase == BURNING:
            self._glow(now)

    def _glow(self, now: datetime) -> None:
        self.phase = AFTERGLOW
        self.glow_since = now
        self.burn_until = None

    def _end(self, now: datetime) -> None:
        if self.started is not None:
            self.spans.append((self.started, now))
        self.spans = [s for s in self.spans if now - s[1] <= KEEP_SPANS]
        self.phase = OFF
        self.source = None
        self.started = self.burn_until = self.glow_since = None
        self.baseline = {}
        self.peak = {}

    def update(self, now: datetime, readings: Mapping[str, Optional[float]], detect: bool = True) -> bool:
        """One tick with the raw indoor readings per system; True when the
        phase changed."""
        before = self.phase
        for system, value in readings.items():
            if value is None:
                continue
            pts = [p for p in self._recent.get(system, []) if now - p[0] <= DETECT_WINDOW]
            pts.append((now, float(value)))
            self._recent[system] = pts
            peak = self.peak.get(system)
            if self.active and (peak is None or value > peak[1]):
                self.peak[system] = (now, float(value))
        # In the afterglow only a rise from where it has cooled to since counts.
        since = self.glow_since if self.phase == AFTERGLOW else None
        rising = self._rising(since) if detect else {}
        if self.phase == OFF and rising:
            self.phase = BURNING
            self.source = DETECTED
            self.started = min(t for t, _ in rising.values()) - SPAN_LEAD
            self.baseline = {s: v for s, (_, v) in rising.items()}
            for s, pts in self._recent.items():
                if s not in self.baseline and pts:
                    self.baseline[s] = min(v for _, v in pts)
            self.peak = {s: pts[-1] for s, pts in self._recent.items() if pts}
        elif self.phase == BURNING:
            if self.source == SWITCH and self.burn_until is not None and now >= self.burn_until:
                self._glow(now)
            elif self.source == DETECTED and self.peak and all(now - t >= PEAK_QUIET for t, _ in self.peak.values()):
                self._glow(now)
        elif self.phase == AFTERGLOW:
            if rising:
                # Another log on the fire.
                self.phase = BURNING
                self.source = DETECTED
                self.glow_since = None
                self.peak = {s: pts[-1] for s, pts in self._recent.items() if pts}
            else:
                back = all(
                    readings.get(s) is not None and readings[s] <= base + END_MARGIN  # type: ignore[operator]
                    for s, base in self.baseline.items()
                )
                if back or (self.glow_since is not None and now - self.glow_since >= AFTERGLOW_MAX):
                    self._end(now)
        return self.phase != before

    def _rising(self, since: Optional[datetime] = None) -> Dict[str, Tuple[datetime, float]]:
        """Systems whose room rose DETECT_RISE within the window (and since
        ``since``): their lowest point before the rise (time, value)."""
        out = {}
        for system, pts in self._recent.items():
            if since is not None:
                pts = [p for p in pts if p[0] >= since]
            if len(pts) < 2:
                continue
            low_t, low = min(pts, key=lambda p: p[1])
            if pts[-1][1] - low >= DETECT_RISE - 1e-9 and pts[-1][0] > low_t:
                out[system] = (low_t, low)
        return out

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "source": self.source,
            "started": _iso(self.started),
            "burn_until": _iso(self.burn_until),
            "glow_since": _iso(self.glow_since),
            "baseline": dict(self.baseline),
            "spans": [[_iso(a), _iso(b)] for a, b in self.spans],
        }

    def load(self, data: Any) -> None:
        if not isinstance(data, Mapping):
            return
        phase = data.get("phase")
        if phase in (BURNING, AFTERGLOW) and _dt(data.get("started")) is not None:
            self.phase = phase
            self.source = data.get("source") if data.get("source") in (SWITCH, DETECTED) else DETECTED
            self.started = _dt(data.get("started"))
            self.burn_until = _dt(data.get("burn_until"))
            self.glow_since = _dt(data.get("glow_since"))
            base = data.get("baseline")
            if isinstance(base, Mapping):
                self.baseline = {str(k): float(v) for k, v in base.items() if isinstance(v, (int, float))}
        spans = data.get("spans")
        if isinstance(spans, list):
            for item in spans:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    a, b = _dt(item[0]), _dt(item[1])
                    if a is not None and b is not None:
                        self.spans.append((a, b))

    def attributes(self, now: datetime) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "source": self.source,
            "since": _iso(self.started),
            "auto_off": _iso(self.burn_until),
            "afterglow_since": _iso(self.glow_since),
            "afterglow_max": _iso(self.glow_since + AFTERGLOW_MAX) if self.glow_since is not None else None,
            "baseline": dict(self.baseline) or None,
            "fires_30d": sum(1 for _, end in self.spans_until(now) if now - end <= timedelta(days=30)),
        }


# -- history ------------------------------------------------------------------


def detect_spans(points: Sequence[Tuple[int, float]], step_s: int = 600) -> List[Span]:
    """Fires in a logged indoor series (bin start -> value), by the same
    rules as live detection."""
    fire = Fireplace()
    end: Optional[datetime] = None
    for ts, value in sorted(points):
        end = datetime.fromtimestamp(ts + step_s, timezone.utc)
        fire.update(end, {"x": value})
    return fire.spans_until(end) if end is not None else []


def without_fires(
    rows: Mapping[int, Mapping[str, float]], systems: Iterable[str], spans: Sequence[Span],
    step_s: int = 600,
) -> Tuple[Dict[int, Mapping[str, float]], List[Span]]:
    """The history with each system's indoor temperature dropped where a
    fire counted: the given spans (the switch's, live detections) and
    the ones detected in the history itself. Returns (rows, all spans)."""
    systems = list(systems)
    found: List[Span] = list(spans)
    for system in systems:
        key = "%s_t_in" % system
        found.extend(detect_spans([(ts, row[key]) for ts, row in rows.items() if key in row], step_s))
    if not found:
        return dict(rows), found
    windows = sorted((a.timestamp(), b.timestamp()) for a, b in found)
    keys = ["%s_t_in" % s for s in systems]
    out: Dict[int, Mapping[str, float]] = {}
    for ts, row in rows.items():
        mid_end = ts + step_s
        if any(a < mid_end and ts < b for a, b in windows):
            out[ts] = {k: v for k, v in row.items() if k not in keys}
        else:
            out[ts] = row
    return out, found
