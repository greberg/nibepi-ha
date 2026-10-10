from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence, Tuple

import context  # noqa: F401
from context import utc
from logic.vvlearn import (
    SLOTS,
    DrawDetector,
    DrawEvent,
    VvProfile,
    is_charging,
    learning_alpha,
    min_drop,
    week_slot,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore

STHLM = ZoneInfo("Europe/Stockholm")


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=STHLM)


def run(
    detector: DrawDetector,
    start: datetime,
    values: Sequence[float],
    charging: Optional[Sequence[bool]] = None,
    drop_min: float = 3.0,
) -> Tuple[List[DrawEvent], datetime]:
    """One value per minute from ``start``; returns the closed events."""
    events = []
    now = start
    for index, value in enumerate(values):
        now = start + timedelta(minutes=index)
        flag = charging[index] if charging is not None else False
        event = detector.step(value, flag, now, STHLM, drop_min)
        if event is not None:
            events.append(event)
    return events, now


def shower(before: int = 30, drop: float = 8.0, minutes: int = 8, after: int = 30, top: float = 50.0) -> List[float]:
    values = [top] * before
    step = drop / minutes
    values += [round(top - step * (i + 1), 3) for i in range(minutes)]
    values += [top - drop] * after
    return values


class SettingsTest(unittest.TestCase):
    def test_alpha(self) -> None:
        self.assertAlmostEqual(learning_alpha(None), 0.10)
        self.assertAlmostEqual(learning_alpha(0), 0.10)
        self.assertAlmostEqual(learning_alpha(5), 0.05)
        self.assertAlmostEqual(learning_alpha(0.5), 0.01)
        self.assertAlmostEqual(learning_alpha(30), 0.10)

    def test_min_drop(self) -> None:
        self.assertEqual(min_drop(None), 3.0)
        self.assertEqual(min_drop(-1), 3.0)
        self.assertEqual(min_drop(5), 5.0)

    def test_charging(self) -> None:
        self.assertTrue(is_charging(20))
        self.assertTrue(is_charging("20"))
        self.assertTrue(is_charging("Hot water"))
        self.assertFalse(is_charging(30))
        self.assertFalse(is_charging(None))


class DrawDetectionTest(unittest.TestCase):
    def test_shower_is_learned_on_its_hour(self) -> None:
        detector = DrawDetector()
        # Tuesday 2026-10-06, BT6 50 °C from 06:40, a shower at 07:10.
        events, _ = run(detector, local(2026, 10, 6, 6, 40), shower())
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertTrue(event.learned)
        self.assertAlmostEqual(event.drop, 8.0, places=2)
        self.assertEqual(event.reason, "no new low for 15 min")
        self.assertEqual(week_slot(event.booked, STHLM), 1 * 24 + 7)

    def test_standby_loss_is_not_a_draw(self) -> None:
        detector = DrawDetector()
        # 0.5 °C/h for 8 hours.
        values = [55.0 - 0.5 * i / 60.0 for i in range(8 * 60)]
        events, _ = run(detector, local(2026, 10, 6, 0, 0), values)
        self.assertEqual(events, [])
        self.assertFalse(detector.in_event)

    def test_small_draw_is_rejected(self) -> None:
        detector = DrawDetector()
        events, _ = run(detector, local(2026, 10, 6, 6, 40), shower(drop=2.0, minutes=2))
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].learned)
        self.assertEqual(events[0].verdict, "below min drop")

    def test_min_drop_setting(self) -> None:
        detector = DrawDetector()
        events, _ = run(detector, local(2026, 10, 6, 6, 40), shower(drop=2.0, minutes=2), drop_min=1.5)
        self.assertTrue(events[0].learned)

    def test_recovery_closes_the_event(self) -> None:
        detector = DrawDetector()
        values = shower(after=0) + [42.0, 42.6]
        events, _ = run(detector, local(2026, 10, 6, 6, 40), values)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reason, "recovered")
        self.assertTrue(events[0].learned)

    def test_charging_evaluates_the_open_draw_and_restarts(self) -> None:
        detector = DrawDetector()
        values = shower(after=3) + [30.0] * 20 + [50.0] * 30
        charging = [False] * (30 + 8 + 3) + [True] * 20 + [False] * 30
        events, _ = run(detector, local(2026, 10, 6, 6, 40), values, charging)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].reason, "charging started")
        self.assertTrue(events[0].learned)
        # The dip during the charge and the climb after it are not draws.
        self.assertFalse(detector.in_event)
        self.assertEqual(detector.peak, 50.0)

    def test_settle_time_after_charging(self) -> None:
        detector = DrawDetector()
        start = local(2026, 10, 6, 6, 0)
        detector.step(50.0, True, start, STHLM)
        # 5 minutes after the charge BT6 still settles: ignored.
        detector.step(45.0, False, start + timedelta(minutes=5), STHLM)
        self.assertIsNone(detector.peak)
        detector.step(50.0, False, start + timedelta(minutes=11), STHLM)
        self.assertEqual(detector.peak, 50.0)

    def test_missing_bt6_resets(self) -> None:
        detector = DrawDetector()
        run(detector, local(2026, 10, 6, 6, 40), [50.0, 50.0, 48.5])
        self.assertTrue(detector.in_event)
        self.assertIsNone(detector.step(None, False, local(2026, 10, 6, 6, 43), STHLM))
        self.assertFalse(detector.in_event)
        self.assertIsNone(detector.peak)

    def test_slow_event_is_rebased(self) -> None:
        detector = DrawDetector()
        # Cools 0.95 °C/h until 1 °C under the peak (opens an event at
        # minute 68), then 0.3 °C/h: after 30 min in the event it crawls
        # under 0.9 °C/h and is re-based instead of closed.
        values = [50.0] * 5
        values += [50.0 - 0.95 * (m - 4) / 60.0 for m in range(5, 69)]
        values += [values[-1] - 0.3 * (m - 68) / 60.0 for m in range(69, 99)]
        start = local(2026, 10, 6, 6, 0)
        events, _ = run(detector, start, values)
        self.assertEqual(events, [])
        self.assertTrue(detector.in_event)
        self.assertEqual(detector.start_ts, start + timedelta(minutes=98))
        self.assertEqual(detector.peak, detector.trough)

    def test_drop_is_booked_on_the_steepest_hour(self) -> None:
        detector = DrawDetector()
        # 6 °C at 18:50-18:56, then 1 °C more until the trough at 19:09:
        # the drop is booked on hour 18, not on the trough's hour.
        values = [50.0, 50.0, 49.0, 48.0, 47.0, 46.0, 45.0, 44.0, 43.0, 43.0, 43.0, 43.0]
        values += [round(43.0 - 0.1 * (i + 1), 2) for i in range(10)]
        values += [42.0] * 20
        events, _ = run(detector, local(2026, 10, 6, 18, 48), values)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0].learned)
        self.assertAlmostEqual(events[0].drop, 8.0)
        self.assertEqual(week_slot(events[0].booked, STHLM), 24 + 18)


class ProfileTest(unittest.TestCase):
    def test_register_blends(self) -> None:
        profile = VvProfile()
        when = local(2026, 10, 6, 7, 5)
        self.assertTrue(profile.register_draw(8.0, when, when, STHLM, 0.1))
        slot = 24 + 7
        self.assertAlmostEqual(profile.profile[slot], 0.8)
        profile.register_draw(8.0, when, when, STHLM, 0.1)
        self.assertAlmostEqual(profile.profile[slot], 1.52)
        self.assertTrue(profile.seen[slot])
        self.assertEqual(profile.last_slot, slot)
        self.assertEqual(profile.learned, 2)

    def test_tiny_drop_is_ignored(self) -> None:
        profile = VvProfile()
        when = local(2026, 10, 6, 7, 5)
        self.assertFalse(profile.register_draw(0.1, when, when, STHLM, 0.1))
        self.assertTrue(profile.is_empty())

    def test_first_day_only_initialises(self) -> None:
        profile = VvProfile()
        profile.profile[7] = 5.0
        self.assertTrue(profile.apply_daily_decay(date(2026, 10, 5), 0.1))
        self.assertEqual(profile.seen_day, "2026-10-05")
        self.assertEqual(profile.profile[7], 5.0)
        self.assertFalse(profile.apply_daily_decay(date(2026, 10, 5), 0.1))

    def test_unseen_hours_of_the_ended_day_decay(self) -> None:
        profile = VvProfile()
        profile.apply_daily_decay(date(2026, 10, 5), 0.1)  # a Monday
        profile.profile[7] = 5.0
        profile.profile[8] = 5.0
        profile.profile[24 + 8] = 5.0  # Tuesday
        profile.seen[7] = True
        profile.apply_daily_decay(date(2026, 10, 6), 0.1)
        self.assertEqual(profile.profile[7], 5.0)
        self.assertEqual(profile.profile[8], 4.5)
        self.assertEqual(profile.profile[24 + 8], 5.0)
        self.assertEqual(profile.seen, [False] * SLOTS)
        self.assertEqual(profile.last_decay_day, "2026-10-06")

    def test_skipped_days_decay_once_each(self) -> None:
        profile = VvProfile()
        profile.apply_daily_decay(date(2026, 10, 5), 0.1)  # Monday
        for day in range(7):
            profile.profile[day * 24 + 7] = 10.0
        profile.apply_daily_decay(date(2026, 10, 8), 0.1)  # Thursday
        self.assertEqual(profile.profile[0 * 24 + 7], 9.0)  # Monday ended
        self.assertEqual(profile.profile[1 * 24 + 7], 9.0)  # Tuesday skipped
        self.assertEqual(profile.profile[2 * 24 + 7], 9.0)  # Wednesday skipped
        self.assertEqual(profile.profile[3 * 24 + 7], 10.0)  # today
        self.assertEqual(profile.profile[4 * 24 + 7], 10.0)

    def test_clock_going_back_does_nothing(self) -> None:
        profile = VvProfile()
        profile.apply_daily_decay(date(2026, 10, 6), 0.1)
        profile.profile[7] = 5.0
        self.assertFalse(profile.apply_daily_decay(date(2026, 10, 5), 0.1))
        self.assertEqual(profile.profile[7], 5.0)
        self.assertEqual(profile.seen_day, "2026-10-06")

    def test_draw_booked_before_midnight_does_not_redecay(self) -> None:
        profile = VvProfile()
        profile.apply_daily_decay(date(2026, 10, 6), 0.1)
        profile.profile[24 + 3] = 5.0  # Tuesday 03:00, unseen
        now = local(2026, 10, 7, 0, 20)  # Wednesday
        profile.apply_daily_decay(now.date(), 0.1)
        self.assertEqual(profile.profile[24 + 3], 4.5)
        booked = local(2026, 10, 6, 23, 50)
        profile.register_draw(6.0, booked, now, STHLM, 0.1)
        self.assertEqual(profile.seen_day, "2026-10-07")
        self.assertEqual(profile.profile[24 + 3], 4.5)
        self.assertAlmostEqual(profile.profile[24 + 23], 0.6)

    def test_round_trip_and_tolerant_load(self) -> None:
        profile = VvProfile()
        when = local(2026, 10, 6, 7, 5)
        profile.register_draw(8.0, when, when, STHLM, 0.1)
        again = VvProfile.from_dict(profile.to_dict())
        self.assertEqual(again, profile)
        broken = VvProfile.from_dict({"profile": [1, 2], "seen_day": "nope", "learned": "x"})
        self.assertEqual(broken.profile, [0.0] * SLOTS)
        self.assertIsNone(broken.seen_day)
        self.assertEqual(VvProfile.from_dict(None), VvProfile())


class DstTest(unittest.TestCase):
    def test_autumn_double_hour_maps_to_one_slot(self) -> None:
        first = utc(2026, 10, 25, 0, 30)  # 02:30 CEST
        second = utc(2026, 10, 25, 1, 30)  # 02:30 CET
        self.assertEqual(week_slot(first, STHLM), 6 * 24 + 2)
        self.assertEqual(week_slot(second, STHLM), 6 * 24 + 2)
        self.assertEqual(week_slot(utc(2026, 10, 25, 2, 30), STHLM), 6 * 24 + 3)

    def test_spring_skips_two_oclock(self) -> None:
        self.assertEqual(week_slot(utc(2026, 3, 29, 0, 30), STHLM), 6 * 24 + 1)
        self.assertEqual(week_slot(utc(2026, 3, 29, 1, 30), STHLM), 6 * 24 + 3)

    def test_learning_through_the_dst_week(self) -> None:
        """Draws every evening at 19:10 local, Mon 2026-10-19 .. Sun 10-25."""
        detector = DrawDetector()
        profile = VvProfile()
        start = datetime(2026, 10, 19, 12, 0, tzinfo=STHLM).astimezone(utc(2026, 1, 1).tzinfo)
        end = start + timedelta(days=7)
        now = start
        while now < end:
            here = now.astimezone(STHLM)
            minute = here.hour * 60 + here.minute
            if 19 * 60 + 10 <= minute < 19 * 60 + 18:
                bt6 = 50.0 - (minute - (19 * 60 + 9))
            elif 19 * 60 + 18 <= minute < 20 * 60:
                bt6 = 42.0
            else:
                bt6 = 50.0
            profile.apply_daily_decay(here.date(), 0.1)
            event = detector.step(bt6, False, now, STHLM)
            if event is not None and event.learned:
                profile.register_draw(event.drop, event.booked, now, STHLM, 0.1)
            now += timedelta(minutes=1)
        for day in range(7):
            self.assertAlmostEqual(profile.profile[day * 24 + 19], 0.8, places=3, msg=day)
        self.assertEqual(profile.learned, 7)
        # Sunday ran 25 hours; its date is still one day in the profile.
        self.assertEqual(profile.seen_day, "2026-10-26")


if __name__ == "__main__":
    unittest.main()
