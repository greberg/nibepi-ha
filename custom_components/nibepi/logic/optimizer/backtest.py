"""Closed-loop backtest: replan on a schedule, apply the plan to a "true" house.

``backtest_heating`` walks through ``truth`` (real prices, real outdoor
temperatures, real solar), replanning every ``replan_every`` slots with
what the planner would have known: its own house model (may differ from
the truth), and an outdoor forecast (truth plus ``forecast_error``). The
chosen offsets are applied to the true house. The baseline holds the base
offset on the same true house. Costs are compared with the heat left in
the house at the end valued at the mean price (``Trajectory.energy_adjusted``).

The same function serves the unit tests (synthetic days) and
``tools/opt_backtest.py`` (exported Home Assistant history).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Sequence

from .energy import EnergyModel
from .heatcurve import HeatCurve
from .horizon import HorizonSlot
from .house import HouseModel
from .planner import ComfortSettings, PlannerSettings, Trajectory, plan_heating, simulate


@dataclass(frozen=True)
class BacktestResult:
    plan: Trajectory
    baseline: Trajectory
    offsets: List[int]
    replans: int
    max_plan_s: float

    @property
    def savings(self) -> float:
        return self.baseline.energy_adjusted - self.plan.energy_adjusted

    @property
    def savings_pct(self) -> float:
        base = self.baseline.energy_adjusted
        return 100.0 * self.savings / base if base > 0 else 0.0


def backtest_heating(
    truth: Sequence[HorizonSlot],
    t_in0: float,
    house_true: HouseModel,
    curve: HeatCurve,
    energy: EnergyModel,
    comfort: ComfortSettings = ComfortSettings(),
    settings: PlannerSettings = PlannerSettings(),
    house_plan: Optional[HouseModel] = None,
    replan_every: int = 4,
    lookahead: Optional[int] = None,
    forecast_error: Optional[Callable[[int, int], float]] = None,
    t_f0: Optional[float] = None,
) -> BacktestResult:
    """``forecast_error(plan_index, slot_index)`` °C added to the planner's t_out.

    A lagged true house (floor heating) carries its slab temperature from
    ``t_f0`` (default: settled at the base offset); the planner is told
    the true slab temperature at each replan, as the runtime estimates it
    from BT2. ``house_plan`` may be the one-node model of the same house,
    to see what ignoring the lag costs.
    """
    truth = list(truth)
    model = house_plan or house_true
    offsets: List[int] = []
    t = t_in0
    if t_f0 is None and truth:
        t_f0 = curve.supply(truth[0].t_out, settings.base_offset)
    t_f = t_f0
    prev: Optional[int] = settings.base_offset
    replans = 0
    worst = 0.0
    i = 0
    while i < len(truth):
        end = len(truth) if lookahead is None else min(len(truth), i + lookahead)
        seen = []
        for j in range(i, end):
            slot = truth[j]
            if forecast_error is not None and j > i:
                slot = replace(slot, t_out=slot.t_out + forecast_error(i, j))
            seen.append(slot)
        started = time.perf_counter()
        plan = plan_heating(seen, t, model, curve, energy, comfort, settings, current_offset=prev, t_f0=t_f)
        worst = max(worst, time.perf_counter() - started)
        replans += 1
        take = plan.offsets[:replan_every]
        # Apply to the true house.
        part = simulate(truth[i:i + len(take)], take, t, house_true, curve, energy, comfort, settings, prev,
                        t_f0=t_f)
        t = part.steps[-1].t_in_end
        t_f = part.steps[-1].slab_end
        offsets.extend(take)
        prev = take[-1]
        i += len(take)
    result = simulate(truth, offsets, t_in0, house_true, curve, energy, comfort, settings, settings.base_offset,
                      t_f0=t_f0)
    baseline = simulate(truth, [settings.base_offset] * len(truth), t_in0, house_true, curve, energy,
                        comfort, settings, settings.base_offset, t_f0=t_f0)
    return BacktestResult(result, baseline, offsets, replans, worst)
