from __future__ import annotations

import math
import random
import unittest
from datetime import timezone

import context  # noqa: F401
from context import utc
from logic.optimizer.energy import EnergyModel
from logic.optimizer.heatcurve import HeatCurve
from logic.optimizer.house import HouseModel, synthetic_history
from logic.optimizer.profile import HouseProfile
from datetime import timedelta

from logic.inputs import PriceSlot
from logic.optimizer.house import LagStep
from logic.optimizer.planner import ComfortSettings
from logic.optrun import (
    HeatingInputs,
    SystemSetup,
    curve_number,
    fit_all,
    house_fit_from,
    models_for,
    models_summary,
    plan_system,
    slab_now,
)

START = utc(2026, 1, 1)
TRUE = HouseModel(0.012, 0.02, 0.0, 0.05)
CAPACITY = 11.25  # kWh/K: 150 m² at the default 75 Wh/K·m²
EMITTER = TRUE.b * CAPACITY
ETA = 0.55
CURVE = HeatCurve(curve=9)
PROFILE = HouseProfile(floor_area_m2=150, floors=1.5, target_temp=21.0, latitude=None, longitude=None)


def t_out(when) -> float:
    h = (when - START).total_seconds() / 3600.0
    return -3 + 5 * math.sin(h / 24 * 2 * math.pi) + 4 * math.sin(h / 100)


def make_rows(days: int = 21, seed: int = 1):
    """10-minute bins as the integration logs them, from a known house."""
    rnd = random.Random(seed)
    offsets = {}

    def offset_at(when) -> int:
        key = int(when.timestamp() // (3 * 3600))
        if key not in offsets:
            offsets[key] = rnd.randint(-3, 3)
        return offsets[key]

    def supply(when, t_in):
        return CURVE.supply(t_out(when), offset_at(when))

    samples = synthetic_history(TRUE, START, days * 24, t_out, supply,
                                noise=lambda i: round(rnd.gauss(0, 0.05), 1))
    energy = EnergyModel(EMITTER, ETA)
    rows = {}
    heat = 1000.0
    for s in samples:
        h = 10 / 60.0
        q = energy.heat_kw(s.t_supply, s.t_in)
        power_kw = q / energy.cop(s.t_supply)
        heat += q * h
        rows[int(s.ts.timestamp())] = {
            "t_out": s.t_out,
            "s1_t_in": s.t_in,
            "s1_supply": s.t_supply,
            "s1_offset": float(offset_at(s.ts)),
            "prio_heat": 1.0,
            "prio_hw": 0.0,
            "heat_kwh": heat,
            "power_w": power_kw * 1000.0,
            "add_kw": 0.0,
            "meter_kwh": (power_kw + 0.4 + rnd.uniform(0, 0.4)) * h,
        }
    return rows


class FitAllTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = make_rows()
        cls.fits = fit_all(cls.rows, [SystemSetup("s1", PROFILE, 9)], START)

    def test_house_fit(self) -> None:
        house = self.fits["systems"]["s1"]["house"]
        self.assertTrue(house["ok"], house["reason"])
        self.assertAlmostEqual(house["a"], TRUE.a, delta=0.2 * TRUE.a)
        self.assertAlmostEqual(house["b"], TRUE.b, delta=0.2 * TRUE.b)
        self.assertIn(house["level"], ("medium", "high"))

    def test_curve_emitter_eta_share(self) -> None:
        system = self.fits["systems"]["s1"]
        self.assertTrue(system["curve"]["ok"], system["curve"]["reason"])
        self.assertAlmostEqual(system["curve"]["bias"], 0.0, delta=0.5)
        self.assertTrue(system["emitter"]["ok"], system["emitter"]["reason"])
        self.assertAlmostEqual(system["emitter"]["value"], EMITTER, delta=0.1 * EMITTER)
        self.assertTrue(self.fits["eta"]["ok"], self.fits["eta"]["reason"])
        self.assertAlmostEqual(self.fits["eta"]["value"], ETA, delta=0.05)
        share = self.fits["hp_share"]
        self.assertTrue(share["ok"], share["reason"])
        self.assertAlmostEqual(share["beta"], 1.0, delta=0.15)
        self.assertAlmostEqual(share["base_kw"], 0.6, delta=0.1)

    def test_models_use_the_fits(self) -> None:
        models = models_for(SystemSetup("s1", PROFILE, 9), self.fits)
        self.assertTrue(models.fit.ok)
        self.assertAlmostEqual(models.energy.carnot_eta, self.fits["eta"]["value"])
        self.assertAlmostEqual(models.energy.emitter_kw_per_k, self.fits["systems"]["s1"]["emitter"]["value"])
        summary = models_summary(models)
        self.assertAlmostEqual(summary["capacity_kwh_per_k"], CAPACITY, delta=0.25 * CAPACITY)
        self.assertGreater(summary["heat_loss_w_per_k"], 0)
        # Another curve number than the one calibrated: the calibration is not used.
        other = models_for(SystemSetup("s1", PROFILE, 7), self.fits)
        self.assertEqual(other.curve, HeatCurve(curve=7))

    def test_two_systems_share_the_heat_meter(self) -> None:
        rows = {k: dict(v, s2_t_in=v["s1_t_in"], s2_supply=v["s1_supply"], s2_offset=v["s1_offset"])
                for k, v in list(self.rows.items())[:500]}
        fits = fit_all(rows, [SystemSetup("s1", PROFILE), SystemSetup("s2", PROFILE)], START)
        self.assertFalse(fits["systems"]["s2"]["emitter"]["ok"])
        self.assertIn("shared", fits["systems"]["s2"]["emitter"]["reason"])


class ModelsFallbackTest(unittest.TestCase):
    def test_no_fits_gives_priors(self) -> None:
        models = models_for(SystemSetup("s1", PROFILE, 9), None)
        self.assertFalse(models.fit.ok)
        self.assertEqual(models.house, models.prior.model)
        self.assertEqual(models.fit.reason, "not fitted yet")

    def test_broken_store(self) -> None:
        prior = HouseModel(0.01, 0.02)
        fit = house_fit_from({"ok": True, "a": "x"}, prior)
        self.assertFalse(fit.ok)
        self.assertEqual(fit.model, prior)

    def test_too_little_history(self) -> None:
        rows = dict(list(make_rows(days=1).items())[:30])
        fits = fit_all(rows, [SystemSetup("s1", PROFILE, 9)], START)
        self.assertFalse(fits["systems"]["s1"]["house"]["ok"])
        self.assertFalse(fits["eta"]["ok"])

    def test_curve_number(self) -> None:
        self.assertEqual(curve_number(None), 9.0)
        self.assertEqual(curve_number(0), 9.0)
        self.assertEqual(curve_number(5), 5.0)


FLOOR_PROFILE = HouseProfile(floor_area_m2=150, floors=1.5, target_temp=21.0, latitude=None, longitude=None,
                             emitter="floor")
FLOOR_TRUE = HouseModel(0.012, 0.03, 0.0, 0.05, 3.0, 9.0)
FLOOR_CURVE = HeatCurve(curve=5)


def make_floor_rows(days: int = 21, seed: int = 3):
    """10-minute bins from a floor-heated house (slab lag 3 h)."""
    rnd = random.Random(seed)
    energy = EnergyModel(FLOOR_TRUE.b * CAPACITY, ETA)
    lag = LagStep(FLOOR_TRUE, 1.0 / 60.0)
    t_in, t_f = 21.0, None
    heat_total = 1000.0
    rows = {}
    offset = 0
    for i in range(days * 24 * 6):
        when = START + timedelta(minutes=10 * i)
        if i % 18 == 0:
            offset = rnd.randint(-3, 3)
        outdoor = t_out(when)
        target = FLOOR_CURVE.supply(outdoor, offset)
        if t_f is None:
            t_f = target
        reading = round(t_in + rnd.gauss(0, 0.05), 1)
        free = FLOOR_TRUE.a * outdoor + FLOOR_TRUE.d
        logged = heat = 0.0
        for _ in range(10):
            t_in, f_end, q = lag.step(t_in, t_f, free, target)
            logged += target if q > 0.0 or target >= t_f else t_f
            heat += q * energy.emitter_kw_per_k
            t_f = f_end
        heat_total += heat
        power_kw = heat / energy.cop(target) * 6.0
        rows[int(when.timestamp())] = {
            "t_out": outdoor, "s1_t_in": reading, "s1_supply": logged / 10.0, "s1_offset": float(offset),
            "prio_heat": 1.0, "prio_hw": 0.0, "heat_kwh": heat_total, "power_w": power_kw * 1000.0 * 0.9,
            "add_kw": 0.0, "meter_kwh": (power_kw + 0.03) / 6.0,
        }
    return rows


class FloorFitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = make_floor_rows()
        cls.fits = fit_all(cls.rows, [SystemSetup("s1", FLOOR_PROFILE, 5)], START)

    def test_fits_the_lag_and_emitter(self) -> None:
        house = self.fits["systems"]["s1"]["house"]
        self.assertTrue(house["ok"], house["reason"])
        self.assertGreaterEqual(house["tau_f"], 2.0)
        self.assertLessEqual(house["tau_f"], 4.5)
        emitter = self.fits["systems"]["s1"]["emitter"]
        self.assertTrue(emitter["ok"], emitter["reason"])
        self.assertAlmostEqual(emitter["value"], FLOOR_TRUE.b * CAPACITY, delta=0.15 * FLOOR_TRUE.b * CAPACITY)

    def test_models_plan_on_the_slab(self) -> None:
        models = models_for(SystemSetup("s1", FLOOR_PROFILE, 5), self.fits)
        self.assertTrue(models.lag_planner)
        summary = models_summary(models)
        self.assertEqual(summary["planner"], "lagged")
        self.assertEqual(summary["emitter"], "floor")
        self.assertGreater(summary["slab_capacity_kwh_per_k"], 0)
        now = START + timedelta(days=21)
        last_day = {k: v for k, v in self.rows.items() if k >= now.timestamp() - 86400}
        slab = slab_now(last_day, "s1", models.house, now)
        self.assertIsNotNone(slab)
        prices = [PriceSlot(now + timedelta(minutes=15 * i), now + timedelta(minutes=15 * (i + 1)),
                            0.8 if i < 24 else 2.0) for i in range(96)]
        result = plan_system(HeatingInputs("s1", models, ComfortSettings(target=21.0), prices, now, timezone.utc,
                                           21.0, -3.0, current_offset=0, history=last_day))
        self.assertIsNone(result.problem)
        self.assertEqual(result.slab, slab)
        steps = result.plan.plan.steps
        self.assertEqual(len(steps), 48)  # 30-minute slots
        self.assertEqual(result.plan.settings.hold_slots, 2)
        self.assertIsNotNone(steps[0].slab_start)
        # Without history the slab is taken as settled at the current offset.
        bare = plan_system(HeatingInputs("s1", models, ComfortSettings(target=21.0), prices, now, timezone.utc,
                                         21.0, -3.0, current_offset=0))
        self.assertAlmostEqual(bare.slab, models.curve.supply(-3.0, 0), places=3)

    def test_heat_pump_meter_calibrates_directly(self) -> None:
        # The pump's own power estimate reads 10 % low; the meter (heat
        # pump only, plus 30 W of circulation) is right.
        hp = fit_all(self.rows, [SystemSetup("s1", FLOOR_PROFILE, 5)], START, meter_scope="heat_pump")
        self.assertEqual(hp["eta"]["electricity"], "meter")
        self.assertTrue(hp["eta"]["ok"], hp["eta"]["reason"])
        self.assertAlmostEqual(hp["eta"]["value"], ETA, delta=0.03)
        share = hp["hp_share"]
        self.assertEqual((share["scope"], share["beta"], share["ok"]), ("heat_pump", 1.0, True))
        self.assertAlmostEqual(share["pump_estimate_ratio"], 1.0 / 0.9, delta=0.08)  # + standby
        # Whole house: the efficiency comes from the pump's estimate.
        whole = self.fits
        self.assertEqual(whole["eta"]["electricity"], "compressor power")
        self.assertGreater(whole["eta"]["value"], ETA / 0.9 - 0.02)
        self.assertEqual(whole["hp_share"]["scope"], "whole_house")

    def test_heat_pump_meter_without_data_falls_back(self) -> None:
        rows = {k: {c: x for c, x in v.items() if c != "meter_kwh"} for k, v in self.rows.items()}
        hp = fit_all(rows, [SystemSetup("s1", FLOOR_PROFILE, 5)], START, meter_scope="heat_pump")
        self.assertIn("compressor power", hp["eta"]["electricity"])
        self.assertFalse(hp["hp_share"]["ok"])

    def test_old_fit_takes_the_prior_lag(self) -> None:
        prior = HouseModel(0.01, 0.02, 0.0, 0.0, 3.0, 9.0)
        fit = house_fit_from({"ok": True, "a": 0.012, "b": 0.025, "c": 0.0, "d": 0.05}, prior)
        self.assertEqual((fit.model.a, fit.model.tau_f, fit.model.tau_d), (0.012, 3.0, 9.0))


if __name__ == "__main__":
    unittest.main()
