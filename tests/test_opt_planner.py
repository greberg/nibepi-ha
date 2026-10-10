from __future__ import annotations

import math
import time
import unittest
from datetime import date, datetime, timedelta

import context  # noqa: F401
from context import utc
from logic.inputs import ForecastPoint, PriceSlot
from logic.optimizer.explain import explain_plan, summary
from logic.optimizer.gate import gate
from logic.optimizer.heatcurve import HeatCurve
from logic.optimizer.horizon import HorizonSlot, build_horizon
from logic.optimizer.house import HouseFit
from logic.optimizer.planner import (
    ComfortSettings,
    PlannerSettings,
    neutral_offset,
    plan_heating,
    simulate,
)
from logic.optimizer.profile import HouseProfile, house_prior
from logic.price import slots_from_numbers

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

CURVE = HeatCurve(curve=9)
PRIOR = house_prior(HouseProfile(floor_area_m2=150, floors=1, latitude=59.3), CURVE)
HOUSE = PRIOR.model
ENERGY = PRIOR.energy_model()
COMFORT = ComfortSettings(target=21.0)
START = utc(2026, 1, 14, 23)  # local midnight in Stockholm

# A winter weekday in SE3, SEK/kWh incl. energy tax, grid fee and VAT
# (illustrative shape: night low, morning and evening peaks).
SE3_DAY = [0.92, 0.88, 0.85, 0.84, 0.86, 0.95, 1.35, 2.10, 2.45, 2.05, 1.70, 1.55,
           1.48, 1.45, 1.52, 1.70, 2.05, 2.60, 2.75, 2.30, 1.80, 1.45, 1.20, 1.02]


def quarter_slots(prices, t_out=-6.0, start=START):
    out = []
    for i, p in enumerate(prices):
        a = start + timedelta(minutes=15 * i)
        out.append(HorizonSlot(a, a + timedelta(minutes=15), p, t_out))
    return out


def hourly_to_quarters(hourly):
    return [p for p in hourly for _ in range(4)]


def base_settings(**kw) -> PlannerSettings:
    return PlannerSettings(base_offset=neutral_offset(HOUSE, CURVE, -6.0, 21.0), **kw)


class PlannerTest(unittest.TestCase):
    def test_shifts_heat_into_cheap_slots(self) -> None:
        prices = [0.5] * 24 + [3.0] * 24 + [1.5] * 48  # cheap night, peak, normal
        settings = base_settings()
        plan = plan_heating(quarter_slots(prices), 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings,
                            current_offset=settings.base_offset)
        cheap = [s.offset for s in plan.plan.steps[:24]]
        peak = [s.offset for s in plan.plan.steps[24:48]]
        self.assertGreater(sum(cheap) / 24, settings.base_offset)
        self.assertLess(sum(peak) / 24, settings.base_offset)
        self.assertGreater(plan.savings, 0)
        self.assertGreaterEqual(plan.plan.min_t_in, COMFORT.lower - 0.02)
        self.assertLessEqual(plan.plan.max_t_in, COMFORT.upper + 0.02)

    def test_respects_bounds(self) -> None:
        prices = [0.1] * 16 + [5.0] * 16 + [0.1] * 16
        settings = PlannerSettings(base_offset=0, max_down=2, max_up=1)
        plan = plan_heating(quarter_slots(prices), 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings)
        self.assertTrue(all(-2 <= o <= 1 for o in plan.offsets))
        top = PlannerSettings(base_offset=9, max_down=1, max_up=3)
        self.assertEqual(top.offsets(), [8, 9, 10])

    def test_switching_penalty_reduces_changes(self) -> None:
        # Prices that alternate every half hour invite flapping.
        prices = [0.6 if (i // 2) % 2 else 2.4 for i in range(96)]
        cheap = plan_heating(quarter_slots(prices), 21.0, HOUSE, CURVE, ENERGY, COMFORT,
                             base_settings(change_cost=0.0, step_cost=0.0))
        costly = plan_heating(quarter_slots(prices), 21.0, HOUSE, CURVE, ENERGY, COMFORT,
                              base_settings(change_cost=0.5, step_cost=0.2))
        self.assertGreater(cheap.plan.changes, costly.plan.changes)
        self.assertLessEqual(costly.plan.changes, 4)

    def test_flat_prices_change_little(self) -> None:
        settings = base_settings()
        plan = plan_heating(quarter_slots([1.5] * 144), 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings,
                            current_offset=settings.base_offset)
        self.assertLessEqual(plan.plan.changes, 2)
        self.assertTrue(all(abs(o - settings.base_offset) <= 1 for o in plan.offsets))
        self.assertTrue(20.7 <= plan.plan.min_t_in and plan.plan.max_t_in <= 21.3)
        # Integer offsets cannot hold 21.0 exactly; the base offset runs a
        # little warm here, so a small saving remains.
        self.assertLess(abs(plan.savings), 0.05 * plan.baseline.energy_cost)

    def test_cold_start_recovers_comfort(self) -> None:
        settings = base_settings()
        plan = plan_heating(quarter_slots([3.0] * 48), 19.2, HOUSE, CURVE, ENERGY, COMFORT, settings)
        self.assertEqual(plan.offsets[0], settings.base_offset + settings.max_up)

    def test_hourly_and_quarter_slots_agree(self) -> None:
        quarter = quarter_slots(hourly_to_quarters(SE3_DAY))
        hourly = [HorizonSlot(START + timedelta(hours=i), START + timedelta(hours=i + 1), p, -6.0)
                  for i, p in enumerate(SE3_DAY)]
        settings = base_settings()
        pq = plan_heating(quarter, 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings, settings.base_offset)
        ph = plan_heating(hourly, 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings, settings.base_offset)
        self.assertEqual(len(pq.plan.steps), 96)
        self.assertEqual(len(ph.plan.steps), 24)
        self.assertAlmostEqual(pq.baseline.energy_cost, ph.baseline.energy_cost, delta=0.03 * ph.baseline.energy_cost)
        self.assertAlmostEqual(pq.plan.energy_adjusted, ph.plan.energy_adjusted, delta=0.05 * ph.plan.energy_adjusted)
        self.assertGreater(pq.savings, 0)
        self.assertGreater(ph.savings, 0)

    def test_performance_144_slots(self) -> None:
        prices = hourly_to_quarters(SE3_DAY) + hourly_to_quarters(SE3_DAY)[:48]
        slots = quarter_slots(prices)
        self.assertEqual(len(slots), 144)
        started = time.perf_counter()
        plan = plan_heating(slots, 21.0, HOUSE, CURVE, ENERGY, COMFORT, base_settings())
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0, "144 slots took %.2f s" % elapsed)
        self.assertEqual(len(plan.offsets), 144)

    def test_simulate_matches_plan(self) -> None:
        settings = base_settings()
        slots = quarter_slots(hourly_to_quarters(SE3_DAY))
        plan = plan_heating(slots, 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings)
        again = simulate(slots, plan.offsets, 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings)
        self.assertAlmostEqual(again.energy_cost, plan.plan.energy_cost, places=9)

    def test_explanations(self) -> None:
        settings = base_settings()
        plan = plan_heating(quarter_slots(hourly_to_quarters(SE3_DAY)), 21.0, HOUSE, CURVE, ENERGY,
                            COMFORT, settings, settings.base_offset)
        tz = ZoneInfo("Europe/Stockholm") if ZoneInfo else None
        blocks = explain_plan(plan, COMFORT, tz) if tz else explain_plan(plan, COMFORT)
        reasons = {b.reason for b in blocks}
        self.assertIn("cheap", reasons)
        self.assertIn("expensive", reasons)
        self.assertEqual(blocks[0].start, START)
        self.assertEqual(blocks[-1].end, START + timedelta(hours=24))
        if tz:
            self.assertTrue(blocks[0].text.startswith("00:00-"))
        self.assertIn("saves", summary(plan))


class HorizonTest(unittest.TestCase):
    def prices(self):
        return [PriceSlot(START + timedelta(minutes=15 * i), START + timedelta(minutes=15 * (i + 1)), 1.0 + i / 100)
                for i in range(96)]

    def test_cuts_current_slot_and_stops_at_gap(self) -> None:
        prices = self.prices()
        del prices[50]
        now = START + timedelta(minutes=5)
        slots = build_horizon(prices, now, -3.0)
        self.assertEqual(slots[0].start, now)
        self.assertAlmostEqual(slots[0].hours, 10 / 60)
        self.assertEqual(len(slots), 50)
        self.assertEqual(build_horizon(prices, START - timedelta(hours=1), -3.0), [])

    def test_missing_forecast_holds_current_outdoor(self) -> None:
        slots = build_horizon(self.prices(), START, -7.5)
        self.assertTrue(all(s.t_out == -7.5 for s in slots))
        # Nothing at all: a neutral default rather than a crash.
        self.assertTrue(all(s.t_out == 0.0 for s in build_horizon(self.prices(), START, None)))
        settings = base_settings()
        plan = plan_heating(slots, 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings)
        self.assertEqual(len(plan.offsets), 96)

    def test_forecast_interpolation_and_bias(self) -> None:
        forecast = [ForecastPoint(START + timedelta(hours=h), -2.0 - h * 0.5, condition="cloudy") for h in range(30)]
        slots = build_horizon(self.prices(), START, -4.0, forecast, bias_hours=12)
        self.assertAlmostEqual(slots[0].t_out, -2.0 - 0.0625 - 2.0 * (1 - 0.125 / 12), places=6)
        later = [s for s in slots if s.start >= START + timedelta(hours=13)][0]
        self.assertAlmostEqual(later.t_out, -2.0 - 0.5 * 13.125, places=6)

    def test_resample_to_hours(self) -> None:
        now = START + timedelta(minutes=20)
        slots = build_horizon(self.prices(), now, -3.0, step_minutes=60)
        self.assertEqual(slots[0].start, now)
        self.assertEqual(slots[0].end, START + timedelta(hours=1))
        self.assertAlmostEqual(slots[0].price, (1.01 * 10 + 1.02 * 15 + 1.03 * 15) / 40)
        self.assertEqual(len(slots), 24)

    def test_solar_from_location(self) -> None:
        start = utc(2026, 3, 20, 0)
        prices = [PriceSlot(start + timedelta(hours=i), start + timedelta(hours=i + 1), 1.0) for i in range(24)]
        forecast = [ForecastPoint(start + timedelta(hours=h), 2.0, condition="sunny") for h in range(24)]
        slots = build_horizon(prices, start, 2.0, forecast, latitude=59.3, longitude=18.1)
        self.assertEqual(slots[0].solar, 0.0)
        self.assertGreater(slots[11].solar, 0.4)

    @unittest.skipIf(ZoneInfo is None, "zoneinfo")
    def test_dst_days(self) -> None:
        tz = ZoneInfo("Europe/Stockholm")
        for day, count, hours in ((date(2026, 10, 25), 100, 25), (date(2026, 3, 29), 92, 23)):
            values = [2.5 if 68 <= i < 80 else 0.8 if i < 24 else 1.5 for i in range(count)]
            prices = slots_from_numbers(values, day, tz)
            now = prices[0].start
            slots = build_horizon(prices, now, -2.0)
            self.assertEqual(len(slots), count)
            self.assertAlmostEqual(sum(s.hours for s in slots), hours)
            settings = base_settings()
            plan = plan_heating(slots, 21.0, HOUSE, CURVE, ENERGY, COMFORT, settings, settings.base_offset)
            self.assertEqual(len(plan.offsets), count)
            self.assertGreater(plan.savings, 0)
            blocks = explain_plan(plan, COMFORT, tz)
            self.assertTrue(blocks[0].text.startswith("00:00-"))
            self.assertEqual(blocks[-1].end, prices[-1].end)


class GateTest(unittest.TestCase):
    def fit(self, level="high", ok=True):
        return HouseFit(HOUSE, ok, "ok" if ok else "bad", 400, 0.9, 0.02, 0.05, 0.05, 0.9, level)

    def test_gate(self) -> None:
        args = dict(horizon_h=24, t_in=21.0, floor=19.5, base_offset=0, max_up=3)
        self.assertTrue(gate(self.fit(), 2, **args).use_optimizer)
        self.assertFalse(gate(self.fit("medium"), 2, **args).use_optimizer)
        self.assertTrue(gate(self.fit("medium"), 2, min_level="medium", **args).use_optimizer)
        self.assertFalse(gate(self.fit(ok=False), 2, **args).use_optimizer)
        self.assertFalse(gate(None, 2, **args).use_optimizer)
        self.assertFalse(gate(self.fit(), 2, **dict(args, horizon_h=3)).use_optimizer)
        self.assertFalse(gate(self.fit(), 2, indoor_problem="stale", **args).use_optimizer)
        cold = gate(self.fit("low"), -3, **dict(args, t_in=19.0))
        self.assertTrue(cold.use_optimizer)
        self.assertEqual(cold.offset, 3)


if __name__ == "__main__":
    unittest.main()
