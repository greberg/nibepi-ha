"""The emitter lag (floor heating) in the house model and its fit."""

from __future__ import annotations

import math
import random
import unittest
from dataclasses import replace

import context  # noqa: F401
from context import utc
from logic.optimizer.heatcurve import HeatCurve
from logic.optimizer.house import (
    DRAIN_RATIO,
    HouseModel,
    LagStep,
    fit_house_model,
    slab_filter,
    slab_temp,
    synthetic_history,
)
from logic.optimizer.profile import HouseProfile, house_prior

START = utc(2026, 1, 1)
CURVE = HeatCurve(curve=5)
FLOOR = house_prior(HouseProfile(floor_area_m2=150, floors=1, latitude=59.3, emitter="floor"), CURVE)


def t_out(when) -> float:
    h = (when - START).total_seconds() / 3600.0
    return -3 + 5 * math.sin(h / 24 * 2 * math.pi) + 4 * math.sin(h / 100)


def history(true: HouseModel, seed: int, days: int = 21):
    """Offsets changing every 3 h, Netatmo-like 0.1 °C readings with noise."""
    rnd = random.Random(seed)
    offsets = {}

    def supply(when, t_in):
        key = int(when.timestamp() // (3 * 3600))
        if key not in offsets:
            offsets[key] = rnd.randint(-3, 3)
        return CURVE.supply(t_out(when), offsets[key])

    return synthetic_history(true, START, days * 24, t_out, supply,
                             noise=lambda i: round(rnd.gauss(0, 0.05), 1))


def euler(model: HouseModel, t_in, t_f, t_out_, supply, hours, n=20000):
    """Fine explicit integration of the lagged model, for reference."""
    h = hours / n
    heat = 0.0
    td = model.drain_h
    for _ in range(n):
        eff = max(supply, t_f - model.tau_f / td * (t_f - t_in))
        df = (eff - t_f) / model.tau_f
        di = model.a * (t_out_ - t_in) + model.b * (t_f - t_in) + model.d
        heat += ((t_f - t_in) + td * df) * h
        t_f += df * h
        t_in += di * h
    return t_in, t_f, heat


class LagModelTest(unittest.TestCase):
    MODEL = HouseModel(0.012, 0.03, 0.0, 0.05, 3.0, 9.0)

    def test_priors_per_emitter(self) -> None:
        self.assertEqual(FLOOR.model.tau_f, 3.0)
        self.assertEqual(FLOOR.model.drain_h, 3.0 * DRAIN_RATIO)
        mixed = house_prior(HouseProfile(floor_area_m2=150, floors=1, latitude=59.3, emitter="mixed"), CURVE).model
        radiators = house_prior(HouseProfile(floor_area_m2=150, floors=1, latitude=59.3), CURVE).model
        self.assertEqual(mixed.tau_f, 1.5)
        self.assertEqual(radiators.tau_f, 0.3)
        # The lag does not change the steady state.
        self.assertEqual((FLOOR.model.a, FLOOR.model.b), (radiators.a, radiators.b))

    def test_exact_step_matches_integration(self) -> None:
        m = self.MODEL
        for t_in, t_f, supply in ((21.0, 28.0, 33.0), (21.0, 30.0, 29.0), (21.0, 32.0, 22.0)):
            free = m.a * -5.0 + m.d
            got = LagStep(m, 1.0).step(t_in, t_f, free, supply)
            ref = euler(m, t_in, t_f, -5.0, supply, 1.0)
            self.assertAlmostEqual(got[0], ref[0], delta=0.002)
            self.assertAlmostEqual(got[1], ref[1], delta=0.03)
            self.assertAlmostEqual(got[2], ref[2], delta=0.05)

    def test_steady_state_is_the_one_node_model(self) -> None:
        m = self.MODEL
        supply = m.steady_supply(21.0, -5.0)
        t_in, t_f, heat = m.lag_step(21.0, supply, -5.0, supply, 0.0, 2.0)
        self.assertAlmostEqual(t_in, 21.0, places=6)
        self.assertAlmostEqual(t_f, supply, places=6)
        self.assertAlmostEqual(heat, (supply - 21.0) * 2.0, places=6)  # K_e (T_s - T_in) h

    def test_slab_coasts_and_keeps_heating(self) -> None:
        m = self.MODEL
        # Offset dropped far: the pump idles, the slab gives off its heat.
        t_in, t_f, heat = m.lag_step(21.0, 32.0, -5.0, 22.0, 0.0, 1.0)
        self.assertEqual(heat, 0.0)
        self.assertLess(t_f, 32.0)
        self.assertGreater(t_f, 22.0 + 5.0)  # far slower than the 3 h charging lag
        self.assertGreater(t_in, 21.0)  # the room still warms
        # The one-node model with the same supply cools at once.
        self.assertLess(m.one_node().step(21.0, -5.0, 22.0, 0.0, 1.0), 21.0)

    def test_filter_follows_the_slab(self) -> None:
        true = HouseModel(0.012, 0.03, 0.0, 0.05, 3.0, 9.0)
        samples = history(true, 4, days=3)
        est = slab_filter(samples, 3.0, 9.0)
        self.assertTrue(all(e is None for e in est[:27]))  # 4.5 h warm-up at 10 min
        known = [(e, s) for e, s in zip(est, samples) if e is not None]
        self.assertGreater(len(known), len(samples) * 0.8)
        # The filtered slab stays within the logged supply range.
        lo = min(s.t_supply for s in samples)
        hi = max(s.t_supply for s in samples)
        self.assertTrue(all(lo - 0.5 <= e <= hi + 0.5 for e, _ in known))

    def test_filter_coasts_through_hot_water(self) -> None:
        # Two 40-minute hot water charges a day: BT2 reads the tank circuit
        # (50 °C) then, while the floor gets nothing and the slab coasts.
        true = HouseModel(0.012, 0.03, 0.0, 0.05, 3.0, 9.0)
        rnd = random.Random(7)
        truth = []
        samples = synthetic_history(
            true, START, 72, t_out, lambda when, t_in: CURVE.supply(t_out(when), rnd.choice((-2, 0, 2))),
            hot_water=lambda when: when.hour in (6, 18) and when.minute < 40, truth=truth)
        self.assertEqual(sum(1 for s in samples if s.hw), 6 * 4)
        aware = slab_filter(samples, 3.0, 9.0)
        blind = slab_filter([replace(s, hw=0.0) for s in samples], 3.0, 9.0)

        def error(est):
            pairs = [(slab_temp(e, s.t_in, 3.0, 9.0), x) for e, s, x in zip(est, samples, truth) if e is not None]
            return max(abs(e - x) for e, x in pairs)

        self.assertLess(error(aware), 0.3)
        self.assertGreater(error(blind), 1.0)


class LagFitTest(unittest.TestCase):
    def test_recovers_the_lag(self) -> None:
        prior = FLOOR.model
        for tau, seed in ((3.0, 1), (2.0, 2), (5.0, 3)):
            true = HouseModel(prior.a * 1.1, prior.b * 0.9, prior.c, prior.d, tau, DRAIN_RATIO * tau)
            fit = fit_house_model(history(true, seed), prior, FLOOR.bounds)
            self.assertTrue(fit.ok, fit.reason)
            self.assertGreaterEqual(fit.model.tau_f, tau / 1.5, fit.details)
            self.assertLessEqual(fit.model.tau_f, tau * 1.5, fit.details)
            self.assertAlmostEqual(fit.model.a, true.a, delta=0.15 * true.a)
            self.assertAlmostEqual(fit.model.b, true.b, delta=0.15 * true.b)
            self.assertLess(fit.sim_rmse, 0.15)
            self.assertEqual(fit.level, "high")
            self.assertIn("lag_grid", fit.details)

    def test_one_node_fit_on_lagged_house_is_worse(self) -> None:
        true = HouseModel(FLOOR.model.a, FLOOR.model.b, 0.0, FLOOR.model.d, 3.0, 9.0)
        samples = history(true, 5)
        lagged = fit_house_model(samples, FLOOR.model, FLOOR.bounds)
        one = fit_house_model(samples, FLOOR.model.one_node(), FLOOR.bounds)
        self.assertLess(lagged.sim_rmse, 0.7 * one.sim_rmse)

    def test_radiator_house_fits_a_short_lag(self) -> None:
        # Hourly bins can't resolve 0.3 h; it comes out at about an hour,
        # below the lagged planner's threshold (1.25 h).
        prior = house_prior(HouseProfile(floor_area_m2=150, floors=1, latitude=59.3, emitter="radiators"), CURVE)
        true = HouseModel(prior.model.a, prior.model.b, 0.0, prior.model.d, 0.3, 0.9)
        fit = fit_house_model(history(true, 6), prior.model, prior.bounds)
        self.assertTrue(fit.ok, fit.reason)
        self.assertLessEqual(fit.model.tau_f, 1.0)


if __name__ == "__main__":
    unittest.main()
