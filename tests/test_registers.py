from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import context  # noqa: F401
from logic.registers import came_online, registers_to_request

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


class RegistersToRequestTest(unittest.TestCase):
    def test_never_seen_and_never_asked_is_due(self):
        self.assertEqual(registers_to_request([47011, 40004], {}, {}, NOW), [40004, 47011])

    def test_fresh_value_is_not_due(self):
        seen = {47011: NOW - timedelta(minutes=5)}
        self.assertEqual(registers_to_request([47011], seen, {}, NOW), [])

    def test_stale_value_is_due(self):
        seen = {47011: NOW - timedelta(minutes=31)}
        self.assertEqual(registers_to_request([47011], seen, {}, NOW), [47011])

    def test_recent_request_waits(self):
        asked = {47011: NOW - timedelta(minutes=9)}
        self.assertEqual(registers_to_request([47011], {}, asked, NOW), [])
        asked = {47011: NOW - timedelta(minutes=10)}
        self.assertEqual(registers_to_request([47011], {}, asked, NOW), [47011])

    def test_duplicates_are_requested_once(self):
        self.assertEqual(registers_to_request([47011, 47011], {}, {}, NOW), [47011])


class CameOnlineTest(unittest.TestCase):
    def test_transitions(self):
        self.assertTrue(came_online(None, "online"))
        self.assertTrue(came_online("offline", " Online "))
        self.assertFalse(came_online("online", "online"))
        self.assertFalse(came_online("online", "offline"))
        self.assertFalse(came_online(None, None))


if __name__ == "__main__":
    unittest.main()
