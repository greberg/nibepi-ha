from __future__ import annotations

import unittest

import context  # noqa: F401
from logic.cards import card_entities, gateway_sensor_keys


class CardEntitiesTest(unittest.TestCase):
    def test_gateway_keys_per_system(self) -> None:
        keys = gateway_sensor_keys(["s1"])
        self.assertEqual(keys["bt50_s1"], 40033)
        self.assertEqual(keys["supply_s1"], 40008)
        self.assertNotIn("bt50_s2", keys)
        self.assertEqual(keys["bus_health"], "bus_health")

    def test_map(self) -> None:
        own = {"planned_offset_s1": "sensor.planned_s1", "gateway_online": "binary_sensor.gw", "x": ""}
        gateway = {40004: "sensor.bt1", 40033: "sensor.bt50", "bus_health": "sensor.bus"}
        found = card_entities(own, gateway.get, ["s1"], {"s1": None})
        self.assertEqual(found["planned_offset_s1"], "sensor.planned_s1")
        self.assertEqual(found["outdoor"], "sensor.bt1")
        self.assertEqual(found["bus_health"], "sensor.bus")
        self.assertEqual(found["indoor_s1"], "sensor.bt50")  # BT50 without a configured sensor
        self.assertNotIn("x", found)
        self.assertNotIn("bt7_hw_top", found)
        self.assertEqual(list(found), sorted(found))

    def test_configured_indoor_wins(self) -> None:
        found = card_entities({}, {40033: "sensor.bt50"}.get, ["s1", "s2"], {"s1": "sensor.netatmo"})
        self.assertEqual(found["indoor_s1"], "sensor.netatmo")
        self.assertNotIn("indoor_s2", found)


if __name__ == "__main__":
    unittest.main()
