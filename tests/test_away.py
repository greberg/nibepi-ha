"""Away (vacation) mode: setpoint, rules component, optimiser band and return."""

from __future__ import annotations

import unittest
from dataclasses import replace as _replace
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.away import (
    HW_ECONOMY,
    HW_MINIMUM,
    HW_NORMAL,
    AwaySettings,
    AwayState,
    away_comfort,
    away_component,
    away_setpoint,
    indoor_active,
    returning_at,
    rules_away,
    start,
)
from logic import curve
from logic.indoor import compute_indoor
from logic.inputs import Decision, Reading
from logic.optimizer.backtest import backtest_heating
from logic.optimizer.heatcurve import HeatCurve
from logic.optimizer.horizon import HorizonSlot
from logic.optimizer.planner import ComfortSettings, PlannerSettings, comfort_schedule, neutral_offset, plan_heating
from logic.optimizer.profile import HouseProfile, house_prior
from logic.hwcontrol import REG_BT6, REG_BT7, REG_COMFORT, REG_PERIOD, REG_PRIO, REG_TEMP_LUX
from logic.lease import MODE_ACTIVE
from logic.vvlearn import SLOTS, VvProfile
from logic.vvplan import HeatWindow
from test_hwcontrol import Rig, local

NOW = utc(2026, 1, 14, 23)
SE3_DAY = [0.92, 0.88, 0.85, 0.84, 0.86, 0.95, 1.35, 2.10, 2.45, 2.05, 1.70, 1.55,
           1.48, 1.45, 1.52, 1.70, 2.05, 2.60, 2.75, 2.30, 1.80, 1.45, 1.20, 1.02]


class StateTest(unittest.TestCase):
    def test_start_without_end(self) -> None:
        state = start(NOW)
        self.assertTrue(state.enabled)
        self.assertIsNone(state.until)
        self.assertTrue(state.active(NOW + timedelta(days=30)))
        self.assertFalse(state.expired(NOW + timedelta(days=30)))

    def test_until_in_the_past_or_naive_means_no_end(self) -> None:
        self.assertIsNone(start(NOW, NOW - timedelta(hours=1)).until)
        self.assertIsNone(start(NOW, NOW).until)
        self.assertIsNone(start(NOW, NOW.replace(tzinfo=None) + timedelta(days=1)).until)
        future = start(NOW, NOW + timedelta(days=2))
        self.assertEqual(future.until, NOW + timedelta(days=2))
        self.assertTrue(future.active(NOW + timedelta(days=1)))
        self.assertFalse(future.active(NOW + timedelta(days=2)))
        self.assertTrue(future.expired(NOW + timedelta(days=2)))

    def test_setback_override_is_checked(self) -> None:
        self.assertEqual(start(NOW, setback=4.5).setback, 4.5)
        self.assertIsNone(start(NOW, setback=-1).setback)
        self.assertIsNone(start(NOW, setback=25).setback)
        settings = AwaySettings()
        self.assertEqual(settings.setback_for(start(NOW)), 3.0)
        self.assertEqual(settings.setback_for(start(NOW, setback=5)), 5.0)

    def test_round_trip_and_tolerant_load(self) -> None:
        state = start(NOW, NOW + timedelta(days=3), 2.5)
        self.assertEqual(AwayState.from_dict(state.to_dict()), state)
        self.assertEqual(AwayState.from_dict(None), AwayState())
        self.assertEqual(AwayState.from_dict({"enabled": "yes"}), AwayState())
        broken = AwayState.from_dict({"enabled": True, "until": "soon", "setback": "x"})
        self.assertTrue(broken.enabled)
        self.assertIsNone(broken.until)
        self.assertIsNone(broken.setback)

    def test_settings_defaults_and_bounds(self) -> None:
        settings = AwaySettings.from_values()
        self.assertEqual(
            (settings.setback, settings.min_indoor, settings.curve_offset, settings.hot_water, settings.preheat,
             settings.return_lead_h),
            (3.0, 16.0, -3.0, HW_ECONOMY, True, 0.0),
        )
        odd = AwaySettings.from_values(setback=30, curve_offset=-20, hot_water="boil", return_lead_h=-2)
        self.assertEqual((odd.setback, odd.curve_offset, odd.hot_water, odd.return_lead_h), (10.0, -10.0, HW_ECONOMY, 0.0))

    def test_return_lead_for_the_rules(self) -> None:
        state = start(NOW, NOW + timedelta(hours=10))
        settings = AwaySettings(return_lead_h=3)
        self.assertEqual(returning_at(state, settings), NOW + timedelta(hours=7))
        self.assertTrue(rules_away(state, settings, NOW + timedelta(hours=6)))
        self.assertFalse(rules_away(state, settings, NOW + timedelta(hours=7)))
        self.assertTrue(rules_away(start(NOW), settings, NOW + timedelta(days=9)))
        self.assertFalse(rules_away(AwayState(), settings, NOW))


class RulesTest(unittest.TestCase):
    def test_setpoint_shift_and_clamp(self) -> None:
        self.assertEqual(away_setpoint(21.0, 3.0, 16.0), 18.0)
        self.assertEqual(away_setpoint(18.0, 5.0, 16.0), 16.0)  # the absolute floor
        self.assertEqual(away_setpoint(15.0, 3.0, 16.0), 15.0)  # never above the normal setpoint
        self.assertEqual(away_setpoint(21.0, -2.0, 16.0), 21.0)
        self.assertIsNone(away_setpoint(None, 3.0, 16.0))

    def test_away_component_only_without_indoor_control(self) -> None:
        on = away_component(indoor_active=True, curve_offset=-3)
        self.assertEqual(on.offset, 0.0)
        off = away_component(indoor_active=False, curve_offset=-3)
        self.assertEqual(off.offset, -3.0)
        self.assertTrue(off.inputs_ok)
        ok = Decision(1.0, "x", True)
        self.assertTrue(indoor_active(True, ok))
        self.assertFalse(indoor_active(False, ok))  # switched off
        self.assertFalse(indoor_active(True, Decision.zero("indoor sensor missing")))
        self.assertFalse(indoor_active(True, None))

    def test_no_double_setback(self) -> None:
        # Indoor active: the lowered setpoint pulls the curve, "away" adds 0.
        reading = Reading(21.0, NOW)
        indoor = compute_indoor(reading, away_setpoint(21.0, 3.0, 16.0), 2.0, NOW)
        decisions = {"indoor": indoor, "away": away_component(indoor_active(True, indoor), -3)}
        plan = curve.plan(decisions, 0.0)
        self.assertEqual(plan.components, {"indoor": -6.0, "away": 0.0})
        self.assertEqual(plan.target, -6)
        # No indoor sensor: the fixed away component lowers the curve.
        missing = compute_indoor(None, 18.0, 2.0, NOW)
        decisions = {"indoor": missing, "away": away_component(indoor_active(True, missing), -3)}
        plan = curve.plan(decisions, 0.0)
        self.assertEqual(plan.target, -3)
        self.assertIn("away", plan.reasons)


class ComfortTest(unittest.TestCase):
    COMFORT = ComfortSettings(target=21.0, below=0.5, above=1.0, hard_floor_below=1.5, sensor_margin=0.1)

    def test_band_moves_down(self) -> None:
        away = away_comfort(self.COMFORT, 3.0, 16.0)
        self.assertAlmostEqual(away.target, 18.0)
        self.assertAlmostEqual(away.lower, self.COMFORT.lower - 3.0)
        self.assertAlmostEqual(away.floor, self.COMFORT.floor - 3.0)
        # Warmer than the lowered target only costs energy.
        self.assertAlmostEqual(away.upper, self.COMFORT.upper)
        self.assertEqual(away.penalty_per_hour(19.5), 0.0)
        self.assertGreater(away.penalty_per_hour(17.0), 0.0)

    def test_floor_never_below_min_indoor(self) -> None:
        away = away_comfort(self.COMFORT, 5.0, 17.0)
        self.assertAlmostEqual(away.target, 17.0)
        self.assertGreaterEqual(away.floor, 17.0 - 1e-9)
        self.assertLessEqual(away.floor, away.lower + 1e-9)
        self.assertIs(away_comfort(self.COMFORT, 0.0, 16.0), self.COMFORT)

# --------------------------------------------------------------------------
# The optimiser while away
# --------------------------------------------------------------------------

FLOOR_CURVE = HeatCurve(curve=5)
FLOOR = house_prior(HouseProfile(floor_area_m2=150, floors=1.5, latitude=59.3, emitter="floor"), FLOOR_CURVE)
RADIATOR_CURVE = HeatCurve(curve=9)
RADIATORS = house_prior(HouseProfile(floor_area_m2=150, floors=1, latitude=59.3), RADIATOR_CURVE)
NORMAL = ComfortSettings(target=21.0, sensor_margin=FLOOR.sensor_margin)
SETTINGS = AwaySettings()
AWAY = away_comfort(NORMAL, SETTINGS.setback, SETTINGS.min_indoor)


def slots(hours: int, minutes: int = 30, t_out=lambda h: -6.0):
    out = []
    for i in range(hours * 60 // minutes):
        a = NOW + timedelta(minutes=minutes * i)
        hour = i * minutes // 60
        out.append(HorizonSlot(a, a + timedelta(minutes=minutes), SE3_DAY[hour % 24], t_out(hour)))
    return out


class OptimiserAwayTest(unittest.TestCase):
    def test_lowered_band_plans_lower_and_cheaper(self) -> None:
        house, energy = RADIATORS.model, RADIATORS.energy_model()
        horizon = slots(24, 15)
        base = neutral_offset(house, RADIATOR_CURVE, -6.0, 21.0)
        comfort = ComfortSettings(target=21.0)
        normal = plan_heating(horizon, 21.0, house, RADIATOR_CURVE, energy, comfort,
                              PlannerSettings(base_offset=base), current_offset=base)
        away_band = away_comfort(comfort, 3.0, 16.0)
        away_base = neutral_offset(house, RADIATOR_CURVE, -6.0, away_band.target)
        away = plan_heating(horizon, 21.0, house, RADIATOR_CURVE, energy, away_band,
                            PlannerSettings(base_offset=away_base), current_offset=base)
        mean = lambda p: sum(p.offsets) / len(p.offsets)  # noqa: E731
        self.assertLess(away_base, base)
        self.assertLess(mean(away), mean(normal) - 1.0)
        self.assertLess(away.plan.energy_cost, 0.8 * normal.plan.energy_cost)
        self.assertGreaterEqual(away.plan.min_t_in, away_band.floor - 0.05)

    def test_never_below_away_min_indoor(self) -> None:
        # A deep setback against a high absolute floor, very expensive power.
        house, energy = FLOOR.model, FLOOR.energy_model()
        band = away_comfort(NORMAL, 8.0, 17.5)
        self.assertGreaterEqual(band.floor, 17.5)
        base = neutral_offset(house, FLOOR_CURVE, -12.0, band.target)
        expensive = [HorizonSlot(s.start, s.end, s.price * 4, -12.0) for s in slots(36)]
        plan = plan_heating(expensive, 18.0, house, FLOOR_CURVE, energy, band,
                            PlannerSettings(base_offset=base, change_cost=0.3, step_cost=0.05, hold_slots=2),
                            current_offset=base)
        self.assertGreaterEqual(plan.plan.min_t_in, 17.5 - 0.05)


class PreheatTest(unittest.TestCase):
    """A floor-heated house away for days: back at the normal band at ``until``.

    Closed loop: replanned every 2 h with a 20 h horizon, so the return
    only comes into view after the first plan.
    """

    UNTIL_H = 22

    @classmethod
    def setUpClass(cls) -> None:
        house, energy = FLOOR.model, FLOOR.energy_model()
        truth = slots(cls.UNTIL_H + 4)
        until = NOW + timedelta(hours=cls.UNTIL_H)
        bands = comfort_schedule(truth, AWAY, NORMAL, until)
        cls.base_away = neutral_offset(house, FLOOR_CURVE, -6.0, AWAY.target)
        base_normal = neutral_offset(house, FLOOR_CURVE, -6.0, NORMAL.target)
        cls.settings = PlannerSettings(base_offset=cls.base_away, max_up=3 + (base_normal - cls.base_away),
                                       change_cost=0.3, step_cost=0.05, hold_slots=2)
        baseline = [base_normal if s.end >= until else cls.base_away for s in truth]
        cls.t_f0 = FLOOR_CURVE.supply(-6.0, cls.base_away)
        # The house has been away for days: settled at the lowered target.
        cls.result = backtest_heating(truth, AWAY.target, house, FLOOR_CURVE, energy, AWAY, cls.settings,
                                      replan_every=4, lookahead=40, t_f0=cls.t_f0,
                                      comforts=bands, baseline_offsets=baseline)
        cls.at = cls.UNTIL_H * 2 - 1  # the slot that ends at the return

    def test_back_at_the_normal_band_at_the_return(self) -> None:
        steps = self.result.plan.steps
        self.assertGreaterEqual(steps[self.at].t_in_end, NORMAL.lower - 0.05)
        self.assertLess(abs(steps[self.at].t_in_end - NORMAL.target), 0.6)
        # and it stays there afterwards
        self.assertGreaterEqual(min(s.t_in_end for s in steps[self.at:]), NORMAL.lower - 0.05)
        self.assertLessEqual(max(s.t_in_end for s in steps), NORMAL.upper)

    def test_heats_back_ahead_of_the_return(self) -> None:
        steps = self.result.plan.steps
        # Still well below the normal band when the return comes into view.
        self.assertLess(max(s.t_in_end for s in steps[:4]), NORMAL.lower - 1.5)
        # It heats hours ahead (the slab lags), above what the lowered base allows.
        offsets = self.result.offsets
        self.assertGreater(max(offsets[4:self.at - 8]), self.base_away + 3)

    def test_without_the_return_the_house_stays_low(self) -> None:
        house, energy = FLOOR.model, FLOOR.energy_model()
        horizon = slots(self.UNTIL_H)
        plan = plan_heating(horizon, AWAY.target, house, FLOOR_CURVE, energy, AWAY, self.settings,
                            current_offset=self.base_away, t_f0=self.t_f0)
        self.assertLess(plan.plan.steps[-1].t_in_end, AWAY.target + 0.3)
        self.assertGreaterEqual(plan.plan.min_t_in, AWAY.lower - 0.05)


# --------------------------------------------------------------------------
# Hot water and learning while away
# --------------------------------------------------------------------------

class AwayRig(Rig):
    away = None

    def inputs(self):
        return _replace(super().inputs(), away=self.away)


def hw(away, **features):
    rig = AwayRig(local(2026, 10, 6, 5, 30))  # Tuesday, inside the VV-AI window before 07:00
    rig.away = away
    return rig, rig.decide(MODE_ACTIVE, **features)


class HotWaterAwayTest(unittest.TestCase):
    ALL = dict(vv_control=True, price_comfort=True, autoluxury=True, priority=True)

    def test_normal_changes_nothing(self) -> None:
        for features in (dict(vv_control=True), dict(price_comfort=True), {}):
            _, plain = hw(None, **features)
            _, away = hw(HW_NORMAL, **features)
            self.assertEqual(away.targets, plain.targets)
            self.assertEqual(away.state, plain.state)

    def test_economy(self) -> None:
        rig, decision = hw(HW_ECONOMY, vv_control=True, price_comfort=True)
        self.assertEqual(decision.targets[REG_COMFORT], 0)
        self.assertIsNone(decision.targets[REG_PERIOD])  # the pump heats itself
        self.assertIsNone(decision.targets[REG_TEMP_LUX])  # no VV-AI window charge
        self.assertEqual(decision.state, "economy")
        self.assertEqual(decision.windows, [])
        self.assertIn("economy mode", decision.reasons["vv_control"])
        # Boosts are off too.
        rig.values[REG_BT7] = 40.0  # at the normal start temperature: priority would fire
        decision = rig.decide(MODE_ACTIVE, **self.ALL)
        self.assertIsNone(decision.targets[REG_TEMP_LUX])
        self.assertEqual(decision.reasons["priority"], "away: off")

    def test_economy_keeps_the_floor(self) -> None:
        rig, _ = hw(HW_ECONOMY)
        rig.values[REG_BT7] = 30.0  # under the 35 °C floor
        decision = rig.decide(MODE_ACTIVE)
        self.assertEqual(decision.targets[REG_TEMP_LUX], 4)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertIn("away_floor", decision.details["heat_sources"])

    def test_economy_ignores_optimiser_charges(self) -> None:
        rig = AwayRig(local(2026, 10, 6, 12, 0))
        rig.away = HW_ECONOMY
        now = rig.now
        window = HeatWindow(now - timedelta(minutes=30), now + timedelta(hours=1), now + timedelta(hours=1),
                            now.date(), 2, 52.0)
        inputs = _replace(rig.inputs(), optimiser_windows=[window])
        decision = rig.controller.decide(MODE_ACTIVE, {"optimiser": True}, inputs, rig.settings)
        self.assertIsNone(decision.targets[REG_TEMP_LUX])
        self.assertEqual(decision.reasons["optimiser"], "away: no planned charges")

    def test_minimum_holds_the_period_and_heats_only_at_the_floor(self) -> None:
        rig, decision = hw(HW_MINIMUM, vv_control=True)
        self.assertEqual(decision.targets[REG_PERIOD], 0)
        self.assertEqual(decision.targets[REG_COMFORT], 0)
        self.assertIsNone(decision.targets[REG_TEMP_LUX])  # inside a VV-AI window, but away
        rig.values[REG_BT7] = 30.0
        decision = rig.decide(MODE_ACTIVE, vv_control=True)
        self.assertEqual(decision.targets[REG_TEMP_LUX], 4)
        self.assertEqual(decision.targets[REG_PERIOD], 0)

    def test_minimum_falls_back_to_economy(self) -> None:
        # VV-AI control off: never takes 47134 (the pump's periodic increase runs).
        _, decision = hw(HW_MINIMUM)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertEqual(decision.targets[REG_COMFORT], 0)
        # The pump's period reads 0: the gateway would restore 0.
        rig, _ = hw(HW_MINIMUM, vv_control=True)
        rig.values[REG_PERIOD] = 0.0
        decision = rig.decide(MODE_ACTIVE, vv_control=True)
        self.assertIsNone(decision.targets[REG_PERIOD])
        self.assertIn("falls back to economy", decision.reasons["vv_control"])
        # No min-temp floor: nothing would heat.
        rig, _ = hw(HW_MINIMUM, vv_control=True)
        rig.settings = _replace(rig.settings, floor_enabled=False)
        decision = rig.decide(MODE_ACTIVE, vv_control=True)
        self.assertIsNone(decision.targets[REG_PERIOD])


class LearningAwayTest(unittest.TestCase):
    def test_hold_skips_decay_and_marks_the_hours_away(self) -> None:
        values = [1.0] * SLOTS
        profile = VvProfile(profile=list(values), seen_day="2026-10-02")  # Friday
        tuesday = local(2026, 10, 6, 9, 30)
        self.assertTrue(profile.hold(tuesday.date(), 1 * 24 + 9))
        self.assertEqual(profile.profile, values)  # no decay of the days away
        self.assertEqual(profile.seen_day, "2026-10-06")
        self.assertTrue(all(profile.seen[24:34]))
        self.assertFalse(any(profile.seen[34:48]))
        self.assertFalse(profile.hold(tuesday.date(), 1 * 24 + 9))  # nothing new
        # Back home the same day: no decay today; tomorrow only the hours after 09:00.
        self.assertFalse(profile.apply_daily_decay(tuesday.date(), 0.1))
        profile.apply_daily_decay(tuesday.date() + timedelta(days=1), 0.1)
        self.assertEqual(profile.profile[24:34], [1.0] * 10)
        self.assertEqual(profile.profile[34:48], [0.9] * 14)
        self.assertEqual(profile.profile[0:24], [1.0] * 24)

    def test_controller_does_not_learn_while_away(self) -> None:
        rig = Rig(local(2026, 10, 6, 7, 0))
        controller = rig.controller
        before = list(controller.profile.profile)
        # A large fast BT6 drop: would be learned at home.
        for minute in range(0, 60, 1):
            rig.now = local(2026, 10, 6, 7, 0) + timedelta(minutes=minute)
            rig.values[REG_BT6] = 50.0 - minute * 0.2
            rig.values[REG_PRIO] = 30.0
            controller.learn_away(rig.inputs())
        rig.now = local(2026, 10, 6, 8, 30)
        rig.values[REG_BT6] = 39.0
        controller.learn_away(rig.inputs())
        self.assertEqual(controller.profile.profile, before)
        self.assertEqual(controller.profile.learned, 0)
        self.assertIsNone(controller.last_event)


class OptionsTest(unittest.TestCase):
    def test_const_matches_the_logic(self) -> None:
        import const
        from logic import away

        self.assertEqual(const.AWAY_HOT_WATER_CHOICES, away.HW_CHOICES)
        settings = away.AwaySettings.from_values(**{
            "setback": const.AWAY_DEFAULTS[const.OPT_AWAY_SETBACK],
            "min_indoor": const.AWAY_DEFAULTS[const.OPT_AWAY_MIN_INDOOR],
            "curve_offset": const.AWAY_DEFAULTS[const.OPT_AWAY_CURVE_OFFSET],
            "hot_water": const.AWAY_DEFAULTS[const.OPT_AWAY_HOT_WATER],
            "preheat": const.AWAY_DEFAULTS[const.OPT_AWAY_PREHEAT],
            "return_lead_h": const.AWAY_DEFAULTS[const.OPT_AWAY_RETURN_LEAD],
        })
        self.assertEqual(settings, away.AwaySettings())

    def test_translations(self) -> None:
        import json
        import os

        import const

        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "nibepi")
        for name in ("strings.json", "translations/en.json", "translations/sv.json"):
            with open(os.path.join(root, name), encoding="utf-8") as f:
                data = json.load(f)
            step = data["options"]["step"]["away"]
            self.assertEqual(set(step["data"]), set(const.AWAY_DEFAULTS), name)
            self.assertEqual(set(data["selector"]["away_hot_water"]["options"]), set(const.AWAY_HOT_WATER_CHOICES))
            self.assertIn("away", data["entity"]["switch"])
            self.assertIn("away_until", data["entity"]["sensor"])
            self.assertEqual(set(data["services"]["set_away"]["fields"]), {"enabled", "until", "setback", "config_entry_id"})


if __name__ == "__main__":
    unittest.main()
