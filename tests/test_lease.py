from __future__ import annotations

import unittest

import context  # noqa: F401
from context import FakeClock, utc
from logic.lease import LeaseAction, LeaseManager


class LeaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(utc(2026, 1, 10, 12, 0))
        self.lease = LeaseManager(47011, clock=self.clock)

    def step(self, mode: str, target, healthy: bool = True):
        action = self.lease.decide(mode, target, healthy)
        if action is not None:
            self.lease.record(action)
        return action

    def test_shadow_and_off_never_set(self) -> None:
        self.assertIsNone(self.step("shadow", 3))
        self.assertIsNone(self.step("off", 3))

    def test_acquire_renew_change(self) -> None:
        self.assertEqual(self.step("active", -2), LeaseAction("set", -2))
        self.clock.advance(60)
        self.assertIsNone(self.step("active", -2))
        self.clock.advance(239)
        self.assertIsNone(self.step("active", -2))
        self.clock.advance(1)  # 300 s after the set
        action = self.step("active", -2)
        self.assertEqual(action, LeaseAction("set", -2, renew=True))
        self.clock.advance(60)
        # A new value is sent at once.
        self.assertEqual(self.step("active", 1), LeaseAction("set", 1))

    def test_clamped_to_lease_limits(self) -> None:
        self.assertEqual(self.step("active", 14).value, 10)
        self.assertEqual(self.step("active", -11).value, -10)

    def test_unhealthy_stops_renewing(self) -> None:
        self.step("active", 2)
        for _ in range(20):
            self.clock.advance(60)
            self.assertIsNone(self.step("active", 2, healthy=False))
        self.assertIsNone(self.step("active", None))
        # Recovery: renewal resumes.
        self.assertIsNotNone(self.step("active", 2))

    def test_release_on_mode_change(self) -> None:
        self.step("active", 2)
        self.assertEqual(self.step("shadow", 2), LeaseAction("release"))
        self.assertIsNone(self.step("shadow", 2))
        self.step("active", 2)
        self.assertEqual(self.step("off", 2), LeaseAction("release"))
        self.assertFalse(self.lease.held)

    def test_unload_release(self) -> None:
        self.assertIsNone(self.lease.release_action())
        self.step("active", 0)
        self.assertEqual(self.lease.release_action(), LeaseAction("release"))

    def test_adopts_own_lease_after_restart(self) -> None:
        self.lease.note_remote_state({"active": True, "value": -3, "owner": "ha", "expires": 1})
        self.assertTrue(self.lease.held)
        # Restored mode shadow: release what the previous run held.
        self.assertEqual(self.step("shadow", 0), LeaseAction("release"))

    def test_adopted_lease_renewed_at_once_in_active(self) -> None:
        self.lease.note_remote_state({"active": True, "value": -3, "owner": "ha"})
        self.assertEqual(self.step("active", -3), LeaseAction("set", -3, renew=True))

    def test_foreign_owner_not_adopted(self) -> None:
        self.lease.note_remote_state({"active": True, "value": 1, "owner": "other"})
        self.assertFalse(self.lease.held)

    def test_gateway_dropped_lease_resend(self) -> None:
        self.step("active", 2)
        self.clock.advance(60)
        self.lease.note_remote_state({"active": False, "owner": None, "error": None})
        self.assertEqual(self.step("active", 2), LeaseAction("set", 2, renew=True))

    def test_grace_lease_taken_over(self) -> None:
        self.lease.note_remote_state({"active": True, "value": -2, "owner": "grace", "error": None})
        self.assertTrue(self.lease.is_grace())
        self.assertEqual(self.step("active", -2), LeaseAction("set", -2, renew=True))
        self.lease.note_remote_state({"active": True, "value": -2, "owner": "ha", "error": None})
        self.assertFalse(self.lease.is_grace())
        self.clock.advance(60)
        self.assertIsNone(self.step("active", -2))

    def test_grace_lease_released_when_not_active(self) -> None:
        self.lease.note_remote_state({"active": True, "value": -2, "owner": "grace"})
        self.assertEqual(self.step("shadow", 0), LeaseAction("release"))

    def test_not_ready_retried_after_one_tick(self) -> None:
        self.step("active", 2)
        self.lease.note_remote_state({"active": False, "error": "Heatpump is not ready yet."})
        self.assertFalse(self.lease.held)
        self.clock.advance(30)
        self.assertIsNone(self.step("active", 2))
        self.clock.advance(30)
        self.assertEqual(self.step("active", 2), LeaseAction("set", 2))

    def test_other_rejection_waits_renew_interval(self) -> None:
        self.step("active", 1)
        self.clock.advance(60)
        self.step("active", 3)
        # Rejected change; the old lease (value 1) stays active.
        self.lease.note_remote_state({"active": True, "value": 1, "owner": "ha", "error": "out of limits"})
        self.assertTrue(self.lease.held)
        self.assertEqual(self.lease.value, 1)
        for _ in range(4):
            self.clock.advance(60)
            self.assertIsNone(self.step("active", 3))
        self.clock.advance(60)
        self.assertEqual(self.step("active", 3), LeaseAction("set", 3))

    def test_payload(self) -> None:
        self.assertEqual(LeaseAction("set", -1).payload(900, "ha"), {"value": -1, "ttl": 900, "owner": "ha"})

    def test_expires(self) -> None:
        self.lease.note_remote_state({"active": True, "owner": "x", "expires": 1760100000000})
        self.assertEqual(self.lease.expires().year, 2025)


if __name__ == "__main__":
    unittest.main()
