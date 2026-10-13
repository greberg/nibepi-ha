"""A comfort band that changes over the horizon (Away mode's return)."""

from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import timedelta, timezone

import context  # noqa: F401
from context import utc
from logic.inputs import PriceSlot
from logic.optimizer.horizon import HorizonSlot
from logic.optimizer.planner import ComfortSettings, comfort_at, comfort_schedule, neutral_offset
from logic.optimizer.profile import HouseProfile
from logic.optrun import HeatingInputs, SystemSetup, models_for, plan_system

NOW = utc(2026, 1, 14, 23)
SE3_DAY = [0.92, 0.88, 0.85, 0.84, 0.86, 0.95, 1.35, 2.10, 2.45, 2.05, 1.70, 1.55,
           1.48, 1.45, 1.52, 1.70, 2.05, 2.60, 2.75, 2.30, 1.80, 1.45, 1.20, 1.02]
NORMAL = ComfortSettings(target=21.0, sensor_margin=0.1)


class ScheduleTest(unittest.TestCase):
    COMFORT = ComfortSettings(target=21.0, below=0.5, above=1.0, hard_floor_below=1.5, sensor_margin=0.1)

    def test_schedule_and_ramp(self) -> None:
        away = replace(self.COMFORT, target=18.0, tracking_above=0.0)
        change = NOW + timedelta(hours=10)
        self.assertIs(comfort_at(away, self.COMFORT, change, NOW), away)
        self.assertIs(comfort_at(away, self.COMFORT, change, change), self.COMFORT)
        self.assertIs(comfort_at(away, None, change, change), away)
        half = comfort_at(away, self.COMFORT, change, change - timedelta(hours=1), ramp_h=2)
        self.assertAlmostEqual(half.target, 19.5)
        slots = [HorizonSlot(NOW + timedelta(hours=i), NOW + timedelta(hours=i + 1), 1.0, 0.0) for i in range(12)]
        bands = comfort_schedule(slots, away, self.COMFORT, change)
        # The slot ending at the return is already held to the normal band.
        self.assertEqual([b.target for b in bands], [18.0] * 9 + [21.0] * 3)


    def test_tracking_above(self) -> None:
        plain = ComfortSettings(target=18.0)
        free = replace(plain, tracking_above=0.0)
        self.assertEqual(free.penalty_per_hour(18.5), 0.0)
        self.assertGreater(plain.penalty_per_hour(18.5), 0.0)
        self.assertEqual(free.penalty_per_hour(17.8), plain.penalty_per_hour(17.8))


class PlanSystemAwayTest(unittest.TestCase):
    def test_plan_system_with_a_return(self) -> None:
        models = models_for(SystemSetup("s1", HouseProfile(floor_area_m2=150, floors=1.5, target_temp=21.0,
                                                           latitude=None, longitude=None, emitter="floor"), 5), None)
        prices = [PriceSlot(NOW + timedelta(minutes=15 * i), NOW + timedelta(minutes=15 * (i + 1)),
                            SE3_DAY[(i // 4) % 24]) for i in range(36 * 4)]
        until = NOW + timedelta(hours=18)
        away = replace(NORMAL, target=18.0, above=NORMAL.upper - 18.0, tracking_above=0.0)
        result = plan_system(HeatingInputs("s1", models, away, prices, NOW, timezone.utc, 18.0, -6.0,
                                           current_offset=-3, comfort_after=NORMAL, comfort_change=until))
        self.assertIsNone(result.problem)
        steps = result.plan.plan.steps
        self.assertEqual(len(result.comforts), len(steps))
        self.assertEqual(result.comforts[0].target, away.target)
        self.assertEqual(result.comforts[-1].target, NORMAL.target)
        at = next(i for i, s in enumerate(steps) if s.end >= until)
        self.assertGreaterEqual(steps[at].t_in_end, NORMAL.lower - 0.1)
        # The offsets may reach the normal base + 3.
        normal_base = neutral_offset(models.house, models.curve, -6.0, 21.0)
        self.assertGreaterEqual(result.plan.settings.base_offset + result.plan.settings.max_up, normal_base + 3)
        # A return beyond the horizon: the lowered band throughout.
        later = plan_system(HeatingInputs("s1", models, away, prices, NOW, timezone.utc, 18.0, -6.0,
                                          current_offset=-3, comfort_after=NORMAL,
                                          comfort_change=NOW + timedelta(days=5)))
        self.assertTrue(all(c is away for c in later.comforts))
        self.assertEqual(later.plan.settings.max_up, 3)
        self.assertTrue(math.isfinite(later.plan.savings))


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
