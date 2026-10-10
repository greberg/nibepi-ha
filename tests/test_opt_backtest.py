from __future__ import annotations

import math
import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.optimizer.backtest import backtest_heating
from logic.optimizer.heatcurve import HeatCurve
from logic.optimizer.horizon import HorizonSlot
from logic.optimizer.house import HouseModel
from logic.optimizer.planner import ComfortSettings, PlannerSettings, neutral_offset
from logic.optimizer.profile import HouseProfile, house_prior

CURVE = HeatCurve(curve=9)
PRIOR = house_prior(HouseProfile(floor_area_m2=150, floors=1.5, latitude=59.3), CURVE)
ENERGY = PRIOR.energy_model()
START = utc(2026, 1, 14, 23)  # 2026-01-15 00:00 Stockholm
# SE3 winter weekday, SEK/kWh incl. tax, grid fee and VAT (illustrative).
SE3_DAY = [0.92, 0.88, 0.85, 0.84, 0.86, 0.95, 1.35, 2.10, 2.45, 2.05, 1.70, 1.55,
           1.48, 1.45, 1.52, 1.70, 2.05, 2.60, 2.75, 2.30, 1.80, 1.45, 1.20, 1.02]


def truth_day():
    slots = []
    for i in range(96):
        hour = i // 4
        a = START + timedelta(minutes=15 * i)
        t_out = -6 + 3 * math.sin((hour - 9) / 24 * 2 * math.pi)  # coldest early morning
        slots.append(HorizonSlot(a, a + timedelta(minutes=15), SE3_DAY[hour], t_out))
    return slots


class BacktestTest(unittest.TestCase):
    def test_saves_without_comfort_violation(self) -> None:
        comfort = ComfortSettings(target=21.0, sensor_margin=PRIOR.sensor_margin)
        settings = PlannerSettings(base_offset=neutral_offset(PRIOR.model, CURVE, -6, 21.0))
        result = backtest_heating(truth_day(), 21.0, PRIOR.model, CURVE, ENERGY, comfort, settings,
                                  replan_every=4)
        self.assertEqual(result.replans, 24)
        self.assertGreater(result.savings, 0)
        self.assertGreater(result.savings_pct, 5.0)
        self.assertGreaterEqual(result.plan.min_t_in, comfort.lower - 0.02)
        self.assertLessEqual(result.plan.max_t_in, comfort.upper)
        self.assertLess(result.plan.below_band_kh, 0.01)
        self.assertLess(result.max_plan_s, 1.0)

    def test_model_error_and_forecast_error(self) -> None:
        # Real house loses 20 % more and reacts 10 % less than the planner believes;
        # the forecast is 2 °C too warm. Hourly replanning from the measured
        # temperature keeps it close to the band.
        true = HouseModel(PRIOR.model.a * 1.2, PRIOR.model.b * 0.9, PRIOR.model.c, PRIOR.model.d)
        comfort = ComfortSettings(target=21.0, sensor_margin=PRIOR.sensor_margin)
        settings = PlannerSettings(base_offset=neutral_offset(true, CURVE, -6, 21.0))
        result = backtest_heating(truth_day(), 21.0, true, CURVE, ENERGY, comfort, settings,
                                  house_plan=PRIOR.model, replan_every=4,
                                  forecast_error=lambda i, j: 2.0)
        self.assertGreaterEqual(result.plan.min_t_in, comfort.lower - 0.5)
        self.assertGreater(result.savings, -0.05 * result.baseline.energy_cost)


if __name__ == "__main__":
    unittest.main()
