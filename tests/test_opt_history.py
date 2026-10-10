from __future__ import annotations

import unittest

import context  # noqa: F401
from logic.opthistory import meter_kind


class MeterKindTest(unittest.TestCase):
    def test_power_units(self) -> None:
        self.assertEqual(meter_kind("W", "power", "measurement"), ("power", "W"))
        self.assertEqual(meter_kind("kW"), ("power", "kW"))
        self.assertEqual(meter_kind(" kw ", None, None), ("power", "kW"))

    def test_energy_units(self) -> None:
        self.assertEqual(meter_kind("kWh", "energy", "total_increasing"), ("energy", "kWh"))
        self.assertEqual(meter_kind("Wh", "energy", "total"), ("energy", "Wh"))
        self.assertEqual(meter_kind("MWh"), ("energy", "MWh"))

    def test_refused(self) -> None:
        self.assertIsNone(meter_kind("kWh", "energy", "measurement"))  # per-period value
        self.assertIsNone(meter_kind("kWh", "power"))  # class contradicts the unit
        self.assertIsNone(meter_kind("W", "energy"))
        self.assertIsNone(meter_kind("A", "current"))
        self.assertIsNone(meter_kind(None, "power"))
        self.assertIsNone(meter_kind(""))


if __name__ == "__main__":
    unittest.main()
