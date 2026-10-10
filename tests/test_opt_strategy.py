from __future__ import annotations

import unittest
from datetime import timedelta, timezone

import context  # noqa: F401
from context import utc
from logic.curve import CurvePlan
from logic.inputs import PriceSlot
from logic.optimizer.gate import GateDecision
from logic.optimizer.profile import HouseProfile
from logic.optrun import (
    HeatingInputs,
    SystemSetup,
    comfort_settings,
    models_for,
    plan_system,
    profile_confidence,
    scale_prices,
)
from logic.optstrategy import (
    SOURCE_FLOOR,
    SOURCE_OPTIMISER,
    SOURCE_RULES,
    STRATEGY_OPTIMISER,
    STRATEGY_RULES,
    apply_choice,
    choose_total,
    hours_left,
    offset_at,
    savings_between,
    time_share,
)

NOW = utc(2026, 1, 14, 23)
SE3_DAY = [0.92, 0.88, 0.85, 0.84, 0.86, 0.95, 1.35, 2.10, 2.45, 2.05, 1.70, 1.55,
           1.48, 1.45, 1.52, 1.70, 2.05, 2.60, 2.75, 2.30, 1.80, 1.45, 1.20, 1.02]
PROFILE = HouseProfile(floor_area_m2=150, floors=1, latitude=59.3, longitude=18.0)


def prices(hours=24, start=NOW):
    out = []
    for i in range(hours * 4):
        a = start + timedelta(minutes=15 * i)
        out.append(PriceSlot(a, a + timedelta(minutes=15), SE3_DAY[(i // 4) % 24]))
    return out


def rules_plan(total: float = 1.6) -> CurvePlan:
    return CurvePlan(total=total, target=2, write=True, components={"price": total})


def inputs(**kw) -> HeatingInputs:
    models = models_for(SystemSetup("s1", PROFILE, 9), None)
    data = dict(
        system="s1",
        models=models,
        comfort=comfort_settings(21.0, 0.5, 1.0, 1.5, models.prior.sensor_margin),
        prices=prices(),
        now=NOW,
        tz=timezone.utc,
        t_in=21.0,
        t_out=-6.0,
        current_offset=0,
        latitude=59.3,
        longitude=18.0,
    )
    data.update(kw)
    return HeatingInputs(**data)


class ChooseTotalTest(unittest.TestCase):
    def test_rules_strategy_ignores_the_gate(self) -> None:
        choice = choose_total(STRATEGY_RULES, 1.6, GateDecision(True, -3, "optimiser"))
        self.assertEqual((choice.source, choice.total), (SOURCE_RULES, 1.6))

    def test_optimiser_replaces_the_sum(self) -> None:
        choice = choose_total(STRATEGY_OPTIMISER, 1.6, GateDecision(True, -3, "optimiser"))
        self.assertEqual((choice.source, choice.total), (SOURCE_OPTIMISER, -3.0))
        plan = apply_choice(rules_plan(), choice, current=0.0)
        self.assertEqual((plan.total, plan.target, plan.write), (-3.0, -3, True))
        self.assertEqual(plan.components, {"price": 1.6})  # the rules' parts stay visible

    def test_gate_refuses_falls_back_with_reason(self) -> None:
        gate = GateDecision(False, None, "model confidence low (0.20) below high")
        choice = choose_total(STRATEGY_OPTIMISER, 1.6, gate)
        self.assertEqual((choice.source, choice.total), (SOURCE_RULES, 1.6))
        self.assertIn("confidence low", choice.reason)
        plan = rules_plan()
        self.assertIs(apply_choice(plan, choice, 0.0), plan)

    def test_comfort_floor(self) -> None:
        gate = GateDecision(True, 5, "below comfort floor 19.5 °C: heating")
        choice = choose_total(STRATEGY_OPTIMISER, -1.0, gate)
        self.assertEqual((choice.source, choice.total), (SOURCE_FLOOR, 5.0))

    def test_no_gate_yet_and_clamp(self) -> None:
        self.assertEqual(choose_total(STRATEGY_OPTIMISER, 0.5, None).source, SOURCE_RULES)
        self.assertEqual(choose_total(STRATEGY_OPTIMISER, 0.5, GateDecision(True, 14, "optimiser")).total, 10.0)

    def test_write_threshold_kept(self) -> None:
        choice = choose_total(STRATEGY_OPTIMISER, 0.0, GateDecision(True, -2, "optimiser"))
        plan = apply_choice(rules_plan(0.0), choice, current=-2.0)
        self.assertEqual((plan.target, plan.write), (-2, False))


class PlanSystemTest(unittest.TestCase):
    def test_plan_and_lookup(self) -> None:
        result = plan_system(inputs())
        self.assertTrue(result.ok, result.problem)
        steps = result.plan.plan.steps
        self.assertEqual(len(steps), 96)
        self.assertEqual(offset_at(steps, NOW + timedelta(minutes=20)), steps[1].offset)
        self.assertIsNone(offset_at(steps, NOW + timedelta(days=2)))
        self.assertAlmostEqual(hours_left(steps, NOW + timedelta(hours=6)), 18.0)
        self.assertTrue(result.blocks)
        self.assertIn("SEK", result.summary)
        # Cheap night, expensive morning: heat is moved, so something is saved.
        self.assertGreater(result.plan.savings, 0)
        self.assertLessEqual(max(s.offset for s in steps), result.base_offset + 3)

    def test_problems(self) -> None:
        self.assertEqual(plan_system(inputs(t_in=None)).problem, "indoor temperature missing")
        self.assertEqual(plan_system(inputs(prices=[])).problem, "no prices cover now")
        self.assertEqual(plan_system(inputs(prices=prices(start=NOW + timedelta(hours=1)))).problem,
                         "no prices cover now")

    def test_savings_split(self) -> None:
        plan = plan_system(inputs()).plan
        whole = savings_between(plan, NOW, NOW + timedelta(days=1))
        self.assertAlmostEqual(whole, plan.savings)
        half = savings_between(plan, NOW, NOW + timedelta(hours=12))
        rest = savings_between(plan, NOW + timedelta(hours=12), NOW + timedelta(days=2))
        self.assertAlmostEqual(half + rest, plan.savings)
        self.assertEqual(savings_between(plan, NOW, NOW), 0.0)


class HelpersTest(unittest.TestCase):
    def test_scale_prices(self) -> None:
        slot = PriceSlot(NOW, NOW + timedelta(hours=1), 150.0)
        self.assertEqual(scale_prices([slot], 1)[0].price, 1.5)  # öre -> SEK
        self.assertEqual(scale_prices([slot], 100)[0].price, 150.0)  # already SEK
        self.assertEqual(scale_prices([slot], "x")[0].price, 150.0)

    def test_time_share(self) -> None:
        a, b = NOW, NOW + timedelta(hours=4)
        self.assertAlmostEqual(time_share(NOW, NOW + timedelta(hours=1), a, b), 0.25)
        self.assertEqual(time_share(NOW, NOW, a, b), 0.0)
        self.assertEqual(time_share(a, b, b, a), 0.0)

    def test_profile_confidence(self) -> None:
        self.assertEqual(profile_confidence(0, False), 0.5)
        self.assertAlmostEqual(profile_confidence(30, False), 0.65)
        self.assertEqual(profile_confidence(500, False), 0.8)
        self.assertEqual(profile_confidence(0, True), 0.8)


if __name__ == "__main__":
    unittest.main()
