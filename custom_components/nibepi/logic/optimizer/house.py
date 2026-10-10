"""Grey-box house model: one thermal mass, fitted from logged temperatures.

Model (continuous time, per climate system, rates per hour)::

    dT_in/dt = a (T_out - T_in) + b (T_s,eff - T_in) + c S + d

- ``a``: loss to outdoors (UA / C), 1/h
- ``b``: gain from the emitters (radiators/floor, K_e / C), 1/h
- ``c``: solar gain per unit of the solar proxy ``S`` (0..1), °C/h
- ``d``: internal gains (people, appliances), °C/h
- ``T_s,eff = max(T_s, T_in)``: a supply below room temperature means the
  compressor is idle and the emitters neither add nor remove heat.

Over a step of ``h`` hours with constant inputs the solution is exact::

    T' = T_eq + (T - T_eq) exp(-k h),  k = a + b,
    T_eq = (a T_out + b T_s + c S + d) / k

The planner uses that, the fit uses the equivalent one-step regression on
resampled (default hourly) means::

    (T[k+1] - T[k]) / h = a (T_out - T_in) + b (T_s - T_in) + c S + d

solved as ridge regression pulled towards a prior (from the house profile,
``profile.py``), so a short or unexciting history gives the prior, not
nonsense. Fits whose signs or time constant are implausible are rejected
and the prior is returned with ``ok`` False.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from .linalg import NormalEquations, inverse


@dataclass(frozen=True)
class HouseModel:
    a: float  # 1/h
    b: float  # 1/h
    c: float = 0.0  # °C/h at solar proxy 1
    d: float = 0.0  # °C/h

    @property
    def time_constant_h(self) -> float:
        return 1.0 / (self.a + self.b)

    def derivative(self, t_in: float, t_out: float, t_supply: float, solar: float = 0.0) -> float:
        supply = t_supply if t_supply > t_in else t_in
        return self.a * (t_out - t_in) + self.b * (supply - t_in) + self.c * solar + self.d

    def step(
        self, t_in: float, t_out: float, t_supply: float, solar: float, hours: float
    ) -> float:
        """Indoor temperature after ``hours`` with constant inputs (exact)."""
        free = self.a * t_out + self.c * solar + self.d
        if t_supply > t_in:
            k = self.a + self.b
            eq = (free + self.b * t_supply) / k
        else:
            k = self.a
            eq = free / k
        return eq + (t_in - eq) * math.exp(-k * hours)

    def steady_supply(self, t_in: float, t_out: float, solar: float = 0.0) -> float:
        """Supply temperature that holds ``t_in`` constant."""
        return t_in + (self.a * (t_in - t_out) - self.c * solar - self.d) / self.b


@dataclass(frozen=True)
class HouseSample:
    """One logged moment. Supply is BT2 (40008 for S1), indoor the room sensor."""

    ts: datetime
    t_in: float
    t_out: float
    t_supply: float
    solar: float = 0.0


@dataclass(frozen=True)
class Bin:
    start: datetime
    t_in: float
    t_out: float
    t_supply: float
    solar: float


def resample(samples: Sequence[HouseSample], step_minutes: int = 60) -> List[Optional[Bin]]:
    """Average samples into consecutive UTC-aligned bins; None for empty bins."""
    if not samples:
        return []
    step = step_minutes * 60
    ordered = sorted(samples, key=lambda s: s.ts.timestamp())
    first = int(ordered[0].ts.timestamp()) // step * step
    last = int(ordered[-1].ts.timestamp()) // step * step
    sums: dict = {}
    for s in ordered:
        key = int(s.ts.timestamp()) // step * step
        acc = sums.setdefault(key, [0, 0.0, 0.0, 0.0, 0.0])
        acc[0] += 1
        acc[1] += s.t_in
        acc[2] += s.t_out
        acc[3] += s.t_supply
        acc[4] += s.solar
    bins: List[Optional[Bin]] = []
    for key in range(first, last + step, step):
        acc = sums.get(key)
        if acc is None:
            bins.append(None)
            continue
        n = float(acc[0])
        bins.append(
            Bin(datetime.fromtimestamp(key, timezone.utc), acc[1] / n, acc[2] / n, acc[3] / n, acc[4] / n)
        )
    return bins


@dataclass(frozen=True)
class FitBounds:
    """Plausible parameter ranges; outside them the fit is rejected."""

    a: Tuple[float, float] = (0.002, 0.08)
    b: Tuple[float, float] = (0.002, 0.2)
    c: Tuple[float, float] = (0.0, 1.0)
    d: Tuple[float, float] = (-0.05, 0.3)
    time_constant_h: Tuple[float, float] = (4.0, 400.0)


@dataclass(frozen=True)
class HouseFit:
    model: HouseModel  # the fit when ok, otherwise the prior
    ok: bool
    reason: str
    samples: int = 0
    r2: float = 0.0
    rmse_rate: float = 0.0  # °C/h, one-step
    sim_rmse: float = 0.0  # °C, open-loop over 24 h chunks
    rel_se_b: float = 1.0  # relative standard error of b
    confidence: float = 0.0  # 0..1
    level: str = "low"  # low / medium / high
    details: dict = field(default_factory=dict)


# Regressor scales for the ridge pull: typical magnitudes of each column.
_SCALES = (20.0, 15.0, 0.3, 0.2)
FULL_CONFIDENCE_HOURS = 14 * 24
HIGH = 0.7
MEDIUM = 0.4


def _rows(bins: Sequence[Optional[Bin]], hours: float):
    for prev, nxt in zip(bins, bins[1:]):
        if prev is None or nxt is None:
            continue
        t_in = 0.5 * (prev.t_in + nxt.t_in)
        t_out = 0.5 * (prev.t_out + nxt.t_out)
        supply = max(0.5 * (prev.t_supply + nxt.t_supply), t_in)
        solar = 0.5 * (prev.solar + nxt.solar)
        y = (nxt.t_in - prev.t_in) / hours
        yield [t_out - t_in, supply - t_in, solar, 1.0], y


def simulate_bins(
    model: HouseModel, bins: Sequence[Optional[Bin]], hours: float, chunk: int
) -> Tuple[float, int]:
    """Open-loop RMSE: restart from the measurement every ``chunk`` bins."""
    err = 0.0
    count = 0
    t = None
    since = 0
    for prev, nxt in zip(bins, bins[1:]):
        if prev is None or nxt is None:
            t = None
            continue
        if t is None or since >= chunk:
            t = prev.t_in
            since = 0
        t = model.step(t, prev.t_out, prev.t_supply, prev.solar, hours)
        since += 1
        err += (t - nxt.t_in) ** 2
        count += 1
    return (err / count) ** 0.5 if count else 0.0, count


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def fit_house_model(
    samples: Sequence[HouseSample],
    prior: HouseModel,
    bounds: FitBounds = FitBounds(),
    step_minutes: int = 60,
    prior_hours: float = 6.0,
    sensor_margin: float = 0.0,
) -> HouseFit:
    """Fit a, b, c, d from a window of history (e.g. the last 14-28 days).

    ``prior_hours``: how many hours of data the prior is worth (ridge
    strength). Kept small: the supply follows the outdoor temperature
    through the curve, so ``a``, ``b`` and ``d`` are nearly collinear and
    a strong prior drags them along that direction. What separates them is
    offset movement (excitation); without it ``rel_se_b`` stays large and
    the confidence low. ``sensor_margin`` (°C, from the profile: one sensor on one
    floor of a multi-floor house) is added to the tolerated simulation
    error before confidence drops, i.e. such houses need a clearly better
    fit to reach the same confidence.
    """
    hours = step_minutes / 60.0
    bins = resample(samples, step_minutes)
    eq = NormalEquations(4)
    for x, y in _rows(bins, hours):
        eq.add(x, y)
    n = eq.rows
    if n < 24:
        return HouseFit(prior, False, "too little data (%d steps)" % n, samples=n)
    prior_theta = [prior.a, prior.b, prior.c, prior.d]
    ridge = [prior_hours / hours * s * s * 0.25 for s in _SCALES]
    theta = eq.solve(ridge, prior_theta)
    if theta is None:
        return HouseFit(prior, False, "singular fit", samples=n)
    if theta[2] < 0:
        # Solar must not cool the house: pin c to 0 and refit.
        pinned = list(ridge)
        pinned[2] = 1e9
        theta = eq.solve(pinned, prior_theta[:2] + [0.0, prior_theta[3]]) or theta
        theta[2] = max(0.0, theta[2])
    a, b, c, d = theta
    model = HouseModel(a, b, c, d)

    rss = eq.residual_sum(theta)
    mean_y = eq.xty[3] / eq.count  # the constant column sums y
    sst = max(1e-12, eq.yty - eq.count * mean_y * mean_y)
    r2 = 1.0 - rss / sst
    rmse_rate = (rss / n) ** 0.5
    sigma2 = rss / max(1, n - 4)
    a_reg = [row[:] for row in eq.xtx]
    for i, lam in enumerate(ridge):
        a_reg[i][i] += lam
    cov = inverse(a_reg)
    se_b = (sigma2 * cov[1][1]) ** 0.5 if cov else float("inf")
    rel_se_b = se_b / abs(b) if b else float("inf")
    sim_rmse, _ = simulate_bins(model, bins, hours, int(round(24 / hours)))
    details = {"a": a, "b": b, "c": c, "d": d, "tau_h": (1.0 / (a + b)) if a + b > 0 else None}

    def reject(why: str) -> HouseFit:
        return HouseFit(prior, False, why, n, r2, rmse_rate, sim_rmse, rel_se_b, 0.0, "low", details)

    if a <= 0 or b <= 0:
        return reject("nonsensical signs (a %.4f, b %.4f)" % (a, b))
    for name, value, (lo, hi) in (
        ("a", a, bounds.a),
        ("b", b, bounds.b),
        ("c", c, bounds.c),
        ("d", d, bounds.d),
    ):
        if not lo <= value <= hi:
            return reject("%s %.4f outside %.4f..%.4f" % (name, value, lo, hi))
    tau = 1.0 / (a + b)
    lo, hi = bounds.time_constant_h
    if not lo <= tau <= hi:
        return reject("time constant %.0f h outside %.0f..%.0f" % (tau, lo, hi))

    data_factor = _clamp01(n * hours / FULL_CONFIDENCE_HOURS)
    sim_factor = _clamp01(1.0 - (sim_rmse - 0.2 - sensor_margin) / 0.6)
    se_factor = _clamp01(1.0 - rel_se_b / 0.5)
    confidence = data_factor * sim_factor * se_factor
    level = "high" if confidence >= HIGH else "medium" if confidence >= MEDIUM else "low"
    return HouseFit(model, True, "ok", n, r2, rmse_rate, sim_rmse, rel_se_b, confidence, level, details)


def synthetic_history(
    model: HouseModel,
    start: datetime,
    hours: int,
    t_out_fn,
    supply_fn,
    solar_fn=lambda when: 0.0,
    t_in0: float = 21.0,
    minutes: int = 10,
    noise=None,
) -> List[HouseSample]:
    """Simulate a house for tests and the backtest tool.

    ``supply_fn(when, t_in)`` gives the supply temperature, so a feedback
    law (or a random offset schedule) can drive it. ``noise(i)`` adds
    sensor noise to the indoor reading only.
    """
    out: List[HouseSample] = []
    t = t_in0
    h = minutes / 60.0
    for i in range(int(hours * 60 / minutes)):
        when = start + timedelta(minutes=i * minutes)
        t_out = t_out_fn(when)
        supply = supply_fn(when, t)
        solar = solar_fn(when)
        reading = t + (noise(i) if noise else 0.0)
        out.append(HouseSample(when, reading, t_out, supply, solar))
        t = model.step(t, t_out, supply, solar, h)
    return out
