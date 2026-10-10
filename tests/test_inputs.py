from __future__ import annotations

import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.inputs import Reading, is_missing, is_stale, js_round, to_fixed, usable


class StalenessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = utc(2026, 1, 10, 12, 0)

    def test_default_is_60_minutes(self) -> None:
        fresh = Reading(21.0, self.now - timedelta(minutes=59))
        old = Reading(21.0, self.now - timedelta(minutes=61))
        self.assertFalse(is_stale(fresh, self.now))
        self.assertTrue(is_stale(old, self.now))
        self.assertTrue(is_stale(old, self.now, None))

    def test_custom_and_disabled(self) -> None:
        old = Reading(21.0, self.now - timedelta(minutes=61))
        self.assertFalse(is_stale(old, self.now, 90))
        self.assertTrue(is_stale(old, self.now, 30))
        self.assertFalse(is_stale(old, self.now, 0))
        # Invalid timeout falls back to the default, like sensorIsStale.
        self.assertTrue(is_stale(old, self.now, -5))

    def test_missing(self) -> None:
        self.assertTrue(is_stale(None, self.now))
        self.assertEqual(usable(None, self.now), "missing")
        self.assertEqual(usable(Reading(9999, self.now), self.now), "missing")
        self.assertEqual(usable(Reading(float("nan"), self.now), self.now), "missing")
        self.assertIsNone(usable(Reading(20.5, self.now), self.now))
        self.assertTrue(is_missing("9999"))
        self.assertFalse(is_missing("21.5"))


class RoundingTest(unittest.TestCase):
    def test_math_round(self) -> None:
        self.assertEqual(js_round(2.5), 3)
        self.assertEqual(js_round(-2.5), -2)
        self.assertEqual(js_round(-2.6), -3)
        self.assertEqual(js_round(0.49), 0)

    def test_to_fixed(self) -> None:
        self.assertEqual(to_fixed(1.005 + 1e-9, 2), 1.01)
        self.assertEqual(to_fixed(-2.345 - 1e-9, 2), -2.35)
        self.assertEqual(to_fixed(-0.001, 2), 0.0)


if __name__ == "__main__":
    unittest.main()
