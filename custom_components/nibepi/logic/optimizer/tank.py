"""Hot water: plan tank reheats into cheap slots ahead of expected draws.

Tank model (one node, the temperature the pump charges on, BT6 40014)::

    T' = amb + (T - amb) exp(-loss h) - draw / cap + charged / cap

- ``cap``: kWh per K (180 l ≈ 0.21 kWh/K; F1255-PC/-R have 180 l);
- ``loss``: standing loss rate, 1/h (≈ 0.012: ~1.7 kWh/day at 50 °C);
- ``charged``: up to ``reheat_kw × h`` while charging, never above the
  charge target; electricity = charged / COP with the condenser at the
  tank temperature plus a larger approach (hot water needs a higher
  condensing temperature than space heating).
- The pump's own control stays in charge underneath: while we "hold" (the
  comfort mode is economy, 47041 = 0) it still starts by itself when the
  tank falls below the economy start temperature and charges to the
  economy stop temperature. That is modelled, so the plan knows what the
  pump will do anyway.

Draws come as ``DrawForecast`` blocks (expected kWh or BT6 drop in °C, and
a confidence 0..1). To stay on the safe side a draw is planned as
``expected × (1 + risk × (1 - confidence))``. The second stage feeds them
from the VV learning profile (``draws_from_week_profile`` takes its 168
hour-of-week values as a plain list, no import of that module).

Planner: dynamic programming over a 0.5 °C tank grid with actions
hold/charge and the previous action in the state (a charge start costs
``start_cost``); a slot below ``floor`` costs ``floor_weight`` per K·h,
which outweighs any price. The baseline is the pump on its own in normal
mode (start/stop at the normal temperatures).

Output: per-slot steps and ``HotWaterBlock``s, each either ``charge``
(target temperature; stage 2 maps that to temporary lux 48132 when the
target is above the normal stop temperature, otherwise to comfort mode
normal) or ``hold`` (comfort mode economy).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import List, Optional, Sequence, Tuple

from .energy import KELVIN
from .horizon import HorizonSlot


@dataclass(frozen=True)
class DrawForecast:
    start: datetime
    end: datetime
    expected_kwh: Optional[float] = None
    temperature_drop: Optional[float] = None  # °C on BT6, as VV learning books it
    confidence: float = 1.0

    def kwh(self, capacity_kwh_per_k: float) -> float:
        if self.expected_kwh is not None:
            return max(0.0, self.expected_kwh)
        if self.temperature_drop is not None:
            return max(0.0, self.temperature_drop) * capacity_kwh_per_k
        return 0.0


def draws_from_week_profile(
    profile: Sequence[float],
    start: datetime,
    end: datetime,
    tz: tzinfo,
    confidence: float = 0.7,
    min_drop: float = 0.1,
) -> List[DrawForecast]:
    """168 hour-of-week BT6 drops (Monday 00:00 local = 0) -> hourly draws."""
    if len(profile) != 168:
        return []
    out: List[DrawForecast] = []
    t = datetime.fromtimestamp(int(start.timestamp()) // 3600 * 3600, timezone.utc)
    while t < end:
        local = t.astimezone(tz)
        drop = profile[local.weekday() * 24 + local.hour]
        nxt = t + timedelta(hours=1)
        if drop is not None and drop > min_drop:
            out.append(DrawForecast(t, nxt, None, float(drop), confidence))
        t = nxt
    return out


@dataclass(frozen=True)
class TankModel:
    capacity_kwh_per_k: float = 0.21
    loss_per_h: float = 0.012
    ambient: float = 20.0
    reheat_kw: float = 4.0
    carnot_eta: float = 0.5
    condenser_approach: float = 6.0
    evaporator_approach: float = 6.0
    brine_temp: float = 2.0

    def cop(self, t_tank: float, t_brine: Optional[float] = None) -> float:
        brine = self.brine_temp if t_brine is None else t_brine
        cond = t_tank + self.condenser_approach + KELVIN
        lift = cond - (brine - self.evaporator_approach + KELVIN)
        return max(1.3, min(5.0, self.carnot_eta * cond / max(1.0, lift)))


@dataclass(frozen=True)
class TankSettings:
    floor: float = 40.0  # BT6 must stay above this
    economy_start: float = 38.0  # pump starts by itself below (economy)
    economy_stop: float = 45.0  # 47049
    normal_start: float = 45.0
    normal_stop: float = 50.0  # 47048
    lux_stop: float = 55.0  # 47047
    charge_target: Optional[float] = None  # None: normal_stop, or lux when that is needed
    start_cost: float = 0.3
    floor_weight: float = 50.0  # per K h
    risk: float = 0.5
    grid_step: float = 0.5


@dataclass(frozen=True)
class TankStep:
    start: datetime
    end: datetime
    price: float
    charging: bool
    forced: bool  # the pump charged by itself (economy start)
    target: Optional[float]  # planned charge target, None while holding
    t_start: float
    t_end: float
    draw_kwh: float
    heat_kwh: float
    electric_kwh: float
    cost: float


@dataclass(frozen=True)
class HotWaterBlock:
    start: datetime
    end: datetime
    action: str  # "charge" or "hold"
    target: Optional[float]
    mode: int  # suggested 47041: 0 economy, 1 normal, 2 luxury
    boost: bool  # use temporary lux (48132) to start the charge
    heat_kwh: float
    cost: float
    reason: str


@dataclass(frozen=True)
class HotWaterPlan:
    steps: List[TankStep]
    blocks: List[HotWaterBlock]
    cost: float
    baseline_cost: float
    min_temp: float
    baseline_min_temp: float
    notes: List[str] = field(default_factory=list)

    @property
    def savings(self) -> float:
        return self.baseline_cost - self.cost


def slot_draws(
    slots: Sequence[HorizonSlot], draws: Sequence[DrawForecast], tank: TankModel, risk: float
) -> List[float]:
    """Planned draw kWh per slot (overlap-weighted, risk-inflated)."""
    out = [0.0] * len(slots)
    for draw in draws:
        total = draw.kwh(tank.capacity_kwh_per_k) * (1.0 + risk * (1.0 - max(0.0, min(1.0, draw.confidence))))
        d0, d1 = draw.start.timestamp(), draw.end.timestamp()
        span = max(1.0, d1 - d0)
        for i, slot in enumerate(slots):
            lo = max(d0, slot.start.timestamp())
            hi = min(d1, slot.end.timestamp())
            if hi > lo:
                out[i] += total * (hi - lo) / span
    return out


def _step(
    tank: TankModel, t: float, hours: float, draw: float, charge: bool, target: float, brine
) -> Tuple[float, float, float]:
    """(t_end, heat_kwh, electric_kwh)."""
    cap = tank.capacity_kwh_per_k
    t_free = tank.ambient + (t - tank.ambient) * math.exp(-tank.loss_per_h * hours) - draw / cap
    heat = 0.0
    if charge:
        heat = max(0.0, min(tank.reheat_kw * hours, cap * (target - t_free)))
    t_end = t_free + heat / cap
    el = heat / tank.cop(0.5 * (t + t_end), brine) if heat > 0 else 0.0
    return t_end, heat, el


def _simulate_pump(slots, draws_kwh, t0, tank, start_temp, stop_temp) -> List[TankStep]:
    """The pump on its own: start below ``start_temp``, charge to ``stop_temp``."""
    steps: List[TankStep] = []
    t = t0
    running = False
    for i, slot in enumerate(slots):
        if t < start_temp:
            running = True
        t_end, heat, el = _step(tank, t, slot.hours, draws_kwh[i], running, stop_temp, slot.t_brine)
        steps.append(TankStep(slot.start, slot.end, slot.price, running, running, None, t, t_end,
                              draws_kwh[i], heat, el, el * slot.price))
        if running and t_end >= stop_temp - 1e-6:
            running = False
        t = t_end
    return steps


HOLD, NORMAL, LUX = 0, 1, 2


def plan_hot_water(
    slots: Sequence[HorizonSlot],
    t_tank0: float,
    draws: Sequence[DrawForecast],
    tank: TankModel = TankModel(),
    settings: TankSettings = TankSettings(),
    tz: tzinfo = timezone.utc,
) -> HotWaterPlan:
    """Cheapest hold / charge-to-normal / charge-to-lux schedule.

    While holding, the pump may still start by itself when the tank is
    below the economy start temperature; that charge is modelled for the
    slot in which the tank is below it (the pump's hysteresis on to the
    economy stop is simplified away; the floor normally sits above the
    economy start, so the plan avoids that region anyway).
    """
    slots = list(slots)
    dk = slot_draws(slots, draws, tank, settings.risk)
    baseline = _simulate_pump(slots, dk, t_tank0, tank, settings.normal_start, settings.normal_stop)
    if not slots:
        return HotWaterPlan([], [], 0.0, 0.0, t_tank0, t_tank0, ["no price slots"])
    targets = {HOLD: settings.economy_stop, NORMAL: settings.charge_target or settings.normal_stop,
               LUX: max(settings.lux_stop, settings.charge_target or 0.0)}
    acts = (HOLD, NORMAL, LUX)
    step = settings.grid_step
    g_lo = min(settings.floor - 12.0, t_tank0 - 1.0)
    g_hi = max(targets[LUX], t_tank0) + 1.0
    n = int(math.ceil((g_hi - g_lo) / step)) + 1
    grid = [g_lo + i * step for i in range(n)]
    last = n - 1
    mean_price = sum(s.price * s.hours for s in slots) / max(1e-9, sum(s.hours for s in slots))
    ref = targets[NORMAL]
    # Hot water left at the end is worth what a cheap charge would cost then
    # (lower quartile), not the mean: otherwise the plan charges speculatively
    # at the end of the horizon.
    ordered = sorted(s.price for s in slots)
    heat_value = tank.capacity_kwh_per_k * ordered[len(ordered) // 4] / tank.cop(ref)

    def interp(vals, t):
        x = (t - g_lo) / step
        if x <= 0:
            return vals[0]
        if x >= last:
            return vals[last]
        j = int(x)
        return vals[j] + (vals[j + 1] - vals[j]) * (x - j)

    def stage(i, t, act):
        slot = slots[i]
        forced = act == HOLD and t < settings.economy_start
        t_end, heat, el = _step(tank, t, slot.hours, dk[i], act != HOLD or forced, targets[act], slot.t_brine)
        cost = el * slot.price
        if t_end < settings.floor:
            cost += settings.floor_weight * (settings.floor - t_end) * slot.hours
        return t_end, cost, heat, el, forced

    def start_cost(prev, act):
        return settings.start_cost if act != HOLD and prev == HOLD else 0.0

    terminal = [(ref - t) * heat_value for t in grid]
    values = [None] * (len(slots) + 1)
    values[len(slots)] = [terminal] * 3  # index: previous action
    for i in range(len(slots) - 1, -1, -1):
        nxt = values[i + 1]
        w = [[0.0] * n for _ in acts]
        for g, t in enumerate(grid):
            for act in acts:
                t_end, cost, _, _, _ = stage(i, t, act)
                w[act][g] = cost + interp(nxt[act], t_end)
        values[i] = [
            [min(w[act][g] + start_cost(prev, act) for act in acts) for g in range(n)] for prev in acts
        ]

    plan: List[int] = []
    t = t_tank0
    prev = HOLD
    for i in range(len(slots)):
        best = None
        for act in acts:
            t_end, cost, _, _, _ = stage(i, t, act)
            v = cost + start_cost(prev, act) + interp(values[i + 1][act], t_end)
            if best is None or v < best[0] - 1e-9:
                best = (v, act, t_end)
        plan.append(best[1])
        prev = best[1]
        t = best[2]

    steps: List[TankStep] = []
    t = t_tank0
    for i, slot in enumerate(slots):
        act = plan[i]
        t_end, cost, heat, el, forced = stage(i, t, act)
        # A planned charge that adds nothing (already at target) is shown as hold.
        planned = act != HOLD and heat > 1e-3
        steps.append(TankStep(slot.start, slot.end, slot.price, planned or forced, forced,
                              targets[act] if planned else None, t, t_end, dk[i], heat, el, el * slot.price))
        t = t_end
    cost = sum(s.cost for s in steps) + (ref - steps[-1].t_end) * heat_value
    base_cost = sum(s.cost for s in baseline) + (ref - baseline[-1].t_end) * heat_value
    blocks = _blocks(steps, settings, mean_price, tz)
    return HotWaterPlan(steps, blocks, cost, base_cost,
                        min([t_tank0] + [s.t_end for s in steps]),
                        min([t_tank0] + [s.t_end for s in baseline]))


def _blocks(
    steps: Sequence[TankStep], settings: TankSettings, mean_price: float, tz: tzinfo
) -> List[HotWaterBlock]:
    def key(s: TankStep) -> bool:
        return s.charging and not s.forced

    blocks: List[HotWaterBlock] = []
    i = 0
    while i < len(steps):
        j = i
        while j + 1 < len(steps) and key(steps[j + 1]) == key(steps[i]):
            j += 1
        chunk = steps[i:j + 1]
        heat = sum(s.heat_kwh for s in chunk)
        cost = sum(s.cost for s in chunk)
        avg = sum(s.price for s in chunk) / len(chunk)
        if key(steps[i]):
            target = max(s.target or 0.0 for s in chunk)
            later = [s for s in steps[j + 1:] if s.draw_kwh > 0.05]
            why = "price %.2f vs %.2f average" % (avg, mean_price)
            if later:
                why += ", before the draw expected at %s" % later[0].start.astimezone(tz).strftime("%H:%M")
            boost = target > settings.normal_stop + 0.1
            blocks.append(HotWaterBlock(chunk[0].start, chunk[-1].end, "charge", target, 2 if boost else 1,
                                        boost, heat, cost, why))
        else:
            forced = any(s.forced for s in chunk)
            why = "pump charges by itself below economy start" if forced else "waiting for cheaper slots"
            blocks.append(HotWaterBlock(chunk[0].start, chunk[-1].end, "hold", None, 0, False, heat, cost, why))
        i = j + 1
    return blocks
