from __future__ import annotations

import json
import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.pump import (
    BUS_HEALTH,
    STALE_AFTER,
    is_fresh,
    own_unique_id,
    parse_bus_health,
    priority_state,
    pump_registers,
    pump_sensor_keys,
)


class PumpSensorsTest(unittest.TestCase):
    def test_registers_per_system(self) -> None:
        s1 = pump_registers(["s1"])
        for reg in (40004, 40013, 40014, 40012, 43005, 43136, 43141, 43086, 40008, 40033):
            self.assertIn(reg, s1)
        self.assertNotIn(40007, s1)
        self.assertNotIn(40032, s1)
        both = pump_registers(["s1", "s2"])
        self.assertIn(40007, both)
        self.assertIn(40032, both)
        self.assertEqual(both, sorted(both))

    def test_card_keys(self) -> None:
        keys = pump_sensor_keys(["s2"])
        self.assertEqual(keys["outdoor"], 40004)
        self.assertEqual(keys["supply_s2"], 40007)
        self.assertEqual(keys["bt50_s2"], 40032)
        self.assertNotIn("supply_s1", keys)
        self.assertEqual(keys[BUS_HEALTH], BUS_HEALTH)

    def test_unique_id_matches_entity(self) -> None:
        self.assertEqual(own_unique_id("abc", "outdoor"), "abc_outdoor")
        self.assertEqual(own_unique_id("abc", "supply", "s2"), "abc_s2_supply")

    def test_fresh(self) -> None:
        now = utc(2026, 10, 17, 12, 0)
        self.assertFalse(is_fresh(None, now))
        self.assertTrue(is_fresh(now - STALE_AFTER, now))
        self.assertFalse(is_fresh(now - STALE_AFTER - timedelta(seconds=1), now))

    def test_priority(self) -> None:
        self.assertEqual(priority_state(20.0), "hot_water")
        self.assertEqual(priority_state(30), "heating")
        self.assertIsNone(priority_state(None))
        self.assertIsNone(priority_state(99))

    def test_bus_health(self) -> None:
        payload = json.dumps({"timestamp": 1, "ok": 600, "chksum": 2, "no_respond": 1, "errors": 3})
        self.assertEqual(parse_bus_health(payload), {"errors": 3, "ok": 600, "chksum": 2, "no_respond": 1})
        self.assertEqual(parse_bus_health(b'{"ok": 5, "chksum": 1, "no_respond": 0}')["errors"], 1)
        self.assertIsNone(parse_bus_health("not json"))
        self.assertIsNone(parse_bus_health("[1]"))
        self.assertIsNone(parse_bus_health('{"ok": 5}'))


if __name__ == "__main__":
    unittest.main()
