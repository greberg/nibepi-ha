"""A meter that counted wrong: rescaling its history (nibepi.rescale_meter)."""

from __future__ import annotations

import unittest

import context  # noqa: F401
from logic import optcost
from logic.metercorr import factor_at, load, rescale_rows, rescale_series, rescale_tally

UNTIL = 1_000_000.0


class MeterCorrectionTest(unittest.TestCase):
    def test_only_bins_before_the_fix(self) -> None:
        rows = {int(UNTIL) - 600: {"meter_kwh": 0.1, "t_out": 5.0}, int(UNTIL): {"meter_kwh": 0.25}, 10: {"t_out": 1.0}}
        self.assertEqual(rescale_rows(rows, UNTIL, 2.5), 1)
        self.assertAlmostEqual(rows[int(UNTIL) - 600]["meter_kwh"], 0.25)
        self.assertEqual(rows[int(UNTIL) - 600]["t_out"], 5.0)
        self.assertEqual(rows[int(UNTIL)]["meter_kwh"], 0.25)

    def test_imports_later_are_corrected_too(self) -> None:
        corrections = load([[UNTIL, 2.5], ["bad", 1], [UNTIL - 600, -1]])
        self.assertEqual(corrections, [(UNTIL, 2.5)])
        series = rescale_series({int(UNTIL) - 600: 0.1, int(UNTIL): 0.25}, corrections)
        self.assertAlmostEqual(series[int(UNTIL) - 600], 0.25)
        self.assertEqual(series[int(UNTIL)], 0.25)
        self.assertEqual(factor_at([(UNTIL, 2.5), (UNTIL + 600, 2.0)], UNTIL), 2.0)

    def test_the_cost_of_today_and_yesterday(self) -> None:
        yesterday = optcost.new_day("2026-11-27")
        optcost.add_measured(yesterday, 10.0, 1.0)
        tally = optcost.roll(yesterday, "2026-11-28")
        optcost.add_measured(tally, 4.0, 2.0)
        self.assertEqual(rescale_tally(tally, "2026-11-28", 2.5), ["2026-11-27", "2026-11-28"])
        optcost.refresh(tally)
        self.assertAlmostEqual(tally["kwh"], 10.0)
        self.assertAlmostEqual(tally["cost"], 20.0)
        self.assertAlmostEqual(tally["previous"]["kwh"], 25.0)
        # A fix before today leaves today alone.
        tally2 = optcost.new_day("2026-11-28")
        optcost.add_measured(tally2, 4.0, 2.0)
        self.assertEqual(rescale_tally(tally2, "2026-11-27", 2.5), [])
        self.assertAlmostEqual(tally2["kwh"], 4.0)


if __name__ == "__main__":
    unittest.main()
