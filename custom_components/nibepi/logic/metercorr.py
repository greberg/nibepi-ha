"""Correcting a meter that counted wrong (``nibepi.rescale_meter``).

A pulse meter set to the wrong pulses per kWh (a Sparsnäs at 1000 imp/kWh
on a 400 imp/kWh meter) reports every kWh times 400/1000: its whole
history is off by one factor up to when the setting was fixed. The
optimiser uses that history to calibrate the compressor's efficiency
(``fit_shared`` with a heat pump meter) and to count the heat pump's
cost, so the correction:

- multiplies the logged ``meter_kwh`` bins that start before ``until``;
- is kept (``until``, ``factor``) and applied to meter bins imported from
  the recorder later (the recorder still holds the wrong values);
- multiplies the measured kWh and cost of the cost tally's days that
  began before ``until`` (today's and the previous day's; a day split
  by ``until`` is scaled whole).

Plain Python, no Home Assistant imports.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, MutableMapping, Tuple

METER_COLUMN = "meter_kwh"
TALLY_FIELDS = ("kwh", "cost", "unpriced_kwh")

Correction = Tuple[float, float]  # (until epoch, factor)


def factor_at(corrections: List[Correction], start: float) -> float:
    """The factor for a bin starting at ``start``: every correction whose
    ``until`` is after it."""
    factor = 1.0
    for until, f in corrections:
        if start < until:
            factor *= f
    return factor


def rescale_rows(rows: MutableMapping[int, Dict[str, float]], until: float, factor: float) -> int:
    """Multiply the meter bins starting before ``until``; the bins changed."""
    changed = 0
    for start, row in rows.items():
        if start < until and METER_COLUMN in row:
            row[METER_COLUMN] = row[METER_COLUMN] * factor
            changed += 1
    return changed


def rescale_series(series: Mapping[int, float], corrections: List[Correction]) -> Dict[int, float]:
    if not corrections:
        return dict(series)
    return {start: value * factor_at(corrections, start) for start, value in series.items()}


def _scale_day(tally: Dict[str, Any], factor: float) -> None:
    for key in TALLY_FIELDS:
        if isinstance(tally.get(key), (int, float)):
            tally[key] = tally[key] * factor


def rescale_tally(tally: Dict[str, Any], until_day: str, factor: float) -> List[str]:
    """Scale the measured figures of the tally's days up to ``until_day``
    (local ISO date); returns the days scaled. ``yesterday`` is left for
    the caller's ``optcost.refresh``."""
    days = []
    previous = tally.get("previous")
    if isinstance(previous, dict) and isinstance(previous.get("day"), str) and previous["day"] <= until_day:
        _scale_day(previous, factor)
        days.append(previous["day"])
    if isinstance(tally.get("day"), str) and tally["day"] <= until_day:
        _scale_day(tally, factor)
        days.append(tally["day"])
    return days


def load(data: Any) -> List[Correction]:
    out: List[Correction] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    until, factor = float(item[0]), float(item[1])
                except (TypeError, ValueError):
                    continue
                if factor > 0:
                    out.append((until, factor))
    return out
