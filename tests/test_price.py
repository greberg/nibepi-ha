from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

import context  # noqa: F401
from context import utc
from logic.inputs import PriceSlot, Reading
from logic.price import (
    PriceCache,
    PricePoint,
    apply_thresholds,
    compute_level,
    compute_price,
    find_optimal_combination,
    find_trading_opportunities,
    hourly_average,
    levels_from_trades,
    normalize_prices,
    optimize_trading,
    slot_at,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

STHLM = ZoneInfo("Europe/Stockholm")


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=STHLM)


class NormalizeTest(unittest.TestCase):
    def test_nordpool_raw_quarter_hours(self) -> None:
        start = local(2026, 1, 10, 0, 0)
        raw = [
            {"start": start + timedelta(minutes=15 * i), "end": start + timedelta(minutes=15 * (i + 1)), "value": i / 100}
            for i in range(96)
        ]
        tomorrow_start = start + timedelta(days=1)
        raw_tomorrow = [
            {"start": (tomorrow_start + timedelta(minutes=15 * i)).isoformat(),
             "end": (tomorrow_start + timedelta(minutes=15 * (i + 1))).isoformat(), "value": 1.0}
            for i in range(96)
        ]
        attrs = {"raw_today": raw, "raw_tomorrow": raw_tomorrow, "today": [0.0] * 96, "current_price": 0.5}
        slots = normalize_prices(attrs, STHLM, local(2026, 1, 10, 12, 0))
        self.assertEqual(len(slots), 192)
        self.assertEqual(slots[1].end - slots[1].start, timedelta(minutes=15))
        # raw_today wins over the numeric today list for the same instants.
        self.assertAlmostEqual(slots[5].price, 0.05)
        self.assertEqual(slots[96].start, tomorrow_start)

    def test_numbers_hourly_and_quarter(self) -> None:
        now = local(2026, 1, 10, 12, 0)
        hourly = normalize_prices({"today": list(range(24))}, STHLM, now)
        self.assertEqual(len(hourly), 24)
        self.assertEqual(hourly[13].start, local(2026, 1, 10, 13, 0))
        self.assertEqual(hourly[13].end - hourly[13].start, timedelta(hours=1))
        quarter = normalize_prices({"today": [1.0] * 96, "tomorrow": [2.0] * 96}, STHLM, now)
        self.assertEqual(len(quarter), 192)
        self.assertEqual(quarter[4].start, local(2026, 1, 10, 1, 0))
        self.assertEqual(quarter[96].start, local(2026, 1, 11, 0, 0))
        self.assertEqual(quarter[96].price, 2.0)

    def test_numbers_with_none(self) -> None:
        now = local(2026, 1, 10, 12, 0)
        slots = normalize_prices({"today": [1.0] * 24, "tomorrow": [None] * 24}, STHLM, now)
        self.assertEqual(len(slots), 24)

    def test_dst_spring_day(self) -> None:
        # 2026-03-29: 23 h in Stockholm, 92 quarter-hour values.
        now = local(2026, 3, 29, 12, 0)
        slots = normalize_prices({"today": list(range(92))}, STHLM, now)
        self.assertEqual(len(slots), 92)
        self.assertEqual(slots[0].start, local(2026, 3, 29, 0, 0))
        # 02:00 does not exist: the 9th slot starts at 03:00 CEST = 01:00 UTC.
        self.assertEqual(slots[8].start.astimezone(timezone.utc), utc(2026, 3, 29, 1, 0))
        self.assertEqual(slots[-1].end, local(2026, 3, 30, 0, 0))
        hours = hourly_average(slots)
        self.assertEqual(len(hours), 23)

    def test_dst_autumn_day(self) -> None:
        # 2026-10-25: 25 h, 100 quarter-hour values; 02:00 happens twice.
        now = local(2026, 10, 25, 12, 0)
        slots = normalize_prices({"today": list(range(100))}, STHLM, now)
        self.assertEqual(len(slots), 100)
        self.assertEqual(slots[-1].end.astimezone(timezone.utc), local(2026, 10, 26, 0, 0).astimezone(timezone.utc))
        hours = hourly_average(slots)
        # The two 02:00 hours stay apart (config_node merged them).
        self.assertEqual(len(hours), 25)
        self.assertEqual(hours[2].price, sum(range(8, 12)) / 4)
        self.assertEqual(hours[3].price, sum(range(12, 16)) / 4)

    def test_tibber_style(self) -> None:
        today = [{"startsAt": "2026-01-10T%02d:00:00.000+01:00" % h, "total": 0.5 + h / 100} for h in range(24)]
        attrs = {"prices": today}
        slots = normalize_prices(attrs, STHLM, local(2026, 1, 10, 12))
        self.assertEqual(len(slots), 24)
        self.assertEqual(slots[0].end, local(2026, 1, 10, 1, 0))
        self.assertEqual(slots[-1].end, local(2026, 1, 11, 0, 0))  # inferred
        quarters = [
            {"startsAt": (local(2026, 1, 10) + timedelta(minutes=15 * i)).isoformat(), "total": 1.0}
            for i in range(96)
        ]
        slots = normalize_prices({"today": quarters}, STHLM, local(2026, 1, 10, 12))
        self.assertEqual(len(slots), 96)
        self.assertEqual(slots[0].end - slots[0].start, timedelta(minutes=15))

    def test_z_suffix_and_garbage(self) -> None:
        attrs = {"raw_today": [
            {"start": "2026-01-10T11:00:00Z", "end": "2026-01-10T12:00:00Z", "value": 1},
            {"start": "nonsense", "value": 2},
            {"start": "2026-01-10T12:00:00Z", "end": "2026-01-10T13:00:00Z", "value": None},
        ]}
        slots = normalize_prices(attrs, STHLM, utc(2026, 1, 10, 11, 30))
        self.assertEqual(len(slots), 1)
        self.assertIsNotNone(slot_at(slots, utc(2026, 1, 10, 11, 30)))

    def test_hourly_average(self) -> None:
        start = local(2026, 1, 10, 0, 0)
        slots = [PriceSlot(start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)), float(i)) for i in range(8)]
        hours = hourly_average(slots)
        self.assertEqual(len(hours), 2)
        self.assertEqual(hours[0].price, 1.5)
        self.assertEqual(hours[1].price, 5.5)
        self.assertEqual(hours[1].start, local(2026, 1, 10, 1, 0))


def points(values: list) -> list:
    return [PricePoint(3600.0 * i, float(v)) for i, v in enumerate(values)]


class LevelTest(unittest.TestCase):
    def test_flat_prices_are_normal(self) -> None:
        self.assertEqual(optimize_trading(points([50] * 24)), ([], []))
        # Spread equal to min_spread is not enough.
        self.assertEqual(optimize_trading(points([50, 70])), ([], []))

    def test_single_trade(self) -> None:
        buys, sells = optimize_trading(points([50, 10, 50, 80, 50]), 12, 20)
        self.assertEqual([p.ts for p in buys], [3600.0])
        self.assertEqual([p.ts for p in sells], [3 * 3600.0])
        levels = levels_from_trades(buys, sells)
        # One buy: index floor(1/2)=0, so it is CHEAP, not VERY_CHEAP.
        self.assertEqual(levels[3600.0], "CHEAP")
        self.assertEqual(levels[3 * 3600.0], "VERY_EXPENSIVE")

    def test_window_limits_pairs(self) -> None:
        data = points([10, 50, 50, 50, 90])
        sorted_data = sorted(data, key=lambda p: p.value)
        # Window 2 h: from index 0 only index 1 is reachable.
        opp = find_trading_opportunities(sorted_data, data, 2, 20)
        self.assertTrue(all(t.sell.ts - t.buy.ts < 2 * 3600 for t in opp))
        self.assertEqual(opp[0].spread, 40)

    def test_quarter_hour_window_scaling(self) -> None:
        data = [PricePoint(900.0 * i, v) for i, v in enumerate([10] + [50] * 7 + [90])]
        sorted_data = sorted(data, key=lambda p: p.value)
        # 2 h window over 15 min points = 8 slots: index 8 is out of reach from 0.
        opp = find_trading_opportunities(sorted_data, data, 2, 20)
        self.assertNotIn((0.0, 8 * 900.0), [(t.buy.ts, t.sell.ts) for t in opp])
        opp = find_trading_opportunities(sorted_data, data, 3, 20)
        self.assertIn((0.0, 8 * 900.0), [(t.buy.ts, t.sell.ts) for t in opp])

    def test_combination_is_non_overlapping(self) -> None:
        data = points([10, 100, 20, 90, 15, 95])
        buys, sells = optimize_trading(data, 12, 20)
        stamps = [p.ts for p in buys] + [p.ts for p in sells]
        self.assertEqual(len(stamps), len(set(stamps)))
        self.assertEqual(sorted(p.value for p in buys), [10, 15, 20])
        self.assertEqual(sorted(p.value for p in sells), [90, 95, 100])
        levels = levels_from_trades(buys, sells)
        self.assertEqual(levels[0.0], "VERY_CHEAP")  # cheapest of 3 buys
        self.assertEqual(levels[2 * 3600.0], "CHEAP")
        self.assertEqual(levels[3 * 3600.0], "EXPENSIVE")
        self.assertEqual(levels[1 * 3600.0], "VERY_EXPENSIVE")

    def test_optimal_combination_empty(self) -> None:
        self.assertEqual(find_optimal_combination([]), [])

    def test_thresholds(self) -> None:
        self.assertEqual(apply_thresholds("CHEAP", 200, 20, 150), "VERY_EXPENSIVE")
        self.assertEqual(apply_thresholds("EXPENSIVE", 10, 20, 150), "VERY_CHEAP")
        self.assertEqual(apply_thresholds("CHEAP", 50, 20, 150), "NORMAL")
        self.assertEqual(apply_thresholds("CHEAP", 50, None, 150), "CHEAP")

    def test_compute_level_quarter_hours(self) -> None:
        start = local(2026, 1, 10, 0, 0)
        # Cheap night (hours 2-3), expensive morning (hours 7-8), in SEK/kWh.
        hourly = [0.5] * 24
        hourly[2] = hourly[3] = 0.1
        hourly[7] = hourly[8] = 1.5
        slots = []
        for h, price in enumerate(hourly):
            for q in range(4):
                s = start + timedelta(minutes=60 * h + 15 * q)
                slots.append(PriceSlot(s, s + timedelta(minutes=15), price))
        level = compute_level(slots, local(2026, 1, 10, 2, 40))
        self.assertIsNotNone(level)
        assert level is not None
        self.assertIn(level.level, ("CHEAP", "VERY_CHEAP"))
        self.assertAlmostEqual(level.price, 10.0)
        level = compute_level(slots, local(2026, 1, 10, 7, 5))
        assert level is not None
        self.assertIn(level.level, ("EXPENSIVE", "VERY_EXPENSIVE"))
        level = compute_level(slots, local(2026, 1, 10, 15, 0))
        assert level is not None
        self.assertEqual(level.level, "NORMAL")
        self.assertIsNone(compute_level(slots, local(2026, 1, 11, 1, 0)))
        level = compute_level(slots, local(2026, 1, 10, 15, 0), very_cheap_below=60, very_expensive_above=100)
        assert level is not None
        self.assertEqual(level.level, "VERY_CHEAP")
        self.assertEqual(level.reason, "own thresholds")


class PriceOffsetTest(unittest.TestCase):
    OFFSETS = {"VERY_CHEAP": 2, "CHEAP": 1, "NORMAL": 0, "EXPENSIVE": -1, "VERY_EXPENSIVE": -2}

    def setUp(self) -> None:
        self.now = utc(2026, 1, 10, 12, 0)

    def level(self, name: str):
        from logic.price import PriceLevel

        return PriceLevel(name, 50.0, self.now)

    def test_level_offsets(self) -> None:
        for name, offset in self.OFFSETS.items():
            d = compute_price(self.level(name), self.OFFSETS, self.now)
            self.assertEqual(d.offset, offset)
            self.assertTrue(d.inputs_ok)

    def test_unset_offset_is_zero(self) -> None:
        d = compute_price(self.level("CHEAP"), {}, self.now)
        self.assertEqual(d.offset, 0)

    def test_no_level(self) -> None:
        d = compute_price(None, self.OFFSETS, self.now)
        self.assertEqual(d.offset, 0)
        self.assertFalse(d.inputs_ok)

    def test_guard(self) -> None:
        warm = Reading(21.6, self.now)
        cold = Reading(20.9, self.now)
        kw = dict(guard_enabled=True, temp_low=0.5, setpoint=21.0)
        d = compute_price(self.level("EXPENSIVE"), self.OFFSETS, self.now, indoor=warm, **kw)
        self.assertEqual(d.offset, -1)
        d = compute_price(self.level("VERY_EXPENSIVE"), self.OFFSETS, self.now, indoor=cold, **kw)
        self.assertEqual(d.offset, 0)
        self.assertTrue(d.inputs_ok)
        # Guard does not apply to cheap levels.
        d = compute_price(self.level("CHEAP"), self.OFFSETS, self.now, indoor=cold, **kw)
        self.assertEqual(d.offset, 1)
        # Guard on and sensor stale: no lowering.
        stale = Reading(25.0, self.now - timedelta(hours=2))
        d = compute_price(self.level("EXPENSIVE"), self.OFFSETS, self.now, indoor=stale, **kw)
        self.assertEqual(d.offset, 0)
        self.assertFalse(d.inputs_ok)
        self.assertIn("stale", d.reason)
        # Guard off: lowering regardless of indoor temperature.
        d = compute_price(self.level("EXPENSIVE"), self.OFFSETS, self.now, indoor=cold)
        self.assertEqual(d.offset, -1)


class CacheTest(unittest.TestCase):
    def test_keeps_last_covering_list(self) -> None:
        cache = PriceCache()
        start = utc(2026, 1, 10, 0)
        slots = [PriceSlot(start + timedelta(hours=h), start + timedelta(hours=h + 1), 1.0) for h in range(24)]
        now = utc(2026, 1, 10, 12)
        self.assertEqual(len(cache.update(slots, now)), 24)
        self.assertEqual(len(cache.update([], now)), 24)
        self.assertEqual(cache.update([], utc(2026, 1, 11, 1)), [])


if __name__ == "__main__":
    unittest.main()
