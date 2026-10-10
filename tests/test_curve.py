from __future__ import annotations

import unittest

import context  # noqa: F401
from logic.curve import combine, component_value, plan, target_value
from logic.inputs import Decision


def d(offset: float, ok: bool = True) -> Decision:
    return Decision(offset, "test", ok)


class CurveTest(unittest.TestCase):
    def test_component_clamp_and_nan(self) -> None:
        self.assertEqual(component_value(12), 10)
        self.assertEqual(component_value(-12.5), -10)
        self.assertEqual(component_value(float("nan")), 0)
        self.assertEqual(component_value(None), 0)
        self.assertEqual(component_value("1.5"), 1.5)

    def test_total_clamp(self) -> None:
        self.assertEqual(combine({"a": d(8), "b": d(8)}), 10)
        self.assertEqual(combine({"a": d(-15), "b": d(-3)}), -10)
        self.assertAlmostEqual(combine({"a": d(1.2), "b": d(-0.4)}, base_adjust=1), 1.8)

    def test_hysteresis(self) -> None:
        # Inside ±0.75 of the register value: keep it.
        self.assertEqual(target_value(1.7, 1), 1)
        self.assertEqual(target_value(0.3, 1), 1)
        self.assertEqual(target_value(1.75, 1), 1)
        # Beyond: rounded total.
        self.assertEqual(target_value(1.76, 1), 2)
        self.assertEqual(target_value(0.2, 1), 0)
        self.assertEqual(target_value(-2.5, 0), -2)  # Math.round semantics
        self.assertEqual(target_value(-2.6, 0), -3)
        self.assertEqual(target_value(2.4, None), 2)

    def test_plan(self) -> None:
        p = plan({"indoor": d(1.2), "price": d(-3), "weather": Decision(0, "stale", False)}, current=0)
        self.assertAlmostEqual(p.total, -1.8)
        self.assertEqual(p.target, -2)
        self.assertTrue(p.write)
        self.assertEqual(p.components["price"], -3)
        self.assertFalse(p.inputs_ok["weather"])
        held = plan({"indoor": d(-1.5)}, current=-2)
        self.assertEqual(held.target, -2)
        self.assertFalse(held.write)


if __name__ == "__main__":
    unittest.main()
