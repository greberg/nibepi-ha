from __future__ import annotations

import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.gateway import device_sw_version, gateway_online, gateway_version, status_hint


class GatewayOnlineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.started = utc(2026, 1, 10, 12, 0)

    def at(self, seconds: float):
        return self.started + timedelta(seconds=seconds)

    def test_unknown_then_off_without_messages(self) -> None:
        self.assertIsNone(gateway_online(None, None, self.started, self.at(119)))
        self.assertIs(gateway_online(None, None, self.started, self.at(120)), False)

    def test_status_payload(self) -> None:
        self.assertIs(gateway_online("online", None, self.started, self.at(5)), True)
        self.assertIs(gateway_online(" Online\n", None, self.started, self.at(5)), True)
        self.assertIs(gateway_online("offline", None, self.started, self.at(5)), False)

    def test_fresh_gateway_state_means_online(self) -> None:
        live = self.at(100)
        self.assertIs(gateway_online(None, live, self.started, self.at(130)), True)
        # An old last will does not override a live state message.
        self.assertIs(gateway_online("offline", live, self.started, self.at(130)), True)

    def test_stale_gateway_state_ignored(self) -> None:
        live = self.at(0)
        self.assertIs(gateway_online(None, live, self.started, self.at(181)), False)
        self.assertIs(gateway_online("offline", live, self.started, self.at(181)), False)

    def test_hint_names_topic(self) -> None:
        self.assertIn("nibe/modbus/status", status_hint("nibe/modbus/status"))


class VersionTest(unittest.TestCase):
    def test_gateway_version(self) -> None:
        self.assertEqual(gateway_version({"version": " 2.0.0 ", "build": "20261017-87eb233"}), ("2.0.0", "20261017-87eb233"))
        self.assertEqual(gateway_version({"version": 2, "build": None}), ("2", None))
        self.assertEqual(gateway_version({"version": True, "build": ["x"]}), (None, None))
        self.assertEqual(gateway_version({}), (None, None))

    def test_device_sw_version(self) -> None:
        self.assertEqual(
            device_sw_version("2.0.0", "20261017-87eb233", "0.6.0"),
            "gateway 2.0.0 (20261017-87eb233), integration 0.6.0",
        )
        self.assertEqual(device_sw_version("2.0.0", None, None), "gateway 2.0.0")
        self.assertEqual(device_sw_version(None, "x", "0.6.0"), "integration 0.6.0")
        self.assertIsNone(device_sw_version(None, None, None))


if __name__ == "__main__":
    unittest.main()
