from __future__ import annotations

import math
import random
import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.optimizer.energy import EnergyModel, fit_carnot_eta, fit_emitter
from logic.optimizer.heatcurve import HeatCurve, fit_heat_curve
from logic.optimizer.house import HouseModel, HouseSample, fit_house_model, synthetic_history
from logic.optimizer.meter import MeterConfig, MeterReading, fit_hp_share, slot_energy
from logic.optimizer.profile import HouseProfile, design_outdoor_temp, house_prior
from logic.optimizer.solar import solar_proxy, sun_elevation

START = utc(2026, 1, 1)
TRUE = HouseModel(0.012, 0.02, 0.1, 0.05)
PRIOR = HouseModel(0.008, 0.015, 0.05, 0.03)
CURVE = HeatCurve(curve=9)


def _hours(when) -> float:
    return (when - START).total_seconds() / 3600.0


def t_out(when) -> float:
    h = _hours(when)
    return -3 + 5 * math.sin(h / 24 * 2 * math.pi) + 4 * math.sin(h / 100)


def solar(when) -> float:
    h = _hours(when) % 24
    return max(0.0, math.sin((h - 6) / 12 * math.pi))


def history(seed: int, days: int = 21, vary_offset: bool = True, model: HouseModel = TRUE):
    rnd = random.Random(seed)
    offsets = {}

    def supply(when, t_in):
        key = int(when.timestamp() // (3 * 3600))
        if key not in offsets:
            offsets[key] = rnd.randint(-3, 3) if vary_offset else 0
        return CURVE.supply(t_out(when), offsets[key])

    # Netatmo-like: 0.1 °C resolution plus noise.
    return synthetic_history(model, START, days * 24, t_out, supply, solar,
                             noise=lambda i: round(rnd.gauss(0, 0.05), 1))


class HouseFitTest(unittest.TestCase):
    def test_recovers_parameters_from_noisy_data(self) -> None:
        for seed in (1, 2, 3):
            fit = fit_house_model(history(seed), PRIOR)
            self.assertTrue(fit.ok, fit.reason)
            m = fit.model
            self.assertAlmostEqual(m.a, TRUE.a, delta=0.15 * TRUE.a)
            self.assertAlmostEqual(m.b, TRUE.b, delta=0.15 * TRUE.b)
            self.assertAlmostEqual(m.c, TRUE.c, delta=0.2 * TRUE.c)
            # What the planner relies on: the supply that holds 21 °C.
            self.assertAlmostEqual(m.steady_supply(21, -5), TRUE.steady_supply(21, -5), delta=0.3)
            self.assertEqual(fit.level, "high")
            self.assertLess(fit.sim_rmse, 0.15)

    def test_without_offset_movement_confidence_drops(self) -> None:
        moved = fit_house_model(history(4), PRIOR)
        still = fit_house_model(history(4, vary_offset=False), PRIOR)
        self.assertGreater(still.rel_se_b, moved.rel_se_b)

    def test_too_little_data_gives_prior(self) -> None:
        fit = fit_house_model(history(1, days=0.5), PRIOR)
        self.assertFalse(fit.ok)
        self.assertEqual(fit.model, PRIOR)
        self.assertEqual(fit.level, "low")

    def test_rejects_nonsensical_signs(self) -> None:
        # Indoor rises while it gets colder outside at constant supply.
        samples = []
        for i in range(24 * 7):
            when = START + timedelta(hours=i)
            samples.append(HouseSample(when, 20 + 0.01 * i + 0.3 * math.sin(i / 5),
                                       5 - 0.05 * i + 2 * math.sin(i / 5), 30.0))
        fit = fit_house_model(samples, PRIOR)
        self.assertFalse(fit.ok)
        self.assertEqual(fit.model, PRIOR)

    def test_too_much_solar_is_pinned_not_rejected(self) -> None:
        # c soaks up daytime effects (sun on the sensor, cooking): above its
        # bound it is pinned there and the rest refitted, not the whole fit lost.
        from logic.optimizer.house import FitBounds
        bounds = FitBounds(c=(0.0, 0.06))
        fit = fit_house_model(history(1), PRIOR, bounds)
        self.assertTrue(fit.ok, fit.reason)
        self.assertAlmostEqual(fit.model.c, 0.06, places=6)
        self.assertTrue(fit.details["c_pinned"])
        self.assertAlmostEqual(fit.model.a, TRUE.a, delta=0.3 * TRUE.a)
        self.assertAlmostEqual(fit.model.b, TRUE.b, delta=0.3 * TRUE.b)
        # The solar gain cut off by the pin shifts into d: a little off, far
        # closer than the prior the rejected fit fell back to.
        self.assertAlmostEqual(fit.model.steady_supply(21, -5), TRUE.steady_supply(21, -5), delta=1.0)

    def test_rejects_outside_bounds(self) -> None:
        fast = HouseModel(0.2, 0.3, 0.0, 0.0)  # time constant 2 h
        fit = fit_house_model(history(5, model=fast), PRIOR)
        self.assertFalse(fit.ok)

    def test_step_is_exact_and_idle_below_room(self) -> None:
        m = TRUE
        t = 21.0
        for _ in range(4):
            t = m.step(t, -5, 35, 0, 0.25)
        self.assertAlmostEqual(t, m.step(21.0, -5, 35, 0, 1.0), places=9)
        # A supply under the room temperature does not cool the house faster.
        self.assertEqual(m.step(21, -5, 15, 0, 1), m.step(21, -5, 21, 0, 1))
        self.assertAlmostEqual(m.derivative(21, -5, m.steady_supply(21, -5)), 0, places=9)


class CurveTest(unittest.TestCase):
    def test_supply_shape(self) -> None:
        c = HeatCurve(curve=9)
        self.assertAlmostEqual(c.supply(20, 0), 20)
        self.assertGreater(c.supply(-20, 0), c.supply(0, 0))
        self.assertAlmostEqual(c.supply(-10, 1) - c.supply(-10, 0), c.gain)
        self.assertEqual(c.supply(-40, 10), c.max_supply)
        self.assertAlmostEqual(c.offset_for_supply(-10, c.supply(-10, 2)), 2)

    def test_fit_recovers_curve(self) -> None:
        true = HeatCurve(curve=9, slope_per_curve=0.08, offset_gain=1.4, bias=1.0)
        rnd = random.Random(7)
        rows = []
        for i in range(300):
            out = rnd.uniform(-15, 10)
            off = rnd.randint(-3, 3)
            rows.append((out, off, true.supply(out, off) + rnd.gauss(0, 0.5)))
        fit = fit_heat_curve(rows, HeatCurve(curve=9))
        self.assertTrue(fit.ok, fit.reason)
        self.assertAlmostEqual(fit.curve.slope, true.slope, delta=0.05)
        self.assertAlmostEqual(fit.curve.gain, 1.4, delta=0.15)
        self.assertAlmostEqual(fit.curve.bias, 1.0, delta=0.5)
        self.assertFalse(fit_heat_curve(rows[:5]).ok)


class EnergyTest(unittest.TestCase):
    def test_cop_matches_datasheet_shape(self) -> None:
        e = EnergyModel()
        self.assertAlmostEqual(e.cop(35, 0), 4.2, delta=0.4)  # B0/W35
        self.assertAlmostEqual(e.cop(45, 0), 3.5, delta=0.4)  # B0/W45
        self.assertGreater(e.cop(35, 5), e.cop(35, -3))
        self.assertGreaterEqual(e.cop(80, -10), e.cop_min)
        self.assertEqual(e.electric_kw(20, 21), 0.0)

    def test_fit_emitter_and_eta(self) -> None:
        true = EnergyModel(emitter_kw_per_k=0.25, carnot_eta=0.55)
        rnd = random.Random(3)
        heat_rows, cop_rows = [], []
        for i in range(200):
            supply = rnd.uniform(28, 45)
            t_in = 21 + rnd.gauss(0, 0.3)
            brine = rnd.uniform(-2, 4)
            heat = true.heat_kw(supply, t_in) * (1 + rnd.gauss(0, 0.05))
            heat_rows.append((heat, 1.0, supply, t_in))
            cop_rows.append((heat, heat / true.cop(supply, brine), supply, brine))
        k = fit_emitter(heat_rows, prior=0.4)
        self.assertTrue(k.ok)
        self.assertAlmostEqual(k.value, 0.25, delta=0.02)
        eta = fit_carnot_eta(cop_rows)
        self.assertTrue(eta.ok)
        self.assertAlmostEqual(eta.value, 0.55, delta=0.03)
        self.assertFalse(fit_emitter(heat_rows[:3], prior=0.4).ok)


class ProfileTest(unittest.TestCase):
    def test_priors_are_plausible(self) -> None:
        p = house_prior(HouseProfile(floor_area_m2=150, floors=1.5, latitude=59.3), CURVE)
        self.assertAlmostEqual(p.ua_kw_per_k, 0.1275, places=4)
        self.assertTrue(20 <= p.model.time_constant_h <= 80, p.model.time_constant_h)
        self.assertTrue(-20 <= p.design_outdoor <= -18)
        # The curve at DUT covers the design load with the derived emitter.
        load = p.ua_kw_per_k * (21 - p.design_outdoor) - p.gains_kw
        self.assertAlmostEqual(p.emitter_kw_per_k * (CURVE.supply(p.design_outdoor, 0) - 21), load, places=6)
        lo, hi = p.bounds.a
        self.assertTrue(lo < p.model.a < hi)

    def test_profile_effects(self) -> None:
        old = house_prior(HouseProfile(era="pre1960"))
        new = house_prior(HouseProfile(era="2011_later"))
        self.assertGreater(old.ua_kw_per_k, 2 * new.ua_kw_per_k)
        self.assertLess(old.uncertainty, house_prior(HouseProfile()).uncertainty)
        heavy = house_prior(HouseProfile(construction="heavy"))
        light = house_prior(HouseProfile(construction="light"))
        self.assertGreater(heavy.model.time_constant_h, light.model.time_constant_h)
        self.assertEqual(house_prior(HouseProfile(floors=1)).sensor_margin, 0.0)
        self.assertEqual(house_prior(HouseProfile(floors=2)).sensor_margin, 0.2)
        self.assertEqual(house_prior(HouseProfile(floors=2, indoor_sensors=2)).sensor_margin, 0.1)
        self.assertLess(design_outdoor_temp(67.8), design_outdoor_temp(55.6))
        self.assertLess(design_outdoor_temp(59.3, 500), design_outdoor_temp(59.3, 0))


class SolarTest(unittest.TestCase):
    def test_sun(self) -> None:
        # Stockholm midsummer noon (11:00 UTC) ~ 54°, midwinter ~ 7°, midnight below.
        self.assertAlmostEqual(sun_elevation(utc(2026, 6, 21, 11), 59.33, 18.07), 54.1, delta=1.0)
        self.assertAlmostEqual(sun_elevation(utc(2026, 12, 21, 11), 59.33, 18.07), 7.2, delta=1.0)
        self.assertLess(sun_elevation(utc(2026, 12, 21, 23), 59.33, 18.07), 0)
        self.assertEqual(solar_proxy(utc(2026, 12, 21, 23), 59.33, 18.07, 0), 0.0)
        self.assertGreater(solar_proxy(utc(2026, 6, 21, 11), 59.33, 18.07, 0.0),
                           solar_proxy(utc(2026, 6, 21, 11), 59.33, 18.07, 1.0))


class MeterTest(unittest.TestCase):
    def bounds(self, n=4):
        return [(START + timedelta(hours=i), START + timedelta(hours=i + 1)) for i in range(n)]

    def test_power_sensor(self) -> None:
        readings = [MeterReading(START + timedelta(minutes=10 * i), 2000.0) for i in range(25)]
        out = slot_energy(readings, self.bounds(), MeterConfig("power", "W"))
        for slot in out:
            self.assertAlmostEqual(slot.kwh, 2.0)
        # Unavailable for an hour: that hour is unknown, not zero.
        readings[7:13] = [MeterReading(r.ts, None) for r in readings[7:13]]
        out = slot_energy(readings, self.bounds(), MeterConfig("power", "W"))
        self.assertIsNone(out[1].kwh)
        self.assertAlmostEqual(out[0].kwh, 2.0)

    def test_energy_counter_with_reset(self) -> None:
        vals = [100.0, 101.0, 102.0, 0.5, 1.5, 2.5]  # reset to 0 at 2.5 h
        readings = [MeterReading(START + timedelta(minutes=30 * i), v) for i, v in enumerate(vals)]
        readings.append(MeterReading(START + timedelta(minutes=30 * 6), None))
        readings.append(MeterReading(START + timedelta(minutes=30 * 7), 3.5))
        out = slot_energy(readings, self.bounds(), MeterConfig("energy", "kWh"))
        self.assertAlmostEqual(out[0].kwh, 2.0)
        self.assertAlmostEqual(out[1].kwh, 1.5)  # the reset 102 -> 0.5 counts 0.5
        self.assertIsNone(out[2].kwh)  # only half covered: 2.5 h reading, then unavailable
        self.assertIsNone(out[3].kwh)  # across the unavailable reading

    def test_hp_share(self) -> None:
        rnd = random.Random(11)
        rows = []
        for i in range(24 * 21):
            hp_kw = max(0.0, rnd.uniform(0.0, 2.5))
            house = 0.6 + rnd.expovariate(1 / 0.4) + 1.05 * hp_kw  # base load is noisy and skewed
            rows.append((house, hp_kw, 1.0))
        share = fit_hp_share(rows)
        self.assertTrue(share.ok, share.reason)
        self.assertAlmostEqual(share.beta, 1.05, delta=0.08)
        self.assertAlmostEqual(share.base_kw, 1.0, delta=0.15)
        self.assertFalse(fit_hp_share(rows[:10]).ok)


if __name__ == "__main__":
    unittest.main()
