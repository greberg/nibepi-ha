from __future__ import annotations

import time
import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.optimizer.horizon import HorizonSlot
from logic.optimizer.tank import (
    DrawForecast,
    PinnedCharge,
    TankModel,
    TankSettings,
    draws_from_week_profile,
    plan_hot_water,
    slot_draws,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

START = utc(2026, 1, 14, 23)


def day(prices_by_hour, hours=24):
    out = []
    for i in range(hours * 4):
        a = START + timedelta(minutes=15 * i)
        out.append(HorizonSlot(a, a + timedelta(minutes=15), prices_by_hour(i // 4 % 24), -5.0))
    return out


def se3(hour: int) -> float:
    if hour < 5:
        return 0.8
    if 6 <= hour < 9 or 16 <= hour < 20:
        return 2.5
    return 1.4


MORNING = DrawForecast(START + timedelta(hours=7), START + timedelta(hours=8), temperature_drop=8.0, confidence=0.8)
EVENING = DrawForecast(START + timedelta(hours=20), START + timedelta(hours=21), expected_kwh=2.0, confidence=0.6)


class TankTest(unittest.TestCase):
    def test_charges_before_draws_in_cheap_slots(self) -> None:
        settings = TankSettings()
        plan = plan_hot_water(day(se3), 46.0, [MORNING, EVENING], TankModel(), settings)
        charges = [b for b in plan.blocks if b.action == "charge"]
        self.assertTrue(charges)
        # The morning draw's charge happens in the cheap night, before 07:00.
        first = charges[0]
        self.assertLess(first.end, MORNING.start)
        self.assertLess(first.start, START + timedelta(hours=5))
        # No planned charge in a peak hour.
        for step in plan.steps:
            if step.charging and not step.forced:
                self.assertLess(step.price, 2.5)
        self.assertGreaterEqual(plan.min_temp, settings.floor - 0.05)
        self.assertGreater(plan.savings, 0)

    def test_never_below_floor_even_with_expensive_prices(self) -> None:
        settings = TankSettings(floor=42.0)
        plan = plan_hot_water(day(lambda h: 3.0), 44.0, [MORNING, EVENING], TankModel(), settings)
        self.assertGreaterEqual(plan.min_temp, settings.floor - 0.05)

    def test_large_draw_uses_lux(self) -> None:
        big = DrawForecast(START + timedelta(hours=7), START + timedelta(hours=7, minutes=30), temperature_drop=13.0)
        plan = plan_hot_water(day(se3), 46.0, [big], TankModel(), TankSettings())
        charge = [b for b in plan.blocks if b.action == "charge"][0]
        self.assertEqual(charge.target, 55.0)
        self.assertTrue(charge.boost)
        self.assertEqual(charge.mode, 2)
        self.assertGreaterEqual(plan.min_temp, 40.0 - 0.05)

    def test_pinned_legionella_replaces_the_charge_before_it(self) -> None:
        pin = PinnedCharge(START + timedelta(hours=3), START + timedelta(hours=5), 60.0)
        plan = plan_hot_water(day(se3), 46.0, [MORNING], TankModel(), TankSettings(), pinned=[pin])
        legionella = [b for b in plan.blocks if b.action == "legionella"]
        self.assertEqual(len(legionella), 1)
        self.assertEqual((legionella[0].start, legionella[0].end, legionella[0].target), (pin.start, pin.end, 60.0))
        # The pinned charge covers the morning draw: no extra charge before it.
        charges = [b for b in plan.blocks if b.action == "charge" and b.start < MORNING.start]
        self.assertEqual(charges, [])
        pinned = [s for s in plan.steps if s.pinned]
        self.assertEqual(len(pinned), 8)
        self.assertGreater(pinned[-1].t_end, 59.0)
        # Without the pin the plan charges before the draw.
        free = plan_hot_water(day(se3), 46.0, [MORNING], TankModel(), TankSettings())
        self.assertTrue([b for b in free.blocks if b.action == "charge" and b.start < MORNING.start])

    def test_pin_is_in_the_baseline_too(self) -> None:
        pin = PinnedCharge(START + timedelta(hours=3), START + timedelta(hours=5), 60.0)
        flat = day(lambda h: 1.0)
        with_pin = plan_hot_water(flat, 48.0, [], TankModel(), TankSettings(), pinned=[pin])
        without = plan_hot_water(flat, 48.0, [], TankModel(), TankSettings())
        # The cycle happens either way: it does not show as savings or loss.
        self.assertGreater(with_pin.cost, without.cost)
        self.assertGreater(with_pin.baseline_cost, without.baseline_cost)

    def test_no_draws_no_charges(self) -> None:
        plan = plan_hot_water(day(lambda h: 1.0), 48.0, [], TankModel(), TankSettings())
        charges = [b for b in plan.blocks if b.action == "charge"]
        self.assertEqual(charges, [])

    def test_low_confidence_plans_more(self) -> None:
        sure = DrawForecast(MORNING.start, MORNING.end, expected_kwh=2.0, confidence=1.0)
        unsure = DrawForecast(MORNING.start, MORNING.end, expected_kwh=2.0, confidence=0.0)
        slots = day(se3)
        self.assertAlmostEqual(sum(slot_draws(slots, [sure], TankModel(), 0.5)), 2.0)
        self.assertAlmostEqual(sum(slot_draws(slots, [unsure], TankModel(), 0.5)), 3.0)

    @unittest.skipIf(ZoneInfo is None, "zoneinfo")
    def test_week_profile(self) -> None:
        tz = ZoneInfo("Europe/Stockholm")
        profile = [0.0] * 168
        profile[3 * 24 + 7] = 6.0  # Thursday 07:00 local
        # START is Thursday 2026-01-15 00:00 local.
        draws = draws_from_week_profile(profile, START, START + timedelta(days=1), tz, confidence=0.5)
        self.assertEqual(len(draws), 1)
        self.assertEqual(draws[0].start, START + timedelta(hours=7))
        self.assertEqual(draws[0].temperature_drop, 6.0)
        self.assertEqual(draws_from_week_profile([1.0] * 10, START, START + timedelta(days=1), tz), [])

    def test_performance(self) -> None:
        slots = day(se3, hours=36)
        started = time.perf_counter()
        plan_hot_water(slots, 46.0, [MORNING, EVENING], TankModel(), TankSettings())
        self.assertLess(time.perf_counter() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
