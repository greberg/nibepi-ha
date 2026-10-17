from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import context  # noqa: F401
from const import OPT_HW_OPT_FLOOR, OPTIMIZER_DEFAULTS
from logic.hwcontrol import (
    REG_BT6,
    REG_BT7,
    REG_COMFORT,
    REG_PERIOD,
    REG_PRIO,
    REG_TEMP_LUX,
    HotWaterController,
    HotWaterInputs,
    HotWaterSettings,
)
from logic.inputs import PriceSlot, Reading
from logic.lease import MODE_ACTIVE
from logic.optimizer.tank import HotWaterBlock
from logic.optrun import TankInputs, plan_tank
from logic.optstrategy import hw_windows
from logic.vvlearn import SLOTS, VvProfile

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

STHLM = ZoneInfo("Europe/Stockholm")
TUE = 1


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=STHLM)


def block(start: datetime, hours: float, action: str = "charge", target: Optional[float] = 52.0,
          mode: int = 1, boost: bool = False) -> HotWaterBlock:
    return HotWaterBlock(start, start + timedelta(hours=hours), action, target if action == "charge" else None,
                         mode, boost, 2.0, 1.0, "test")


def learned_profile() -> VvProfile:
    values = [0.0] * SLOTS
    values[TUE * 24 + 7] = 2.0  # economy day, peak 07:00
    return VvProfile(profile=values)


class Rig:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.controller = HotWaterController(learned_profile())
        self.settings = HotWaterSettings()
        self.values: Dict[int, float] = {
            REG_BT6: 45.0, REG_BT7: 45.0, REG_PRIO: 30.0, REG_COMFORT: 1.0, REG_PERIOD: 20.0,
            REG_TEMP_LUX: 0.0, 47043: 48.0, 47044: 45.0, 47045: 42.0, 47047: 56.0, 47048: 52.0, 47049: 48.0,
        }
        self.windows = None
        self.problem: Optional[str] = None

    def decide(self, **features: bool):
        readings = {reg: Reading(v, self.now - timedelta(minutes=1)) for reg, v in self.values.items()}
        inputs = HotWaterInputs(self.now, STHLM, readings, None, (), {}, self.windows, self.problem)
        return self.controller.decide(MODE_ACTIVE, features, inputs, self.settings)


class WindowsTest(unittest.TestCase):
    def test_charge_blocks_become_windows(self) -> None:
        now = local(2026, 10, 6, 12, 0)
        blocks = [
            block(now - timedelta(hours=3), 1),  # over
            block(now - timedelta(hours=2), 2.5, "hold"),
            block(now + timedelta(hours=1), 1, target=56.0, mode=2, boost=True),
            block(now - timedelta(minutes=15), 0.5),  # running
        ]
        windows = hw_windows(blocks, now, STHLM)
        self.assertEqual(len(windows), 2)
        first, second = windows
        self.assertEqual((first.start, first.target, first.day_mode), (now - timedelta(minutes=15), 52.0, 2))
        self.assertEqual((second.target, second.day_mode), (56.0, 3))
        self.assertEqual(second.peak, second.end)
        self.assertEqual(first.cheap_start, None)


class ControllerTest(unittest.TestCase):
    def test_optimiser_window_drives_the_charge(self) -> None:
        rig = Rig(local(2026, 10, 6, 12, 0))  # no VV-AI window at noon
        rig.windows = hw_windows([block(rig.now - timedelta(minutes=10), 1)], rig.now, STHLM)
        decision = rig.decide(optimiser=True)
        self.assertEqual(decision.targets[REG_PERIOD], 0)
        self.assertEqual(decision.targets[REG_TEMP_LUX], 4)
        self.assertEqual(decision.details["plan_source"], "optimiser")
        self.assertEqual(decision.reasons["optimiser"], "drives the charges")
        self.assertEqual(decision.next_heat, rig.now - timedelta(minutes=10))
        # BT7 reaches the block's target: the charge ends, the run is closed.
        rig.values[REG_BT7] = 52.5
        decision = rig.decide(optimiser=True)
        self.assertIsNone(decision.targets[REG_TEMP_LUX])
        self.assertEqual(decision.targets[REG_PERIOD], 0)
        self.assertIn("reached", decision.reasons["vv_control"])

    def test_outside_the_blocks_it_holds(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30))  # inside the VV-AI window, which is not used
        rig.windows = []
        decision = rig.decide(optimiser=True, vv_control=True)
        self.assertEqual(decision.targets[REG_PERIOD], 0)
        self.assertIsNone(decision.targets[REG_TEMP_LUX])
        self.assertEqual(decision.state, "block")

    def test_floor_still_applies(self) -> None:
        rig = Rig(local(2026, 10, 6, 12, 0))
        rig.windows = []
        rig.values[REG_BT7] = 30.0
        decision = rig.decide(optimiser=True)
        self.assertEqual(decision.targets[REG_TEMP_LUX], 4)
        self.assertIn("floor", decision.reasons["vv_control"])

    def test_no_plan_without_vv_control_leaves_it_to_the_pump(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30))
        rig.problem = "BT6 missing or stale"
        decision = rig.decide(optimiser=True)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertIsNone(decision.targets[REG_TEMP_LUX])
        self.assertIn("no optimiser plan", decision.reasons["vv_control"])
        self.assertEqual(decision.reasons["optimiser"], "no plan (BT6 missing or stale)")

    def test_no_plan_falls_back_to_vv_ai(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30))
        decision = rig.decide(optimiser=True, vv_control=True)
        self.assertEqual(decision.targets[REG_TEMP_LUX], 4)
        self.assertEqual(decision.details["plan_source"], "vv_ai")
        self.assertIn("VV-AI plan used", decision.reasons["optimiser"])

    def test_regulation_takes_precedence(self) -> None:
        rig = Rig(local(2026, 10, 6, 12, 0))
        rig.windows = hw_windows([block(rig.now, 1)], rig.now, STHLM)
        decision = rig.decide(optimiser=True, priority=True)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertIn("precedence", decision.reasons["vv_control"])

    def test_off_by_default(self) -> None:
        rig = Rig(local(2026, 10, 6, 12, 0))
        rig.windows = hw_windows([block(rig.now, 1)], rig.now, STHLM)
        decision = rig.decide()
        self.assertEqual(decision.reasons["optimiser"], "off")
        self.assertIsNone(decision.targets[REG_PERIOD])


class PlanTankTest(unittest.TestCase):
    NOW = datetime(2026, 1, 13, 23, tzinfo=timezone.utc)  # Wednesday 00:00 in Stockholm

    def inputs(self, **kw) -> TankInputs:
        prices = []
        for i in range(24 * 4):
            a = self.NOW + timedelta(minutes=15 * i)
            hour = i // 4
            prices.append(PriceSlot(a, a + timedelta(minutes=15), 0.8 if hour < 5 else 2.0))
        profile = [0.0] * 168
        profile[2 * 24 + 7] = 8.0  # Wednesday 07:00 local: a shower
        data = dict(prices=prices, now=self.NOW, tz=STHLM, bt6=47.0, economy_stop=48.0, normal_start=45.0,
                    normal_stop=52.0, lux_stop=56.0, floor=OPTIMIZER_DEFAULTS[OPT_HW_OPT_FLOOR], profile=profile, confidence=0.8)
        data.update(kw)
        return TankInputs(**data)

    def test_charges_in_the_cheap_night(self) -> None:
        result = plan_tank(self.inputs())
        self.assertIsNone(result.problem)
        charges = [b for b in result.plan.blocks if b.action == "charge"]
        self.assertTrue(charges)
        self.assertLess(charges[0].start, self.NOW + timedelta(hours=5))
        self.assertGreaterEqual(result.plan.min_temp, 45.0 - 0.5)

    def test_legionella_window_is_pinned_and_not_a_controller_window(self) -> None:
        start = self.NOW + timedelta(hours=2)
        result = plan_tank(self.inputs(legionella=(start, start + timedelta(hours=2), 60.0)))
        blocks = [b for b in result.plan.blocks if b.action == "legionella"]
        self.assertEqual([(b.start, b.target) for b in blocks], [(start, 60.0)])
        windows = hw_windows(result.plan.blocks, self.NOW, STHLM)
        self.assertFalse([w for w in windows if w.start < start + timedelta(hours=2) and w.end > start])

    def test_default_floor_is_45(self) -> None:
        self.assertEqual(OPTIMIZER_DEFAULTS[OPT_HW_OPT_FLOOR], 45.0)

    def test_problems(self) -> None:
        self.assertEqual(plan_tank(self.inputs(bt6=None)).problem, "BT6 missing or stale")
        self.assertEqual(plan_tank(self.inputs(lux_stop=None)).problem, "stop temperatures unknown")
        self.assertEqual(plan_tank(self.inputs(prices=[])).problem, "no prices cover now")


if __name__ == "__main__":
    unittest.main()
