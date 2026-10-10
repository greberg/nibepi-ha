from __future__ import annotations

import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.inputs import Reading
from logic.indoor import compute_indoor


class IndoorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = utc(2026, 1, 10, 12, 0)

    def reading(self, value: float, minutes_old: float = 1) -> Reading:
        return Reading(value, self.now - timedelta(minutes=minutes_old))

    def test_p_controller(self) -> None:
        d = compute_indoor(self.reading(20.4), 21.0, 2.0, self.now)
        self.assertTrue(d.inputs_ok)
        self.assertAlmostEqual(d.offset, 1.2)
        d = compute_indoor(self.reading(22.0), 21.0, 1.5, self.now)
        self.assertAlmostEqual(d.offset, -1.5)

    def test_rounded_to_two_decimals(self) -> None:
        d = compute_indoor(self.reading(20.333), 21.0, 1.0, self.now)
        self.assertEqual(d.offset, 0.67)

    def test_not_clamped_here(self) -> None:
        # curve.component_value clamps to ±10, like curveComponent.
        d = compute_indoor(self.reading(10.0), 21.0, 2.0, self.now)
        self.assertEqual(d.offset, 22.0)

    def test_stale_sensor(self) -> None:
        d = compute_indoor(self.reading(20.0, minutes_old=61), 21.0, 2.0, self.now)
        self.assertEqual(d.offset, 0)
        self.assertFalse(d.inputs_ok)
        self.assertIn("stale", d.reason)
        # Timeout 0 disables the check.
        d = compute_indoor(self.reading(20.0, minutes_old=600), 21.0, 2.0, self.now, 0)
        self.assertTrue(d.inputs_ok)

    def test_missing_inputs(self) -> None:
        for args, word in (
            ((None, 21.0, 2.0), "missing"),
            ((self.reading(float("nan")), 21.0, 2.0), "missing"),
            ((self.reading(3.9), 21.0, 2.0), "implausible"),
            ((self.reading(20.0), None, 2.0), "setpoint"),
            ((self.reading(20.0), 21.0, None), "factor"),
        ):
            d = compute_indoor(args[0], args[1], args[2], self.now)
            self.assertEqual(d.offset, 0)
            self.assertFalse(d.inputs_ok)
            self.assertIn(word, d.reason)


if __name__ == "__main__":
    unittest.main()
