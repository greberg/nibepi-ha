from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import context  # noqa: F401
from context import UTC, utc
from logic.inputs import PriceSlot
from logic.legionella import (
    MARGIN,
    RESULT_BT7,
    RESULT_CANCELLED,
    RESULT_DONE,
    RESULT_TIMEOUT,
    RETRY_AFTER,
    LegionellaGuard,
    LegionellaSettings,
    LegionellaState,
    due_times,
    last_done_from_history,
    legionella_pin,
    parameters,
    plan_window,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

STHLM = ZoneInfo("Europe/Stockholm")
H = timedelta(hours=1)


def slots(start: datetime, prices, minutes: int = 15):
    out = []
    t = start
    for price in prices:
        out.append(PriceSlot(t, t + timedelta(minutes=minutes), float(price)))
        t += timedelta(minutes=minutes)
    return out


class Parameters(unittest.TestCase):
    def test_pump_values_win(self):
        p = parameters(LegionellaSettings(), 1, 7, 65.0)
        self.assertEqual((p.temperature, p.temperature_source), (65.0, "pump"))
        self.assertEqual((p.interval, p.interval_source, p.pump_periodic), (timedelta(days=7), "pump", True))

    def test_fallbacks(self):
        settings = LegionellaSettings.from_values(temperature=62, interval_days=10)
        p = parameters(settings, 0, 7, None)
        self.assertEqual((p.temperature, p.temperature_source), (62.0, "option"))
        self.assertEqual((p.interval, p.interval_source, p.pump_periodic), (timedelta(days=10), "option", False))
        p = parameters(settings, None, None, 30.0)  # out of range: option
        self.assertEqual(p.temperature_source, "option")
        self.assertFalse(p.pump_periodic)
        p = parameters(settings, 1, None, None)  # periodic on, interval unread
        self.assertEqual((p.interval_source, p.pump_periodic), ("option", True))

    def test_settings_are_clamped(self):
        s = LegionellaSettings.from_values(temperature=90, interval_days=0, window_hours=3, max_hours=1, start_hour=30)
        self.assertEqual((s.temperature, s.interval_days, s.window_hours, s.max_hours, s.start_hour), (70.0, 1, 3.0, 3.0, 23))
        self.assertEqual(LegionellaSettings.from_values(), LegionellaSettings())

    def test_due_times(self):
        ref = utc(2026, 10, 1, 12)
        own = parameters(LegionellaSettings(), 0, None, None)
        self.assertEqual(due_times(ref, own), (ref + timedelta(days=13), ref + timedelta(days=14)))
        pump = parameters(LegionellaSettings(), 1, 14, None)
        self.assertEqual(due_times(ref, pump), (ref + timedelta(days=15), ref + timedelta(days=16)))


class Window(unittest.TestCase):
    def test_cheapest_time_weighted(self):
        now = utc(2026, 10, 12, 10)
        prices = [5] * 8 + [1] * 8 + [5] * 80  # cheap 12:00-14:00 UTC
        start, how = plan_window(now, now, now + 24 * H, 2 * H, slots(now, prices), STHLM)
        self.assertEqual((start, how), (utc(2026, 10, 12, 12), "price"))

    def test_tie_prefers_night_then_earlier(self):
        now = utc(2026, 10, 12, 10)  # 12:00 local
        start, how = plan_window(now, now, now + 24 * H, 2 * H, slots(now, [3] * 96), STHLM)
        self.assertEqual(start.astimezone(STHLM).hour, 22)
        self.assertEqual(how, "price")

    def test_no_prices_default_hour(self):
        now = utc(2026, 10, 12, 10)
        start, how = plan_window(now, now, now + 24 * H, 2 * H, (), STHLM, start_hour=2)
        self.assertEqual((start.astimezone(STHLM).hour, how), (2, "default hour"))
        # The default hour is not in the span: at once.
        start, how = plan_window(now, now, now + 6 * H, 2 * H, (), STHLM, start_hour=2)
        self.assertEqual((start, how), (now, "now"))

    def test_deadline_close_or_passed(self):
        now = utc(2026, 10, 12, 10)
        self.assertEqual(plan_window(now, now - 5 * H, now + H, 2 * H, (), STHLM), (now, "deadline"))
        self.assertEqual(plan_window(now, now - 5 * H, now - H, 2 * H, (), STHLM), (now, "deadline"))

    def test_partial_prices_only_covered_windows(self):
        now = utc(2026, 10, 12, 10)
        # Prices only for 4 h: the window must lie inside them.
        start, how = plan_window(now, now, now + 24 * H, 2 * H, slots(now, [4] * 8 + [2] * 8), STHLM)
        self.assertEqual((start, how), (now + 2 * H, "price"))


class History(unittest.TestCase):
    def test_last_hold(self):
        t0 = 1_000_000.0
        points = [
            (t0, 50.0),
            (t0 + 600, 60.0),  # 5 min only
            (t0 + 900, 55.0),
            (t0 + 3600, 59.6),
            (t0 + 3900, 61.0),
            (t0 + 4500, None),  # 15 min at >= 59.5, then unavailable
            (t0 + 5000, 60.0),
        ]
        self.assertEqual(last_done_from_history(points, 59.5, 600, t0 + 5300), t0 + 3600 + 600)
        # The last value holds until ``end``.
        self.assertEqual(last_done_from_history(points, 59.5, 600, t0 + 6000), t0 + 5600)
        self.assertIsNone(last_done_from_history(points[:3], 59.5, 600, t0 + 900))
        self.assertIsNone(last_done_from_history([], 59.5, 600, t0))


class Rig:
    def __init__(self, now: datetime, periodic=0, interval=None, stop=None, **settings) -> None:
        self.now = now
        self.guard = LegionellaGuard()
        self.settings = LegionellaSettings.from_values(**settings)
        self.periodic, self.interval, self.stop = periodic, interval, stop
        self.prices = ()
        self.bt7 = 50.0

    def params(self):
        return parameters(self.settings, self.periodic, self.interval, self.stop)

    def tick(self, mode="active", minutes=0, lux=55.0):
        self.now += timedelta(minutes=minutes)
        p = self.params()
        temp = self.guard.state.run_temperature or p.temperature
        self.guard.observe(self.now, self.bt7, temp, self.settings.hold_minutes)
        return self.guard.step(self.now, STHLM, mode, self.settings, self.bt7, p, self.prices, lux)


class Guard(unittest.TestCase):
    def test_counts_from_first_start_and_detects_pump_cycles(self):
        rig = Rig(utc(2026, 10, 1, 12))
        status = rig.tick()
        self.assertEqual(status.state, "idle")
        self.assertEqual(status.info["due"], utc(2026, 10, 14, 12))
        self.assertEqual(rig.guard.state.since, utc(2026, 10, 1, 12))
        rig.bt7 = 60.0
        rig.tick(minutes=1)
        rig.tick(minutes=9)
        self.assertIsNone(rig.guard.state.last_done)
        rig.tick(minutes=1)  # 10 min held
        self.assertEqual(rig.guard.state.last_done, rig.now)
        self.assertEqual(rig.guard.state.last_done_by, "pump")
        done = rig.now
        rig.tick(minutes=5)
        self.assertEqual(rig.guard.state.last_done, done, "one hold counts once")
        rig.bt7 = 59.4  # under the tolerance
        rig.tick(minutes=1)
        self.assertIsNone(rig.guard.hold_since)

    def test_pump_periodic_on_waits_a_day_after_the_interval(self):
        rig = Rig(utc(2026, 10, 1, 12), periodic=1, interval=7)
        rig.guard.state.last_done = utc(2026, 10, 1, 0)
        status = rig.tick()
        self.assertEqual(status.info["due"], utc(2026, 10, 9, 0))
        self.assertIn("pump's periodic increase", status.reason)

    def test_planned_then_runs_and_completes(self):
        start = utc(2026, 10, 14, 10)
        rig = Rig(start)
        rig.guard.state.last_done = start - timedelta(days=13, hours=1)  # due 1 h ago
        rig.prices = slots(start, [5] * 40 + [1] * 8 + [5] * 48)  # cheap 20:00-22:00 UTC
        status = rig.tick()
        self.assertEqual(status.state, "planned")
        self.assertEqual(status.info["planned_start"], utc(2026, 10, 14, 20))
        self.assertFalse(status.run)
        status = rig.tick(minutes=600)
        self.assertEqual(status.state, "running")
        self.assertTrue(status.run)
        self.assertEqual(status.temperature, 60.0)
        self.assertEqual(rig.guard.state.lux_before, 55.0)
        rig.bt7 = 59.6
        rig.tick(minutes=30)
        status = rig.tick(minutes=10)
        self.assertEqual(status.state, "done")
        self.assertFalse(status.run)
        self.assertEqual(rig.guard.state.last_done_by, "ha")
        self.assertEqual(rig.guard.state.last_result, RESULT_DONE)
        self.assertEqual(rig.guard.ended, RESULT_DONE)
        status = rig.tick(minutes=60)
        self.assertEqual(status.state, "done")
        self.assertEqual(status.info["due"], rig.guard.state.last_done + timedelta(days=13))
        status = rig.tick(minutes=7 * 60)
        self.assertEqual(status.state, "idle")

    def test_pin_for_the_optimiser(self):
        start = utc(2026, 10, 14, 10)
        rig = Rig(start)
        rig.guard.state.last_done = start - timedelta(days=13, hours=1)
        rig.prices = slots(start, [5] * 40 + [1] * 8 + [5] * 48)
        status = rig.tick()
        self.assertEqual(legionella_pin(status), (utc(2026, 10, 14, 20), utc(2026, 10, 14, 22), 60.0))
        status = rig.tick(minutes=600)
        self.assertEqual(status.state, "running")
        self.assertEqual(legionella_pin(status), (rig.now, rig.now + 2 * H, 60.0))
        self.assertIsNone(legionella_pin(None))
        rig.guard.state.last_done = rig.now
        self.assertIsNone(legionella_pin(rig.tick(minutes=1)))

    def test_shadow_plans_but_never_runs(self):
        rig = Rig(utc(2026, 10, 14, 10))
        rig.guard.state.last_done = rig.now - timedelta(days=15)  # overdue
        status = rig.tick(mode="shadow")
        self.assertEqual(status.state, "planned")
        self.assertFalse(status.run)
        self.assertIn("would run now", status.reason)
        self.assertIsNone(rig.guard.state.running_since)

    def test_off_and_disabled(self):
        rig = Rig(utc(2026, 10, 14, 10))
        rig.guard.state.last_done = rig.now - timedelta(days=15)
        self.assertEqual(rig.tick(mode="off").state, "off")
        rig.settings = LegionellaSettings.from_values(enabled=False)
        self.assertEqual(rig.tick().state, "disabled")

    def test_timeout_aborts_and_retries_later(self):
        rig = Rig(utc(2026, 10, 14, 10))
        rig.guard.state.last_done = rig.now - timedelta(days=15)
        self.assertEqual(rig.tick().state, "running")
        status = rig.tick(minutes=4 * 60)
        self.assertEqual(status.state, "aborted")
        self.assertEqual(rig.guard.ended, RESULT_TIMEOUT)
        self.assertEqual(rig.guard.state.retry_after, rig.now + RETRY_AFTER)
        status = rig.tick(minutes=60)
        self.assertEqual(status.state, "aborted")
        self.assertFalse(status.run)
        self.assertIsNone(rig.guard.ended)
        status = rig.tick(minutes=11 * 60)
        self.assertEqual(status.state, "running")

    def test_stale_bt7_aborts(self):
        rig = Rig(utc(2026, 10, 14, 10))
        rig.guard.state.last_done = rig.now - timedelta(days=15)
        rig.tick()
        rig.bt7 = None
        status = rig.tick(minutes=1)
        self.assertEqual((status.state, rig.guard.ended), ("aborted", RESULT_BT7))

    def test_leaving_active_cancels(self):
        rig = Rig(utc(2026, 10, 14, 10))
        rig.guard.state.last_done = rig.now - timedelta(days=15)
        rig.tick()
        status = rig.tick(mode="shadow", minutes=1)
        self.assertFalse(status.run)
        self.assertEqual(rig.guard.ended, RESULT_CANCELLED)
        self.assertIsNone(rig.guard.state.retry_after)

    def test_run_now_request(self):
        rig = Rig(utc(2026, 10, 2, 10))
        rig.guard.requested = True
        status = rig.tick(mode="shadow")
        self.assertFalse(status.run)
        self.assertFalse(rig.guard.requested)
        rig.guard.requested = True
        status = rig.tick()
        self.assertTrue(status.run)
        self.assertIn("requested", status.reason)

    def test_lux_stop_override(self):
        rig = Rig(utc(2026, 10, 14, 10))
        rig.guard.state.last_done = rig.now - timedelta(days=15)
        rig.tick(lux=55.0)
        g = rig.guard
        self.assertEqual(g.lux_stop(rig.now, 60.0, True), 55.0)
        rig.tick(mode="shadow", minutes=1)  # ends the cycle
        self.assertEqual(g.lux_stop(rig.now + timedelta(minutes=5), 60.0, False), 55.0)
        self.assertEqual(g.lux_stop(rig.now + timedelta(minutes=20), 54.0, False), 54.0)

    def test_history(self):
        now = utc(2026, 10, 14, 10)
        g = LegionellaGuard()
        g.apply_history(None, now - timedelta(days=10), now)
        self.assertEqual(g.state.since, now - timedelta(days=10))
        self.assertTrue(g.state.history_checked)
        g.apply_history(now - timedelta(days=2), now, now)  # once only
        self.assertIsNone(g.state.last_done)
        g = LegionellaGuard()
        g.apply_history(now - timedelta(days=2), now - timedelta(days=10), now)
        self.assertEqual((g.state.last_done, g.state.last_done_by), (now - timedelta(days=2), "history"))

    def test_state_round_trip_and_tolerant_load(self):
        state = LegionellaState(
            last_done=utc(2026, 10, 1, 2), last_done_by="ha", since=None, history_checked=True,
            last_result=RESULT_DONE, lux_before=55.0, ended_at=utc(2026, 10, 1, 3),
        )
        self.assertEqual(LegionellaState.from_dict(state.to_dict()), state)
        self.assertEqual(LegionellaState.from_dict(None), LegionellaState())
        broken = LegionellaState.from_dict({"last_done": "x", "history_checked": "yes", "lux_before": "a"})
        self.assertEqual(broken, LegionellaState())

    def test_margin(self):
        self.assertEqual(MARGIN, timedelta(days=1))
        self.assertEqual(utc(2026, 1, 1).tzinfo, UTC)


if __name__ == "__main__":
    unittest.main()
