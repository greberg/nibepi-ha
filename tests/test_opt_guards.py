"""The optimiser's guards (15 Nov): pinned fits, the neutral-offset check,
the reality check, the range/comfort/rate guards and the tank's start."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import context  # noqa: F401
from logic.optimizer.gate import gate
from logic.optimizer.heatcurve import HeatCurve
from logic.optimizer.house import FitBounds, HouseFit, HouseModel, pinned_parameters
from logic.optimizer.neutral import check_neutral, model_neutral
from logic.optimizer.planner import PlannerSettings
from logic.optrun import STRAT_GAP, tank_start
from logic.optstrategy import (
    SOURCE_FLOOR,
    SOURCE_OPTIMISER,
    StrategyChoice,
    guard_choice,
)
from logic.optwatch import PAUSE, ForecastWatch
from logic.texts import code_of

T0 = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)

# The fit of 15 Nov (Husmodell S1) and its learned curve.
FIT_15_NOV = HouseModel(0.01125, 0.02305, 0.2769, -0.0154, 3.0, 9.0)
CURVE_15_NOV = HeatCurve(curve=6, slope_per_curve=0.10737, offset_gain=0.8426, bias=4.744)
BOUNDS_15_NOV = FitBounds(a=(0.00105, 0.0137), b=(0.0014, 0.0183), c=(0.0, 0.2769), d=(-0.0154, 0.1431))


def fit(level="high", **details) -> HouseFit:
    return HouseFit(FIT_15_NOV, True, "ok", 600, 0.9, 0.01, 0.3, 0.1, 0.8, level, details)


class PinnedTest(unittest.TestCase):
    def test_parameters_at_their_bounds(self) -> None:
        self.assertEqual(pinned_parameters(FIT_15_NOV, BOUNDS_15_NOV), ["c", "d"])
        inside = HouseModel(0.004, 0.008, 0.05, 0.03)
        self.assertEqual(pinned_parameters(inside, BOUNDS_15_NOV), [])
        # No sun effect is a real answer, not a pinned one.
        self.assertEqual(pinned_parameters(HouseModel(0.004, 0.008, 0.0, 0.03), BOUNDS_15_NOV), [])

    def test_gate_refuses_a_pinned_or_biased_fit_at_any_level(self) -> None:
        d = gate(fit(pinned=["c", "d"]), -5, 24.0, 21.0, 19.5, -5, 3, min_level="medium")
        self.assertFalse(d.use_optimizer)
        self.assertEqual(code_of(d.reason), "gate.pinned")
        check = {"ok": False, "days": 9, "model": -5.4, "actual": -1.6, "bias": -3.8}
        d = gate(fit(neutral_check=check), -5, 24.0, 21.0, 19.5, -5, 3, min_level="medium")
        self.assertEqual(code_of(d.reason), "gate.biased")
        self.assertIn("-5.4", d.reason)
        # Below the floor it still heats.
        d = gate(fit(pinned=["d"]), -5, 24.0, 19.0, 19.5, -5, 3)
        self.assertTrue(d.use_optimizer)
        self.assertEqual(d.offset, -2)


def days_of(offset: float, days: int = 6, t_in: float = 21.0, t_out: float = 7.0, drift: float = 0.0):
    rows = {}
    start = int(T0.timestamp()) - days * 86400
    n = days * 144
    for i in range(n):
        rows[start + i * 600] = {"s1_t_in": t_in + drift * (i % 144) / 143.0, "t_out": t_out, "s1_offset": offset}
    return rows


class NeutralCheckTest(unittest.TestCase):
    def test_the_15_nov_model_is_caught(self) -> None:
        # Weeks at -1.5 held 21 °C at 7 °C; the model says about -5.5.
        check = check_neutral(days_of(-1.5), "s1", FIT_15_NOV, CURVE_15_NOV)
        self.assertFalse(check.ok)
        self.assertEqual(check.days, 6)
        self.assertAlmostEqual(check.actual, -1.5)
        self.assertLess(check.bias, -3.0)

    def test_a_model_that_agrees_passes(self) -> None:
        held = model_neutral(FIT_15_NOV, CURVE_15_NOV, 21.0, 7.0) + 1.0
        check = check_neutral(days_of(held), "s1", FIT_15_NOV, CURVE_15_NOV)
        self.assertTrue(check.ok)
        self.assertAlmostEqual(check.bias, -1.0, places=6)

    def test_drifting_or_too_few_days_tell_nothing(self) -> None:
        self.assertTrue(check_neutral(days_of(-1.5, drift=1.0), "s1", FIT_15_NOV, CURVE_15_NOV).ok)
        few = check_neutral(days_of(-1.5, days=2), "s1", FIT_15_NOV, CURVE_15_NOV)
        self.assertTrue(few.ok)
        self.assertEqual(few.days, 2)
        self.assertIsNone(few.bias)


@dataclass
class Step:
    start: datetime
    end: datetime
    t_in_start: float
    t_in_end: float


def flat_plan(made: datetime, value: float = 21.0):
    return [Step(made + timedelta(minutes=30 * i), made + timedelta(minutes=30 * (i + 1)), value, value) for i in range(12)]


class ForecastWatchTest(unittest.TestCase):
    def test_a_sustained_shortfall_pauses(self) -> None:
        watch = ForecastWatch()
        watch.record("s1", T0, flat_plan(T0))
        now = T0 + timedelta(hours=2)
        self.assertIsNone(watch.check("s1", now, 20.6, True))
        self.assertIsNone(watch.check("s1", now + timedelta(minutes=15), 20.6, True))
        pause = watch.check("s1", now + timedelta(minutes=30), 20.6, True)
        self.assertIsNotNone(pause)
        self.assertEqual(pause.until, now + timedelta(minutes=30) + PAUSE)
        self.assertIs(watch.paused("s1", now + timedelta(hours=1)), pause)
        self.assertIsNone(watch.paused("s1", pause.until))
        # It survives a restart.
        again = ForecastWatch()
        watch.pauses["s1"] = pause
        again.load(watch.to_dict())
        self.assertEqual(again.pauses["s1"], pause)

    def test_small_or_interrupted_shortfalls_and_rules_do_not(self) -> None:
        watch = ForecastWatch()
        watch.record("s1", T0, flat_plan(T0))
        now = T0 + timedelta(hours=2)
        self.assertIsNone(watch.check("s1", now, 20.8, True))  # 0.2 under: fine
        self.assertIsNone(watch.check("s1", now, 20.6, True))
        self.assertIsNone(watch.check("s1", now + timedelta(minutes=15), 20.9, True))  # recovered
        self.assertIsNone(watch.check("s1", now + timedelta(minutes=30), 20.6, True))
        self.assertIsNone(watch.check("s1", now + timedelta(minutes=40), 20.6, False))  # rules steer
        self.assertIsNone(watch.check("s1", now + timedelta(minutes=45), 20.6, True))
        self.assertEqual(watch.pauses, {})
        # Warmer than predicted is no reason.
        self.assertIsNone(watch.check("s1", now + timedelta(minutes=50), 22.0, True))


def choice(total: float, source: str = SOURCE_OPTIMISER) -> StrategyChoice:
    return StrategyChoice("optimiser", source, total, "optimiser")


class GuardTest(unittest.TestCase):
    def test_range_follows_the_level(self) -> None:
        c, _ = guard_choice(choice(-8), -1.0, None, 21.0, 20.5, "medium", None, T0)
        self.assertEqual(c.total, -3.0)
        self.assertEqual(code_of(c.reason), "guard.range")
        c, _ = guard_choice(choice(-8), -1.0, None, 21.0, 20.5, "high", None, T0)
        self.assertEqual(c.total, -4.0)
        c, _ = guard_choice(choice(-2), -1.0, None, 21.0, 20.5, "medium", None, T0)
        self.assertEqual((c.total, c.reason), (-2, "optimiser"))

    def test_below_the_band_never_colder_than_the_rules(self) -> None:
        c, _ = guard_choice(choice(-2), 0.8, None, 20.3, 20.5, "high", None, T0)
        self.assertEqual(c.total, 0.8)
        self.assertEqual(code_of(c.reason), "guard.comfort")

    def test_one_step_down_per_quarter(self) -> None:
        # 14 Nov 20:48: -1 to -8 at once.
        c, down = guard_choice(choice(-3), -1.0, -1.0, 21.4, 20.5, "medium", None, T0)
        self.assertEqual(c.total, -2.0)
        self.assertEqual(code_of(c.reason), "guard.rate")
        self.assertEqual(down, T0)
        c, down2 = guard_choice(choice(-3), -1.0, -2.0, 21.4, 20.5, "medium", down, T0 + timedelta(minutes=5))
        self.assertEqual(c.total, -2.0)
        self.assertEqual(down2, down)
        c, down3 = guard_choice(choice(-3), -1.0, -2.0, 21.4, 20.5, "medium", down, T0 + timedelta(minutes=15))
        self.assertEqual(c.total, -3.0)
        self.assertEqual(down3, T0 + timedelta(minutes=15))
        # Up is not limited.
        c, _ = guard_choice(choice(1), -1.0, -3.0, 21.4, 20.5, "medium", down3, T0 + timedelta(minutes=16))
        self.assertEqual(c.total, 1)

    def test_floor_and_rules_pass_unchanged(self) -> None:
        floor = choice(5, SOURCE_FLOOR)
        self.assertIs(guard_choice(floor, -1.0, -1.0, 19.0, 20.5, "medium", None, T0)[0], floor)

    def test_planner_range(self) -> None:
        settings = PlannerSettings(base_offset=-5, max_down=3, max_up=3, lowest=-3, highest=1)
        self.assertEqual(settings.offsets(), [-3, -2])
        # A model far off: the rules' range alone.
        settings = PlannerSettings(base_offset=-8, max_down=3, max_up=3, lowest=-3, highest=1)
        self.assertEqual(settings.offsets(), [-3, -2, -1, 0, 1])
        self.assertEqual(PlannerSettings(base_offset=-5).offsets(), list(range(-8, -1)))


class StoredFitTest(unittest.TestCase):
    def test_a_stored_medium_fit_on_its_bounds_is_low_at_load(self) -> None:
        from logic.optimizer.profile import HouseProfile
        from logic.optrun import SystemSetup, models_for

        profile = HouseProfile(196, 1, 21.0, "2011_later", "heavy", 1, "floor", 55.408, 12.951, 2)
        stored = {"systems": {"s1": {"house": {
            "ok": True, "reason": "ok", "level": "medium", "confidence": 0.554, "a": 0.01125, "b": 0.02305,
            "c": 0.27692307692307694, "d": -0.015384615384615385, "tau_f": 3.0, "tau_d": 9.0,
        }}}}
        models = models_for(SystemSetup("s1", profile, 6.0), stored)
        self.assertEqual(models.fit.level, "low")
        self.assertEqual(models.fit.details["pinned"], ["c", "d"])


class TankStartTest(unittest.TestCase):
    def test_a_draw_does_not_empty_the_tank(self) -> None:
        # 14 Nov: after a shower BT6 17 °C, BT7 49 °C.
        self.assertEqual(tank_start(17.0, 49.0), 49.0 - STRAT_GAP)
        self.assertEqual(tank_start(47.0, 49.0), 47.0)
        self.assertEqual(tank_start(17.0, None), 17.0)


if __name__ == "__main__":
    unittest.main()
