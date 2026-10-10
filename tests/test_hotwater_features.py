from __future__ import annotations

import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.hotwater import COMFORT_DEFAULTS, AutoLuxury, HwPriority, comfort_mode

START = utc(2026, 10, 6, 5, 0)


class ComfortModeTest(unittest.TestCase):
    def test_node_red_defaults(self) -> None:
        self.assertEqual(comfort_mode("VERY_CHEAP", COMFORT_DEFAULTS)[0], 2)
        self.assertEqual(comfort_mode("CHEAP", COMFORT_DEFAULTS)[0], 1)
        for level in ("NORMAL", "EXPENSIVE", "VERY_EXPENSIVE"):
            self.assertEqual(comfort_mode(level, COMFORT_DEFAULTS)[0], 0)
        self.assertEqual(comfort_mode("CHEAP", {}), (1, "CHEAP: normal"))

    def test_unchanged_and_missing_price(self) -> None:
        self.assertEqual(comfort_mode("NORMAL", {"NORMAL": "unchanged"}), (None, "NORMAL: unchanged"))
        self.assertIsNone(comfort_mode("NORMAL", {"NORMAL": "7"})[0])
        self.assertEqual(comfort_mode(None, COMFORT_DEFAULTS), (None, "no price for the current hour"))


class AutoLuxuryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.feature = AutoLuxury()
        self.now = START

    def step(self, bt6, stop=55.0, pump_lux=0, held=False, diff=20, samples=10):
        result = self.feature.step(bt6, stop, pump_lux, held, self.now, diff, samples)
        self.now += timedelta(minutes=1)
        return result

    def test_fast_drop_triggers_and_target_ends_it(self) -> None:
        for _ in range(10):
            self.assertFalse(self.step(50.0).heat)
        # 21 °C within 9 samples: trigger 30.
        values = [48.0, 45.0, 42.0, 39.0, 36.0, 33.0, 31.0, 29.0]
        results = [self.step(v) for v in values]
        self.assertFalse(results[-2].heat)
        self.assertTrue(results[-1].heat)
        self.assertEqual(self.feature.target, 45.0)  # 50 - 5
        self.assertTrue(self.step(40.0).heat)
        self.assertFalse(self.step(45.0).heat)
        self.assertFalse(self.feature.active)

    def test_reference_is_time_minus_one_samples_back(self) -> None:
        self.step(50.0)
        self.assertEqual(self.feature.trigger, 30.0)
        for _ in range(8):
            self.step(40.0)
        # The list is full (10) on the next sample, which pops the 50 as
        # the reference one last time.
        self.assertEqual(self.feature.trigger, 30.0)
        self.step(40.0)
        self.assertEqual(self.feature.trigger, 30.0)
        self.step(40.0)
        self.assertEqual(self.feature.trigger, 20.0)

    def test_stop_temperature_ends_it(self) -> None:
        self.step(50.0)
        self.assertTrue(self.step(30.0).heat)
        self.assertFalse(self.step(39.0, stop=40.0).heat)

    def test_time_limit(self) -> None:
        self.step(50.0)
        self.assertTrue(self.step(30.0).heat)
        self.now += timedelta(hours=3)
        self.assertFalse(self.step(31.0).heat)
        self.assertEqual(self.feature.step(31.0, 55.0, 0, False, self.now).reason, "BT6 31.0, trigger 30.0")

    def test_lux_running_elsewhere_blocks_trigger(self) -> None:
        self.step(50.0)
        self.assertFalse(self.step(30.0, pump_lux=4).heat)
        self.assertTrue(self.step(29.0, pump_lux=4, held=True).heat)

    def test_missing_input_resets(self) -> None:
        self.step(50.0)
        self.assertTrue(self.step(30.0).heat)
        result = self.step(None)
        self.assertFalse(result.heat)
        self.assertEqual(result.reason, "BT6 missing")
        self.assertFalse(self.feature.active)
        self.assertEqual(self.feature.history, [])
        self.assertFalse(self.step(30.0, stop=None).heat)


class HwPriorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.feature = HwPriority()

    def step(self, bt7, autolux=False, pump_lux=0, held=False, minutes=0):
        return self.feature.step(bt7, 45.0, 52.0, pump_lux, held, autolux, START + timedelta(minutes=minutes))

    def test_start_and_stop(self) -> None:
        self.assertFalse(self.step(46.0).heat)
        self.assertTrue(self.step(45.0).heat)
        self.assertTrue(self.step(50.0).heat)
        self.assertFalse(self.step(51.0).heat)

    def test_autoluxury_keeps_it_running(self) -> None:
        self.step(44.0)
        self.assertTrue(self.step(51.0, autolux=True).heat)
        self.assertFalse(self.step(51.0).heat)

    def test_blocked_by_lux_running_elsewhere(self) -> None:
        self.assertFalse(self.step(40.0, pump_lux=4).heat)

    def test_missing_input(self) -> None:
        self.step(44.0)
        self.assertFalse(self.feature.step(None, 45.0, 52.0, 0, False, False, START).heat)
        self.assertFalse(self.feature.active)


if __name__ == "__main__":
    unittest.main()
