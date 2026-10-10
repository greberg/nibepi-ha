"""Heating planner: dynamic programming over the price horizon.

State: indoor temperature on a grid (default 0.1 °C) and the offset chosen
in the previous slot. Action: the curve offset for the slot, an integer in
``base_offset - max_down .. base_offset + max_up`` (and within ±10).

Cost of a slot (currency, same unit as the prices, e.g. SEK):

- energy: price × electricity, electricity = heat / COP, heat from the
  emitter model at the supply the curve gives for that offset;
- comfort, per hour, on the indoor temperature at the end of the slot:
  ``tracking × (T - target)²`` everywhere (keeps the plan near the target
  when prices are flat), plus ``band_weight × x² + band_linear × x`` for
  ``x`` degrees outside ``[target - below, target + above]`` (much heavier
  below than above: 0.1 K under the band for an hour costs ~0.7, which is
  more than shifting can earn), plus ``floor_weight × x`` below the hard floor, which
  dwarfs any price so the floor wins;
- switching: ``change_cost + step_cost × |Δoffset|`` whenever the offset
  changes (no flapping, and the pump's own regulation gets time to act).

Terminal value: heat left in the house at the end is worth what it would
cost to make later, ``(target - T_end) × C × mean price / COP``, so the
plan does not drain the house just before the horizon ends.

The backward pass computes, per slot and action, the stage cost and the
interpolated cost-to-go for every grid temperature (O(slots × actions ×
grid)), then takes the minimum over the previous offset with the
switching cost (O(slots × actions² × grid)). The forward pass replays the
policy from the real (continuous) start temperature, so the predicted
trajectory is not quantised.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

from .energy import EnergyModel
from .heatcurve import OFFSET_LIMIT, HeatCurve
from .horizon import HorizonSlot
from .house import HouseModel


@dataclass(frozen=True)
class ComfortSettings:
    target: float = 21.0
    below: float = 0.5  # no band penalty down to target - below
    above: float = 1.0  # ... and up to target + above
    hard_floor_below: float = 1.5  # floor = target - this
    tracking_weight: float = 1.0  # per K² h
    band_weight_below: float = 20.0  # per K² h
    band_weight_above: float = 2.0  # per K² h
    band_linear_below: float = 5.0  # per K h under the band
    band_linear_above: float = 1.0  # per K h over the band
    floor_weight: float = 200.0  # per K h below the floor
    sensor_margin: float = 0.0  # raises the lower edge and floor (profile.sensor_margin)

    @property
    def lower(self) -> float:
        return self.target - self.below + self.sensor_margin

    @property
    def upper(self) -> float:
        return self.target + self.above

    @property
    def floor(self) -> float:
        return self.target - self.hard_floor_below + self.sensor_margin

    def penalty_per_hour(self, t: float) -> float:
        dev = t - self.target
        pen = self.tracking_weight * dev * dev
        if t < self.lower:
            x = self.lower - t
            pen += self.band_weight_below * x * x + self.band_linear_below * x
        elif t > self.upper:
            x = t - self.upper
            pen += self.band_weight_above * x * x + self.band_linear_above * x
        if t < self.floor:
            pen += self.floor_weight * (self.floor - t)
        return pen


@dataclass(frozen=True)
class PlannerSettings:
    base_offset: int = 0
    max_down: int = 3
    max_up: int = 3
    change_cost: float = 0.05
    step_cost: float = 0.02
    grid_step: float = 0.1
    terminal_value: bool = True

    def offsets(self) -> List[int]:
        lo = max(-OFFSET_LIMIT, self.base_offset - self.max_down)
        hi = min(OFFSET_LIMIT, self.base_offset + self.max_up)
        return list(range(lo, hi + 1))

    def switch_cost(self, prev: Optional[int], new: int) -> float:
        if prev is None or prev == new:
            return 0.0
        return self.change_cost + self.step_cost * abs(new - prev)


@dataclass(frozen=True)
class PlanStep:
    start: datetime
    end: datetime
    price: float
    t_out: float
    solar: float
    offset: int
    supply: float
    cop: float
    t_in_start: float
    t_in_end: float
    heat_kwh: float
    electric_kwh: float
    energy_cost: float
    comfort_cost: float


@dataclass(frozen=True)
class Trajectory:
    steps: List[PlanStep]
    energy_cost: float
    comfort_cost: float
    switch_cost: float
    terminal_cost: float  # value of heat left (negative = stored heat is worth something)
    electric_kwh: float
    min_t_in: float
    max_t_in: float
    below_band_kh: float  # degree-hours under the lower band edge
    changes: int

    @property
    def total(self) -> float:
        return self.energy_cost + self.comfort_cost + self.switch_cost + self.terminal_cost

    @property
    def energy_adjusted(self) -> float:
        """Energy cost corrected for the heat left at the end."""
        return self.energy_cost + self.terminal_cost


@dataclass(frozen=True)
class Plan:
    plan: Trajectory
    baseline: Trajectory
    offsets: List[int]
    settings: PlannerSettings
    elapsed_s: float = 0.0
    notes: List[str] = field(default_factory=list)

    @property
    def savings(self) -> float:
        """Energy cost saved vs. the baseline, corrected for heat left over."""
        return self.baseline.energy_adjusted - self.plan.energy_adjusted

    @property
    def first_offset(self) -> Optional[int]:
        return self.plan.steps[0].offset if self.plan.steps else None


class _Ctx:
    """Per-horizon constants shared by the backward and forward passes."""

    def __init__(self, slots, house, curve, energy, comfort, settings):
        self.slots = slots
        self.house = house
        self.curve = curve
        self.energy = energy
        self.comfort = comfort
        self.settings = settings
        prices = [s.price for s in slots]
        hours = [s.hours for s in slots]
        total_h = sum(hours) or 1.0
        self.mean_price = sum(p * h for p, h in zip(prices, hours)) / total_h
        mean_out = sum(s.t_out * s.hours for s in slots) / total_h if slots else 0.0
        steady = house.steady_supply(comfort.target, mean_out)
        self.cop_ref = energy.cop(max(steady, comfort.target))
        self.capacity = energy.emitter_kw_per_k / house.b  # kWh/K
        self.heat_value = self.capacity * self.mean_price / self.cop_ref  # per K at the end

    def terminal(self, t: float) -> float:
        if not self.settings.terminal_value:
            return 0.0
        return (self.comfort.target - t) * self.heat_value

    def transition(self, slot: HorizonSlot, offset: int, t: float):
        """(t_end, supply, cop, heat_kwh, electric_kwh) for one slot."""
        h = slot.hours
        supply = self.curve.supply(slot.t_out, offset)
        t_end = self.house.step(t, slot.t_out, supply, slot.solar, h)
        cop = self.energy.cop(supply, slot.t_brine)
        if supply > t:
            heat = self.energy.emitter_kw_per_k * max(0.0, supply - 0.5 * (t + t_end)) * h
        else:
            heat = 0.0
        return t_end, supply, cop, heat, heat / cop


def _simulate(ctx: _Ctx, offsets: Sequence[int], t_in0: float, prev: Optional[int]) -> Trajectory:
    steps: List[PlanStep] = []
    t = t_in0
    energy_cost = comfort_cost = switch_cost = kwh = below = 0.0
    lo = hi = t_in0
    changes = 0
    comfort = ctx.comfort
    for slot, offset in zip(ctx.slots, offsets):
        t_end, supply, cop, heat, el = ctx.transition(slot, offset, t)
        e_cost = el * slot.price
        c_cost = comfort.penalty_per_hour(t_end) * slot.hours
        s_cost = ctx.settings.switch_cost(prev, offset)
        if prev is not None and offset != prev:
            changes += 1
        steps.append(
            PlanStep(slot.start, slot.end, slot.price, slot.t_out, slot.solar, offset, supply, cop,
                     t, t_end, heat, el, e_cost, c_cost)
        )
        energy_cost += e_cost
        comfort_cost += c_cost
        switch_cost += s_cost
        kwh += el
        below += max(0.0, comfort.lower - t_end) * slot.hours
        lo = min(lo, t_end)
        hi = max(hi, t_end)
        t = t_end
        prev = offset
    return Trajectory(steps, energy_cost, comfort_cost, switch_cost, ctx.terminal(t), kwh, lo, hi,
                      below, changes)


def simulate(
    slots: Sequence[HorizonSlot],
    offsets: Sequence[int],
    t_in0: float,
    house: HouseModel,
    curve: HeatCurve,
    energy: EnergyModel,
    comfort: ComfortSettings = ComfortSettings(),
    settings: PlannerSettings = PlannerSettings(),
    prev_offset: Optional[int] = None,
) -> Trajectory:
    """Run a given offset schedule through the model (baseline, backtests)."""
    ctx = _Ctx(list(slots), house, curve, energy, comfort, settings)
    return _simulate(ctx, offsets, t_in0, prev_offset)


def neutral_offset(
    house: HouseModel, curve: HeatCurve, t_out: float, target: float, solar: float = 0.0
) -> int:
    """Offset that holds ``target`` at ``t_out`` according to the model."""
    supply = house.steady_supply(target, t_out, solar)
    value = curve.offset_for_supply(t_out, supply)
    return max(-OFFSET_LIMIT, min(OFFSET_LIMIT, int(math.floor(value + 0.5))))


def plan_heating(
    slots: Sequence[HorizonSlot],
    t_in0: float,
    house: HouseModel,
    curve: HeatCurve,
    energy: EnergyModel,
    comfort: ComfortSettings = ComfortSettings(),
    settings: PlannerSettings = PlannerSettings(),
    current_offset: Optional[int] = None,
    clock=None,
) -> Plan:
    """Cheapest offset schedule for ``slots`` from indoor ``t_in0``.

    ``current_offset`` is what the register holds now (switching away from
    it costs like any change). The baseline is the base offset held all
    horizon long.
    """
    import time as _time

    tick = clock or _time.perf_counter
    t0 = tick()
    slots = list(slots)
    ctx = _Ctx(slots, house, curve, energy, comfort, settings)
    actions = settings.offsets()
    n_act = len(actions)
    base = settings.base_offset
    baseline = _simulate(ctx, [base] * len(slots), t_in0, current_offset)
    if not slots:
        return Plan(baseline, baseline, [], settings, 0.0, ["no price slots"])

    step = settings.grid_step
    g_lo = min(comfort.floor - 1.0, t_in0 - 0.5)
    g_hi = max(comfort.upper + 1.5, t_in0 + 0.5)
    n_grid = int(math.ceil((g_hi - g_lo) / step)) + 1
    grid = [g_lo + i * step for i in range(n_grid)]
    inv = 1.0 / step
    last = n_grid - 1

    sw = [[settings.switch_cost(p, a) for a in actions] for p in actions]

    # Comfort constants hoisted for the inner loop.
    tgt, tw = comfort.target, comfort.tracking_weight
    lower, upper, floor = comfort.lower, comfort.upper, comfort.floor
    wb, wa, fw = comfort.band_weight_below, comfort.band_weight_above, comfort.floor_weight
    lb, la = comfort.band_linear_below, comfort.band_linear_above
    a, b, c, d = house.a, house.b, house.c, house.d
    ke = energy.emitter_kw_per_k

    terminal = [ctx.terminal(t) for t in grid]
    values: List[List[List[float]]] = [None] * (len(slots) + 1)  # type: ignore[list-item]
    values[len(slots)] = [terminal] * n_act

    for k in range(len(slots) - 1, -1, -1):
        slot = slots[k]
        h = slot.hours
        nxt = values[k + 1]
        free = a * slot.t_out + c * slot.solar + d
        eq_off = free / a
        al_off = math.exp(-a * h)
        al_on = math.exp(-(a + b) * h)
        rows = []
        for ai, off in enumerate(actions):
            supply = curve.supply(slot.t_out, off)
            eq_on = (free + b * supply) / (a + b)
            unit = slot.price * ke * h / energy.cop(supply, slot.t_brine)
            vn = nxt[ai]
            row = [0.0] * n_grid
            for i in range(n_grid):
                t = grid[i]
                if supply > t:
                    tn = eq_on + (t - eq_on) * al_on
                    cost = unit * (supply - 0.5 * (t + tn))
                    if cost < 0.0:
                        cost = 0.0
                else:
                    tn = eq_off + (t - eq_off) * al_off
                    cost = 0.0
                dev = tn - tgt
                pen = tw * dev * dev
                if tn < lower:
                    x = lower - tn
                    pen += wb * x * x + lb * x
                    if tn < floor:
                        pen += fw * (floor - tn)
                elif tn > upper:
                    x = tn - upper
                    pen += wa * x * x + la * x
                x = (tn - g_lo) * inv
                if x <= 0.0:
                    v = vn[0]
                elif x >= last:
                    v = vn[last]
                else:
                    j = int(x)
                    v = vn[j] + (vn[j + 1] - vn[j]) * (x - j)
                row[i] = cost + pen * h + v
            rows.append(row)
        cols = list(zip(*rows))  # per grid point: cost of each action
        values[k] = [
            [min([w + s for w, s in zip(col, sw_p)]) for col in cols] for sw_p in sw
        ]

    # Forward pass from the real start temperature.
    def interp(vals: List[float], t: float) -> float:
        x = (t - g_lo) * inv
        if x <= 0.0:
            return vals[0]
        if x >= last:
            return vals[last]
        j = int(x)
        return vals[j] + (vals[j + 1] - vals[j]) * (x - j)

    chosen: List[int] = []
    t = t_in0
    prev = current_offset
    for k, slot in enumerate(slots):
        start_ref = prev if prev is not None else base
        order = sorted(range(n_act), key=lambda i: (abs(actions[i] - start_ref), abs(actions[i] - base), actions[i]))
        best_i = order[0]
        best_v = None
        best_t = t
        for ai in order:
            off = actions[ai]
            tn, supply, cop, heat, el = ctx.transition(slot, off, t)
            v = (el * slot.price + comfort.penalty_per_hour(tn) * slot.hours
                 + settings.switch_cost(prev, off) + interp(values[k + 1][ai], tn))
            if best_v is None or v < best_v - 1e-9:
                best_v, best_i, best_t = v, ai, tn
        chosen.append(actions[best_i])
        prev = actions[best_i]
        t = best_t

    result = _simulate(ctx, chosen, t_in0, current_offset)
    return Plan(result, baseline, chosen, settings, tick() - t0)
