"""The user's electricity meter -> kWh per slot, and the heat pump's share.

The user picks one Home Assistant entity, either

- a power sensor (``kind="power"``, W or kW, instantaneous; HA reports on
  change, so a value holds until the next one), or
- an energy sensor (``kind="energy"``, Wh/kWh/MWh, ``total_increasing``;
  resets to zero or small values happen, e.g. daily meters or a restart).

``slot_energy`` turns either into kWh per slot with a coverage figure;
unavailable/unknown states arrive as ``None`` and break the series.

The meter most likely measures the whole house. ``fit_hp_share``
separates the heat pump by regressing the house's slot energy on a heat
pump activity signal, hour by hour::

    house_kwh / h = base_kw + beta * activity

Best activity signal: the pump's own compressor input power (43141 W)
plus the internal electric addition (43084 kW); then ``beta`` ≈ 1 checks
or corrects the pump's estimate and ``base_kw`` is the household's mean
load. Without 43141 the compressor frequency (43136 Hz) works, with
``beta`` in kW per Hz. The household load is noisy (cooking, EV, sauna),
so this needs weeks of data and is gated on its fit; it is never used to
steer, only to calibrate the COP (``energy.fit_carnot_eta``) and as a
sanity check: the heat pump estimate must not exceed the house total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Tuple

from .linalg import NormalEquations

UNIT_TO_KW = {"w": 0.001, "kw": 1.0, "mw": 1000.0}
UNIT_TO_KWH = {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0}


@dataclass(frozen=True)
class MeterReading:
    ts: datetime
    value: Optional[float]  # None: unavailable / unknown


@dataclass(frozen=True)
class MeterConfig:
    kind: str  # "power" or "energy"
    unit: str  # W, kW, Wh, kWh, MWh
    max_hold: timedelta = timedelta(minutes=30)  # power: longest a value is trusted to hold
    max_gap: timedelta = timedelta(hours=2)  # energy: longest interval to spread a delta over
    reset_ratio: float = 0.5  # energy: a drop below this fraction of the last value is a reset


@dataclass(frozen=True)
class SlotEnergy:
    start: datetime
    end: datetime
    kwh: Optional[float]  # scaled to the full slot when coverage allows, else None
    coverage: float  # 0..1 of the slot backed by data


def _intervals(readings: Sequence[MeterReading], config: MeterConfig) -> List[Tuple[float, float, float]]:
    """(t0, t1, kWh) pieces in epoch seconds."""
    kind = config.kind.lower()
    unit = config.unit.lower()
    ordered = sorted(readings, key=lambda r: r.ts.timestamp())
    out: List[Tuple[float, float, float]] = []
    if kind == "power":
        factor = UNIT_TO_KW.get(unit)
        if factor is None:
            raise ValueError("power unit %r" % config.unit)
        hold = config.max_hold.total_seconds()
        for cur, nxt in zip(ordered, ordered[1:]):
            if cur.value is None:
                continue
            t0 = cur.ts.timestamp()
            t1 = min(nxt.ts.timestamp(), t0 + hold)
            if t1 > t0:
                out.append((t0, t1, max(0.0, cur.value) * factor * (t1 - t0) / 3600.0))
        return out
    if kind == "energy":
        factor = UNIT_TO_KWH.get(unit)
        if factor is None:
            raise ValueError("energy unit %r" % config.unit)
        gap = config.max_gap.total_seconds()
        last: Optional[MeterReading] = None
        for cur in ordered:
            if cur.value is None:
                last = None  # unavailable: don't bridge across it
                continue
            if last is not None and last.value is not None:
                t0, t1 = last.ts.timestamp(), cur.ts.timestamp()
                delta = cur.value - last.value
                if delta < 0:
                    # Reset: a small new value is what was counted since it.
                    delta = cur.value if cur.value < last.value * config.reset_ratio else None
                if delta is not None and 0 < t1 - t0 <= gap:
                    out.append((t0, t1, delta * factor))
            last = cur
        return out
    raise ValueError("meter kind %r" % config.kind)


def slot_energy(
    readings: Sequence[MeterReading],
    bounds: Sequence[Tuple[datetime, datetime]],
    config: MeterConfig,
    min_coverage: float = 0.8,
) -> List[SlotEnergy]:
    """kWh per slot; pieces are split over slots in proportion to time."""
    pieces = _intervals(readings, config)
    result = []
    j = 0
    for start, end in bounds:
        s0, s1 = start.timestamp(), end.timestamp()
        while j < len(pieces) and pieces[j][1] <= s0:
            j += 1
        kwh = 0.0
        covered = 0.0
        k = j
        while k < len(pieces) and pieces[k][0] < s1:
            t0, t1, e = pieces[k]
            lo, hi = max(t0, s0), min(t1, s1)
            if hi > lo:
                kwh += e * (hi - lo) / (t1 - t0)
                covered += hi - lo
            k += 1
        coverage = covered / (s1 - s0) if s1 > s0 else 0.0
        value = kwh / coverage if coverage >= min_coverage else None
        result.append(SlotEnergy(start, end, value, coverage))
    return result


@dataclass(frozen=True)
class HpShare:
    base_kw: float
    beta: float
    r2: float
    samples: int
    ok: bool
    reason: str

    def hp_kwh(self, activity: float, hours: float) -> float:
        return max(0.0, self.beta * activity * hours)


def fit_hp_share(
    rows: Sequence[Tuple[float, float, float]], expect_beta: Optional[Tuple[float, float]] = (0.8, 1.4)
) -> HpShare:
    """(house_kwh, activity, hours) rows -> base load and heat pump scale.

    ``activity`` is the slot mean of the activity signal in kW (43141/1000
    + 43084) or Hz (43136); ``expect_beta`` is the plausible range for
    beta (for kW signals ≈ 1; pass None for Hz).
    """
    eq = NormalEquations(2)
    n = 0
    for house_kwh, activity, hours in rows:
        if hours <= 0 or house_kwh is None or activity is None:
            continue
        eq.add([1.0, activity], house_kwh / hours)
        n += 1
    if n < 48:
        return HpShare(0.0, 1.0, 0.0, n, False, "too few slots (%d)" % n)
    theta = eq.solve([1e-6, 1e-6])
    if theta is None:
        return HpShare(0.0, 1.0, 0.0, n, False, "no variation in heat pump activity")
    base, beta = theta
    mean = eq.xty[0] / eq.count
    sst = max(1e-12, eq.yty - eq.count * mean * mean)
    r2 = 1.0 - eq.residual_sum(theta) / sst
    if base < 0 or beta <= 0:
        return HpShare(base, beta, r2, n, False, "nonsensical base %.2f kW / beta %.3f" % (base, beta))
    if expect_beta is not None and not expect_beta[0] <= beta <= expect_beta[1]:
        return HpShare(base, beta, r2, n, False, "beta %.2f outside %.2f..%.2f" % ((beta,) + tuple(expect_beta)))
    return HpShare(base, beta, r2, n, True, "ok")
