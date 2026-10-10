from __future__ import annotations

import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.inputs import (
    Reading,
    is_missing,
    is_stale,
    js_round,
    state_timestamp,
    state_value,
    to_fixed,
    usable,
)


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


class HaStateTest(unittest.TestCase):
    def test_state_value(self) -> None:
        self.assertEqual(state_value("21.4"), 21.4)
        for missing in ("unavailable", "unknown", "Unknown", "", None, "on"):
            self.assertIsNone(state_value(missing))

    def test_netatmo_unchanged_value_is_not_stale(self) -> None:
        # Netatmo reported 21.4 every 10 minutes for two hours; the value did
        # not change, so last_updated is two hours old but last_reported new.
        now = utc(2026, 1, 10, 12, 0)
        last_updated = now - timedelta(hours=2)
        last_reported = now - timedelta(minutes=8)
        ts = state_timestamp(last_reported, last_updated)
        self.assertEqual(ts, last_reported)
        self.assertIsNone(usable(Reading(21.4, ts), now, 60))
        # Without last_reported (HA before 2024.3) last_updated is used.
        ts = state_timestamp(None, last_updated)
        self.assertEqual(ts, last_updated)
        self.assertEqual(usable(Reading(21.4, ts), now, 60), "stale")
        self.assertIsNone(state_timestamp(None, None))


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
