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

Emitter lag (floor heating)
---------------------------

With floor heating the water heats a concrete slab first, and the slab
heats the room: an offset change shows up indoors hours later, and the
slab keeps heating after the offset drops. ``tau_f > 0`` adds that as a
second state, the slab (or emitter) temperature ``T_f``::

    dT_f/dt  = (T_s,eff - T_f) / tau_f
    T_s,eff  = max(T_s, T_f - (tau_f / tau_d) (T_f - T_in))
    dT_in/dt = a (T_out - T_in) + b (T_f - T_in) + c S + d

While the pump heats, the slab follows the supply with time constant
``tau_f``. When the supply asked for is below what the slab gives off,
the compressor idles and the slab coasts down towards the room with
``tau_d = C_s / K_e`` (its capacity over the emitter conductance); the
max() switches smoothly between the two, so the pump never cools the
slab. At steady state ``T_f = T_s`` and the model is the one-node model,
so ``a``, ``b``, ``c``, ``d`` keep their meaning, and the heat the pump
delivers is the room's share plus what the slab stores::

    Q = K_e (T_f - T_in) + C_s dT_f/dt        (0 while coasting)

Over a step with constant inputs each regime is a linear 2x2 system,
solved exactly (``LagStep``); the regime that keeps the slab warmer at
the end of the step is the one that applies. ``tau_d`` defaults to
``DRAIN_RATIO`` x ``tau_f``: it is not identifiable from indoor and
supply temperatures alone.

``tau_f`` is fitted by a grid search: for each candidate the slab
temperature is filtered from the measured supply (BT2) and indoor
temperature, the same ridge regression gives ``a``..``d`` with ``T_f``
in place of ``T_s``, and the candidate with the lowest open-loop
simulation error wins (with a mild pull towards the emitter type's
prior). Hourly bins do not resolve lags much below an hour: a radiator
house fits as about 1 h, which the planner treats as one node
(``planner.LAG_PLANNER_MIN_H``).

``T_f`` is the slab's excess over the room scaled so that it settles at
the supply. The slab's own temperature is ``tau_f / tau_d`` of the way
back to the room (``slab_temp``): the water hands its heat to the slab
with conductance ``U`` and the slab to the room with ``K_e``, so
``tau_f = C_s / (U + K_e)``, ``tau_d = C_s / K_e`` and the max() above
is "the pump charges while the supply is above the slab".

BT3: the slab observed
----------------------

While the floor water circulates (heating medium pump on, no hot water
charging: then BT2/BT3 show the tank circuit) the return BT3 leaves the
loops a little above the slab: with constant flow the heat into the slab
is proportional to BT2 - BT3, and so is BT3's gap to the slab::

    T_slab = BT3 - kappa (BT2 - BT3)      kappa = 1 / (exp(NTU) - 1)

BT3 rather than the mean of BT2 and BT3: floor loops are long (NTU of 1-3),
so the water leaves close to the slab; the mean water temperature, right
for radiators, would put the slab half of BT2 - BT3 too warm. kappa is
fitted. ``fit_slab`` regresses these observations against the slab
filtered from BT2 for a grid of (tau_f, tau_d): tau_f and tau_d come out
directly, without going through the room temperature, and tau_d
(otherwise fixed at ``DRAIN_RATIO`` x tau_f) is fitted when the data tell
it. ``slab_track`` then follows the slab with BT3, the room regression
uses it, and ``fit_house_model`` keeps whichever of the BT3 and the
BT2-only fit simulates the room better (BT2-only without enough BT3).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from .linalg import NormalEquations, inverse


# Slab coasting time constant over its charging time constant (tau_d / tau_f).
DRAIN_RATIO = 3.0
# Candidate emitter lags for the fit, hours.
LAG_GRID_H = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
# Pull towards the prior lag: score = sim_rmse x (1 + w ln(tau / prior)^2).
LAG_PRIOR_WEIGHT = 0.03
# A sample with hot water charging this share of it: BT2/BT3 show the tank
# circuit (the valve QN10 sends the water to the coil), not the floor.
HW_SHARE = 0.1


@dataclass(frozen=True)
class HouseModel:
    a: float  # 1/h
    b: float  # 1/h
    c: float = 0.0  # °C/h at solar proxy 1
    d: float = 0.0  # °C/h
    tau_f: float = 0.0  # h, emitter/slab lag; 0: one node (supply acts at once)
    tau_d: float = 0.0  # h, slab coasting (C_s / K_e); 0: DRAIN_RATIO x tau_f
    # Slab below BT3 per K of BT2 - BT3 (``fit_slab``); None: BT3 not used.
    kappa: Optional[float] = None

    @property
    def time_constant_h(self) -> float:
        return 1.0 / (self.a + self.b)

    @property
    def lagged(self) -> bool:
        return self.tau_f > 0.0

    @property
    def drain_h(self) -> float:
        return self.tau_d if self.tau_d > 0.0 else DRAIN_RATIO * self.tau_f

    @property
    def slab_coupling(self) -> float:
        """Indoor gain per K of the slab's own temperature over the room, 1/h.

        ``b`` is per K of the state T_f, which settles at the supply; the
        slab itself sits ``tau_f / tau_d`` of the way back to the room.
        """
        return self.b * self.drain_h / (self.drain_h - self.tau_f) if self.lagged else self.b

    def one_node(self) -> "HouseModel":
        """The same house without the emitter lag."""
        return replace(self, tau_f=0.0, tau_d=0.0)

    def derivative(self, t_in: float, t_out: float, t_supply: float, solar: float = 0.0) -> float:
        supply = t_supply if t_supply > t_in else t_in
        return self.a * (t_out - t_in) + self.b * (supply - t_in) + self.c * solar + self.d

    def step(
        self, t_in: float, t_out: float, t_supply: float, solar: float, hours: float
    ) -> float:
        """Indoor temperature after ``hours`` with constant inputs (exact, one node)."""
        free = self.a * t_out + self.c * solar + self.d
        if t_supply > t_in:
            k = self.a + self.b
            eq = (free + self.b * t_supply) / k
        else:
            k = self.a
            eq = free / k
        return eq + (t_in - eq) * math.exp(-k * hours)

    def lag_step(
        self, t_in: float, t_f: float, t_out: float, t_supply: float, solar: float, hours: float
    ) -> Tuple[float, float, float]:
        """(T_in, T_f, heat / K_e in K·h) after ``hours`` (lagged model)."""
        return LagStep(self, hours).step(t_in, t_f, self.a * t_out + self.c * solar + self.d, t_supply)

    def steady_supply(self, t_in: float, t_out: float, solar: float = 0.0) -> float:
        """Supply temperature that holds ``t_in`` constant (also the slab's)."""
        return t_in + (self.a * (t_in - t_out) - self.c * solar - self.d) / self.b


def _expm2(p: float, q: float, r: float, s: float, h: float) -> Tuple[float, float, float, float]:
    """exp(A h) for A = [[p, q], [r, s]], row-major."""
    m = 0.5 * (p + s)
    e = 0.5 * (p - s)
    disc = e * e + q * r
    if disc > 1e-14:
        dl = math.sqrt(disc)
        ch, sh = math.cosh(dl * h), math.sinh(dl * h) / dl
    elif disc < -1e-14:
        dl = math.sqrt(-disc)
        ch, sh = math.cos(dl * h), math.sin(dl * h) / dl
    else:
        ch, sh = 1.0, h
    em = math.exp(m * h)
    return (em * (ch + sh * (p - m)), em * sh * q, em * sh * r, em * (ch + sh * (s - m)))


class LagStep:
    """Exact step of the lagged model over ``hours`` (precomputed matrices).

    State (T_in, T_f). Charging: A = [[-(a+b), b], [0, -1/tau_f]] with the
    equilibrium T_f = T_s; coasting: A = [[-(a+b), b], [1/tau_d, -1/tau_d]]
    with T_f = T_in = free / a. ``free`` = a T_out + c S + d.
    """

    __slots__ = ("hours", "a", "b", "k", "td", "charge", "drain")

    def __init__(self, model: HouseModel, hours: float) -> None:
        a, b = model.a, model.b
        td = model.drain_h
        self.hours = hours
        self.a, self.b, self.k, self.td = a, b, a + b, td
        self.charge = _expm2(-(a + b), b, 0.0, -1.0 / model.tau_f, hours)
        self.drain = _expm2(-(a + b), b, 1.0 / td, -1.0 / td, hours)

    def step(self, t_in: float, t_f: float, free: float, t_supply: float) -> Tuple[float, float, float]:
        eq_in = (free + self.b * t_supply) / self.k
        m = self.charge
        di, df = t_in - eq_in, t_f - t_supply
        n_in = eq_in + m[0] * di + m[1] * df
        n_f = t_supply + m[2] * di + m[3] * df
        if t_supply < t_f:
            eq = free / self.a
            m = self.drain
            di, df = t_in - eq, t_f - eq
            c_f = eq + m[2] * di + m[3] * df
            if c_f > n_f:
                return eq + m[0] * di + m[1] * df, c_f, 0.0
        heat = 0.5 * (t_f + n_f - t_in - n_in) * self.hours + self.td * (n_f - t_f)
        return n_in, n_f, heat if heat > 0.0 else 0.0

    def coast(self, t_in: float, t_f: float, free: float) -> Tuple[float, float]:
        """(T_in, T_f) with the compressor idle: the slab only gives off heat."""
        eq = free / self.a
        m = self.drain
        di, df = t_in - eq, t_f - eq
        return eq + m[0] * di + m[1] * df, eq + m[2] * di + m[3] * df


def slab_filter(
    samples: Sequence["HouseSample"],
    tau_f: float,
    tau_d: float,
    max_gap_s: float = 1800.0,
    warmup_h: Optional[float] = None,
) -> List[Optional[float]]:
    """The slab temperature T_f per sample, filtered from supply and indoor.

    The supply here is the measured BT2, not the curve's target. Above the
    slab's own temperature (``slab_temp``: what it gives off, below T_f)
    the water charges it; at or below, the compressor idles (BT2 then
    follows the slab, the circulation pump still runs) and the slab coasts
    towards the room. This is the max() of ``LagStep``, so the two regimes
    meet smoothly. Samples charging hot water, or with the heating medium
    pump stopped, carry no heat to the floor: the slab coasts. ``samples``
    in time order. T_f starts at the measured supply after a gap and is
    None for ``warmup_h`` (default 1.5 tau_f) after that.
    """
    warm = 1.5 * tau_f if warmup_h is None else warmup_h
    return _filter(_columns(samples), tau_f, tau_d, max_gap_s, warm)


def _columns(samples: Sequence["HouseSample"]):
    """Per-sample arrays for the filter: time, supply, indoor, heats floor."""
    return (
        [s.ts.timestamp() for s in samples],
        [s.t_supply for s in samples],
        [s.t_in for s in samples],
        [heats_floor(s) for s in samples],
    )


def _filter(cols, tau_f: float, tau_d: float, max_gap_s: float, warm: float) -> List[Optional[float]]:
    times, supply, t_in, heats = cols
    out: List[Optional[float]] = []
    keep = 1.0 - tau_f / tau_d
    exp = math.exp
    t_f = 0.0
    since = 0.0
    last = None
    for i, ts in enumerate(times):
        if last is None or ts - last > max_gap_s:
            t_f = supply[i]
            since = ts
        else:
            j = i - 1
            h = (ts - last) / 3600.0
            room = t_in[j]
            # Charge above the slab's own temperature (``slab_temp``), else coast.
            if heats[j] and supply[j] > room + keep * (t_f - room):
                t_f = supply[j] + (t_f - supply[j]) * exp(-h / tau_f)
            else:
                t_f = room + (t_f - room) * exp(-h / tau_d)
        last = ts
        out.append(t_f if (ts - since) / 3600.0 >= warm else None)
    return out


def slab_temp(t_f: float, t_in: float, tau_f: float, tau_d: float) -> float:
    """The slab's own temperature (what BT3 sees) from the model state T_f.

    T_f is the slab's excess over the room scaled so that it settles at
    the supply: T_f - T_in = (tau_d / (tau_d - tau_f)) (T_slab - T_in).
    """
    return t_f - tau_f / tau_d * (t_f - t_in)


def state_from_slab(t_slab: float, t_in: float, tau_f: float, tau_d: float) -> float:
    """The inverse of ``slab_temp``."""
    return t_in + tau_d / (tau_d - tau_f) * (t_slab - t_in)


def heats_floor(s: "HouseSample") -> bool:
    """Water from the heat pump can reach the floor during this sample."""
    return s.hw < HW_SHARE and (s.pump is None or s.pump >= 0.5)


def _filter_step(prev: "HouseSample", t_f: float, tau_f: float, tau_d: float, h: float) -> float:
    if heats_floor(prev) and prev.t_supply > slab_temp(t_f, prev.t_in, tau_f, tau_d):
        return prev.t_supply + (t_f - prev.t_supply) * math.exp(-h / tau_f)
    return prev.t_in + (t_f - prev.t_in) * math.exp(-h / tau_d)


@dataclass(frozen=True)
class HouseSample:
    """One logged moment. Supply is BT2 (40008 for S1), indoor the room sensor."""

    ts: datetime
    t_in: float
    t_out: float
    t_supply: float
    solar: float = 0.0
    t_return: Optional[float] = None  # BT3 40012; None when not logged
    hw: float = 0.0  # share of the sample charging hot water (43086 = 20)
    pump: Optional[float] = None  # share with the heating medium pump (GP1, 43437) running
    compressor: Optional[float] = None  # compressor frequency, Hz (43136)


@dataclass(frozen=True)
class Bin:
    start: datetime
    t_in: float
    t_out: float
    t_supply: float
    solar: float
    t_f: Optional[float] = None  # filtered slab temperature (lagged fits)
    hw: float = 0.0  # share of the samples charging hot water


def resample(
    samples: Sequence[HouseSample],
    step_minutes: int = 60,
    slab: Optional[Sequence[Optional[float]]] = None,
) -> List[Optional[Bin]]:
    """Average samples into consecutive UTC-aligned bins; None for empty bins.

    ``slab``: T_f per sample (same order as ``samples``); a bin's T_f is
    the mean of the samples that have one, None when it has none. The
    supply is the mean of the samples not charging hot water (all of them
    when every one does); ``hw`` the bin's hot water share.
    """
    if not samples:
        return []
    step = step_minutes * 60
    pairs = sorted(
        zip(samples, slab if slab is not None else [None] * len(samples)),
        key=lambda p: p[0].ts.timestamp(),
    )
    first = int(pairs[0][0].ts.timestamp()) // step * step
    last = int(pairs[-1][0].ts.timestamp()) // step * step
    sums: dict = {}
    for s, t_f in pairs:
        key = int(s.ts.timestamp()) // step * step
        acc = sums.setdefault(key, [0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0, 0.0])
        acc[0] += 1
        acc[1] += s.t_in
        acc[2] += s.t_out
        acc[3] += s.t_supply
        acc[4] += s.solar
        if t_f is not None:
            acc[5] += 1
            acc[6] += t_f
        acc[7] += s.hw
        if s.hw < HW_SHARE:
            acc[8] += 1
            acc[9] += s.t_supply
    bins: List[Optional[Bin]] = []
    for key in range(first, last + step, step):
        acc = sums.get(key)
        if acc is None:
            bins.append(None)
            continue
        n = float(acc[0])
        supply = acc[9] / acc[8] if acc[8] else acc[3] / n
        bins.append(
            Bin(datetime.fromtimestamp(key, timezone.utc), acc[1] / n, acc[2] / n, supply, acc[4] / n,
                acc[6] / acc[5] if acc[5] else None, acc[7] / n)
        )
    return bins


# --------------------------------------------------------------------------
# BT3: the slab observed while the water circulates
# --------------------------------------------------------------------------

# Candidate coasting times over the lag (tau_d / tau_f) when BT3 shows
# enough coasting to tell them apart.
RATIO_GRID = (1.5, 2.0, 3.0, 4.0, 6.0)
KAPPA_MAX = 1.0
PUMP_ON_SHARE = 0.9  # GP1 ran this share of the sample: the water circulated
IDLE_HZ = 5.0  # compressor below this: idle
COAST_DT = 0.3  # K, BT2 - BT3 below this: no heat into the slab
MIN_SLAB_OBS_H = 48.0  # hours of BT3 observations for a slab fit
RATIO_MIN_GAIN = 0.05  # tau_d is fitted when that lowers BT3's RMSE this much
SLAB_GAIN = 0.5  # weight of a BT3 observation in ``slab_track``, per sample
SAMPLE_S = 600.0


def slab_observable(samples: Sequence[HouseSample], max_gap_s: float = 1800.0) -> List[bool]:
    """Whether each sample's BT3 shows the floor slab.

    It does while floor water circulates: not while (or in the sample
    right after) the pump charges hot water, when BT2/BT3 show the tank
    circuit; not with the heating medium pump stopped (GP1 below
    ``PUMP_ON_SHARE`` of the sample), when the water stands still in the
    heat pump. Without pump data (history from before it was logged) a
    coasting slab and still water look the same, BT2 ~ BT3, so BT3 is
    only taken while the compressor runs, and not in a sample next to an
    idle one (it may have started or stopped within it). A return above
    the supply (cooling, or a sensor fault) is left out too.
    """
    n = len(samples)
    times = [s.ts.timestamp() for s in samples]
    out: List[bool] = []

    def idle(j: int, i: int) -> bool:
        if not 0 <= j < n or abs(times[j] - times[i]) > max_gap_s:
            return False
        hz = samples[j].compressor
        return hz is not None and hz < IDLE_HZ

    for i, s in enumerate(samples):
        after_hw = i > 0 and times[i] - times[i - 1] <= max_gap_s and samples[i - 1].hw >= HW_SHARE
        ok = (
            s.t_return is not None
            and s.hw < HW_SHARE
            and not after_hw
            and s.t_return <= s.t_supply + 0.5
        )
        if ok:
            if s.pump is not None:
                ok = s.pump >= PUMP_ON_SHARE
            else:
                ok = not (idle(i, i) or idle(i - 1, i) or idle(i + 1, i))
        out.append(ok)
    return out


def slab_observation(s: HouseSample, kappa: float) -> float:
    """The slab's own temperature from BT3: ``kappa`` (BT2 - BT3) below it.

    The water leaves the loop between the slab and the supply; the heat it
    hands over is proportional to BT2 - BT3 (constant flow), and so is the
    gap between BT3 and the slab.
    """
    return s.t_return - kappa * max(0.0, s.t_supply - s.t_return)  # type: ignore[operator]


@dataclass(frozen=True)
class SlabFit:
    """The slab's lag, coasting time and BT3 offset, fitted against BT3."""

    ok: bool
    reason: str
    tau_f: float = 0.0
    tau_d: float = 0.0
    kappa: float = 0.0
    rmse: float = 0.0  # °C, BT3's slab vs the slab filtered from BT2
    hours: float = 0.0  # of BT3 observations
    coast_hours: float = 0.0  # ... with no heat going in
    ratio_fitted: bool = False  # tau_d fitted (else the prior's tau_d / tau_f)
    grid: dict = field(default_factory=dict)


def fit_slab(
    samples: Sequence[HouseSample],
    prior: HouseModel,
    lag_grid: Optional[Sequence[float]] = None,
    ratio_grid: Optional[Sequence[float]] = None,
    max_gap_s: float = 1800.0,
) -> SlabFit:
    """tau_f, tau_d and kappa from BT3: the slab made observable.

    For each candidate (tau_f, tau_d) the slab is filtered from BT2 as in
    ``slab_filter``, turned into the slab's own temperature and compared
    with BT3 on the observable samples (``slab_observable``):
    BT3 - slab = kappa (BT2 - BT3) + noise, so kappa is a one-line least
    squares per candidate. The lowest RMSE wins, pulled mildly towards the
    prior. tau_d shows while the slab coasts, and in how fast it warms
    while charged; unless another ratio beats the prior's tau_d / tau_f
    by ``RATIO_MIN_GAIN`` the prior's ratio stays. ``samples`` in time
    order.
    """
    obs = slab_observable(samples, max_gap_s)
    hours = sum(obs) * SAMPLE_S / 3600.0
    if hours < MIN_SLAB_OBS_H:
        return SlabFit(False, "too little BT3 data (%.0f h)" % hours, hours=hours)
    gap = [s.t_supply - s.t_return if o else 0.0 for s, o in zip(samples, obs)]  # type: ignore[operator]
    coast = sum(1 for o, g in zip(obs, gap) if o and g < COAST_DT) * SAMPLE_S / 3600.0
    prior_ratio = prior.drain_h / prior.tau_f
    ratios = sorted(set(ratio_grid if ratio_grid is not None else RATIO_GRID) | {prior_ratio})
    cols = _columns(samples)
    times, t_in = cols[0], cols[2]
    pairs = [i for i in range(len(samples) - 1) if obs[i] and times[i + 1] - times[i] <= max_gap_s]
    bt3 = {i: samples[i].t_return for i in pairs}
    dts = {i: max(0.0, gap[i]) for i in pairs}
    best = None
    best_prior = None
    grid: dict = {}
    for tau in (lag_grid if lag_grid is not None else LAG_GRID_H):
        for ratio in ratios:
            td = tau * ratio
            est = _filter(cols, tau, td, max_gap_s, 1.5 * tau)
            keep = 1.0 - tau / td
            sxx = sxy = syy = 0.0
            n = 0
            for i in pairs:
                e0, e1 = est[i], est[i + 1]
                if e0 is None or e1 is None:
                    continue
                # BT3 is the sample's mean: compare with the slab's own
                # temperature (``slab_temp``) halfway through it.
                r0, r1 = t_in[i], t_in[i + 1]
                mid = 0.5 * (r0 + keep * (e0 - r0) + r1 + keep * (e1 - r1))
                y = bt3[i] - mid  # type: ignore[operator]
                x = dts[i]
                sxx += x * x
                sxy += x * y
                syy += y * y
                n += 1
            if n * SAMPLE_S / 3600.0 < MIN_SLAB_OBS_H / 2:
                continue
            kappa = min(KAPPA_MAX, max(0.0, sxy / sxx)) if sxx > 0 else 0.0
            rmse = max(0.0, (syy - 2 * kappa * sxy + kappa * kappa * sxx) / n) ** 0.5
            pull = math.log(tau / prior.tau_f) ** 2 + math.log(ratio / prior_ratio) ** 2
            score = rmse * (1.0 + LAG_PRIOR_WEIGHT * pull)
            grid["%g/%g" % (tau, ratio)] = round(rmse, 4)
            cand = (score, tau, td, kappa, rmse)
            if best is None or score < best[0]:
                best = cand
            if ratio == prior_ratio and (best_prior is None or score < best_prior[0]):
                best_prior = cand
    if best is None or best_prior is None:
        return SlabFit(False, "BT3 does not overlap the filtered slab", hours=hours, coast_hours=coast)
    # tau_d counts as identified when another coasting time fits BT3 clearly
    # better than the prior's ratio; else that ratio stays.
    fitted = best[4] < (1.0 - RATIO_MIN_GAIN) * best_prior[4]
    _, tau, td, kappa, rmse = best if fitted else best_prior
    return SlabFit(True, "ok", tau, td, kappa, rmse, hours, coast, fitted, grid)


def slab_track(
    samples: Sequence[HouseSample],
    tau_f: float,
    tau_d: float,
    kappa: float,
    gain: float = SLAB_GAIN,
    max_gap_s: float = 1800.0,
    warmup_h: Optional[float] = None,
) -> List[Optional[float]]:
    """T_f per sample, filtered from BT2 and pulled towards BT3.

    ``slab_filter`` between observations; an observable sample moves the
    state ``gain`` of the way to what BT3 says (``slab_observation``
    turned into the state). After a gap the state restarts at BT3's slab
    when it is observable (ready at once), else at BT2 with the warm-up.
    """
    obs = slab_observable(samples, max_gap_s)
    warm = 1.5 * tau_f if warmup_h is None else warmup_h
    out: List[Optional[float]] = []
    t_f = 0.0
    prev: Optional[HouseSample] = None
    since = 0.0
    ready = False
    for s, seen in zip(samples, obs):
        ts = s.ts.timestamp()
        target = state_from_slab(slab_observation(s, kappa), s.t_in, tau_f, tau_d) if seen else None
        if prev is None or ts - prev.ts.timestamp() > max_gap_s:
            since = ts
            ready = target is not None
            t_f = target if target is not None else s.t_supply
        else:
            t_f = _filter_step(prev, t_f, tau_f, tau_d, (ts - prev.ts.timestamp()) / 3600.0)
            if target is not None:
                t_f += gain * (target - t_f)
                ready = True
        prev = s
        out.append(t_f if ready or (ts - since) / 3600.0 >= warm else None)
    return out


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


def _rows(bins: Sequence[Optional[Bin]], hours: float, lagged: bool = False):
    for prev, nxt in zip(bins, bins[1:]):
        if prev is None or nxt is None:
            continue
        t_in = 0.5 * (prev.t_in + nxt.t_in)
        t_out = 0.5 * (prev.t_out + nxt.t_out)
        if lagged:
            if prev.t_f is None or nxt.t_f is None:
                continue
            supply = 0.5 * (prev.t_f + nxt.t_f)
        else:
            supply = max(0.5 * (prev.t_supply + nxt.t_supply), t_in)
        solar = 0.5 * (prev.solar + nxt.solar)
        y = (nxt.t_in - prev.t_in) / hours
        yield [t_out - t_in, supply - t_in, solar, 1.0], y


def simulate_bins(
    model: HouseModel, bins: Sequence[Optional[Bin]], hours: float, chunk: int
) -> Tuple[float, int]:
    """Open-loop RMSE: restart from the measurement every ``chunk`` bins.

    A lagged model restarts its slab from the bin's filtered T_f and skips
    bins without one.
    """
    err = 0.0
    count = 0
    t = None
    t_f = 0.0
    since = 0
    lag = LagStep(model, hours) if model.lagged else None
    for prev, nxt in zip(bins, bins[1:]):
        if prev is None or nxt is None or (lag is not None and t is None and prev.t_f is None):
            t = None
            continue
        if t is None or since >= chunk:
            if lag is not None and prev.t_f is None:
                t = None
                continue
            t = prev.t_in
            t_f = prev.t_f if prev.t_f is not None else 0.0
            since = 0
        if lag is not None:
            # Measured supply, as in slab_filter: below the slab the pump
            # idles and the slab coasts (``LagStep`` picks the regime);
            # while it mostly charges hot water the floor gets nothing.
            free = model.a * prev.t_out + model.c * prev.solar + model.d
            if prev.hw < 0.5:
                t, t_f, _ = lag.step(t, t_f, free, prev.t_supply)
            else:
                t, t_f = lag.coast(t, t_f, free)
        else:
            t = model.step(t, prev.t_out, prev.t_supply, prev.solar, hours)
        since += 1
        err += (t - nxt.t_in) ** 2
        count += 1
    return (err / count) ** 0.5 if count else 0.0, count


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _solve(bins, hours: float, prior: HouseModel, prior_hours: float, lagged: bool):
    """Ridge regression of a..d; (theta, normal equations, ridge) or None."""
    eq = NormalEquations(4)
    for x, y in _rows(bins, hours, lagged):
        eq.add(x, y)
    if eq.rows < 24:
        return None, eq, None
    prior_theta = [prior.a, prior.b, prior.c, prior.d]
    ridge = [prior_hours / hours * s * s * 0.25 for s in _SCALES]
    theta = eq.solve(ridge, prior_theta)
    if theta is not None and theta[2] < 0:
        # Solar must not cool the house: pin c to 0 and refit.
        pinned = list(ridge)
        pinned[2] = 1e9
        theta = eq.solve(pinned, prior_theta[:2] + [0.0, prior_theta[3]]) or theta
        theta[2] = max(0.0, theta[2])
    return theta, eq, ridge


def _plausible(theta: Sequence[float], bounds: FitBounds) -> bool:
    a, b, c, d = theta
    if not (bounds.a[0] <= a <= bounds.a[1] and bounds.b[0] <= b <= bounds.b[1]
            and bounds.c[0] <= c <= bounds.c[1] and bounds.d[0] <= d <= bounds.d[1]):
        return False
    return bounds.time_constant_h[0] <= 1.0 / (a + b) <= bounds.time_constant_h[1]


def fit_house_model(
    samples: Sequence[HouseSample],
    prior: HouseModel,
    bounds: FitBounds = FitBounds(),
    step_minutes: int = 60,
    prior_hours: float = 6.0,
    sensor_margin: float = 0.0,
    lag_grid: Optional[Sequence[float]] = None,
) -> HouseFit:
    """Fit a, b, c, d (and the emitter lag) from a window of history (e.g. the last 14-28 days).

    ``prior_hours``: how many hours of data the prior is worth (ridge
    strength). Kept small: the supply follows the outdoor temperature
    through the curve, so ``a``, ``b`` and ``d`` are nearly collinear and
    a strong prior drags them along that direction. What separates them is
    offset movement (excitation); without it ``rel_se_b`` stays large and
    the confidence low. ``sensor_margin`` (°C, from the profile: one sensor on one
    floor of a multi-floor house) is added to the tolerated simulation
    error before confidence drops, i.e. such houses need a clearly better
    fit to reach the same confidence.

    A lagged prior (``prior.tau_f > 0``, from the emitter type) makes this
    a grid search over ``lag_grid`` (default ``LAG_GRID_H``): each
    candidate lag gets its own regression on the filtered slab temperature,
    and the lowest simulation error, pulled mildly towards the prior's lag,
    wins. The coasting time keeps the prior's ratio to the lag.
    """
    hours = step_minutes / 60.0
    chunk = int(round(24 / hours))
    ordered = sorted(samples, key=lambda s: s.ts.timestamp())
    grid_scores: dict = {}
    kappa: Optional[float] = None
    extra: dict = {}
    if prior.lagged:
        ratio = prior.drain_h / prior.tau_f
        best = None
        for tau in (lag_grid if lag_grid is not None else LAG_GRID_H):
            slab = slab_filter(ordered, tau, tau * ratio)
            cand_bins = resample(ordered, step_minutes, slab)
            theta, eq, ridge = _solve(cand_bins, hours, prior, prior_hours, True)
            if theta is None or theta[0] <= 0 or theta[1] <= 0:
                continue
            cand = HouseModel(theta[0], theta[1], theta[2], theta[3], tau, tau * ratio)
            rmse, _ = simulate_bins(cand, cand_bins, hours, chunk)
            score = rmse * (1.0 + LAG_PRIOR_WEIGHT * math.log(tau / prior.tau_f) ** 2)
            grid_scores[tau] = round(rmse, 4)
            if best is None or score < best[0]:
                best = (score, tau, theta, eq, ridge, cand_bins, rmse)
        if best is None:
            # Nothing usable: report why with the prior's lag.
            bins = resample(ordered, step_minutes, slab_filter(ordered, prior.tau_f, prior.drain_h))
            theta, eq, ridge = _solve(bins, hours, prior, prior_hours, True)
            tau_f, tau_d = prior.tau_f, prior.drain_h
        else:
            _, tau_f, theta, eq, ridge, bins, bt2_rmse = best
            tau_d = tau_f * ratio
        extra["slab_source"] = "bt2"
        # BT3: the slab observed; kept when it simulates the room at least as well.
        slab_fit = fit_slab(ordered, prior, lag_grid)
        extra["bt3"] = {
            "ok": slab_fit.ok, "reason": slab_fit.reason, "hours": round(slab_fit.hours, 1),
            "coast_hours": round(slab_fit.coast_hours, 1),
        }
        if slab_fit.ok:
            extra["bt3"].update({
                "tau_f": slab_fit.tau_f, "tau_d": slab_fit.tau_d, "kappa": round(slab_fit.kappa, 3),
                "slab_rmse": round(slab_fit.rmse, 3), "tau_d_fitted": slab_fit.ratio_fitted,
            })
            track = slab_track(ordered, slab_fit.tau_f, slab_fit.tau_d, slab_fit.kappa)
            bins3 = resample(ordered, step_minutes, track)
            theta3, eq3, ridge3 = _solve(bins3, hours, prior, prior_hours, True)
            if theta3 is not None and _plausible(theta3, bounds):
                cand = HouseModel(theta3[0], theta3[1], theta3[2], theta3[3], slab_fit.tau_f, slab_fit.tau_d,
                                  slab_fit.kappa)
                rmse3, _ = simulate_bins(cand, bins3, hours, chunk)
                extra["bt3"]["sim_rmse"] = round(rmse3, 4)
                if best is not None:
                    extra["bt2_sim_rmse"] = round(bt2_rmse, 4)
                if best is None or theta is None or not _plausible(theta, bounds) or rmse3 <= bt2_rmse:
                    theta, eq, ridge, bins = theta3, eq3, ridge3, bins3
                    tau_f, tau_d, kappa = slab_fit.tau_f, slab_fit.tau_d, slab_fit.kappa
                    extra["slab_source"] = "bt3"
            else:
                extra["bt3"]["ok"] = False
                extra["bt3"]["reason"] = "room fit on the BT3 slab implausible"
    else:
        bins = resample(ordered, step_minutes)
        theta, eq, ridge = _solve(bins, hours, prior, prior_hours, False)
        tau_f = tau_d = 0.0
    n = eq.rows
    if n < 24:
        return HouseFit(prior, False, "too little data (%d steps)" % n, samples=n)
    if theta is None:
        return HouseFit(prior, False, "singular fit", samples=n)
    a, b, c, d = theta
    model = HouseModel(a, b, c, d, tau_f, tau_d, kappa)

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
    sim_rmse, _ = simulate_bins(model, bins, hours, chunk) if a > 0 and b > 0 else (0.0, 0)
    details = {"a": a, "b": b, "c": c, "d": d, "tau_h": (1.0 / (a + b)) if a + b > 0 else None}
    if prior.lagged:
        details.update({"tau_f": tau_f, "tau_d": tau_d, "lag_grid": grid_scores, **extra})

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
    t_f0: Optional[float] = None,
    kappa: Optional[float] = None,
    hot_water=None,
    water_noise=None,
    pump_stops: bool = False,
    truth: Optional[list] = None,
) -> List[HouseSample]:
    """Simulate a house for tests and the backtest tool.

    ``supply_fn(when, t_in)`` gives the supply temperature, so a feedback
    law (or a random offset schedule) can drive it. ``noise(i)`` adds
    sensor noise to the indoor reading only. A lagged model integrates its
    slab too (from ``t_f0``, default the first supply) in 1-minute steps;
    the logged supply is what the curve asks for while the pump heats and
    the slab's own temperature while it coasts, like BT2 with the
    circulation pump running.

    Lagged only: ``kappa`` also logs BT3 (``t_return``), the water leaving
    the floor loop, ``kappa / (1 + kappa)`` of the way from the slab to
    the supply while it heats (the slab is ``kappa`` (BT2 - BT3) below
    BT3), the slab itself while it coasts. ``hot_water(when)`` True: the
    pump charges the tank for that sample (hw 1, BT2/BT3 50/45 °C) and the
    slab coasts. ``water_noise(i)`` is added to BT2 and BT3.
    ``pump_stops``: the circulation pump stops while the compressor idles
    (intermittent mode); the still water drifts towards 18 °C. ``truth``
    collects the slab's own temperature at each sample.
    """
    out: List[HouseSample] = []
    t = t_in0
    t_f = t_f0
    h = minutes / 60.0
    lag = LagStep(model, 1.0 / 60.0) if model.lagged else None
    still = None
    still_k = math.exp(-1.0 / 60.0)
    for i in range(int(hours * 60 / minutes)):
        when = start + timedelta(minutes=i * minutes)
        t_out = t_out_fn(when)
        supply = supply_fn(when, t)
        solar = solar_fn(when)
        reading = t + (noise(i) if noise else 0.0)
        if lag is None:
            out.append(HouseSample(when, reading, t_out, supply, solar))
            t = model.step(t, t_out, supply, solar, h)
            continue
        if t_f is None:
            t_f = supply
        if truth is not None:
            truth.append(slab_temp(t_f, t, model.tau_f, model.drain_h))
        free = model.a * t_out + model.c * solar + model.d
        if hot_water is not None and hot_water(when):
            for _ in range(minutes):
                t, t_f = lag.coast(t, t_f, free)
            still = None
            if kappa is None:
                out.append(HouseSample(when, reading, t_out, 50.0, solar, hw=1.0))
                continue
            wn = water_noise(i) if water_noise else 0.0
            out.append(HouseSample(when, reading, t_out, 50.0 + wn, solar, 45.0 + wn, 1.0, 1.0, 60.0))
            continue
        bt2 = bt3 = pump = hz = 0.0
        for _ in range(minutes):
            slab = slab_temp(t_f, t, model.tau_f, model.drain_h)
            t, t_f2, heat = lag.step(t, t_f, free, supply)
            if heat > 0.0:
                still = None
                bt2 += supply
                bt3 += slab + (supply - slab) * (kappa or 0.0) / (1.0 + (kappa or 0.0))
                pump += 1.0
                hz += 50.0
            elif pump_stops:
                still = slab if still is None else 18.0 + (still - 18.0) * still_k
                bt2 += still
                bt3 += still
            else:
                bt2 += slab
                bt3 += slab
                pump += 1.0
            t_f = t_f2
        if kappa is None:
            out.append(HouseSample(when, reading, t_out, bt2 / minutes, solar))
            continue
        wn = (water_noise(i) if water_noise else 0.0, water_noise(i + 7919) if water_noise else 0.0)
        out.append(HouseSample(when, reading, t_out, bt2 / minutes + wn[0], solar, bt3 / minutes + wn[1],
                               0.0, pump / minutes, hz / minutes))
    return out
