"""Solar gain proxy: sun elevation from Home Assistant's location, dimmed by clouds.

``solar_proxy`` is 0 at night and up to 1 with a clear sky and the sun
overhead: ``sin(elevation) * (1 - 0.75 * cloud^3.4)`` (Kasten-Czeplak
cloud attenuation). The house model's ``c`` scales it to °C/h, so only its
shape matters. Sun position uses the low-precision almanac formulas
(≈ 0.5° accuracy), plenty for this.

Cloud cover comes from the HA weather condition of the nearest forecast
point (``CONDITION_CLOUD``); unknown conditions count as 0.6.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

J2000 = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)

CONDITION_CLOUD = {
    "sunny": 0.0,
    "clear-night": 0.0,
    "windy": 0.3,
    "partlycloudy": 0.5,
    "windy-variant": 0.6,
    "exceptional": 0.8,
    "cloudy": 1.0,
    "fog": 1.0,
    "rainy": 0.9,
    "pouring": 1.0,
    "snowy": 0.9,
    "snowy-rainy": 0.95,
    "hail": 1.0,
    "lightning": 0.9,
    "lightning-rainy": 1.0,
}
DEFAULT_CLOUD = 0.6


def sun_elevation(when: datetime, latitude: float, longitude: float) -> float:
    """Sun elevation in degrees (``when`` timezone aware)."""
    d = (when - J2000).total_seconds() / 86400.0
    g = math.radians((357.529 + 0.98560028 * d) % 360.0)
    q = (280.459 + 0.98564736 * d) % 360.0
    lam = math.radians(q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g))
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.degrees(math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam)))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24.0
    hour_angle = math.radians((gmst * 15.0 + longitude - ra) % 360.0)
    lat = math.radians(latitude)
    sin_el = math.sin(lat) * math.sin(dec) + math.cos(lat) * math.cos(dec) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_el))))


def cloud_from_condition(condition: Optional[str]) -> float:
    if not condition:
        return DEFAULT_CLOUD
    return CONDITION_CLOUD.get(condition.lower(), DEFAULT_CLOUD)


def solar_proxy(
    when: datetime, latitude: float, longitude: float, cloud: float = DEFAULT_CLOUD
) -> float:
    elevation = sun_elevation(when, latitude, longitude)
    if elevation <= 0:
        return 0.0
    cloud = max(0.0, min(1.0, cloud))
    return math.sin(math.radians(elevation)) * (1.0 - 0.75 * cloud ** 3.4)
