"""The capacity guard: no price boost while the compressor is at its top
with a heating deficit (the night of 16-17 Nov)."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import context  # noqa: F401
from logic.capacity import MIN_TOP_HZ, Capacity, hold_boost
from logic.inputs import Decision
from logic.pump import PUMP_SENSORS
from logic.texts import code_of

T0 = datetime(2026, 11, 16, 22, tzinfo=timezone.utc)
HEAT, HW = 30, 20


def learned(top: float = 90.0) -> Capacity:
    cap = Capacity()
    cap.update(T0 - timedelta(days=1), top, 0.0, HEAT)
    return cap


class CapacityTest(unittest.TestCase):
    def test_the_16_nov_night_is_saturated(self) -> None:
        cap = learned()
        self.assertFalse(cap.update(T0, 83.0, -450.0, HEAT))  # not at the top yet
        self.assertTrue(cap.update(T0 + timedelta(hours=3), 90.0, -800.0, HEAT))
        self.assertTrue(cap.saturated)
        # Short dips do not release it; a clear drop or a paid deficit does.
        cap.update(T0 + timedelta(hours=4), 82.0, -900.0, HEAT)
        self.assertTrue(cap.saturated)
        cap.update(T0 + timedelta(hours=5), 70.0, -900.0, HEAT)
        self.assertFalse(cap.saturated)
        cap.update(T0 + timedelta(hours=6), 90.0, -400.0, HEAT)
        self.assertTrue(cap.saturated)
        cap.update(T0 + timedelta(hours=7), 90.0, -100.0, HEAT)
        self.assertFalse(cap.saturated)

    def test_a_small_deficit_or_hot_water_is_not(self) -> None:
        cap = learned()
        cap.update(T0, 90.0, -200.0, HEAT)
        self.assertFalse(cap.saturated)
        cap.update(T0, 90.0, -800.0, HW)  # the compressor works for hot water
        self.assertFalse(cap.saturated)
        cap.update(T0, 90.0, -800.0, HEAT)
        cap.update(T0, 90.0, -800.0, HW)  # ... and keeps the state
        self.assertTrue(cap.saturated)

    def test_unknown_top_or_readings_never_hold(self) -> None:
        cap = Capacity()
        cap.update(T0, MIN_TOP_HZ - 5, -900.0, HEAT)
        self.assertIsNone(cap.top)
        self.assertFalse(cap.saturated)
        cap = learned()
        cap.update(T0, 90.0, -800.0, HEAT)
        cap.update(T0, None, -800.0, HEAT)
        self.assertFalse(cap.saturated)

    def test_the_top_is_learned_and_kept(self) -> None:
        cap = Capacity()
        for day in range(20):
            cap.update(T0 + timedelta(days=day), 60.0 + day, 0.0, HEAT)
        self.assertEqual(cap.top, 79.0)
        self.assertEqual(len(cap.daily_top), 14)
        again = Capacity()
        again.load(cap.to_dict())
        self.assertEqual(again.top, 79.0)

    def test_hold_boost(self) -> None:
        cap = learned()
        boost = Decision(2.0, "VERY_CHEAP", True)
        lower = Decision(-3.0, "EXPENSIVE", True)
        self.assertIs(hold_boost(cap, boost), boost)
        cap.update(T0, 90.0, -800.0, HEAT)
        held = hold_boost(cap, boost)
        self.assertEqual(held.offset, 0.0)
        self.assertEqual(code_of(held.reason), "price.capacity_held")
        self.assertEqual(held.details["held_boost"], 2.0)
        self.assertIs(hold_boost(cap, lower), lower)


class AdditionSensorsTest(unittest.TestCase):
    def test_registers(self) -> None:
        self.assertEqual(
            (PUMP_SENSORS["addition_power"], PUMP_SENSORS["addition_steps"], PUMP_SENSORS["addition_hours"]),
            (43084, 43091, 43081),
        )


if __name__ == "__main__":
    unittest.main()
