from __future__ import annotations

import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.gateway import gateway_online, status_hint


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


if __name__ == "__main__":
    unittest.main()
