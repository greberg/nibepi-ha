"""The heat pump's electricity use right now (W).

Best source: the user's meter when its scope is the heat pump.

- A power sensor: its value, while it is fresh.
- An energy sensor: the rate over the last ``ENERGY_WINDOW`` of the
  minute samples the optimiser already keeps.

Otherwise the pump's own figures: compressor input power (43141) plus the
electric addition (43084) only while steps are on (43091), because 43084
also shows what the controller asks of a blocked addition. These read low
next to a meter (the Sparsnäs meter showed about 4.6x), so the source is
named.

Plain Python, no Home Assistant imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

SOURCE_METER = "meter"
SOURCE_PUMP = "pump"
ENERGY_WINDOW = timedelta(minutes=15)
MIN_ENERGY_SPAN = timedelta(minutes=4)


@dataclass(frozen=True)
class PowerNow:
    watts: Optional[float]
    source: Optional[str]
    compressor_w: Optional[float] = None
    addition_w: Optional[float] = None


def meter_watts(kind: str, unit_scale: float, samples: Sequence, now: datetime) -> Optional[float]:
    """The meter's current power in W from the optimiser's minute samples
    (``ts``, ``value``; None when unavailable or too old). ``unit_scale``
    turns the meter's unit into kW (power) or kWh (energy)."""
    if not samples:
        return None
    if kind == "power":
        last = samples[-1]
        if last.value is None or now - last.ts > timedelta(minutes=2):
            return None
        return float(last.value) * unit_scale * 1000.0
    recent = [s for s in samples if s.value is not None and now - s.ts <= ENERGY_WINDOW]
    if len(recent) < 2:
        return None
    first, last = recent[0], recent[-1]
    span = last.ts - first.ts
    delta = float(last.value) - float(first.value)
    if span < MIN_ENERGY_SPAN or delta < 0:  # too short, or a meter reset
        return None
    return delta * unit_scale / (span.total_seconds() / 3600.0) * 1000.0


def pump_watts(compressor_w: Optional[float], addition_kw: Optional[float], steps: Optional[float]) -> PowerNow:
    if compressor_w is None:
        return PowerNow(None, None)
    addition = float(addition_kw) * 1000.0 if addition_kw is not None and steps is not None and steps > 0 else 0.0
    return PowerNow(float(compressor_w) + addition, SOURCE_PUMP, float(compressor_w), addition)


def power_now(
    meter_w: Optional[float], compressor_w: Optional[float], addition_kw: Optional[float], steps: Optional[float]
) -> PowerNow:
    pump = pump_watts(compressor_w, addition_kw, steps)
    if meter_w is not None:
        return PowerNow(meter_w, SOURCE_METER, pump.compressor_w, pump.addition_w)
    return pump
