"""History for the cost optimiser: 10-minute samples, recorder import, meter.

Pure helpers between Home Assistant and ``logic.optimizer``:

- ``SampleLog``: a rolling column store of 10-minute bins (UTC-aligned
  epoch seconds), shared columns such as ``t_out`` and per-system ones
  such as ``s1_t_in``. Persisted column-wise (compact JSON).
- ``BinAccumulator``: averages the 60 s tick's readings into the current
  bin and hands back the finished bin when the next one starts.
- ``parse_states`` / ``resample_states``: Home Assistant recorder history
  (state changes: a value holds until the next one) into per-bin
  time-weighted means, or the last value for counters.
- ``bin_meter_energy``: the user's meter into kWh per bin, through
  ``optimizer.meter.slot_energy``.
- ``meter_kind``: which kind of meter an entity is.
- Gateway discovery: the NibePi gateway publishes one MQTT discovery
  device (``homeassistant/device/<id>/config``) whose register sensors have
  ``unique_id`` ``<id>_<register>``; ``discovery_device_id`` and
  ``gateway_ids`` find ``<id>`` so the recorder history of a register can
  be looked up.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .inputs import state_value, to_float
from .optimizer.house import HouseSample
from .optimizer.meter import MeterConfig, MeterReading, slot_energy
from .optimizer.solar import DEFAULT_CLOUD, solar_proxy

STEP_S = 600
RETENTION_DAYS = 35
BOOTSTRAP_DAYS = 28
# A recorded value holds until the next state change; Home Assistant only
# records changes, so a steady temperature produces no rows for hours.
DEFAULT_MAX_HOLD_S = 6 * 3600.0

# Columns. Shared: outdoor and the pump's own values. Per system (prefixed
# "s1_"/"s2_"): indoor, supply and curve offset.
SHARED_COLUMNS = (
    "t_out",  # BT1 40004
    "bt10",  # brine in 40015
    "bt6",  # hot water charge 40014
    "prio_hw",  # share of the bin with 43086 = 20 (hot water)
    "prio_heat",  # share of the bin with 43086 = 30 (heating)
    "power_w",  # compressor input power 43141
    "add_kw",  # internal electric addition 43084
    "freq_hz",  # compressor frequency 43136
    "bt3",  # return 40012 (the water back from the floor loops)
    "pump_on",  # share of the bin with the heating medium pump GP1 running (43437 > 0)
    "heat_kwh",  # heat meter, heating 44300 (counter)
    "hw_kwh",  # heat meter, hot water 44298 (counter)
    "meter_kwh",  # the user's meter, kWh in the bin
)
SYSTEM_COLUMNS = ("t_in", "supply", "offset")
COUNTERS = frozenset({"heat_kwh", "hw_kwh"})
SUMMED = frozenset({"meter_kwh"})  # energy per bin: an hour is the sum
PRIO_HW = 20
PRIO_HEAT = 30


def column(name: str, system: Optional[str] = None) -> str:
    return "%s_%s" % (system, name) if system else name


def bin_start(ts: float, step: int = STEP_S) -> int:
    return int(ts // step) * step


# --------------------------------------------------------------------------
# Meter
# --------------------------------------------------------------------------

POWER_UNITS = {"w": "W", "kw": "kW", "mw": "MW"}
ENERGY_UNITS = {"wh": "Wh", "kwh": "kWh", "mwh": "MWh"}


def meter_kind(
    unit: Any, device_class: Any = None, state_class: Any = None
) -> Optional[Tuple[str, str]]:
    """``("power", unit)`` or ``("energy", unit)``, or None when unusable.

    W/kW/MW is a power meter, Wh/kWh/MWh an energy meter. An energy meter
    must count up (state class ``total_increasing`` or ``total``, or none
    given); a ``measurement`` in kWh is a per-period value, not a meter.
    A device class that contradicts the unit is refused.
    """
    if not isinstance(unit, str):
        return None
    key = unit.strip().lower()
    dev = str(device_class).strip().lower() if device_class else None
    state = str(state_class).strip().lower() if state_class else None
    if key in POWER_UNITS:
        if dev not in (None, "power"):
            return None
        return "power", POWER_UNITS[key]
    if key in ENERGY_UNITS:
        if dev not in (None, "energy"):
            return None
        if state not in (None, "total", "total_increasing"):
            return None
        return "energy", ENERGY_UNITS[key]
    return None


def bin_meter_energy(
    readings: Sequence[MeterReading],
    starts: Iterable[int],
    config: MeterConfig,
    step: int = STEP_S,
    min_coverage: float = 0.8,
) -> Dict[int, float]:
    """kWh per bin start; bins without enough coverage are left out."""
    bounds = [
        (datetime.fromtimestamp(s, timezone.utc), datetime.fromtimestamp(s + step, timezone.utc))
        for s in sorted(set(starts))
    ]
    if not bounds or not readings:
        return {}
    out: Dict[int, float] = {}
    for slot in slot_energy(readings, bounds, config, min_coverage):
        if slot.kwh is not None:
            out[int(slot.start.timestamp())] = slot.kwh
    return out


# --------------------------------------------------------------------------
# Recorder history
# --------------------------------------------------------------------------


def _row_time(value: Any) -> Optional[float]:
    if isinstance(value, datetime):
        return value.timestamp()
    number = to_float(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _row(row: Any) -> Tuple[Any, Optional[float]]:
    """(state, epoch seconds) of a recorder row in any of its formats.

    ``State`` objects, minimal-response dicts (``state``/``last_changed``)
    and the compressed format (``s``/``lu``/``lc``, epoch floats).
    """
    if isinstance(row, Mapping):
        state = row.get("s", row.get("state"))
        for key in ("lu", "lc", "last_updated", "last_changed"):
            if row.get(key) is not None:
                return state, _row_time(row[key])
        return state, None
    state = getattr(row, "state", None)
    for key in ("last_updated", "last_changed"):
        value = getattr(row, key, None)
        if value is not None:
            return state, _row_time(value)
    return state, None


def parse_states(
    rows: Iterable[Any], convert: Optional[Callable[[float], Optional[float]]] = None
) -> List[Tuple[float, Optional[float]]]:
    """Recorder rows -> sorted (epoch, value); unavailable/unknown is None."""
    out: List[Tuple[float, Optional[float]]] = []
    for row in rows or ():
        state, ts = _row(row)
        if ts is None:
            continue
        value = state_value(state)
        if value is not None and convert is not None:
            value = convert(value)
        out.append((ts, value))
    out.sort(key=lambda p: p[0])
    return out


def meter_readings(points: Sequence[Tuple[float, Optional[float]]]) -> List[MeterReading]:
    return [MeterReading(datetime.fromtimestamp(ts, timezone.utc), value) for ts, value in points]


def resample_states(
    points: Sequence[Tuple[float, Optional[float]]],
    start: float,
    end: float,
    step: int = STEP_S,
    max_hold: float = DEFAULT_MAX_HOLD_S,
    mode: str = "mean",
    min_coverage: float = 0.5,
) -> Dict[int, float]:
    """State changes -> one value per bin in [start, end).

    Each value holds until the next point, at most ``max_hold`` seconds;
    a None (unavailable) holds nothing. ``mean``: the time-weighted mean of
    the bin, kept when at least ``min_coverage`` of it is covered.
    ``last``: the value in force at the end of the bin (counters).
    """
    first = bin_start(start, step)
    out: Dict[int, float] = {}
    ordered = sorted(points, key=lambda p: p[0])
    if mode == "last":
        j = 0
        current: Optional[Tuple[float, Optional[float]]] = None
        b = first
        while b < end:
            edge = b + step
            while j < len(ordered) and ordered[j][0] < edge:
                current = ordered[j]
                j += 1
            if current is not None and current[1] is not None and edge - current[0] <= max_hold:
                out[b] = float(current[1])
            b = edge
        return out
    if mode != "mean":
        raise ValueError("mode %r" % mode)
    sums: Dict[int, float] = {}
    covered: Dict[int, float] = {}
    for i, (ts, value) in enumerate(ordered):
        if value is None:
            continue
        t0 = max(ts, float(first))
        t1 = ts + max_hold
        if i + 1 < len(ordered):
            t1 = min(t1, ordered[i + 1][0])
        t1 = min(t1, float(end))
        while t0 < t1:
            b = bin_start(t0, step)
            seg = min(t1, b + step)
            sums[b] = sums.get(b, 0.0) + value * (seg - t0)
            covered[b] = covered.get(b, 0.0) + (seg - t0)
            t0 = seg
    for b, cov in covered.items():
        if cov >= min_coverage * step and cov > 0:
            out[b] = sums[b] / cov
    return out


def indicator(points: Sequence[Tuple[float, Optional[float]]], wanted: float) -> List[Tuple[float, Optional[float]]]:
    """1.0 where the value equals ``wanted`` (e.g. prio 20), else 0.0."""
    return [(ts, None if v is None else (1.0 if int(v) == int(wanted) else 0.0)) for ts, v in points]


def running(points: Sequence[Tuple[float, Optional[float]]]) -> List[Tuple[float, Optional[float]]]:
    """1.0 where the value is above 0 (e.g. a pump's speed), else 0.0."""
    return [(ts, None if v is None else (1.0 if v > 0 else 0.0)) for ts, v in points]


def import_series(
    rows: Iterable[Any],
    kind: str,
    start: float,
    end: float,
    convert: Optional[Callable[[float], Optional[float]]] = None,
    meter: Optional[MeterConfig] = None,
    wanted: Optional[float] = None,
) -> Dict[int, float]:
    """Recorder rows of one entity -> one value per 10-minute bin.

    ``kind``: ``mean`` (temperatures, power), ``last`` (counters),
    ``indicator`` (share of the bin at ``wanted``, e.g. prio 20),
    ``running`` (share of the bin above 0, e.g. a pump) or ``meter``
    (kWh per bin from the user's meter, ``meter`` config).
    """
    points = parse_states(rows, convert)
    if kind == "meter":
        if meter is None:
            raise ValueError("meter config missing")
        starts = range(bin_start(start), int(end), STEP_S)
        return bin_meter_energy(meter_readings(points), starts, meter)
    if kind == "indicator":
        return resample_states(indicator(points, wanted if wanted is not None else 0), start, end)
    if kind == "running":
        return resample_states(running(points), start, end)
    return resample_states(points, start, end, mode="last" if kind == "last" else "mean")


# --------------------------------------------------------------------------
# Gateway discovery
# --------------------------------------------------------------------------

_UNIQUE_ID = re.compile(r"^(nibepi_[0-9A-Za-z]+)_(\d+)$")


def discovery_device_id(payload: Any, prefix: str) -> Optional[str]:
    """The discovery id of the gateway with ``prefix``, from its device config.

    The gateway's ``homeassistant/device/<id>/config`` names its status
    topic as availability (``avty_t`` = ``<prefix>status``); that ties the
    discovered device to this integration's gateway.
    """
    try:
        data = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
    except (TypeError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    topic = data.get("avty_t", data.get("availability_topic"))
    if topic != prefix + "status":
        return None
    device = data.get("dev", data.get("device"))
    ids = device.get("ids", device.get("identifiers")) if isinstance(device, Mapping) else None
    if isinstance(ids, str):
        return ids
    if isinstance(ids, (list, tuple)) and ids and isinstance(ids[0], str):
        return ids[0]
    return None


def gateway_ids(unique_ids: Iterable[Any]) -> Set[str]:
    """Discovery ids of NibePi gateways among MQTT sensor unique ids."""
    found: Set[str] = set()
    for uid in unique_ids:
        if isinstance(uid, str):
            match = _UNIQUE_ID.match(uid)
            if match:
                found.add(match.group(1))
    return found


def register_unique_id(device_id: str, register: Any) -> str:
    """``<device>_<register>``; also ``<device>_bus_health`` for the bus sensor."""
    return "%s_%s" % (device_id, register)


# --------------------------------------------------------------------------
# The sample log
# --------------------------------------------------------------------------


class SampleLog:
    """10-minute bins: {bin start epoch: {column: value}}."""

    VERSION = 1

    def __init__(self, step: int = STEP_S) -> None:
        self.step = step
        self.rows: Dict[int, Dict[str, float]] = {}

    def __len__(self) -> int:
        return len(self.rows)

    def put(self, start: int, values: Mapping[str, Optional[float]]) -> None:
        """Live values; they replace what a bootstrap filled in."""
        clean = {k: float(v) for k, v in values.items() if to_float(v) is not None}
        if clean:
            self.rows.setdefault(bin_start(start, self.step), {}).update(clean)

    def fill(self, name: str, series: Mapping[int, float]) -> int:
        """Imported values; only where the bin has no value yet."""
        added = 0
        for start, value in series.items():
            if to_float(value) is None:
                continue
            row = self.rows.setdefault(bin_start(start, self.step), {})
            if name not in row:
                row[name] = float(value)
                added += 1
        return added

    def trim(self, oldest: float) -> None:
        for start in [s for s in self.rows if s < oldest]:
            del self.rows[start]

    def span(self) -> Optional[Tuple[int, int]]:
        if not self.rows:
            return None
        return min(self.rows), max(self.rows)

    def count(self, name: str) -> int:
        return sum(1 for row in self.rows.values() if name in row)

    def columns(self) -> List[str]:
        names: Set[str] = set()
        for row in self.rows.values():
            names.update(row)
        return sorted(names)

    def series(self, name: str, since: Optional[float] = None) -> List[Tuple[int, float]]:
        return sorted(
            (start, row[name])
            for start, row in self.rows.items()
            if name in row and (since is None or start >= since)
        )

    def snapshot(self, since: Optional[float] = None) -> Dict[int, Dict[str, float]]:
        """A copy for an executor job (plain dicts, safe to read off-loop)."""
        return {s: dict(r) for s, r in self.rows.items() if since is None or s >= since}

    def to_dict(self) -> Dict[str, Any]:
        span = self.span()
        if span is None:
            return {"version": self.VERSION, "step": self.step, "start": None, "columns": {}}
        first, last = span
        count = (last - first) // self.step + 1
        cols: Dict[str, List[Optional[float]]] = {}
        for name in self.columns():
            values: List[Optional[float]] = [None] * count
            for start, row in self.rows.items():
                if name in row:
                    values[(start - first) // self.step] = round(row[name], 3)
            cols[name] = values
        return {"version": self.VERSION, "step": self.step, "start": first, "columns": cols}

    @classmethod
    def from_dict(cls, data: Any) -> "SampleLog":
        """Tolerant load: anything malformed gives an empty log."""
        log = cls()
        if not isinstance(data, Mapping):
            return log
        step = to_float(data.get("step"))
        if step is None or int(step) != STEP_S:
            return log
        first = to_float(data.get("start"))
        cols = data.get("columns")
        if first is None or not isinstance(cols, Mapping):
            return log
        for name, values in cols.items():
            if not isinstance(name, str) or not isinstance(values, list):
                continue
            for i, value in enumerate(values):
                number = to_float(value)
                if number is not None:
                    log.rows.setdefault(int(first) + i * log.step, {})[name] = number
        return log


class BinAccumulator:
    """Means of the tick's values per bin; counters keep the last value."""

    def __init__(self, step: int = STEP_S) -> None:
        self.step = step
        self.start: Optional[int] = None
        self._sums: Dict[str, float] = {}
        self._counts: Dict[str, int] = {}
        self._last: Dict[str, float] = {}

    def _result(self) -> Dict[str, float]:
        out = {k: self._sums[k] / self._counts[k] for k in self._sums if self._counts[k]}
        out.update(self._last)
        return out

    def add(
        self, ts: float, values: Mapping[str, Optional[float]]
    ) -> Optional[Tuple[int, Dict[str, float]]]:
        """Add one tick; returns (start, values) of a bin that just ended."""
        key = bin_start(ts, self.step)
        done = None
        if self.start is not None and key != self.start:
            result = self._result()
            if result:
                done = (self.start, result)
            self._sums, self._counts, self._last = {}, {}, {}
        self.start = key
        for name, value in values.items():
            number = to_float(value)
            if number is None:
                continue
            if name in COUNTERS:
                self._last[name] = number
            else:
                self._sums[name] = self._sums.get(name, 0.0) + number
                self._counts[name] = self._counts.get(name, 0) + 1
        return done


# --------------------------------------------------------------------------
# Log -> model inputs
# --------------------------------------------------------------------------


def house_samples(
    rows: Mapping[int, Mapping[str, float]],
    system: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    step: int = STEP_S,
) -> List[HouseSample]:
    """``HouseSample``s from the bins that have indoor, outdoor and supply.

    The solar proxy uses the sun position with an average cloud cover:
    the weather of past days is not kept. BT3, the circulation pump and
    the compressor frequency are passed on where logged (None otherwise,
    e.g. in history from before 0.2.2), the hot water share as 0 when
    unknown. BT3 and the pump are the heat pump's, so only system 1 gets
    them.
    """
    t_in_key, supply_key = column("t_in", system), column("supply", system)
    primary = system in (None, "", "s1")
    out: List[HouseSample] = []
    for start in sorted(rows):
        row = rows[start]
        if t_in_key not in row or "t_out" not in row or supply_key not in row:
            continue
        mid = datetime.fromtimestamp(start + step / 2, timezone.utc)
        solar = 0.0
        if latitude is not None and longitude is not None:
            solar = solar_proxy(mid, latitude, longitude, DEFAULT_CLOUD)
        out.append(
            HouseSample(
                datetime.fromtimestamp(start, timezone.utc), row[t_in_key], row["t_out"], row[supply_key], solar,
                row.get("bt3") if primary else None,
                float(row.get("prio_hw", 0.0)),
                row.get("pump_on") if primary else None,
                row.get("freq_hz"),
            )
        )
    return out


def hourly(
    rows: Mapping[int, Mapping[str, float]], names: Sequence[str], step: int = STEP_S, min_bins: int = 6
) -> List[Tuple[int, Dict[str, float]]]:
    """Hourly means of ``names`` over hours whose bins all have them.

    Counters (``COUNTERS``) become the increase over the hour (from the
    previous hour's last bin), so an hour needs the bin before it too.
    Per-bin energies (``SUMMED``) are added up and need every bin.
    """
    per_hour = 3600 // step
    hours: Dict[int, List[int]] = {}
    for start in rows:
        hours.setdefault(start // 3600 * 3600, []).append(start)
    out: List[Tuple[int, Dict[str, float]]] = []
    for hour in sorted(hours):
        starts = sorted(hours[hour])
        if len(starts) < min(min_bins, per_hour):
            continue
        result: Dict[str, float] = {}
        ok = True
        for name in names:
            if name in COUNTERS:
                before = rows.get(hour - step, {}).get(name)
                after = rows.get(hour + 3600 - step, {}).get(name)
                if before is None or after is None or after < before:
                    ok = False
                    break
                result[name] = after - before
                continue
            values = [rows[s][name] for s in starts if name in rows[s]]
            if len(values) < len(starts) or (name in SUMMED and len(values) < per_hour):
                ok = False
                break
            result[name] = sum(values) if name in SUMMED else sum(values) / len(values)
        if ok:
            out.append((hour, result))
    return out


def retention_start(now: datetime, days: int = RETENTION_DAYS) -> float:
    return (now - timedelta(days=days)).timestamp()
