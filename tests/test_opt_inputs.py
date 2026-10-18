"""The optimiser's missing inputs: why the indoor temperature or the
prices cannot be used (texts in both languages), the indoor reading over
time (an entity unavailable at start, a restored value) and when a plan
that failed on an input is retried."""

from __future__ import annotations

import unittest
from datetime import timedelta, timezone

import context  # noqa: F401
from context import utc
from logic.indoor import MIN_VALID_INDOOR
from logic.inputs import (
    PriceSlot,
    Reading,
    entity_absence,
    state_timestamp,
    state_value,
    timeout_minutes,
    unusable,
    usable,
)
from logic.optimizer.gate import gate
from logic.optimizer.house import HouseFit
from logic.optrun import (
    INPUT_PROBLEMS,
    RETRY_MIN_GAP,
    TankInputs,
    indoor_problem,
    input_problem,
    plan_system,
    plan_tank,
    prices_problem,
    problem_input,
    should_replan,
)
from logic.texts import CATALOGUE, msg
from test_opt_strategy import inputs, prices

NOW = utc(2026, 1, 14, 23)
UTC = timezone.utc


def both(text):
    return text.render("en"), text.render("sv")


class UnusableTest(unittest.TestCase):
    def test_reasons_with_details(self) -> None:
        self.assertEqual(unusable(None, NOW), ("missing", {}))
        self.assertEqual(unusable(Reading(9999, NOW), NOW), ("missing", {}))
        self.assertEqual(unusable(Reading(float("inf"), NOW), NOW), ("missing", {}))
        old = Reading(21.0, NOW - timedelta(minutes=75, seconds=30))
        self.assertEqual(unusable(old, NOW), ("stale", {"minutes": 75, "timeout": 60.0}))
        self.assertEqual(unusable(old, NOW, 30), ("stale", {"minutes": 75, "timeout": 30.0}))
        self.assertIsNone(unusable(old, NOW, 0))  # check disabled
        self.assertIsNone(unusable(Reading(2.0, NOW), NOW))
        self.assertEqual(unusable(Reading(2.0, NOW), NOW, minimum=MIN_VALID_INDOOR), ("implausible", {"value": 2.0}))
        self.assertIsNone(unusable(Reading(21.0, NOW), NOW, minimum=MIN_VALID_INDOOR))

    def test_usable_keeps_its_answers(self) -> None:
        self.assertEqual(usable(None, NOW), "missing")
        self.assertEqual(usable(Reading(21.0, NOW - timedelta(hours=2)), NOW), "stale")
        self.assertIsNone(usable(Reading(2.0, NOW), NOW))

    def test_timeout_minutes(self) -> None:
        self.assertEqual(timeout_minutes(None), 60.0)
        self.assertEqual(timeout_minutes("45"), 45.0)
        self.assertEqual(timeout_minutes(-5), 60.0)
        self.assertEqual(timeout_minutes("x"), 60.0)
        self.assertIsNone(timeout_minutes(0))

    def test_entity_absence(self) -> None:
        self.assertEqual(entity_absence(None), ("not_found", {}))
        self.assertEqual(entity_absence("unavailable"), ("unavailable", {}))
        self.assertEqual(entity_absence("unknown"), ("unknown", {}))
        self.assertEqual(entity_absence(""), ("unknown", {}))
        self.assertEqual(entity_absence("warm"), ("not_numeric", {"state": "warm"}))


class IndoorReadingOverTimeTest(unittest.TestCase):
    """The runtime reads the entity's state on every tick (no cache): these
    are the pure steps it takes (state_value, state_timestamp, unusable)."""

    def read(self, state, last_reported, last_updated):
        value = state_value(state)
        if value is None:
            return None, entity_absence(state)
        return Reading(value, state_timestamp(last_reported, last_updated)), None

    def test_unavailable_at_start_then_a_value(self) -> None:
        reading, why = self.read("unavailable", NOW, NOW)
        self.assertIsNone(reading)
        self.assertEqual(why, ("unavailable", {}))
        # The first value two minutes later is used at once.
        later = NOW + timedelta(minutes=2)
        reading, why = self.read("21.4", later, later)
        self.assertIsNone(why)
        self.assertIsNone(unusable(reading, later + timedelta(seconds=30)))

    def test_restored_value_is_neither_fresh_nor_stale_forever(self) -> None:
        # A value restored at start (Home Assistant writes it with the start
        # time) is fresh for the timeout, then stale until the next report.
        reading, _ = self.read("21.0", NOW, NOW)
        self.assertIsNone(unusable(reading, NOW + timedelta(minutes=59)))
        self.assertEqual(unusable(reading, NOW + timedelta(minutes=61))[0], "stale")
        self.assertEqual(unusable(reading, NOW + timedelta(hours=5))[1]["minutes"], 300)
        # Netatmo reports the same value again: last_updated stands still,
        # last_reported moves, and the reading is fresh again.
        report = NOW + timedelta(hours=5)
        reading, _ = self.read("21.0", report, NOW)
        self.assertEqual(reading.ts, report)
        self.assertIsNone(unusable(reading, report + timedelta(minutes=1)))
        # A restored state with an old last_reported: stale at once, fresh on
        # the next report.
        reading, _ = self.read("21.0", NOW - timedelta(days=2), NOW - timedelta(days=2))
        self.assertEqual(unusable(reading, NOW)[0], "stale")
        reading, _ = self.read("21.0", NOW + timedelta(minutes=10), NOW - timedelta(days=2))
        self.assertIsNone(unusable(reading, NOW + timedelta(minutes=11)))


class IndoorProblemTest(unittest.TestCase):
    def test_texts(self) -> None:
        cases = {
            ("not_found", ()): ("indoor sensor sensor.netatmo not found", "Inomhusgivaren sensor.netatmo hittades inte"),
            ("unavailable", ()): (
                "indoor sensor sensor.netatmo is unavailable", "Inomhusgivaren sensor.netatmo är inte tillgänglig",
            ),
            ("unknown", ()): ("indoor sensor sensor.netatmo has no value", "Inomhusgivaren sensor.netatmo har inget värde"),
            ("not_numeric", (("state", "warm"),)): (
                "indoor sensor sensor.netatmo is not a number (warm)",
                "Inomhusgivaren sensor.netatmo är inte ett tal (warm)",
            ),
            ("bad_unit", (("unit", "K"),)): (
                "indoor sensor sensor.netatmo: unit K is not a temperature",
                "Inomhusgivaren sensor.netatmo: enheten K är ingen temperatur",
            ),
            ("missing", ()): (
                "no indoor temperature from sensor.netatmo yet", "Ingen inomhustemperatur från sensor.netatmo än",
            ),
            ("stale", (("minutes", 75.9), ("timeout", 60.0))): (
                "indoor temperature from sensor.netatmo is 75 min old (limit 60 min)",
                "Inomhustemperaturen från sensor.netatmo är 75 min gammal (gräns 60 min)",
            ),
            ("implausible", (("value", -3.25),)): (
                "implausible indoor temperature from sensor.netatmo (-3.2 °C)",
                "Orimlig inomhustemperatur från sensor.netatmo (−3,2 °C)",
            ),
        }
        for (reason, details), texts in cases.items():
            with self.subTest(reason=reason):
                problem = indoor_problem("sensor.netatmo", reason, dict(details))
                self.assertEqual(both(problem), texts)
                self.assertTrue(input_problem(problem))
                self.assertEqual(problem_input(problem), "indoor")
        self.assertEqual(both(indoor_problem("BT50", "missing")),
                         ("no indoor temperature from BT50 yet", "Ingen inomhustemperatur från BT50 än"))
        # Unknown reason or stale without its details: still a text.
        self.assertEqual(indoor_problem("BT50", "odd").code, "opt.no_indoor")
        self.assertEqual(indoor_problem("BT50", "stale", {}).code, "opt.indoor_missing")

    def test_from_unusable(self) -> None:
        old = Reading(21.0, NOW - timedelta(minutes=90))
        problem = indoor_problem("sensor.t", *unusable(old, NOW, 60))
        self.assertEqual(problem, "indoor temperature from sensor.t is 90 min old (limit 60 min)")

    def test_plan_and_gate_say_which_sensor(self) -> None:
        problem = indoor_problem("sensor.netatmo", "unavailable")
        result = plan_system(inputs(t_in=None, t_in_problem=problem))
        self.assertEqual(result.problem.code, "opt.indoor_unavailable")
        self.assertEqual(plan_system(inputs(t_in=None)).problem.code, "opt.no_indoor")
        fit = HouseFit(None, True, "ok", 400, 0.9, 0.02, 0.05, 0.05, 0.9, "high")
        decision = gate(fit, 2, 24, None, 19.5, 0, 3, indoor_problem="missing", indoor_reason=problem)
        self.assertEqual(decision.reason, "indoor sensor sensor.netatmo is unavailable")
        self.assertEqual(gate(fit, 2, 24, None, 19.5, 0, 3, indoor_problem="stale").reason.code, "indoor.sensor_stale")


class PricesProblemTest(unittest.TestCase):
    def test_why_no_prices_cover_now(self) -> None:
        self.assertEqual(both(prices_problem([], NOW, UTC)), ("no prices", "Inga priser"))
        ended = prices(hours=3, start=NOW - timedelta(hours=4))
        self.assertEqual(both(prices_problem(ended, NOW, UTC)),
                         ("prices end at 22:00", "Priserna tar slut kl. 22:00"))
        later = prices(hours=3, start=NOW + timedelta(hours=2))
        self.assertEqual(both(prices_problem(later, NOW, UTC)),
                         ("prices start at 01:00", "Priserna börjar kl. 01:00"))
        status = msg("price.tibber_timeout", service="tibber.get_prices")
        self.assertEqual(both(prices_problem(ended, NOW, UTC, status)), (
            "no prices: tibber.get_prices timed out", "Inga priser: tibber.get_prices svarade inte i tid",
        ))
        # "ok" or a plain status: the prices themselves decide.
        self.assertEqual(prices_problem([], NOW, UTC, msg("price.status_ok")).code, "opt.prices_none")
        self.assertEqual(prices_problem([], NOW, UTC, "ok").code, "opt.prices_none")
        gap = [PriceSlot(NOW - timedelta(hours=1), NOW + timedelta(hours=1), 1.0)]
        self.assertEqual(prices_problem(gap, NOW, UTC).code, "opt.no_prices")

    def test_plans_use_it(self) -> None:
        ended = prices(hours=3, start=NOW - timedelta(hours=4))
        self.assertEqual(plan_system(inputs(prices=ended)).problem.code, "opt.prices_end")
        status = msg("price.no_sensor")
        self.assertEqual(plan_system(inputs(prices=[], price_status=status)).problem,
                         "no prices: no price sensor configured")
        tank = plan_tank(TankInputs([], NOW, UTC, 45.0, 50.0, 45.0, 50.0, 60.0, 45.0, [0.0] * 168,
                                    price_status=status))
        self.assertEqual(tank.problem.code, "opt.no_prices_why")
        self.assertEqual(problem_input(tank.problem), "prices")


class RetryTest(unittest.TestCase):
    def test_input_problems(self) -> None:
        for code in INPUT_PROBLEMS:
            self.assertIn(code, CATALOGUE)
        self.assertTrue(input_problem(msg("opt.no_bt6")))
        self.assertEqual(problem_input(msg("opt.no_bt6")), "bt6")
        self.assertEqual(problem_input(msg("opt.no_stops")), "stops")
        self.assertFalse(input_problem(msg("common.error", error="boom")))
        self.assertFalse(input_problem(None))
        self.assertFalse(input_problem("indoor temperature missing"))  # no code
        self.assertIsNone(problem_input(msg("away.no_charges")))

    def test_should_replan(self) -> None:
        last = NOW
        tick = NOW + timedelta(seconds=58)  # the next tick, a plan's run time later
        # Nothing failed on an input: no extra plan.
        self.assertFalse(should_replan(tick, last, [], False))
        self.assertFalse(should_replan(tick, last, [], True))
        # The input is back: plan now.
        self.assertTrue(should_replan(tick, last, [True], True))
        self.assertTrue(should_replan(tick, last, [False, True], True))
        # Still missing: after a successful plan wait for the quarter hour,
        # before the first one since start retry every tick.
        self.assertFalse(should_replan(tick, last, [False], True))
        self.assertTrue(should_replan(tick, last, [False], False))
        # Never right after a plan, nor before the first one.
        self.assertFalse(should_replan(NOW + RETRY_MIN_GAP - timedelta(seconds=1), last, [True], False))
        self.assertFalse(should_replan(tick, None, [True], False))
        # The every-tick retry stops after the startup window; an input that
        # comes back still triggers a plan.
        self.assertTrue(should_replan(tick, last, [False], False, since_start=timedelta(minutes=19)))
        self.assertFalse(should_replan(tick, last, [False], False, since_start=timedelta(minutes=20)))
        self.assertTrue(should_replan(tick, last, [True], False, since_start=timedelta(hours=5)))


if __name__ == "__main__":
    unittest.main()
