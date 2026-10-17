from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from typing import Dict, Optional

import context  # noqa: F401
from context import FakeClock
from logic.hwcontrol import (
    REG_BT6,
    REG_BT7,
    REG_COMFORT,
    REG_PERIOD,
    REG_PRIO,
    REG_TEMP_LUX,
    HotWaterController,
    HotWaterInputs,
    HotWaterLeases,
    HotWaterSettings,
)
from logic.inputs import Reading
from logic.lease import MODE_ACTIVE, MODE_OFF, MODE_SHADOW, LeaseAction
from logic.vvlearn import SLOTS, VvProfile

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

STHLM = ZoneInfo("Europe/Stockholm")
TUE = 1


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=STHLM)


def learned_profile() -> VvProfile:
    values = [0.0] * SLOTS
    values[TUE * 24 + 7] = 2.0  # economy day, peak 07:00
    return VvProfile(profile=values)


class Rig:
    """A controller with fresh pump readings at ``now``."""

    def __init__(self, now: datetime, profile: Optional[VvProfile] = None) -> None:
        self.now = now
        self.controller = HotWaterController(profile or learned_profile())
        self.settings = HotWaterSettings()
        self.values: Dict[int, Optional[float]] = {
            REG_BT6: 45.0,
            REG_BT7: 45.0,
            REG_PRIO: 30.0,
            REG_COMFORT: 1.0,
            REG_PERIOD: 20.0,
            REG_TEMP_LUX: 0.0,
            47043: 48.0,
            47044: 45.0,
            47045: 42.0,
            47047: 56.0,
            47048: 52.0,
            47049: 48.0,
        }
        self.age: Dict[int, float] = {}
        self.price_level: Optional[str] = None
        self.held: Dict[int, bool] = {}

    def inputs(self) -> HotWaterInputs:
        readings = {
            reg: Reading(value, self.now - timedelta(minutes=self.age.get(reg, 1)))
            for reg, value in self.values.items()
            if value is not None
        }
        return HotWaterInputs(self.now, STHLM, readings, self.price_level, (), dict(self.held))

    def decide(self, mode: str = MODE_ACTIVE, **features: bool):
        return self.controller.decide(mode, features, self.inputs(), self.settings)


class DecisionTest(unittest.TestCase):
    def test_off_does_nothing(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30))
        decision = rig.decide(MODE_OFF, vv_control=True, price_comfort=True)
        self.assertEqual(decision.state, "off")
        self.assertEqual(set(decision.targets.values()), {None})

    def test_all_features_off_is_idle(self) -> None:
        decision = Rig(local(2026, 10, 6, 5, 30)).decide()
        self.assertEqual(decision.state, "idle")
        self.assertEqual(set(decision.targets.values()), {None})
        # The plan is still shown.
        self.assertEqual(decision.next_heat, local(2026, 10, 6, 5, 0))

    def test_price_comfort_mode(self) -> None:
        rig = Rig(local(2026, 10, 6, 12))
        rig.price_level = "CHEAP"
        decision = rig.decide(price_comfort=True)
        self.assertEqual(decision.targets[REG_COMFORT], 1)
        self.assertEqual(decision.state, "normal")
        rig.price_level = "VERY_CHEAP"
        self.assertEqual(rig.decide(price_comfort=True).targets[REG_COMFORT], 2)
        rig.price_level = None
        decision = rig.decide(price_comfort=True)
        self.assertIsNone(decision.targets[REG_COMFORT])
        self.assertEqual(decision.state, "fault")

    def test_vv_control_heats_in_the_window(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30))
        decision = rig.decide(vv_control=True)
        self.assertEqual(decision.targets[REG_PERIOD], 0)
        self.assertEqual(decision.targets[REG_TEMP_LUX], 4)
        self.assertEqual(decision.state, "heat_now")
        self.assertEqual(decision.details["heat_sources"], ["vv_control"])

    def test_vv_control_blocks_outside_the_window(self) -> None:
        rig = Rig(local(2026, 10, 6, 3, 0))
        decision = rig.decide(vv_control=True)
        self.assertEqual(decision.targets[REG_PERIOD], 0)
        self.assertIsNone(decision.targets[REG_TEMP_LUX])
        self.assertEqual(decision.state, "block")

    def test_floor_heats_outside_the_window(self) -> None:
        rig = Rig(local(2026, 10, 6, 12, 0))
        rig.values[REG_BT7] = 30.0
        decision = rig.decide(vv_control=True)
        self.assertEqual(decision.targets[REG_TEMP_LUX], 4)
        self.assertIn("floor", decision.reasons["vv_control"])

    def test_regulation_takes_precedence_over_vv_control(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30))
        decision = rig.decide(vv_control=True, autoluxury=True)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertIn("precedence", decision.reasons["vv_control"])

    def test_autoluxury_and_priority_share_temporary_lux(self) -> None:
        rig = Rig(local(2026, 10, 6, 12, 0))
        rig.values[REG_BT7] = 44.0  # at/below the normal start temperature 45
        decision = rig.decide(autoluxury=True, priority=True)
        self.assertEqual(decision.targets[REG_TEMP_LUX], 4)
        self.assertEqual(decision.details["heat_sources"], ["priority"])
        self.assertIsNone(decision.targets[REG_PERIOD])

    def test_stale_bt7_hands_hot_water_back(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30))
        rig.age[REG_BT7] = 61
        decision = rig.decide(vv_control=True)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertIsNone(decision.targets[REG_TEMP_LUX])
        self.assertEqual(decision.state, "fault")
        self.assertFalse(decision.inputs_ok)

    def test_missing_stop_temperatures(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30))
        rig.values[47047] = None
        decision = rig.decide(vv_control=True)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertIn("stop temperatures", decision.reasons["vv_control"])

    def test_zero_period_is_not_taken_as_baseline(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30))
        rig.values[REG_PERIOD] = 0.0
        decision = rig.decide(vv_control=True)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertIsNone(decision.targets[REG_TEMP_LUX])
        # Held by us (the 0 is ours): keep going.
        rig.held[REG_PERIOD] = True
        self.assertEqual(rig.decide(vv_control=True).targets[REG_PERIOD], 0)

    def test_nothing_learned_without_floor_leaves_it_to_the_pump(self) -> None:
        rig = Rig(local(2026, 10, 6, 5, 30), VvProfile())
        rig.settings = HotWaterSettings(floor_enabled=False)
        decision = rig.decide(vv_control=True)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertEqual(decision.state, "idle")
        rig.settings = HotWaterSettings()
        self.assertEqual(rig.decide(vv_control=True).targets[REG_PERIOD], 0)


class LearningTest(unittest.TestCase):
    def test_learns_in_shadow_and_off_switch_stops_it(self) -> None:
        rig = Rig(local(2026, 10, 6, 6, 40), VvProfile())
        changed = False
        values = [50.0] * 30 + [50.0 - i - 1 for i in range(8)] + [42.0] * 20
        for value in values:
            rig.values[REG_BT6] = value
            changed = rig.controller.learn(rig.inputs(), rig.settings, True) or changed
            rig.controller.decide(MODE_SHADOW, {"learning": True}, rig.inputs(), rig.settings)
            rig.now += timedelta(minutes=1)
        self.assertTrue(changed)
        self.assertAlmostEqual(rig.controller.profile.profile[TUE * 24 + 7], 0.8)
        self.assertEqual(rig.controller.last_event.verdict, "learned")
        rig.controller.learn(rig.inputs(), rig.settings, False)
        self.assertIsNone(rig.controller.detector.peak)

    def test_charging_is_not_learned(self) -> None:
        rig = Rig(local(2026, 10, 6, 6, 40), VvProfile())
        rig.values[REG_PRIO] = 20.0
        for value in [50.0] * 5 + [30.0] * 10 + [50.0] * 5:
            rig.values[REG_BT6] = value
            rig.controller.learn(rig.inputs(), rig.settings, True)
            rig.now += timedelta(minutes=1)
        self.assertTrue(rig.controller.profile.is_empty())


class LeaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock(local(2026, 10, 6, 5, 30))
        self.leases = HotWaterLeases(clock=self.clock)
        self.rig = Rig(self.clock.now)

    def apply(self, mode: str, decision, healthy: bool = True, in_grace: bool = False):
        actions = self.leases.decide(mode, decision, healthy, in_grace)
        for lease, action in actions:
            lease.record(action)
        return {lease.register: action for lease, action in actions}

    def test_active_sets_all_three(self) -> None:
        self.rig.price_level = "CHEAP"
        decision = self.rig.decide(vv_control=True, price_comfort=True)
        actions = self.apply(MODE_ACTIVE, decision)
        self.assertEqual(
            actions,
            {
                REG_COMFORT: LeaseAction("set", 1),
                REG_PERIOD: LeaseAction("set", 0),
                REG_TEMP_LUX: LeaseAction("set", 4),
            },
        )
        self.assertEqual(
            actions[REG_TEMP_LUX].payload(900, "ha"), {"value": 4, "ttl": 900, "owner": "ha"}
        )
        # Same targets a minute later: nothing to send until renewal.
        self.clock.advance(60)
        self.assertEqual(self.apply(MODE_ACTIVE, decision), {})
        self.clock.advance(300)
        self.assertEqual(len(self.apply(MODE_ACTIVE, decision)), 3)

    def test_target_reached_releases_temporary_lux_only(self) -> None:
        self.apply(MODE_ACTIVE, self.rig.decide(vv_control=True))
        self.rig.values[REG_BT7] = 48.0
        self.rig.held = self.leases.held()
        actions = self.apply(MODE_ACTIVE, self.rig.decide(vv_control=True))
        self.assertEqual(actions, {REG_TEMP_LUX: LeaseAction("release")})
        self.assertTrue(self.leases.leases[REG_PERIOD].held)

    def test_shadow_never_sets_and_releases_held(self) -> None:
        decision = self.rig.decide(vv_control=True)
        self.assertEqual(self.apply(MODE_SHADOW, decision), {})
        self.apply(MODE_ACTIVE, decision)
        actions = self.apply(MODE_SHADOW, decision)
        self.assertEqual(
            actions, {REG_PERIOD: LeaseAction("release"), REG_TEMP_LUX: LeaseAction("release")}
        )

    def test_unhealthy_tick_releases(self) -> None:
        decision = self.rig.decide(vv_control=True)
        self.apply(MODE_ACTIVE, decision)
        actions = self.apply(MODE_ACTIVE, decision, healthy=False)
        self.assertEqual(set(actions), {REG_PERIOD, REG_TEMP_LUX})
        self.assertEqual({a.kind for a in actions.values()}, {"release"})

    def test_stale_input_releases(self) -> None:
        self.apply(MODE_ACTIVE, self.rig.decide(vv_control=True))
        self.rig.age[REG_BT7] = 120
        self.rig.held = self.leases.held()
        actions = self.apply(MODE_ACTIVE, self.rig.decide(vv_control=True))
        self.assertEqual(
            actions, {REG_PERIOD: LeaseAction("release"), REG_TEMP_LUX: LeaseAction("release")}
        )

    def test_grace_sends_nothing_in_active_mode(self) -> None:
        decision = self.rig.decide(vv_control=True)
        self.assertEqual(self.apply(MODE_ACTIVE, decision, in_grace=True), {})
        self.assertEqual(self.apply(MODE_ACTIVE, decision), {
            REG_PERIOD: LeaseAction("set", 0), REG_TEMP_LUX: LeaseAction("set", 4)
        })

    def test_release_all(self) -> None:
        self.apply(MODE_ACTIVE, self.rig.decide(vv_control=True))
        released = {lease.register for lease, _ in self.leases.release_all()}
        self.assertEqual(released, {REG_PERIOD, REG_TEMP_LUX})

    def test_limits(self) -> None:
        lease = self.leases.leases[REG_COMFORT]
        self.assertEqual(lease.decide(MODE_ACTIVE, 7, True), LeaseAction("set", 2))


class LegionellaTest(unittest.TestCase):
    """The legionella guard inside the controller (logic/legionella.py)."""

    def overdue_rig(self) -> Rig:
        rig = Rig(local(2026, 10, 13, 12))
        rig.values[47050] = 0.0
        rig.values[47046] = 60.0
        rig.controller.legionella.state.last_done = rig.now - timedelta(days=15)
        return rig

    def test_cycle_overrides_vv_control_and_away(self) -> None:
        from dataclasses import replace

        rig = self.overdue_rig()
        decision = rig.decide(vv_control=True)
        self.assertEqual(decision.state, "legionella")
        self.assertEqual(decision.targets[47047], 60.0)
        self.assertEqual(decision.targets[REG_TEMP_LUX], 4)
        self.assertIsNone(decision.targets[REG_PERIOD], "47134 released so the pump may charge")
        self.assertIn("legionella", decision.details["heat_sources"])
        # Away economy does not pause it.
        inputs = replace(rig.inputs(), away="economy")
        decision = rig.controller.decide(MODE_ACTIVE, {"vv_control": True}, inputs, rig.settings)
        self.assertEqual(decision.targets[47047], 60.0)
        self.assertEqual(decision.targets[REG_COMFORT], 0, "47041 left to the other features")

    def test_shadow_plans_and_writes_nothing(self) -> None:
        rig = self.overdue_rig()
        decision = rig.decide(MODE_SHADOW, vv_control=True)
        self.assertNotEqual(decision.state, "legionella")
        self.assertIsNone(decision.targets[47047])
        self.assertEqual(decision.legionella.state, "planned")

    def test_off_still_watches_bt7(self) -> None:
        rig = Rig(local(2026, 10, 13, 12))
        rig.values[REG_BT7] = 61.0
        rig.decide(MODE_OFF)
        rig.now += timedelta(minutes=10)
        decision = rig.decide(MODE_OFF)
        self.assertEqual(decision.legionella.state, "off")
        self.assertEqual(rig.controller.legionella.state.last_done, rig.now)

    def test_plans_use_the_luxury_stop_from_before_the_cycle(self) -> None:
        rig = self.overdue_rig()
        rig.decide(vv_control=True)
        rig.values[47047] = 60.0  # the leased value comes back from the pump
        rig.held = {47047: True}
        rig.now += timedelta(minutes=1)
        decision = rig.decide(vv_control=True)
        self.assertEqual(decision.details["stop_temperatures"]["luxury"], 56.0)

    def test_lux_stop_lease_has_one_decimal(self) -> None:
        leases = HotWaterLeases()
        rig = self.overdue_rig()
        actions = dict((lease.register, action) for lease, action in leases.decide(MODE_ACTIVE, rig.decide(), True))
        self.assertEqual(actions[47047], LeaseAction("set", 60.0))
        self.assertEqual(leases.leases[47047].decide(MODE_ACTIVE, 62.54, True), LeaseAction("set", 62.5))
        self.assertEqual(leases.leases[47047].decide(MODE_ACTIVE, 80, True), LeaseAction("set", 70))


if __name__ == "__main__":
    unittest.main()
