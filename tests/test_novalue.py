"""The pump's no-value (-3276.8 °C after a power cut) is never a reading."""

from __future__ import annotations

import unittest

import context  # noqa: F401
from logic.inputs import pump_value


class NoValueTest(unittest.TestCase):
    def test_sentinels_are_dropped(self) -> None:
        for v in (-3276.8, -32768.0, -327.68, -214748364.8):
            self.assertIsNone(pump_value(v))

    def test_real_values_pass(self) -> None:
        for v in (-12.8, -128.0, -30.5, 0.0, 47.9, None):
            self.assertEqual(pump_value(v), v)


if __name__ == "__main__":
    unittest.main()
