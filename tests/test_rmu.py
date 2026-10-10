from __future__ import annotations

import unittest
from datetime import timedelta

import context  # noqa: F401
from context import FakeClock, utc
from logic.inputs import Reading
from logic.lease import LeaseAction
from logic.rmu import rmu_decide, rmu_lease_manager, room_temperature


class RmuFeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(utc(2026, 1, 10, 12, 0))
        self.lease = rmu_lease_manager(10060, clock=self.clock)

    def reading(self, value: float, minutes_old: float = 1) -> Reading:
        return Reading(value, self.clock.now - timedelta(minutes=minutes_old))

    def step(self, enabled: bool, reading, timeout=None):
        action, room = rmu_decide(self.lease, enabled, reading, self.clock.now, timeout)
        if action is not None:
            self.lease.record(action)
        return action

    def test_off_sends_nothing(self) -> None:
        self.assertIsNone(self.step(False, self.reading(21.0)))

    def test_set_rounded_to_one_decimal(self) -> None:
        self.assertEqual(self.step(True, self.reading(21.44)), LeaseAction("set", 21.4))
        self.assertEqual(
            LeaseAction("set", 21.4).payload(900, "ha"), {"value": 21.4, "ttl": 900, "owner": "ha"}
        )

    def test_small_change_waits_for_renewal(self) -> None:
        self.step(True, self.reading(21.40))
        self.clock.advance(60)
        # 21.43 still rounds to 21.4.
        self.assertIsNone(self.step(True, self.reading(21.43)))
        self.clock.advance(60)
        self.assertEqual(self.step(True, self.reading(21.5)), LeaseAction("set", 21.5))
        self.clock.advance(299)
        self.assertIsNone(self.step(True, self.reading(21.5)))
        self.clock.advance(1)
        self.assertEqual(self.step(True, self.reading(21.5)), LeaseAction("set", 21.5, renew=True))

    def test_clamped_to_lease_limits(self) -> None:
        self.assertEqual(self.step(True, self.reading(2.0)).value, 5.0)
        self.assertEqual(self.step(True, self.reading(40.0)).value, 35.0)

    def test_switch_off_releases_once(self) -> None:
        self.step(True, self.reading(21.0))
        self.assertEqual(self.step(False, self.reading(21.0)), LeaseAction("release"))
        self.assertIsNone(self.step(False, self.reading(21.0)))

    def test_stale_releases(self) -> None:
        self.step(True, self.reading(21.0))
        self.assertEqual(self.step(True, self.reading(21.0, minutes_old=61)), LeaseAction("release"))
        self.assertIsNone(self.step(True, self.reading(21.0, minutes_old=61)))
        # Fresh again: set at once.
        self.assertEqual(self.step(True, self.reading(21.0)), LeaseAction("set", 21.0))

    def test_sensor_timeout_option(self) -> None:
        self.assertIsNone(self.step(True, self.reading(21.0, minutes_old=20), timeout=10))
        self.assertIsNotNone(self.step(True, self.reading(21.0, minutes_old=20), timeout=30))

    def test_missing_or_implausible_releases(self) -> None:
        self.step(True, self.reading(21.0))
        self.assertEqual(self.step(True, None), LeaseAction("release"))
        self.step(True, self.reading(21.0))
        self.assertEqual(self.step(True, self.reading(55.0)), LeaseAction("release"))
        self.step(True, self.reading(21.0))
        self.assertEqual(self.step(True, self.reading(-31.0)), LeaseAction("release"))

    def test_room_temperature_reasons(self) -> None:
        now = self.clock.now
        self.assertEqual(room_temperature(None, now).reason, "room temperature missing")
        self.assertEqual(room_temperature(self.reading(9999), now).reason, "room temperature missing")
        self.assertEqual(room_temperature(self.reading(21.0, 90), now).reason, "room temperature stale")
        self.assertEqual(room_temperature(self.reading(-30.0), now).value, -30.0)

    def test_adopted_lease_after_restart(self) -> None:
        self.lease.note_remote_state({"active": True, "value": 21.4, "owner": "ha"})
        self.assertEqual(self.lease.value, 21.4)
        # Restored switch off: release what the previous run held.
        self.assertEqual(self.step(False, self.reading(21.4)), LeaseAction("release"))

    def test_adopted_lease_renewed(self) -> None:
        self.lease.note_remote_state({"active": True, "value": 21.4, "owner": "grace"})
        self.assertEqual(self.step(True, self.reading(21.4)), LeaseAction("set", 21.4, renew=True))

    def test_not_connected_rejection_backs_off(self) -> None:
        self.step(True, self.reading(21.0))
        self.lease.note_remote_state(
            {"active": False, "error": "RMU40 S1 is not connected: enable RMU40 for climate system 1"}
        )
        self.assertFalse(self.lease.held)
        for _ in range(4):
            self.clock.advance(60)
            self.assertIsNone(self.step(True, self.reading(21.0)))
        self.clock.advance(60)
        self.assertEqual(self.step(True, self.reading(21.0)), LeaseAction("set", 21.0))


if __name__ == "__main__":
    unittest.main()
