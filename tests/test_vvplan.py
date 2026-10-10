from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import context  # noqa: F401
from context import utc
from logic.inputs import PriceSlot
from logic.vvlearn import SLOTS
from logic.vvplan import (
    HeatWindow,
    PlanSettings,
    PriceLine,
    StopTemps,
    VvControl,
    build_plan,
    day_mode,
    day_peaks,
    day_sums,
    heat_allowed,
    next_heat_start,
    run_end,
    wall_time,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

STHLM = ZoneInfo("Europe/Stockholm")
STOPS = StopTemps(economy=48.0, normal=52.0, luxury=56.0)
TUE, WED, THU, FRI, SUN = 1, 2, 3, 4, 6


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=STHLM)


def profile(values: Dict[int, float]) -> List[float]:
    result = [0.0] * SLOTS
    for slot, value in values.items():
        result[slot] = value
    return result


def quarter_prices(start: datetime, hours: int, price: float, cheap: Optional[Dict[datetime, float]] = None) -> List[PriceSlot]:
    slots = []
    when = start
    for _ in range(hours * 4):
        value = price
        if cheap and when in cheap:
            value = cheap[when]
        slots.append(PriceSlot(when, when + timedelta(minutes=15), value))
        when += timedelta(minutes=15)
    return slots


class SettingsTest(unittest.TestCase):
    def test_defaults_and_validation(self) -> None:
        self.assertEqual(PlanSettings.from_values(), PlanSettings())
        settings = PlanSettings.from_values(day_small=4, day_medium=2, day_large=1, preheat_hours=30, price_window_hours=99)
        self.assertEqual((settings.day_small, settings.day_medium, settings.day_large), (4.0, 3.0, 6.0))
        self.assertEqual(settings.preheat_hours, 12)
        self.assertEqual(settings.price_window_hours, 24.0)
        self.assertEqual(PlanSettings.from_values(preheat_hours=0).window_hours, 1)


class DaysTest(unittest.TestCase):
    def test_early_hours_belong_to_the_evening_before(self) -> None:
        sums = day_sums(profile({0: 1.0, 2: 2.0, FRI * 24 + 1: 4.0}))
        self.assertEqual(sums[SUN], 1.0)  # Monday 00:00
        self.assertEqual(sums[0], 2.0)
        self.assertEqual(sums[THU], 4.0)

    def test_day_mode(self) -> None:
        settings = PlanSettings()
        self.assertEqual([day_mode(v, settings) for v in (0.5, 1.0, 3.0, 6.0)], [0, 1, 2, 3])

    def test_second_peak_needs_threshold_and_gap(self) -> None:
        settings = PlanSettings()
        peaks, two = day_peaks(profile({WED * 24 + 7: 4.0, WED * 24 + 19: 3.5}), WED, settings)
        self.assertEqual((peaks, two), ([7, 19], True))
        peaks, two = day_peaks(profile({WED * 24 + 7: 4.0, WED * 24 + 11: 3.5}), WED, settings)
        self.assertEqual((peaks, two), ([7], False))
        peaks, two = day_peaks(profile({WED * 24 + 7: 4.0, WED * 24 + 19: 2.5}), WED, settings)
        self.assertEqual((peaks, two), ([7], False))
        # A peak just after midnight belongs to the day before (offset 24+).
        peaks, _ = day_peaks(profile({THU * 24 + 1: 4.0}), WED, settings)
        self.assertEqual(peaks, [25])


class PlanTest(unittest.TestCase):
    now = local(2026, 10, 6, 0, 30)  # Tuesday

    def test_window_ends_at_the_peak(self) -> None:
        windows = build_plan(profile({TUE * 24 + 7: 2.0}), self.now, STHLM, PlanSettings(), STOPS)
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertEqual(window.start, local(2026, 10, 6, 5, 0))
        self.assertEqual(window.end, local(2026, 10, 6, 7, 0))
        self.assertEqual(window.day_mode, 1)
        self.assertEqual(window.target, 48.0)
        self.assertIsNone(window.cheap_start)

    def test_two_peaks_cap_luxury_at_normal(self) -> None:
        windows = build_plan(
            profile({WED * 24 + 7: 4.0, WED * 24 + 19: 3.5}), self.now, STHLM, PlanSettings(), STOPS
        )
        self.assertEqual([w.peak for w in windows], [local(2026, 10, 7, 7), local(2026, 10, 7, 19)])
        self.assertEqual({w.day_mode for w in windows}, {2})
        self.assertEqual({w.target for w in windows}, {52.0})

    def test_one_heavy_peak_is_luxury(self) -> None:
        windows = build_plan(profile({WED * 24 + 19: 7.0}), self.now, STHLM, PlanSettings(), STOPS)
        self.assertEqual(windows[0].day_mode, 3)
        self.assertEqual(windows[0].target, 56.0)

    def test_preheat_hours(self) -> None:
        settings = PlanSettings.from_values(preheat_hours=4)
        window = build_plan(profile({TUE * 24 + 7: 2.0}), self.now, STHLM, settings, STOPS)[0]
        self.assertEqual(window.start, local(2026, 10, 6, 3, 0))

    def test_unknown_stop_temperature_gives_no_target(self) -> None:
        window = build_plan(profile({TUE * 24 + 7: 2.0}), self.now, STHLM, PlanSettings(), StopTemps())[0]
        self.assertIsNone(window.target)


class PricePlanTest(unittest.TestCase):
    now = local(2026, 10, 6, 0, 30)
    settings = PlanSettings.from_values(use_price=True)

    def test_cheapest_quarter_hours_before_the_peak(self) -> None:
        cheap = {local(2026, 10, 6, 3, 15) + timedelta(minutes=15 * i): 10.0 for i in range(8)}
        prices = quarter_prices(local(2026, 10, 6, 0, 0), 12, 100.0, cheap)
        window = build_plan(profile({TUE * 24 + 7: 2.0}), self.now, STHLM, self.settings, STOPS, prices)[0]
        self.assertTrue(window.price_placed)
        self.assertEqual(window.start, local(2026, 10, 6, 3, 15))
        self.assertEqual(window.end, local(2026, 10, 6, 5, 15))
        self.assertEqual(window.cheap_start, local(2026, 10, 6, 3, 15))

    def test_ties_go_to_the_later_window(self) -> None:
        prices = quarter_prices(local(2026, 10, 6, 0, 0), 12, 50.0)
        window = build_plan(profile({TUE * 24 + 7: 2.0}), self.now, STHLM, self.settings, STOPS, prices)[0]
        self.assertEqual(window.start, local(2026, 10, 6, 5, 0))

    def test_search_stays_inside_the_price_window(self) -> None:
        # Cheapest at 00:00-02:00, but only 7 h back from 08:00 are searched:
        # 01:00-03:00 (half at 1, half at 20) beats 02:00-04:00 (20).
        cheap = {local(2026, 10, 6, 0, 0) + timedelta(minutes=15 * i): 1.0 for i in range(8)}
        cheap.update({local(2026, 10, 6, 2, 0) + timedelta(minutes=15 * i): 20.0 for i in range(8)})
        prices = quarter_prices(local(2026, 10, 6, 0, 0), 12, 100.0, cheap)
        window = build_plan(profile({TUE * 24 + 8: 2.0}), self.now, STHLM, self.settings, STOPS, prices)[0]
        self.assertEqual(window.start, local(2026, 10, 6, 1, 0))

    def test_hourly_prices_work_too(self) -> None:
        prices = [
            PriceSlot(local(2026, 10, 6, h), local(2026, 10, 6, h) + timedelta(hours=1), 5.0 if h in (1, 2) else 80.0)
            for h in range(0, 12)
        ]
        window = build_plan(profile({TUE * 24 + 7: 2.0}), self.now, STHLM, self.settings, STOPS, prices)[0]
        self.assertEqual(window.start, local(2026, 10, 6, 1, 0))

    def test_without_price_coverage_the_window_stays_at_the_peak(self) -> None:
        prices = quarter_prices(local(2026, 10, 6, 10, 0), 2, 10.0)
        window = build_plan(profile({TUE * 24 + 7: 2.0}), self.now, STHLM, self.settings, STOPS, prices)[0]
        self.assertFalse(window.price_placed)
        self.assertEqual(window.start, local(2026, 10, 6, 5, 0))

    def test_price_line_average(self) -> None:
        line = PriceLine(quarter_prices(utc(2026, 1, 1), 1, 10.0, {utc(2026, 1, 1, 0, 30): 30.0}))
        self.assertAlmostEqual(line.average(utc(2026, 1, 1, 0, 15), utc(2026, 1, 1, 0, 45)), 20.0)
        self.assertIsNone(line.average(utc(2026, 1, 1, 0, 30), utc(2026, 1, 1, 1, 30)))


class DstPlanTest(unittest.TestCase):
    def test_spring_peak_uses_the_real_instant(self) -> None:
        now = local(2026, 3, 29, 0, 30)
        window = build_plan(profile({SUN * 24 + 7: 2.0}), now, STHLM, PlanSettings(), STOPS)[0]
        self.assertEqual(window.peak, utc(2026, 3, 29, 5, 0))  # 07:00 CEST
        self.assertEqual(window.end - window.start, timedelta(hours=2))

    def test_autumn_window_is_two_real_hours(self) -> None:
        now = local(2026, 10, 25, 0, 30)
        window = build_plan(profile({SUN * 24 + 3: 2.0}), now, STHLM, PlanSettings(), STOPS)[0]
        self.assertEqual(window.peak, utc(2026, 10, 25, 2, 0))  # 03:00 CET
        self.assertEqual(window.start, utc(2026, 10, 25, 0, 0))  # 02:00 CEST

    def test_autumn_prices_by_instant(self) -> None:
        now = local(2026, 10, 24, 20, 0)
        cheap = {utc(2026, 10, 25, 0, 15) + timedelta(minutes=15 * i): 1.0 for i in range(8)}
        prices = quarter_prices(utc(2026, 10, 24, 20, 0), 10, 90.0, cheap)
        settings = PlanSettings.from_values(use_price=True)
        windows = build_plan(profile({SUN * 24 + 4: 2.0}), now, STHLM, settings, STOPS, prices)
        self.assertEqual(windows[0].start, utc(2026, 10, 25, 0, 15))

    def test_wall_time(self) -> None:
        self.assertEqual(wall_time(date(2026, 3, 28), 25, STHLM), utc(2026, 3, 29, 0, 0))
        self.assertEqual(wall_time(date(2026, 10, 25), 3, STHLM), utc(2026, 10, 25, 2, 0))


def window(start: datetime, end: datetime, peak: datetime, mode: int = 2, target: Optional[float] = 52.0, cheap: Optional[datetime] = None) -> HeatWindow:
    return HeatWindow(
        start, end, peak, start.date(), mode, target,
        cheap, cheap + timedelta(hours=2) if cheap is not None else None,
    )


class HeatAllowedTest(unittest.TestCase):
    def test_cheap_hours_then_the_last_two(self) -> None:
        plan = [window(local(2026, 10, 6, 1), local(2026, 10, 6, 7), local(2026, 10, 6, 7), cheap=local(2026, 10, 6, 2))]
        self.assertFalse(heat_allowed(plan, local(2026, 10, 6, 1, 30)))
        self.assertTrue(heat_allowed(plan, local(2026, 10, 6, 2, 30)))
        self.assertFalse(heat_allowed(plan, local(2026, 10, 6, 4, 30)))
        self.assertTrue(heat_allowed(plan, local(2026, 10, 6, 5, 30)))

    def test_without_cheap_hours_or_windows(self) -> None:
        self.assertTrue(heat_allowed([], local(2026, 10, 6, 1)))
        plan = [window(local(2026, 10, 6, 1), local(2026, 10, 6, 3), local(2026, 10, 6, 3))]
        self.assertTrue(heat_allowed(plan, local(2026, 10, 6, 2)))
        self.assertFalse(heat_allowed(plan, local(2026, 10, 6, 4)))

    def test_run_end_joins_touching_windows(self) -> None:
        plan = [
            window(local(2026, 10, 6, 5), local(2026, 10, 6, 7), local(2026, 10, 6, 7)),
            window(local(2026, 10, 6, 7), local(2026, 10, 6, 9), local(2026, 10, 6, 9)),
            window(local(2026, 10, 6, 17), local(2026, 10, 6, 19), local(2026, 10, 6, 19)),
        ]
        self.assertEqual(run_end(plan, local(2026, 10, 6, 6)), local(2026, 10, 6, 9))
        self.assertEqual(next_heat_start(plan, local(2026, 10, 6, 10), None, False), local(2026, 10, 6, 17))
        self.assertEqual(next_heat_start(plan, local(2026, 10, 6, 6), local(2026, 10, 6, 9), False), local(2026, 10, 6, 17))


class ControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.control = VvControl()
        self.plan = [
            window(local(2026, 10, 6, 5), local(2026, 10, 6, 7), local(2026, 10, 6, 7), mode=1, target=48.0),
            window(local(2026, 10, 6, 17), local(2026, 10, 6, 19), local(2026, 10, 6, 19), mode=2, target=52.0),
        ]

    def decide(self, bt7: float, when: datetime, floor: bool = True, floor_temp: Optional[float] = 35.0, use_price: bool = False, stops: StopTemps = STOPS):
        return self.control.decide(bt7, when, STHLM, self.plan, stops, floor, floor_temp, use_price)

    def test_floor_heats_with_hysteresis(self) -> None:
        result = self.decide(34.0, local(2026, 10, 6, 12))
        self.assertEqual((result.heat, result.target, result.source), (True, 39.0, "floor"))
        self.assertTrue(self.decide(38.0, local(2026, 10, 6, 12, 10)).heat)
        self.assertFalse(self.decide(39.0, local(2026, 10, 6, 12, 20)).heat)
        self.assertFalse(self.decide(36.0, local(2026, 10, 6, 12, 30)).heat)

    def test_floor_is_capped_at_the_luxury_stop(self) -> None:
        result = self.decide(40.0, local(2026, 10, 6, 12), floor_temp=54.0)
        self.assertEqual(result.target, 56.0)
        result = VvControl().decide(40.0, local(2026, 10, 6, 12), STHLM, [], STOPS, True, 55.0, False)
        self.assertEqual(result.target, 56.0)

    def test_floor_off(self) -> None:
        self.assertFalse(self.decide(20.0, local(2026, 10, 6, 12), floor=False).heat)

    def test_plan_heats_then_closes_the_window(self) -> None:
        self.assertFalse(self.decide(45.0, local(2026, 10, 6, 4, 50)).heat)
        result = self.decide(45.0, local(2026, 10, 6, 5, 10))
        self.assertEqual((result.heat, result.target, result.source), (True, 48.0, "plan"))
        self.assertFalse(self.decide(48.0, local(2026, 10, 6, 5, 40)).heat)
        self.assertEqual(self.control.closed_until, local(2026, 10, 6, 7))
        # A draw in the same window does not reopen it.
        self.assertFalse(self.decide(44.0, local(2026, 10, 6, 6, 0)).heat)
        # The second peak's window heats again.
        self.assertTrue(self.decide(44.0, local(2026, 10, 6, 17, 0)).heat)

    def test_price_control_waits_for_the_cheap_hours(self) -> None:
        self.plan = [
            window(local(2026, 10, 6, 1), local(2026, 10, 6, 7), local(2026, 10, 6, 7), target=52.0, cheap=local(2026, 10, 6, 2))
        ]
        self.assertFalse(self.decide(45.0, local(2026, 10, 6, 1, 30), use_price=True).heat)
        self.assertTrue(self.decide(45.0, local(2026, 10, 6, 2, 30), use_price=True).heat)
        self.assertTrue(self.decide(45.0, local(2026, 10, 6, 1, 30), use_price=False).heat)

    def test_window_without_target(self) -> None:
        self.plan = [window(local(2026, 10, 6, 5), local(2026, 10, 6, 7), local(2026, 10, 6, 7), target=None)]
        self.assertFalse(self.decide(40.0, local(2026, 10, 6, 6), floor=False).heat)


if __name__ == "__main__":
    unittest.main()
