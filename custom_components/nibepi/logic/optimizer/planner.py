"""Heating planner: dynamic programming over the price horizon.

State: indoor temperature on a grid (default 0.1 °C) and the offset chosen
in the previous slot. Action: the curve offset for the slot, an integer in
``base_offset - max_down .. base_offset + max_up`` (and within ±10).

Cost of a slot (currency, same unit as the prices, e.g. SEK):

- energy: price × electricity, electricity = heat / COP, heat from the
  emitter model at the supply the curve gives for that offset;
- comfort, per hour, on the indoor temperature at the end of the slot:
  ``tracking × (T - target)²`` everywhere (keeps the plan near the target
  when prices are flat; above the target ``tracking_above`` when set, which
  Away mode sets to 0), plus ``band_weight × x² + band_linear × x`` for
  ``x`` degrees outside ``[target - below, target + above]`` (much heavier
  below than above: 0.1 K under the band for an hour costs ~0.7, which is
  more than shifting can earn), plus ``floor_weight × x`` below the hard floor, which
  dwarfs any price so the floor wins;
- switching: ``change_cost + step_cost × |Δoffset|`` whenever the offset
  changes (no flapping, and the pump's own regulation gets time to act).

The comfort band may change over the horizon (Away mode with a known
return: the lowered band until the return, the normal one from then on).
``plan_heating(comforts=...)`` takes one ``ComfortSettings`` per slot,
applied to the temperature at the end of that slot; ``comfort_schedule``
builds them. The terminal value then uses the last slot's target.

Terminal value: heat left in the house at the end is worth what it would
cost to make later, ``(target - T_end) × C × mean price / COP``, so the
plan does not drain the house just before the horizon ends.

The backward pass computes, per slot and action, the stage cost and the
interpolated cost-to-go for every grid temperature (O(slots × actions ×
grid)), then takes the minimum over the previous offset with the
switching cost (O(slots × actions² × grid)). The forward pass replays the
policy from the real (continuous) start temperature, so the predicted
trajectory is not quantised.

Lagged houses (floor heating, ``house.tau_f >= LAG_PLANNER_MIN_H``)
-------------------------------------------------------------------

The slab temperature ``T_f`` is a second state (``house.py``): what the
offset does now shows up indoors hours later, and heat in the slab keeps
warming the room after the offset drops. A planner that ignores it heats
too late for an expensive peak and overshoots after it. ``_plan_lagged``
therefore runs the same DP on a 2-D grid, indoor temperature (default
0.2 °C) × slab temperature (0.5 °C), with bilinear interpolation of the
cost-to-go; the forward pass again replays the policy from the real
(continuous) indoor and slab temperatures.

- Discretising ``T_f`` keeps the DP exact in the sense the 1-D planner
  is: every grid state has its own optimal continuation. Carrying ``T_f``
  along candidate paths instead (one value per indoor grid point) would
  merge paths with different stored heat and pick the wrong one exactly
  when it matters, before a peak.
- To stay fast, offsets are decided per block of ``hold_slots`` slots
  (floor heating: 2 × 30 min, i.e. at most one change an hour; with hours
  of lag more frequent changes cannot do anything), the slab grid of each
  block covers only the supply temperatures reachable by then, and the
  switching cost uses a distance transform (O(actions) per state instead
  of O(actions²)).
- The terminal value also counts the heat in the slab: ``(T_ref - T_f) ×
  C_s`` with ``C_s = K_e × tau_d`` and ``T_ref`` the supply that holds the
  target at the horizon's last outdoor temperature.
- Energy: the pump's heat is the room's share plus what the slab stores,
  ``K_e ∫(T_f - T_in) dt + C_s ΔT_f`` (none while the slab coasts).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import List, Optional, Sequence, Tuple

from .energy import EnergyModel
from .heatcurve import OFFSET_LIMIT, HeatCurve
from .horizon import HorizonSlot
from .house import HouseModel, LagStep

# Below this emitter lag (h) the one-node planner is used: radiators react
# within a slot or two, and hourly fits put lags under an hour at about 1 h.
LAG_PLANNER_MIN_H = 1.25


@dataclass(frozen=True)
class ComfortSettings:
    target: float = 21.0
    below: float = 0.5  # no band penalty down to target - below
    above: float = 1.0  # ... and up to target + above
    hard_floor_below: float = 1.5  # floor = target - this
    tracking_weight: float = 1.0  # per K² h
    # Above the target, when set (Away: 0, warmer only costs energy).
    tracking_above: Optional[float] = None
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
        pen = (self.tracking_above if dev > 0 and self.tracking_above is not None else self.tracking_weight) * dev * dev
        if t < self.lower:
            x = self.lower - t
            pen += self.band_weight_below * x * x + self.band_linear_below * x
        elif t > self.upper:
            x = t - self.upper
            pen += self.band_weight_above * x * x + self.band_linear_above * x
        if t < self.floor:
            pen += self.floor_weight * (self.floor - t)
        return pen


def comfort_at(
    now: ComfortSettings,
    after: Optional[ComfortSettings],
    change_at: Optional[datetime],
    when: datetime,
    ramp_h: float = 0.0,
) -> ComfortSettings:
    """The comfort band in force at ``when``: ``now`` until ``change_at -
    ramp_h``, then linearly towards ``after``, which holds from ``change_at``."""
    if after is None or change_at is None:
        return now
    if when >= change_at:
        return after
    if ramp_h <= 0:
        return now
    ramp_start = change_at - timedelta(hours=ramp_h)
    if when <= ramp_start:
        return now
    f = (when - ramp_start).total_seconds() / (ramp_h * 3600.0)

    def mix(a: float, b: float) -> float:
        return a + (b - a) * f

    return replace(
        now,
        target=mix(now.target, after.target),
        below=mix(now.below, after.below),
        above=mix(now.above, after.above),
        hard_floor_below=mix(now.hard_floor_below, after.hard_floor_below),
        tracking_above=mix(
            now.tracking_above if now.tracking_above is not None else now.tracking_weight,
            after.tracking_above if after.tracking_above is not None else after.tracking_weight,
        ),
    )


def comfort_schedule(
    slots: Sequence["HorizonSlot"],
    now: ComfortSettings,
    after: Optional[ComfortSettings],
    change_at: Optional[datetime],
    ramp_h: float = 0.0,
) -> List[ComfortSettings]:
    """One comfort band per slot, at the slot's end (where it is penalised)."""
    return [comfort_at(now, after, change_at, slot.end, ramp_h) for slot in slots]


@dataclass(frozen=True)
class PlannerSettings:
    base_offset: int = 0
    max_down: int = 3
    max_up: int = 3
    change_cost: float = 0.05
    step_cost: float = 0.02
    grid_step: float = 0.1
    terminal_value: bool = True
    hold_slots: int = 1  # lagged planner: offsets change only every this many slots
    lag_grid_step: float = 0.2  # lagged planner: indoor grid, °C
    slab_grid_step: float = 0.5  # lagged planner: slab grid, °C

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
    slab_start: Optional[float] = None  # lagged model: slab temperature T_f
    slab_end: Optional[float] = None


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
    above_band_kh: float = 0.0  # degree-hours over the upper band edge

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

    def __init__(self, slots, house, curve, energy, comfort, settings, comforts=None):
        self.slots = slots
        self.house = house
        self.curve = curve
        self.energy = energy
        self.comfort = comfort
        self.settings = settings
        if comforts is None:
            self.comforts = [comfort] * len(slots)
        else:
            self.comforts = list(comforts)
            if len(self.comforts) != len(slots):
                raise ValueError("one comfort band per slot expected")
        # The band at the end of the horizon values the heat left over.
        self.end_comfort = self.comforts[-1] if self.comforts else comfort
        self.grid_low = min([c.floor for c in self.comforts] or [comfort.floor])
        self.grid_high = max([c.upper for c in self.comforts] or [comfort.upper])
        self.lagged = house.tau_f >= LAG_PLANNER_MIN_H
        prices = [s.price for s in slots]
        hours = [s.hours for s in slots]
        total_h = sum(hours) or 1.0
        self.mean_price = sum(p * h for p, h in zip(prices, hours)) / total_h
        mean_out = sum(s.t_out * s.hours for s in slots) / total_h if slots else 0.0
        steady = house.steady_supply(comfort.target, mean_out)
        self.cop_ref = energy.cop(max(steady, comfort.target))
        self.capacity = energy.emitter_kw_per_k / house.b  # kWh/K
        self.heat_value = self.capacity * self.mean_price / self.cop_ref  # per K at the end
        # Slab: capacity C_s = K_e tau_d, and the temperature that holds the
        # target at the end of the horizon.
        self.slab_capacity = energy.emitter_kw_per_k * house.drain_h if self.lagged else 0.0
        self.slab_value = self.slab_capacity * self.mean_price / self.cop_ref
        last_out = slots[-1].t_out if slots else mean_out
        self.slab_ref = house.steady_supply(self.end_comfort.target, last_out)
        self._steps: dict = {}

    def lag(self, hours: float) -> LagStep:
        key = round(hours, 6)
        step = self._steps.get(key)
        if step is None:
            step = self._steps[key] = LagStep(self.house, hours)
        return step

    def terminal(self, t: float, t_f: Optional[float] = None) -> float:
        if not self.settings.terminal_value:
            return 0.0
        value = (self.end_comfort.target - t) * self.heat_value
        if self.lagged and t_f is not None:
            value += (self.slab_ref - t_f) * self.slab_value
        return value

    def transition(self, slot: HorizonSlot, offset: int, t: float, t_f: Optional[float] = None):
        """(t_end, supply, cop, heat_kwh, electric_kwh, t_f_end) for one slot."""
        h = slot.hours
        supply = self.curve.supply(slot.t_out, offset)
        cop = self.energy.cop(supply, slot.t_brine)
        if self.lagged and t_f is not None:
            house = self.house
            free = house.a * slot.t_out + house.c * slot.solar + house.d
            t_end, f_end, heat = self.lag(h).step(t, t_f, free, supply)
            heat *= self.energy.emitter_kw_per_k
            return t_end, supply, cop, heat, heat / cop, f_end
        t_end = self.house.step(t, slot.t_out, supply, slot.solar, h)
        if supply > t:
            heat = self.energy.emitter_kw_per_k * max(0.0, supply - 0.5 * (t + t_end)) * h
        else:
            heat = 0.0
        return t_end, supply, cop, heat, heat / cop, None


def _simulate(
    ctx: _Ctx, offsets: Sequence[int], t_in0: float, prev: Optional[int], t_f0: Optional[float] = None
) -> Trajectory:
    steps: List[PlanStep] = []
    t = t_in0
    t_f = t_f0 if ctx.lagged else None
    energy_cost = comfort_cost = switch_cost = kwh = below = above = 0.0
    lo = hi = t_in0
    changes = 0
    for slot, offset, comfort in zip(ctx.slots, offsets, ctx.comforts):
        t_end, supply, cop, heat, el, f_end = ctx.transition(slot, offset, t, t_f)
        e_cost = el * slot.price
        c_cost = comfort.penalty_per_hour(t_end) * slot.hours
        s_cost = ctx.settings.switch_cost(prev, offset)
        if prev is not None and offset != prev:
            changes += 1
        steps.append(
            PlanStep(slot.start, slot.end, slot.price, slot.t_out, slot.solar, offset, supply, cop,
                     t, t_end, heat, el, e_cost, c_cost, t_f, f_end)
        )
        energy_cost += e_cost
        comfort_cost += c_cost
        switch_cost += s_cost
        kwh += el
        below += max(0.0, comfort.lower - t_end) * slot.hours
        above += max(0.0, t_end - comfort.upper) * slot.hours
        lo = min(lo, t_end)
        hi = max(hi, t_end)
        t = t_end
        t_f = f_end
        prev = offset
    return Trajectory(steps, energy_cost, comfort_cost, switch_cost, ctx.terminal(t, t_f), kwh, lo, hi,
                      below, changes, above)


def steady_slab(house: HouseModel, curve: HeatCurve, t_out: float, offset: int) -> float:
    """Slab temperature after holding ``offset`` for long: the curve's supply."""
    return curve.supply(t_out, offset)


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
    t_f0: Optional[float] = None,
    comforts: Optional[Sequence[ComfortSettings]] = None,
) -> Trajectory:
    """Run a given offset schedule through the model (baseline, backtests).

    A lagged house starts its slab at ``t_f0`` (default: settled at the
    first slot's supply for ``prev_offset``, or the first offset).
    ``comforts``: one band per slot (default ``comfort`` throughout).
    """
    ctx = _Ctx(list(slots), house, curve, energy, comfort, settings, comforts)
    if ctx.lagged and t_f0 is None and ctx.slots:
        first = prev_offset if prev_offset is not None else (offsets[0] if offsets else settings.base_offset)
        t_f0 = steady_slab(house, curve, ctx.slots[0].t_out, first)
    return _simulate(ctx, offsets, t_in0, prev_offset, t_f0)


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
    t_f0: Optional[float] = None,
    comforts: Optional[Sequence[ComfortSettings]] = None,
    baseline_offsets: Optional[Sequence[int]] = None,
) -> Plan:
    """Cheapest offset schedule for ``slots`` from indoor ``t_in0``.

    ``current_offset`` is what the register holds now (switching away from
    it costs like any change). The baseline is the base offset held all
    horizon long. A lagged house (floor heating) also needs the slab
    temperature now, ``t_f0`` (``optrun.slab_now`` estimates it from the
    history); without it the slab is taken as settled at the current
    offset's supply.

    ``comforts`` gives one comfort band per slot (Away mode's return; see
    ``comfort_schedule``); ``comfort`` then only sets the reference
    efficiency. ``baseline_offsets`` replaces the base offset held all
    horizon long as the baseline (one per slot).
    """
    import time as _time

    tick = clock or _time.perf_counter
    t0 = tick()
    slots = list(slots)
    ctx = _Ctx(slots, house, curve, energy, comfort, settings, comforts)
    if baseline_offsets is not None:
        base_line = [int(o) for o in baseline_offsets]
        if len(base_line) != len(slots):
            raise ValueError("one baseline offset per slot expected")
    else:
        base_line = [settings.base_offset] * len(slots)
    if ctx.lagged:
        if t_f0 is None and slots:
            held = current_offset if current_offset is not None else settings.base_offset
            t_f0 = steady_slab(house, curve, slots[0].t_out, held)
        return _plan_lagged(ctx, t_in0, t_f0, current_offset, tick, t0, base_line)
    actions = settings.offsets()
    n_act = len(actions)
    base = settings.base_offset
    baseline = _simulate(ctx, base_line, t_in0, current_offset)
    if not slots:
        return Plan(baseline, baseline, [], settings, 0.0, ["no price slots"])

    step = settings.grid_step
    g_lo = min(ctx.grid_low - 1.0, t_in0 - 0.5)
    g_hi = max(ctx.grid_high + 1.5, t_in0 + 0.5)
    n_grid = int(math.ceil((g_hi - g_lo) / step)) + 1
    grid = [g_lo + i * step for i in range(n_grid)]
    inv = 1.0 / step
    last = n_grid - 1

    sw = [[settings.switch_cost(p, a) for a in actions] for p in actions]

    a, b, c, d = house.a, house.b, house.c, house.d
    ke = energy.emitter_kw_per_k

    terminal = [ctx.terminal(t) for t in grid]
    values: List[List[List[float]]] = [None] * (len(slots) + 1)  # type: ignore[list-item]
    values[len(slots)] = [terminal] * n_act

    for k in range(len(slots) - 1, -1, -1):
        slot = slots[k]
        h = slot.hours
        nxt = values[k + 1]
        # Comfort constants of this slot, hoisted for the inner loop.
        cs = ctx.comforts[k]
        tgt, tw = cs.target, cs.tracking_weight
        twa = cs.tracking_above if cs.tracking_above is not None else tw
        lower, upper, floor = cs.lower, cs.upper, cs.floor
        wb, wa, fw = cs.band_weight_below, cs.band_weight_above, cs.floor_weight
        lb, la = cs.band_linear_below, cs.band_linear_above
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
                pen = (twa if dev > 0.0 else tw) * dev * dev
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
            tn, supply, cop, heat, el, _ = ctx.transition(slot, off, t)
            v = (el * slot.price + ctx.comforts[k].penalty_per_hour(tn) * slot.hours
                 + settings.switch_cost(prev, off) + interp(values[k + 1][ai], tn))
            if best_v is None or v < best_v - 1e-9:
                best_v, best_i, best_t = v, ai, tn
        chosen.append(actions[best_i])
        prev = actions[best_i]
        t = best_t

    result = _simulate(ctx, chosen, t_in0, current_offset)
    return Plan(result, baseline, chosen, settings, tick() - t0)


def _distance_min(q_rows: List[List[float]], change: float, step: float) -> List[List[float]]:
    """``out[p][x] = min_a q[a][x] + switch(p, a)`` for offsets in a row.

    ``switch(p, a)`` is 0 for a == p, else ``change + step × |a - p|``;
    a forward and a backward sweep give the ``step × |a - p|`` part in
    O(actions) per state.
    """
    n = len(q_rows)
    fwd = [q_rows[0]]
    for a in range(1, n):
        prev = fwd[-1]
        fwd.append([x if x < y + step else y + step for x, y in zip(q_rows[a], prev)])
    best = [None] * n  # type: ignore[list-item]
    best[n - 1] = fwd[n - 1]
    for a in range(n - 2, -1, -1):
        nxt = best[a + 1]
        best[a] = [x if x < y + step else y + step for x, y in zip(fwd[a], nxt)]
    return [[q if q < m + change else m + change for q, m in zip(q_rows[p], best[p])] for p in range(n)]


def _plan_lagged(
    ctx: _Ctx, t_in0: float, t_f0: float, current_offset: Optional[int], tick, t0, base_line: List[int]
) -> Plan:
    """The DP over (indoor, slab) for a lagged house; see the module docstring."""
    slots = ctx.slots
    settings = ctx.settings
    house, curve, energy = ctx.house, ctx.curve, ctx.energy
    actions = settings.offsets()
    n_act = len(actions)
    base = settings.base_offset
    baseline = _simulate(ctx, base_line, t_in0, current_offset, t_f0)
    if not slots:
        return Plan(baseline, baseline, [], settings, 0.0, ["no price slots"])

    hold = max(1, int(settings.hold_slots))
    blocks = [list(range(i, min(i + hold, len(slots)))) for i in range(0, len(slots), hold)]
    n_blocks = len(blocks)

    # Indoor grid (shared) and per-slot constants.
    step_in = settings.lag_grid_step
    g_lo = min(ctx.grid_low - 1.0, t_in0 - 0.5)
    g_hi = max(ctx.grid_high + 1.5, t_in0 + 0.5)
    n_in = int(math.ceil((g_hi - g_lo) / step_in)) + 1
    grid_in = [g_lo + i * step_in for i in range(n_in)]
    inv_in = 1.0 / step_in
    last_in = n_in - 1

    ke = energy.emitter_kw_per_k
    a_, c_, d_ = house.a, house.c, house.d
    td = house.drain_h
    # (hours, free, eq_coast, charge matrix, coast matrix, [(supply, eq_in, unit)], comfort constants)
    per_slot = []
    sup_lo: List[float] = []
    sup_hi: List[float] = []
    for slot in slots:
        lag = ctx.lag(slot.hours)
        free = a_ * slot.t_out + c_ * slot.solar + d_
        acts = []
        for off in actions:
            supply = curve.supply(slot.t_out, off)
            unit = slot.price * ke / energy.cop(supply, slot.t_brine)
            acts.append((supply, (free + lag.b * supply) / lag.k, unit))
        cs = ctx.comforts[len(per_slot)]
        per_slot.append((slot.hours, free, free / a_, lag.charge, lag.drain, acts,
                         (cs.target, cs.tracking_weight,
                          cs.tracking_above if cs.tracking_above is not None else cs.tracking_weight,
                          cs.lower, cs.upper, cs.floor, cs.band_weight_below,
                          cs.band_weight_above, cs.floor_weight, cs.band_linear_below, cs.band_linear_above)))
        sup_lo.append(min(x[0] for x in acts))
        sup_hi.append(max(x[0] for x in acts))

    # Slab grid at the start of each block: what is reachable by then.
    step_f = settings.slab_grid_step
    f_grids = []
    lo = hi = t_f0
    for k in range(n_blocks + 1):
        if k > 0:
            for i in blocks[k - 1]:
                lo = min(lo, sup_lo[i])
                hi = max(hi, sup_hi[i])
        f_lo = max(g_lo, lo - step_f)
        f_hi = max(f_lo + step_f, hi + step_f)
        n_f = int(math.ceil((f_hi - f_lo) / step_f)) + 1
        f_grids.append((f_lo, n_f))

    inv_f = 1.0 / step_f

    # Terminal values on the end grid.
    f_lo, n_f = f_grids[n_blocks]
    term = [ctx.terminal(grid_in[i], f_lo + j * step_f) for j in range(n_f) for i in range(n_in)]
    values: List[Optional[List[List[float]]]] = [None] * (n_blocks + 1)
    values[n_blocks] = [term] * n_act

    def block_run(k: int, ai: int, ti: float, tf: float):
        """(cost, T_in, T_f) at the end of block ``k`` with action ``ai``."""
        cost = 0.0
        for i in blocks[k]:
            h, free, eqd, cm, dm, acts, (tgt, tw, twa, lower, upper, floor, wb, wa, fw, lb, la) = per_slot[i]
            sup, eqi, unit = acts[ai]
            di = ti - eqi
            df = tf - sup
            ni = eqi + cm[0] * di + cm[1] * df
            nf = sup + cm[3] * df
            coasting = False
            if sup < tf:
                di2 = ti - eqd
                df2 = tf - eqd
                cf = eqd + dm[2] * di2 + dm[3] * df2
                if cf > nf:
                    ni = eqd + dm[0] * di2 + dm[1] * df2
                    nf = cf
                    coasting = True
            if not coasting:
                heat = 0.5 * (tf + nf - ti - ni) * h + td * (nf - tf)
                if heat > 0.0:
                    cost += unit * heat
            dev = ni - tgt
            pen = (twa if dev > 0.0 else tw) * dev * dev
            if ni < lower:
                x = lower - ni
                pen += wb * x * x + lb * x
                if ni < floor:
                    pen += fw * (floor - ni)
            elif ni > upper:
                x = ni - upper
                pen += wa * x * x + la * x
            cost += pen * h
            ti, tf = ni, nf
        return cost, ti, tf

    def interp(vals: List[float], k: int, ti: float, tf: float) -> float:
        f_lo, n_f = f_grids[k]
        x = (ti - g_lo) * inv_in
        if x <= 0.0:
            i0, wx = 0, 0.0
        elif x >= last_in:
            i0, wx = last_in - 1, 1.0
        else:
            i0 = int(x)
            wx = x - i0
        y = (tf - f_lo) * inv_f
        if y <= 0.0:
            j0, wy = 0, 0.0
        elif y >= n_f - 1:
            j0, wy = n_f - 2, 1.0
        else:
            j0 = int(y)
            wy = y - j0
        r0 = j0 * n_in + i0
        r1 = r0 + n_in
        v0 = vals[r0] + (vals[r0 + 1] - vals[r0]) * wx
        v1 = vals[r1] + (vals[r1 + 1] - vals[r1]) * wx
        return v0 + (v1 - v0) * wy

    for k in range(n_blocks - 1, 0, -1):
        f_lo, n_f = f_grids[k]
        nxt = values[k + 1]
        nf_lo, nn_f = f_grids[k + 1]
        rows = []
        for ai in range(n_act):
            vn = nxt[ai]  # type: ignore[index]
            row = []
            append = row.append
            for j in range(n_f):
                tf0 = f_lo + j * step_f
                for i in range(n_in):
                    cost, ti, tf = block_run(k, ai, grid_in[i], tf0)
                    # Bilinear interpolation of the next block's values (inlined).
                    x = (ti - g_lo) * inv_in
                    if x <= 0.0:
                        i0, wx = 0, 0.0
                    elif x >= last_in:
                        i0, wx = last_in - 1, 1.0
                    else:
                        i0 = int(x)
                        wx = x - i0
                    y = (tf - nf_lo) * inv_f
                    if y <= 0.0:
                        j0, wy = 0, 0.0
                    elif y >= nn_f - 1:
                        j0, wy = nn_f - 2, 1.0
                    else:
                        j0 = int(y)
                        wy = y - j0
                    r0 = j0 * n_in + i0
                    r1 = r0 + n_in
                    v0 = vn[r0] + (vn[r0 + 1] - vn[r0]) * wx
                    v1 = vn[r1] + (vn[r1 + 1] - vn[r1]) * wx
                    append(cost + v0 + (v1 - v0) * wy)
            rows.append(row)
        values[k] = _distance_min(rows, settings.change_cost, settings.step_cost)

    # Forward pass from the real indoor and slab temperatures.
    chosen: List[int] = []
    ti, tf = t_in0, t_f0
    prev = current_offset
    for k in range(n_blocks):
        start_ref = prev if prev is not None else base
        order = sorted(range(n_act), key=lambda i: (abs(actions[i] - start_ref), abs(actions[i] - base), actions[i]))
        best = None
        for ai in order:
            cost, ni, nf = block_run(k, ai, ti, tf)
            cost += settings.switch_cost(prev, actions[ai])
            if k + 1 < n_blocks:
                cost += interp(values[k + 1][ai], k + 1, ni, nf)  # type: ignore[index]
            else:
                cost += ctx.terminal(ni, nf)
            if best is None or cost < best[0] - 1e-9:
                best = (cost, ai, ni, nf)
        assert best is not None
        _, ai, ti, tf = best
        chosen.extend([actions[ai]] * len(blocks[k]))
        prev = actions[ai]

    result = _simulate(ctx, chosen, t_in0, current_offset, t_f0)
    return Plan(result, baseline, chosen, settings, tick() - t0)
