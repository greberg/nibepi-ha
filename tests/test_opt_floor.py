"""The lag-aware planner on a floor-heated house."""

from __future__ import annotations

import math
import random
import time
import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.optimizer.backtest import backtest_heating
from logic.optimizer.heatcurve import HeatCurve
from logic.optimizer.horizon import HorizonSlot
from logic.optimizer.planner import (
    LAG_PLANNER_MIN_H,
    ComfortSettings,
    PlannerSettings,
    _distance_min,
    _Ctx,
    neutral_offset,
    plan_heating,
    simulate,
)
from logic.optimizer.profile import HouseProfile, house_prior

CURVE = HeatCurve(curve=5)
PRIOR = house_prior(HouseProfile(floor_area_m2=150, floors=1.5, latitude=59.3, emitter="floor"), CURVE)
HOUSE = PRIOR.model  # tau_f 3 h, tau_d 9 h
ENERGY = PRIOR.energy_model()
COMFORT = ComfortSettings(target=21.0, sensor_margin=PRIOR.sensor_margin)
START = utc(2026, 1, 14, 23)  # 2026-01-15 00:00 Stockholm
SE3_DAY = [0.92, 0.88, 0.85, 0.84, 0.86, 0.95, 1.35, 2.10, 2.45, 2.05, 1.70, 1.55,
           1.48, 1.45, 1.52, 1.70, 2.05, 2.60, 2.75, 2.30, 1.80, 1.45, 1.20, 1.02]
BASE = neutral_offset(HOUSE, CURVE, -6.0, 21.0)
FLOOR_SETTINGS = PlannerSettings(base_offset=BASE, change_cost=0.3, step_cost=0.05, hold_slots=2)


def half_hours(hours: int, prices=None, t_out=None):
    out = []
    for i in range(hours * 2):
        a = START + timedelta(minutes=30 * i)
        hour = i // 2
        price = prices[hour] if prices else SE3_DAY[hour % 24]
        temp = t_out(hour) if t_out else -6 + 3 * math.sin((hour % 24 - 9) / 24 * 2 * math.pi)
        out.append(HorizonSlot(a, a + timedelta(minutes=30), price, temp))
    return out


class LagPlannerTest(unittest.TestCase):
    def test_floor_house_uses_the_lagged_planner(self) -> None:
        self.assertGreaterEqual(HOUSE.tau_f, LAG_PLANNER_MIN_H)
        plan = plan_heating(half_hours(24), 21.0, HOUSE, CURVE, ENERGY, COMFORT, FLOOR_SETTINGS,
                            current_offset=BASE, t_f0=CURVE.supply(-6, BASE))
        self.assertIsNotNone(plan.plan.steps[0].slab_end)
        radiators = house_prior(HouseProfile(floor_area_m2=150, floors=1.5, latitude=59.3), CURVE).model
        plan = plan_heating(half_hours(24), 21.0, radiators, CURVE, ENERGY, COMFORT, FLOOR_SETTINGS)
        self.assertIsNone(plan.plan.steps[0].slab_end)

    def test_preheats_the_slab_before_the_peak(self) -> None:
        # Flat cheap night, a 4 h peak from 08:00, then normal.
        prices = [0.6] * 8 + [3.0] * 4 + [1.2] * 24
        plan = plan_heating(half_hours(36, prices, lambda h: -6.0), 21.0, HOUSE, CURVE, ENERGY, COMFORT,
                            FLOOR_SETTINGS, current_offset=BASE, t_f0=CURVE.supply(-6, BASE))
        offsets = [s.offset for s in plan.plan.steps]
        before = offsets[2 * 3:2 * 8]  # 03:00-08:00
        peak = offsets[2 * 8:2 * 12]
        self.assertGreater(sum(before) / len(before), BASE)
        self.assertLess(sum(peak) / len(peak), BASE)
        # The heat goes in before the peak: the slab is warmest when it starts.
        slab = [s.slab_end for s in plan.plan.steps]
        self.assertEqual(max(slab[:2 * 12]), max(slab[2 * 6:2 * 9]))
        # Comfort holds through the peak and after it.
        self.assertGreaterEqual(plan.plan.min_t_in, COMFORT.lower - 0.02)
        self.assertLessEqual(plan.plan.max_t_in, COMFORT.upper)
        self.assertGreater(plan.savings, 0)
        # At most one change per hour (1 h hold) and few of them.
        self.assertLessEqual(plan.plan.changes, 6)
        for i in range(1, len(offsets), 2):
            self.assertEqual(offsets[i], offsets[i - 1])

    def test_terminal_value_counts_the_slab(self) -> None:
        slots = half_hours(24)
        ctx = _Ctx(slots, HOUSE, CURVE, ENERGY, COMFORT, FLOOR_SETTINGS)
        self.assertLess(ctx.terminal(21.0, 32.0), ctx.terminal(21.0, 28.0))
        stored = (ctx.terminal(21.0, 28.0) - ctx.terminal(21.0, 32.0)) / 4.0
        self.assertAlmostEqual(stored, ENERGY.emitter_kw_per_k * HOUSE.drain_h * ctx.mean_price / ctx.cop_ref)
        # Without it the plan drains the slab before the horizon ends.
        drained = plan_heating(slots, 21.0, HOUSE, CURVE, ENERGY, COMFORT,
                               PlannerSettings(base_offset=BASE, change_cost=0.3, step_cost=0.05, hold_slots=2,
                                               terminal_value=False),
                               current_offset=BASE, t_f0=CURVE.supply(-6, BASE))
        kept = plan_heating(slots, 21.0, HOUSE, CURVE, ENERGY, COMFORT, FLOOR_SETTINGS,
                            current_offset=BASE, t_f0=CURVE.supply(-6, BASE))
        self.assertLess(drained.plan.steps[-1].slab_end, kept.plan.steps[-1].slab_end - 1.0)

    def test_energy_matches_steady_state(self) -> None:
        slots = half_hours(12, [1.0] * 12, lambda h: -6.0)
        supply = CURVE.supply(-6.0, BASE)
        t_in = HOUSE.one_node().step(21.0, -6.0, supply, 0.0, 1000.0)  # settled
        lagged = simulate(slots, [BASE] * len(slots), t_in, HOUSE, CURVE, ENERGY, COMFORT, FLOOR_SETTINGS,
                          BASE, t_f0=supply)
        one = simulate(slots, [BASE] * len(slots), t_in, HOUSE.one_node(), CURVE, ENERGY, COMFORT,
                       FLOOR_SETTINGS, BASE)
        self.assertAlmostEqual(lagged.electric_kwh, one.electric_kwh, delta=0.01 * one.electric_kwh)

    def test_distance_transform_is_the_switching_minimum(self) -> None:
        rnd = random.Random(1)
        q = [[rnd.uniform(0, 3) for _ in range(5)] for _ in range(7)]
        settings = PlannerSettings(base_offset=0, change_cost=0.3, step_cost=0.05)
        got = _distance_min(q, settings.change_cost, settings.step_cost)
        for p in range(7):
            for x in range(5):
                want = min(q[a][x] + settings.switch_cost(p - 3, a - 3) for a in range(7))
                self.assertAlmostEqual(got[p][x], want)

    def test_fast_enough_for_a_pi(self) -> None:
        # 36 h of 30-minute slots with a cold, varying forecast (wider slab
        # grid). A Pi 4 is ~6-10x slower than a laptop; it must stay < 5 s.
        slots = half_hours(36, t_out=lambda h: -10 + 6 * math.sin(h / 36 * math.pi))
        started = time.perf_counter()
        plan = plan_heating(slots, 21.0, HOUSE, CURVE, ENERGY, COMFORT, FLOOR_SETTINGS,
                            current_offset=BASE, t_f0=CURVE.supply(-10, BASE))
        elapsed = time.perf_counter() - started
        self.assertEqual(len(plan.offsets), 72)
        self.assertLess(elapsed, 1.5)


def quarter_hours(hours: int):
    out = []
    for slot in half_hours(hours):
        mid = slot.start + timedelta(minutes=15)
        out.append(HorizonSlot(slot.start, mid, slot.price, slot.t_out))
        out.append(HorizonSlot(mid, slot.end, slot.price, slot.t_out))
    return out


# The one-node planner as 0.2.0 ran it: 15-minute slots, default switching.
ONE_NODE_SETTINGS = PlannerSettings(base_offset=BASE)


class LagBacktestTest(unittest.TestCase):
    """A floor-heated house: lag-aware vs the one-node planner on it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lagged = backtest_heating(half_hours(36), 21.0, HOUSE, CURVE, ENERGY, COMFORT, FLOOR_SETTINGS,
                                      replan_every=4, lookahead=48)
        cls.one_node = backtest_heating(quarter_hours(36), 21.0, HOUSE, CURVE, ENERGY, COMFORT, ONE_NODE_SETTINGS,
                                        house_plan=HOUSE.one_node(), replan_every=8, lookahead=96)

    def test_saves_at_least_as_much(self) -> None:
        self.assertGreater(self.lagged.savings_pct, 10.0)
        self.assertGreater(self.lagged.savings_pct, self.one_node.savings_pct - 1.0)

    def test_comfort_is_respected(self) -> None:
        plan = self.lagged.plan
        self.assertGreaterEqual(plan.min_t_in, COMFORT.lower - 0.02)
        self.assertLessEqual(plan.max_t_in, COMFORT.upper)
        self.assertEqual(plan.below_band_kh, 0.0)

    def test_no_deeper_dips(self) -> None:
        # Hourly replanning from the measured indoor temperature lets the
        # one-node plan get by on cost; it cuts closer to the band though.
        self.assertGreaterEqual(self.lagged.plan.min_t_in, self.one_node.plan.min_t_in)
        self.assertLessEqual(self.lagged.plan.max_t_in, self.one_node.plan.max_t_in)

    def test_predictions_hold(self) -> None:
        # Open loop: what the plan predicts vs what the floor-heated house
        # does with its offsets. The one-node plan's night heat arrives in
        # the day, it predicts the house too cold then too warm.
        t_f0 = CURVE.supply(-6, BASE)
        for slots, model, settings, worst in (
            (half_hours(24), HOUSE, FLOOR_SETTINGS, 0.01),
            (quarter_hours(24), HOUSE.one_node(), ONE_NODE_SETTINGS, None),
        ):
            plan = plan_heating(slots, 21.0, model, CURVE, ENERGY, COMFORT, settings, current_offset=BASE,
                                t_f0=t_f0)
            real = simulate(slots, plan.offsets, 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings, BASE, t_f0=t_f0)
            error = max(abs(r.t_in_end - p.t_in_end) for r, p in zip(real.steps, plan.plan.steps))
            if worst is not None:
                self.assertLess(error, worst)
            else:
                self.assertGreater(error, 0.3)


if __name__ == "__main__":
    unittest.main()
