from __future__ import annotations

import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.inputs import ForecastPoint, Reading
from logic.weather import WeatherSettings, compute_weather, feel_temperature, wind_sector


class WeatherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = utc(2026, 1, 10, 12, 0)
        self.outdoor = Reading(0.0, self.now - timedelta(minutes=2))

    def forecast(self, temps, start_offset_min: int = 0, **extra) -> list:
        start = self.now + timedelta(minutes=start_offset_min)
        return [ForecastPoint(start + timedelta(hours=i), t, **extra) for i, t in enumerate(temps)]

    def run_weather(self, forecast, settings=None, curve=9, **kw):
        return compute_weather(self.outdoor, curve, forecast, self.now - timedelta(minutes=5), self.now, settings, **kw)

    def test_colder_ahead_raises(self) -> None:
        # outdoor 0, -5 in 6 h, curve 9: 5 * 1.08 / 1.9 = 2.842... -> 2.84
        d = self.run_weather(self.forecast([0, -1, -2, -3, -4, -4.5, -5, -6]))
        self.assertTrue(d.inputs_ok)
        self.assertEqual(d.offset, 2.84)

    def test_warmer_ahead_lowers(self) -> None:
        d = self.run_weather(self.forecast([0, 1, 2, 3, 4, 5, 6]), curve=5)
        # (0 - 6) * 0.6 / 1.5 = -2.4
        self.assertEqual(d.offset, -2.4)

    def test_up_down_blocks(self) -> None:
        d = self.run_weather(self.forecast([0] * 6 + [-5]), WeatherSettings(enable_up=False))
        self.assertEqual(d.offset, 0)
        self.assertTrue(d.inputs_ok)
        d = self.run_weather(self.forecast([0] * 6 + [5]), WeatherSettings(enable_down=False))
        self.assertEqual(d.offset, 0)

    def test_forecast_adjust(self) -> None:
        # Forecast says 3 now and -2 later; measured outdoor is 0, so the
        # adjusted later value is -5.
        d = self.run_weather(self.forecast([3, 2, 1, 0, -1, -1.5, -2]), WeatherSettings(forecast_adjust=True))
        self.assertEqual(d.details["forecast_later"], -5)
        self.assertEqual(d.offset, 2.84)

    def test_stale_and_missing(self) -> None:
        fc = self.forecast([0] * 8)
        d = compute_weather(self.outdoor, 9, fc, self.now - timedelta(hours=3, minutes=1), self.now)
        self.assertEqual(d.offset, 0)
        self.assertIn("stale", d.reason)
        d = compute_weather(self.outdoor, 9, None, None, self.now)
        self.assertIn("no forecast", d.reason)
        d = compute_weather(None, 9, fc, self.now, self.now)
        self.assertIn("outdoor", d.reason)
        old = Reading(0.0, self.now - timedelta(hours=2))
        d = compute_weather(old, 9, fc, self.now, self.now)
        self.assertIn("stale", d.reason)
        d = compute_weather(self.outdoor, None, fc, self.now, self.now)
        self.assertIn("heat curve", d.reason)
        d = compute_weather(self.outdoor, 0, fc, self.now, self.now)
        self.assertEqual(d.offset, 0)

    def test_forecast_must_cover_now(self) -> None:
        d = self.run_weather(self.forecast([0] * 8, start_offset_min=180))
        self.assertEqual(d.offset, 0)
        self.assertFalse(d.inputs_ok)

    def test_short_forecast_uses_last_point(self) -> None:
        d = self.run_weather(self.forecast([0, -1, -2, -5]))  # only 3 h ahead
        self.assertEqual(d.details["forecast_later"], -5)
        self.assertGreater(d.offset, 0)

    def test_sun_factor_only_in_daylight(self) -> None:
        fc = self.forecast([0] * 6 + [0], condition="sunny")
        settings = WeatherSettings(sun_enable=True, sun_factors={"clear": 2.0, "half_clear": 1.0})
        d = self.run_weather(fc, settings, daylight=True)
        # (0 - 0 - 2) * 1.08 / 1.9 = -1.1368 -> -1.14
        self.assertEqual(d.offset, -1.14)
        d = self.run_weather(fc, settings, daylight=False)
        self.assertEqual(d.offset, 0)

    def test_wind(self) -> None:
        settings = WeatherSettings(wind_enable=True, wind_factors={"n": 1.0, "s": 1.0, "w": 1.0, "e": 1.0})
        fc = self.forecast([-5] * 7, wind_speed=10.0, wind_bearing=270)
        d = self.run_weather(fc, settings)
        self.assertLess(d.details["feel"], -5)
        self.assertGreater(d.offset, self.run_weather(self.forecast([-5] * 7)).offset)
        # Wind enabled but no wind data: 0.
        d = self.run_weather(self.forecast([-5] * 7), settings)
        self.assertEqual(d.offset, 0)
        self.assertIn("wind", d.reason)

    def test_feel_temperature(self) -> None:
        f = {"n": 1.0, "s": 1.0, "w": 1.0, "e": 1.0}
        # m/s wind chill: -5 °C at 10 m/s.
        self.assertAlmostEqual(feel_temperature(-5, 10, 0.4, f), -5)  # bearing 0: no sector
        self.assertAlmostEqual(feel_temperature(-5, 10, 90, f), -13.68, places=2)
        self.assertEqual(feel_temperature(-5, 1.5, 90, f), -5)  # calm
        self.assertEqual(feel_temperature(12, 10, 90, f), 12)  # warm
        # Factor 0 (config_node's default): positive feel -> Infinity -> temp.
        self.assertEqual(feel_temperature(-1, 3, 90, {"e": 0}), -1)
        self.assertEqual(feel_temperature(5, 10, 90, {"e": 0}), 0)
        self.assertEqual(feel_temperature(-5, 10, 90, {"e": 0.5}), -6.84)

    def test_sectors(self) -> None:
        self.assertEqual(wind_sector(10), "n")
        self.assertEqual(wind_sector(350), "n")
        self.assertEqual(wind_sector(180), "s")
        self.assertEqual(wind_sector(270), "w")
        self.assertEqual(wind_sector(90), "e")
        self.assertEqual(wind_sector(45.4), "n")
        self.assertIsNone(wind_sector(0))


if __name__ == "__main__":
    unittest.main()
