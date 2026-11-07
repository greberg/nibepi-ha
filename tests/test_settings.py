from __future__ import annotations

import unittest

import context  # noqa: F401
from logic.settings import (
    HW_STOP_ECONOMY,
    HW_STOP_LUXURY,
    HW_STOP_NORMAL,
    gateway_supports,
    order_problem,
    setting_payload,
    setting_value,
)
from logic.texts import code_of


class SettingsTest(unittest.TestCase):
    def test_gateway_supports(self) -> None:
        state = {"version": "2.0.0", "settings": ["47047", "47048", "47049"]}
        self.assertTrue(gateway_supports(state, 47047))
        self.assertFalse(gateway_supports(state, 47011))
        # An older gateway has no settings list: the entities stay unavailable.
        self.assertFalse(gateway_supports({"version": "2.0.0"}, 47047))
        self.assertFalse(gateway_supports(None, 47047))

    def test_value_while_leased_is_the_baseline(self) -> None:
        self.assertEqual(setting_value(50.0, None), 50.0)
        self.assertEqual(setting_value(50.0, {"active": False, "baseline": None}), 50.0)
        # A legionella cycle holds 47047 at 65: the setting is what it restores.
        self.assertEqual(setting_value(65.0, {"active": True, "baseline": 55}), 55.0)
        self.assertEqual(setting_value(65.0, {"restoring": True, "baseline": 55}), 55.0)
        self.assertEqual(setting_value(None, None), None)

    def test_order(self) -> None:
        current = {47049: 45.0, 47048: 47.0, 47047: 50.0}
        self.assertIsNone(order_problem(HW_STOP_LUXURY, 55.0, current))
        self.assertEqual(code_of(order_problem(HW_STOP_LUXURY, 46.0, current)), "setting.order")
        self.assertEqual(code_of(order_problem(HW_STOP_ECONOMY, 48.0, current)), "setting.order")
        self.assertIsNone(order_problem(HW_STOP_NORMAL, 47.0, current))
        self.assertIsNone(order_problem(HW_STOP_NORMAL, 60.0, {47049: 45.0, 47047: None}))

    def test_payload(self) -> None:
        self.assertEqual(setting_payload(HW_STOP_LUXURY, 55), "55")
        self.assertEqual(setting_payload(HW_STOP_LUXURY, 54.7), "54.5")


if __name__ == "__main__":
    unittest.main()
