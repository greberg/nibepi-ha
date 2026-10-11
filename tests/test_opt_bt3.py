"""BT3 (return temperature) as an observation of the floor heating slab."""

from __future__ import annotations

import math
import random
import unittest
from dataclasses import replace
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.optimizer.heatcurve import HeatCurve
from logic.optimizer.house import (
    HouseModel,
    HouseSample,
    fit_house_model,
    fit_slab,
    slab_filter,
    slab_observable,
    slab_temp,
    slab_track,
    synthetic_history,
)
from logic.optimizer.profile import HouseProfile, house_prior

START = utc(2026, 1, 1)
CURVE = HeatCurve(curve=5)
PROFILE = HouseProfile(floor_area_m2=150, floors=1, latitude=None, longitude=None, emitter="floor")
FLOOR = house_prior(PROFILE, CURVE)


def t_out(when) -> float:
    h = (when - START).total_seconds() / 3600.0
    return -3 + 5 * math.sin(h / 24 * 2 * math.pi) + 4 * math.sin(h / 100)


def hot_water(when) -> bool:
    """Two 40-minute charges a day."""
    return when.hour in (6, 18) and when.minute < 40


def floor_house(tau_f: float, ratio: float) -> HouseModel:
    p = FLOOR.model
    return HouseModel(p.a * 1.1, p.b * 0.9, p.c, p.d, tau_f, tau_f * ratio)


def history(true: HouseModel, kappa: float, seed: int, days: int = 21, pump_stops: bool = False):
    """Offsets changing every 3 h, hot water twice a day, noisy sensors at 0.1 °C."""
    rnd = random.Random(seed)
    offsets = {}

    def supply(when, t_in):
        key = int(when.timestamp() // (3 * 3600))
        if key not in offsets:
            offsets[key] = rnd.randint(-4, 3)
        return CURVE.supply(t_out(when), offsets[key])

    truth = []
    samples = synthetic_history(
        true, START, days * 24, t_out, supply, noise=lambda i: round(rnd.gauss(0, 0.05), 1),
        kappa=kappa, hot_water=hot_water, water_noise=lambda i: round(rnd.gauss(0, 0.1), 1),
        pump_stops=pump_stops, truth=truth,
    )
    return samples, truth, offsets


def without_bt3(samples):
    return [replace(s, t_return=None) for s in samples]


class ObservableTest(unittest.TestCase):
    def sample(self, minute: int, **kw) -> HouseSample:
        base = dict(t_return=27.0, hw=0.0, pump=1.0, compressor=50.0)
        base.update(kw)
        return HouseSample(START + timedelta(minutes=minute), 21.0, -5.0, 30.0, 0.0, **base)

    def test_exclusions(self) -> None:
        samples = [
            self.sample(0),
            self.sample(10, hw=1.0, t_return=45.0),  # hot water: the tank circuit
            self.sample(20),  # right after a charge: still mixed
            self.sample(30),
            self.sample(40, pump=0.4, compressor=0.0),  # pump stopped: still water
            self.sample(50, t_return=None),  # not logged
            self.sample(60, t_return=31.0),  # return above supply
            self.sample(70, pump=1.0, compressor=0.0),  # coasting with the pump on: the slab
        ]
        self.assertEqual(slab_observable(samples), [True, False, False, True, False, False, False, True])

    def test_without_pump_data_only_a_running_compressor(self) -> None:
        # Coasting slab and still water look alike (BT2 ~ BT3): not trusted,
        # nor a sample next to an idle one.
        samples = [self.sample(10 * i, pump=None, compressor=hz) for i, hz in enumerate((50, 50, 50, 0, 0, 50, 50))]
        self.assertEqual(slab_observable(samples), [True, True, False, False, False, False, True])
        # Nothing known about the compressor either: taken.
        self.assertTrue(all(slab_observable([self.sample(0, pump=None, compressor=None)])))


class SlabFitTest(unittest.TestCase):
    """A floor house whose slab coasts twice as slow as it charges (prior: 3x)."""

    CASES = ((3.0, 2.0, 0.25, 1), (5.0, 2.0, 0.15, 3))

    @classmethod
    def setUpClass(cls) -> None:
        cls.runs = []
        for tau_f, ratio, kappa, seed in cls.CASES:
            true = floor_house(tau_f, ratio)
            samples, truth, offsets = history(true, kappa, seed)
            bt3 = fit_house_model(samples, FLOOR.model, FLOOR.bounds)
            bt2 = fit_house_model(without_bt3(samples), FLOOR.model, FLOOR.bounds)
            cls.runs.append((true, kappa, samples, truth, offsets, bt3, bt2))

    def test_bt3_recovers_the_slab_better(self) -> None:
        for true, kappa, _, _, _, bt3, bt2 in self.runs:
            self.assertTrue(bt3.ok and bt2.ok, (bt3.reason, bt2.reason))
            self.assertEqual(bt3.details["slab_source"], "bt3", bt3.details)
            self.assertEqual(bt2.details["slab_source"], "bt2")
            m3, m2 = bt3.model, bt2.model
            self.assertAlmostEqual(m3.tau_f, true.tau_f, delta=0.1 * true.tau_f)
            self.assertLessEqual(abs(m3.tau_f - true.tau_f), abs(m2.tau_f - true.tau_f))
            # The coasting time: fitted from BT3, the prior's 3x from BT2 alone.
            self.assertTrue(bt3.details["bt3"]["tau_d_fitted"])
            self.assertAlmostEqual(m3.drain_h, true.drain_h, delta=0.1 * true.drain_h)
            self.assertLessEqual(abs(m3.drain_h - true.drain_h), abs(m2.drain_h - true.drain_h))
            self.assertAlmostEqual(m3.kappa, kappa, delta=0.03)
            self.assertIsNone(m2.kappa)
            # The slab -> room coupling.
            self.assertAlmostEqual(m3.slab_coupling, true.slab_coupling, delta=0.08 * true.slab_coupling)
            self.assertLess(abs(m3.slab_coupling - true.slab_coupling), abs(m2.slab_coupling - true.slab_coupling))
            self.assertAlmostEqual(m3.b, true.b, delta=0.1 * true.b)
            # Multi-step simulation of the room: at least as good.
            self.assertLessEqual(bt3.sim_rmse, bt2.sim_rmse)
            self.assertEqual(bt3.details["bt2_sim_rmse"], round(bt2.sim_rmse, 4))

    def test_slab_now_is_closer_with_bt3(self) -> None:
        for _, _, samples, truth, _, bt3, bt2 in self.runs:
            m3, m2 = bt3.model, bt2.model
            err3, err2 = [], []
            # Every 6 h over the last week, from the day before (as the planner does).
            for end in range(len(samples) - 7 * 144, len(samples), 36):
                window = samples[end - 144:end + 1]
                e3 = slab_track(window, m3.tau_f, m3.drain_h, m3.kappa)[-1]
                e2 = slab_filter(without_bt3(window), m2.tau_f, m2.drain_h)[-1]
                err3.append(abs(slab_temp(e3, window[-1].t_in, m3.tau_f, m3.drain_h) - truth[end]))
                err2.append(abs(slab_temp(e2, window[-1].t_in, m2.tau_f, m2.drain_h) - truth[end]))
            self.assertLess(sum(err3) / len(err3), 0.15)
            self.assertLess(sum(err3) / len(err3), sum(err2) / len(err2))

    def test_short_history_after_a_restart(self) -> None:
        # Two hours of history: BT2 alone is still warming up, BT3 knows.
        _, _, samples, truth, _, bt3, _ = self.runs[0]
        m = bt3.model
        end = len(samples) - 30
        while samples[end].hw or samples[end - 1].hw:
            end -= 1
        window = samples[end - 12:end + 1]
        self.assertIsNone(slab_filter(window, m.tau_f, m.drain_h)[-1])
        est = slab_track(window, m.tau_f, m.drain_h, m.kappa)[-1]
        self.assertAlmostEqual(slab_temp(est, window[-1].t_in, m.tau_f, m.drain_h), truth[end], delta=0.3)

    def test_falls_back_without_bt3(self) -> None:
        _, _, samples, _, _, _, bt2 = self.runs[0]
        # One day of BT3 only (just upgraded): not enough, BT2 as before.
        recent = samples[-144:]
        mixed = without_bt3(samples[:-144]) + recent
        fit = fit_house_model(mixed, FLOOR.model, FLOOR.bounds)
        self.assertTrue(fit.ok, fit.reason)
        self.assertEqual(fit.details["slab_source"], "bt2")
        self.assertIn("too little BT3", fit.details["bt3"]["reason"])
        self.assertIsNone(fit.model.kappa)
        self.assertFalse(fit_slab(without_bt3(samples), FLOOR.model).ok)
        self.assertEqual(bt2.details["bt3"]["hours"], 0.0)


class StillWaterTest(unittest.TestCase):
    def test_pump_stopping_with_the_compressor(self) -> None:
        # Intermittent pump: the water stands still in the heat pump while
        # the compressor idles and BT2/BT3 drift towards the room it is in.
        true = floor_house(3.0, 2.0)
        samples, _, _ = history(true, 0.25, 5, pump_stops=True)
        self.assertTrue(any(s.pump == 0.0 for s in samples))
        bt2 = fit_house_model(without_bt3(samples), FLOOR.model, FLOOR.bounds)
        for name, data in (("pump logged", samples), ("no pump data", [replace(s, pump=None) for s in samples])):
            fit = fit_house_model(data, FLOOR.model, FLOOR.bounds)
            self.assertEqual(fit.details["slab_source"], "bt3", name)
            self.assertAlmostEqual(fit.model.tau_f, 3.0, delta=0.3, msg=name)
            self.assertLess(fit.details["bt3"]["slab_rmse"], 0.2, name)
            self.assertLessEqual(fit.sim_rmse, bt2.sim_rmse, name)


if __name__ == "__main__":
    unittest.main()
