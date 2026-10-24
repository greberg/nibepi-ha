from __future__ import annotations

import unittest
from collections import namedtuple
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic import optcost
from logic.inputs import PriceSlot

Step = namedtuple("Step", "start end price electric_kwh")
T0 = utc(2026, 1, 15, 0)


def quarter(i: int) -> timedelta:
    return timedelta(minutes=15 * i)


class CostTest(unittest.TestCase):
    def test_price_at(self) -> None:
        slots = [PriceSlot(T0 + quarter(i), T0 + quarter(i + 1), 1.0 + i) for i in range(4)]
        self.assertEqual(optcost.price_at(slots, T0 + timedelta(minutes=20)), 2.0)
        self.assertIsNone(optcost.price_at(slots, T0 + timedelta(hours=2)))

    def test_energy_between_is_pro_rata(self) -> None:
        steps = [Step(T0, T0 + timedelta(minutes=30), 2.0, 1.0), Step(T0 + timedelta(minutes=30), T0 + timedelta(hours=1), 1.0, 0.5)]
        kwh, cost = optcost.energy_between(steps, T0 + timedelta(minutes=15), T0 + timedelta(minutes=45))
        self.assertAlmostEqual(kwh, 0.5 + 0.25)
        self.assertAlmostEqual(cost, 0.5 * 2.0 + 0.25 * 1.0)
        self.assertEqual(optcost.energy_between(steps, T0, T0), (0.0, 0.0))

    def test_day_tally(self) -> None:
        tally = optcost.roll({}, "2026-01-15")
        optcost.add_measured(tally, 0.5, 2.0, applied=True)
        optcost.add_measured(tally, 0.25, None, applied=True)
        optcost.add_planned(tally, "heating", 0.6, 1.0)
        optcost.add_planned(tally, "hot_water", 0.1, 0.25)
        optcost.add_planned(tally, "baseline", 0.8, 1.5)
        self.assertIs(optcost.roll(tally, "2026-01-15"), tally)
        s = optcost.summary(tally)
        self.assertEqual((s["kwh"], s["cost"], s["unpriced_kwh"]), (0.75, 1.0, 0.25))
        self.assertEqual((s["predicted_kwh"], s["predicted_cost"], s["baseline_heating_cost"]), (0.7, 1.25, 1.5))
        self.assertEqual(s["cost_vs_predicted_pct"], -20.0)
        self.assertEqual(s["hours_measured"], round(2 / 6.0, 1))
        nxt = optcost.roll(tally, "2026-01-16")
        self.assertEqual(nxt["kwh"], 0.0)
        self.assertEqual(nxt["yesterday"]["cost"], 1.0)
        self.assertNotIn("yesterday", nxt["yesterday"])
        self.assertIsNone(optcost.summary(optcost.new_day("x"))["cost_vs_predicted_pct"])

    def test_gap_only_when_the_plans_steered(self) -> None:
        tally = optcost.roll({}, "2026-01-15")
        optcost.add_planned(tally, "heating", 1.0, 2.0)
        for i in range(10):
            optcost.add_measured(tally, 0.1, 1.0, applied=i < 9)
        s = optcost.summary(tally)
        self.assertEqual(s["plan_applied_share"], 0.9)
        self.assertEqual(s["cost_vs_predicted_pct"], -50.0)
        # One more bin with the rules steering: below 90 %, no gap.
        optcost.add_measured(tally, 0.1, 1.0)
        s = optcost.summary(tally)
        self.assertEqual(s["plan_applied_share"], 0.82)
        self.assertIsNone(s["cost_vs_predicted_pct"])
        self.assertEqual(s["predicted_cost"], 2.0)

    def test_tally_from_before_applied_tracking_has_no_gap(self) -> None:
        old = {"day": "2026-01-15", "kwh": 1.0, "cost": 1.0, "bins": 6, "planned": {"heating": [1.0, 2.0]}}
        s = optcost.summary(old)
        self.assertEqual(s["plan_applied_share"], 0.0)
        self.assertIsNone(s["cost_vs_predicted_pct"])


if __name__ == "__main__":
    unittest.main()
