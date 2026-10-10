"""tibber.get_prices response normalisation and refresh timing.

Fixtures follow the action's response as built by Home Assistant's Tibber
integration: ``{"prices": {"<nickname>": [{"start_time", "price"}]}}`` with
``start_time`` being Tibber's ``startsAt`` string (millisecond precision,
local offset) and ``price`` the total in SEK/kWh.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import context  # noqa: F401
from context import utc
from logic.inputs import PriceSlot
from logic.price import (
    compute_level,
    hourly_average,
    normalize_tibber_response,
    slot_at,
    slots_from_starts,
    tibber_homes,
    tibber_refresh_due,
    tibber_request,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

STHLM = ZoneInfo("Europe/Stockholm")


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=STHLM)


def tibber_items(first: datetime, count: int, step_min: int, price=lambda i: 1.0) -> List[Dict[str, Any]]:
    """Tibber-style items, stepping in real time from ``first`` (DST-safe)."""
    start = first.astimezone(timezone.utc)
    return [
        {
            "start_time": (start + timedelta(minutes=step_min * i)).astimezone(STHLM).isoformat(timespec="milliseconds"),
            "price": price(i),
        }
        for i in range(count)
    ]


def response(**homes: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"prices": dict(homes)}


class TibberHomesTest(unittest.TestCase):
    def test_home_names(self) -> None:
        self.assertEqual(tibber_homes(response(Hemma=[], Stugan=[])), ["Hemma", "Stugan"])

    def test_bad_response(self) -> None:
        self.assertEqual(tibber_homes(None), [])
        self.assertEqual(tibber_homes({"prices": []}), [])


class NormalizeTibberTest(unittest.TestCase):
    def test_hourly_today_and_tomorrow(self) -> None:
        items = tibber_items(local(2026, 1, 10), 48, 60, lambda i: 0.5 + i / 100)
        self.assertEqual(items[1]["start_time"], "2026-01-10T01:00:00.000+01:00")
        result = normalize_tibber_response(response(Hemma=items), STHLM)
        self.assertIsNone(result.warning)
        self.assertEqual(result.home, "Hemma")
        self.assertEqual(len(result.slots), 48)
        self.assertEqual(result.slots[0].start, local(2026, 1, 10, 0))
        self.assertEqual(result.slots[0].end, local(2026, 1, 10, 1))
        # The last slot gets the typical length: tomorrow 24:00.
        self.assertEqual(result.slots[-1].end, local(2026, 1, 12, 0))
        self.assertAlmostEqual(result.slots[-1].price, 0.97)

    def test_quarter_hours(self) -> None:
        items = tibber_items(local(2026, 1, 10), 192, 15, lambda i: float(i % 4))
        slots = normalize_tibber_response(response(Hemma=items), STHLM).slots
        self.assertEqual(len(slots), 192)
        self.assertTrue(all(s.end - s.start == timedelta(minutes=15) for s in slots))
        self.assertEqual(slots[-1].end, local(2026, 1, 12, 0))
        hours = hourly_average(slots)
        self.assertEqual(len(hours), 48)
        self.assertAlmostEqual(hours[0].price, 1.5)

    def test_unit_factor_gives_ore(self) -> None:
        # SEK/kWh with the default factor 100: the level's price is in öre.
        items = tibber_items(local(2026, 1, 10), 96, 15, lambda i: 1.2345)
        slots = normalize_tibber_response(response(Hemma=items), STHLM).slots
        level = compute_level(slots, local(2026, 1, 10, 12, 20), unit_factor=100)
        assert level is not None
        self.assertAlmostEqual(level.price, 123.45)
        self.assertEqual(level.hour_start, local(2026, 1, 10, 12))

    def test_dst_autumn_day(self) -> None:
        # 2026-10-25: 25 h; the offset goes from +02:00 to +01:00.
        items = tibber_items(local(2026, 10, 25), 100, 15, lambda i: float(i))
        self.assertTrue(items[0]["start_time"].endswith("+02:00"))
        self.assertTrue(items[-1]["start_time"].endswith("+01:00"))
        slots = normalize_tibber_response(response(Hemma=items), STHLM).slots
        self.assertEqual(len(slots), 100)
        self.assertEqual(slots[-1].end, local(2026, 10, 26, 0).astimezone(timezone.utc))
        self.assertEqual(slots[-1].end - slots[0].start, timedelta(hours=25))
        self.assertEqual(len(hourly_average(slots)), 25)
        # The second 02:00 (CET, 01:00 UTC) has its own prices.
        self.assertEqual(slot_at(slots, utc(2026, 10, 25, 1, 0)).price, 12.0)

    def test_dst_spring_day(self) -> None:
        items = tibber_items(local(2026, 3, 29), 23, 60)
        slots = normalize_tibber_response(response(Hemma=items), STHLM).slots
        self.assertEqual(len(slots), 23)
        self.assertEqual(slots[2].start, local(2026, 3, 29, 3))
        self.assertEqual(slots[-1].end, local(2026, 3, 30, 0).astimezone(timezone.utc))

    def test_two_homes(self) -> None:
        resp = response(
            Hemma=tibber_items(local(2026, 1, 10), 24, 60, lambda i: 1.0),
            Stuga=tibber_items(local(2026, 1, 10), 24, 60, lambda i: 2.0),
        )
        first = normalize_tibber_response(resp, STHLM)
        self.assertEqual(first.home, "Hemma")
        self.assertEqual(first.homes, ["Hemma", "Stuga"])
        self.assertIn("several Tibber homes", first.warning or "")
        self.assertEqual(first.slots[0].price, 1.0)
        chosen = normalize_tibber_response(resp, STHLM, "stuga ")
        self.assertEqual(chosen.home, "Stuga")
        self.assertIsNone(chosen.warning)
        self.assertEqual(chosen.slots[0].price, 2.0)
        unknown = normalize_tibber_response(resp, STHLM, "Lägenhet")
        self.assertEqual(unknown.slots, [])
        self.assertIn("not in the response", unknown.warning or "")

    def test_tomorrow_missing_or_empty(self) -> None:
        today_only = normalize_tibber_response(
            response(Hemma=tibber_items(local(2026, 1, 10), 96, 15)), STHLM
        )
        self.assertEqual(len(today_only.slots), 96)
        self.assertEqual(today_only.slots[-1].end, local(2026, 1, 11, 0))
        empty = normalize_tibber_response(response(Hemma=[]), STHLM)
        self.assertEqual(empty.slots, [])
        self.assertIsNone(empty.warning)
        self.assertEqual(normalize_tibber_response({"prices": {}}, STHLM).slots, [])
        self.assertIsNotNone(normalize_tibber_response({"prices": {}}, STHLM).warning)
        self.assertIsNotNone(normalize_tibber_response(None, STHLM).warning)
        self.assertIsNotNone(normalize_tibber_response({"other": 1}, STHLM).warning)

    def test_other_start_formats_and_garbage(self) -> None:
        items = [
            {"start_time": "2023-12-09 03:00:00+02:00", "price": 0.46914},  # docs example
            {"start_time": datetime(2023, 12, 9, 3, 15, tzinfo=timezone(timedelta(hours=2))), "price": 0.5},
            {"start_time": "2023-12-09T01:30:00Z", "price": "0.6"},
            {"start_time": "nonsense", "price": 1.0},
            {"start_time": "2023-12-09T01:45:00Z", "price": None},
            "garbage",
        ]
        slots = normalize_tibber_response(response(Hemma=items), STHLM).slots
        self.assertEqual([s.price for s in slots], [0.46914, 0.5, 0.6])
        self.assertEqual(slots[-1].end, utc(2023, 12, 9, 1, 45))

    def test_hole_and_single_item(self) -> None:
        items = tibber_items(local(2026, 1, 10), 24, 60)
        del items[10:13]  # three hours missing
        slots = normalize_tibber_response(response(Hemma=items), STHLM).slots
        self.assertEqual(slots[9].end, local(2026, 1, 10, 10))
        self.assertIsNone(slot_at(slots, local(2026, 1, 10, 11)))
        self.assertEqual(slots_from_starts([(utc(2026, 1, 10, 0), 1.0)]), [])


class TibberRequestTest(unittest.TestCase):
    def test_today_through_tomorrow(self) -> None:
        self.assertEqual(
            tibber_request(utc(2026, 1, 10, 22, 30), STHLM),  # 23:30 local
            {"start": "2026-01-10T00:00:00+01:00", "end": "2026-01-12T00:00:00+01:00"},
        )
        # Over the autumn DST change the offsets differ.
        self.assertEqual(
            tibber_request(local(2026, 10, 24, 12), STHLM),
            {"start": "2026-10-24T00:00:00+02:00", "end": "2026-10-26T00:00:00+01:00"},
        )
        # Just after local midnight it is already the new day.
        self.assertEqual(tibber_request(utc(2026, 1, 9, 23, 5), STHLM)["start"], "2026-01-10T00:00:00+01:00")


class RefreshTest(unittest.TestCase):
    def slots(self, days: int, first: datetime = local(2026, 1, 10)) -> List[PriceSlot]:
        start = first.astimezone(timezone.utc)
        return [
            PriceSlot(start + timedelta(minutes=15 * i), start + timedelta(minutes=15 * (i + 1)), 1.0)
            for i in range(96 * days)
        ]

    def test_first_and_clock_jump(self) -> None:
        now = local(2026, 1, 10, 10, 5)
        self.assertTrue(tibber_refresh_due(now, STHLM, None, []))
        self.assertTrue(tibber_refresh_due(now, STHLM, now + timedelta(minutes=1), self.slots(1)))

    def test_new_hour(self) -> None:
        last = local(2026, 1, 10, 10, 58)
        self.assertTrue(tibber_refresh_due(local(2026, 1, 10, 11, 0), STHLM, last, self.slots(2)))

    def test_within_hour_with_good_list(self) -> None:
        last = local(2026, 1, 10, 10, 1)
        self.assertFalse(tibber_refresh_due(local(2026, 1, 10, 10, 50), STHLM, last, self.slots(1)))

    def test_missing_data_retries_every_15_min(self) -> None:
        last = local(2026, 1, 10, 10, 1)
        self.assertFalse(tibber_refresh_due(local(2026, 1, 10, 10, 10), STHLM, last, []))
        self.assertTrue(tibber_refresh_due(local(2026, 1, 10, 10, 16), STHLM, last, []))

    def test_waits_for_tomorrow_after_13(self) -> None:
        last = local(2026, 1, 10, 13, 1)
        self.assertFalse(tibber_refresh_due(local(2026, 1, 10, 13, 10), STHLM, last, self.slots(1)))
        self.assertTrue(tibber_refresh_due(local(2026, 1, 10, 13, 16), STHLM, last, self.slots(1)))
        self.assertFalse(tibber_refresh_due(local(2026, 1, 10, 13, 16), STHLM, last, self.slots(2)))
        # Before 13:00 tomorrow's prices are not expected.
        early = local(2026, 1, 10, 12, 1)
        self.assertFalse(tibber_refresh_due(local(2026, 1, 10, 12, 40), STHLM, early, self.slots(1)))

    def test_autumn_repeated_hour_is_a_new_hour(self) -> None:
        # 02:30 CEST (00:30 UTC) and 02:40 CET (01:40 UTC) share the wall hour.
        covering = self.slots(1, local(2026, 10, 25))
        self.assertTrue(
            tibber_refresh_due(utc(2026, 10, 25, 1, 40), STHLM, utc(2026, 10, 25, 0, 30), covering)
        )


if __name__ == "__main__":
    unittest.main()
