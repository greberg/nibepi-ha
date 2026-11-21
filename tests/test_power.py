"""The heat pump's power now: the meter first, else the pump's figures."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import context  # noqa: F401
from logic.optimizer.meter import MeterReading
from logic.power import SOURCE_METER, SOURCE_PUMP, meter_watts, power_now

T0 = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)


class PowerTest(unittest.TestCase):
    def test_a_power_meter(self) -> None:
        samples = [MeterReading(T0 - MIN, 1850.0)]
        self.assertEqual(meter_watts("power", 0.001, samples, T0), 1850.0)
        self.assertIsNone(meter_watts("power", 0.001, [MeterReading(T0 - 5 * MIN, 1850.0)], T0))
        self.assertIsNone(meter_watts("power", 0.001, [MeterReading(T0, None)], T0))

    def test_an_energy_meter(self) -> None:
        samples = [MeterReading(T0 - 10 * MIN, 100.0), MeterReading(T0 - 5 * MIN, 100.15), MeterReading(T0, 100.3)]
        self.assertAlmostEqual(meter_watts("energy", 1.0, samples, T0), 1800.0)
        reset = [MeterReading(T0 - 10 * MIN, 100.0), MeterReading(T0, 0.2)]
        self.assertIsNone(meter_watts("energy", 1.0, reset, T0))
        self.assertIsNone(meter_watts("energy", 1.0, samples[-1:], T0))

    def test_the_pump_counts_the_addition_only_while_steps_are_on(self) -> None:
        blocked = power_now(None, 1500.0, 3.0, 0.0)
        self.assertEqual((blocked.watts, blocked.source, blocked.addition_w), (1500.0, SOURCE_PUMP, 0.0))
        self.assertEqual(power_now(None, 1500.0, 3.0, 6.0).watts, 4500.0)
        self.assertEqual(power_now(1850.0, 400.0, 0.0, 0.0).source, SOURCE_METER)
        self.assertIsNone(power_now(None, None, 3.0, 6.0).watts)


if __name__ == "__main__":
    unittest.main()
