"""Forecast anticipation: raise or lower the curve ahead of a temperature change.

Ports ``runWeather`` and ``checkWind`` from config_node.js:

    offset = toFixed((outdoor - T - sun) * (curve * 1.2 / 10) / (curve / 10 + 1), 2)

where ``T`` is the forecast temperature ``hours`` ahead (``hours_sN``,
default 6 as in weather.html), or with wind control on the wind-chill
"feel" temperature at that time; ``curve`` is the pump's heat curve
(47007 S1 / 47006 S2) and ``sun`` the configured sun factor when the target
time is in daylight with a clear sky.

- ``forecast_adjust``: the forecast is shifted by (measured outdoor -
  forecast now) before use.
- ``enable_up`` / ``enable_down`` (both default on) block raising or
  lowering.
- Heat curve 0 (own curve) or unknown, outdoor sensor missing/stale, or no
  usable forecast: offset 0. A forecast older than 3 hours is stale
  (``WEATHER_STALE_MS``).
- ``feel_temperature`` is ``checkWind``'s wind chill in m/s with the
  per-direction factors (default 0 when unset, as in config_node).

Deliberate differences:

- The forecast comes from a Home Assistant weather entity
  (``weather.get_forecasts``, hourly) instead of SMHI; the runtime converts
  wind speed to m/s. Points are picked by time (closest to now and to
  now + hours, within 90 minutes) rather than by array index, because HA
  providers can skip hours. A forecast shorter than ``hours`` uses its last
  point, as config_node capped ``hours`` to the series length.
- HA conditions replace SMHI symbols: ``sunny`` (SMHI 1-2) uses the
  ``clear`` factor and ``partlycloudy`` (SMHI 3-4) the ``half_clear``
  factor; ``mostly_clear`` has no HA equivalent. Daylight at the target
  time is passed in by the runtime (HA's sun helper instead of SunCalc).
- Wind gust is not required for a point to count (only graphed upstream).
- Wind bearings are rounded to whole degrees so fractional HA bearings fall
  into a direction sector instead of between them.
- The offset is recomputed every tick from the cached forecast and the
  current outdoor temperature; config_node recomputed it once an hour.
  The 0.75 write threshold in ``curve`` absorbs the extra movement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional, Sequence

from .inputs import Decision, ForecastPoint, Reading, to_fixed, to_float, usable

DEFAULT_HOURS = 6
FORECAST_STALE = timedelta(hours=3)
POINT_TOLERANCE = timedelta(minutes=90)

SUN_CONDITIONS = {"sunny": "clear", "partlycloudy": "half_clear"}


@dataclass
class WeatherSettings:
    hours: float = DEFAULT_HOURS
    enable_up: bool = True
    enable_down: bool = True
    forecast_adjust: bool = False
    wind_enable: bool = False
    wind_factors: Dict[str, float] = field(
        default_factory=lambda: {"n": 0.0, "s": 0.0, "w": 0.0, "e": 0.0}
    )
    sun_enable: bool = False
    sun_factors: Dict[str, float] = field(
        default_factory=lambda: {"clear": 0.0, "half_clear": 0.0}
    )


def wind_sector(bearing: float) -> Optional[str]:
    """``checkWind``'s direction sectors (degrees the wind comes from)."""
    deg = int(math.floor(bearing + 0.5))
    if (1 <= deg <= 45) or (315 <= deg <= 360):
        return "n"
    if 136 <= deg <= 225:
        return "s"
    if 226 <= deg <= 314:
        return "w"
    if 46 <= deg <= 135:
        return "e"
    return None


def feel_temperature(
    temperature: float, wind_speed: float, bearing: float, factors: Dict[str, float]
) -> float:
    """``checkWind``: wind chill (m/s formula) scaled by the sector factor."""
    sector = wind_sector(bearing)
    factor = to_float(factors.get(sector, 0.0)) if sector else 1.0
    if factor is None:
        factor = 0.0
    v = math.pow(wind_speed, 0.16)
    feel = to_fixed(13.12 + 0.6215 * temperature - 13.956 * v + 0.48669 * temperature * v, 2)
    if feel > 0:
        # JavaScript: x / 0 is Infinity, which the next check turns into temp.
        feel = math.inf if factor == 0 else to_fixed(feel / factor, 2)
    else:
        feel = to_fixed(feel * factor, 2)
    if feel > temperature:
        feel = temperature
    if wind_speed < 2 or temperature > 10 or wind_speed > 35 or sector is None:
        feel = temperature
    return feel


def _closest(points: Sequence[ForecastPoint], when: datetime) -> Optional[ForecastPoint]:
    best = None
    best_gap = None
    for point in points:
        gap = abs((point.time - when).total_seconds())
        if best_gap is None or gap < best_gap:
            best, best_gap = point, gap
    if best is None or best_gap is None or best_gap > POINT_TOLERANCE.total_seconds():
        return None
    return best


def compute_weather(
    outdoor: Optional[Reading],
    heat_curve: Optional[float],
    forecast: Optional[Sequence[ForecastPoint]],
    fetched_at: Optional[datetime],
    now: datetime,
    settings: Optional[WeatherSettings] = None,
    daylight: Optional[bool] = None,
    sensor_timeout_min: Optional[float] = None,
) -> Decision:
    settings = settings or WeatherSettings()
    problem = usable(outdoor, now, sensor_timeout_min)
    if problem is not None:
        return Decision.zero("outdoor sensor %s" % problem)
    assert outdoor is not None
    outside = float(outdoor.value)
    curve = to_float(heat_curve)
    if curve is None:
        return Decision.zero("heat curve unknown")
    if curve == 0:
        return Decision.zero("own heat curve (0) is not supported", inputs_ok=True)
    if not forecast or fetched_at is None:
        return Decision.zero("no forecast")
    if now - fetched_at > FORECAST_STALE:
        return Decision.zero("forecast stale (fetched %s)" % fetched_at.isoformat())

    hours = to_float(settings.hours)
    if hours is None or hours < 0:
        hours = 0.0
    points = sorted((p for p in forecast if to_float(p.temperature) is not None), key=lambda p: p.time)
    point_now = _closest(points, now)
    target = now + timedelta(hours=hours)
    point_later = _closest(points, target)
    if point_later is None and points and points[-1].time > now and points[-1].time < target:
        point_later = points[-1]
    if point_now is None or point_later is None:
        return Decision.zero("forecast does not cover now and %g h ahead" % hours)
    temp_now = float(point_now.temperature)  # type: ignore[arg-type]
    temp_later = float(point_later.temperature)  # type: ignore[arg-type]

    sun = 0.0
    if settings.sun_enable and daylight is True:
        key = SUN_CONDITIONS.get((point_later.condition or "").lower())
        if key is not None:
            sun = to_float(settings.sun_factors.get(key, 0.0)) or 0.0

    wind_set = None
    if settings.wind_enable:
        speed = to_float(point_later.wind_speed)
        bearing = to_float(point_later.wind_bearing)
        if speed is not None and bearing is not None:
            wind_set = feel_temperature(temp_later, speed, bearing, settings.wind_factors)

    if settings.forecast_adjust:
        temp_later = to_fixed((outside - temp_now) + temp_later, 2)
        if wind_set is not None:
            wind_set = to_fixed((outside - temp_now) + wind_set, 2)

    details = {
        "outdoor": outside,
        "forecast_now": temp_now,
        "forecast_later": temp_later,
        "forecast_time": point_later.time.isoformat(),
        "heat_curve": curve,
        "sun_factor": sun,
    }
    if settings.wind_enable:
        if wind_set is None:
            return Decision.zero("wind control on but forecast lacks wind", **details)
        details["feel"] = wind_set
        basis = wind_set
    else:
        basis = temp_later

    offset = to_fixed((outside - basis - sun) * (curve * 1.2 / 10) / ((curve / 10) + 1), 2)
    if offset > 0 and not settings.enable_up:
        return Decision.zero("raising blocked (%+.2f)" % offset, inputs_ok=True, **details)
    if offset <= 0 and not settings.enable_down:
        return Decision.zero("lowering blocked (%+.2f)" % offset, inputs_ok=True, **details)
    return Decision(
        offset,
        "outdoor %.1f, %s %.1f in %g h" % (outside, "feel" if settings.wind_enable else "forecast", basis, hours),
        True,
        details,
    )
