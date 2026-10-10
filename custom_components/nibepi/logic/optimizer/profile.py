"""House profile -> priors for the house and energy models.

What the user enters (or Home Assistant already knows): location, heated
floor area, number of floors, optionally building era and construction,
and the target indoor temperature. From that we derive starting values
for the model, plausible ranges for sanity-checking a fit, and how much
to distrust a single indoor sensor. The optimiser behaves sensibly from
day one on these priors; the learned fit takes over as data comes in.

Formulas and assumptions (rough Swedish single-family house figures,
deliberately middle-of-the-road; see docs/optimizer.md):

- Specific heat loss ``H/A`` (W/K per m² heated floor, transmission +
  ventilation + infiltration) by era, consistent with typical Swedish
  space-heating demand of ~130 (pre-1960) down to ~50 kWh/m²·year
  (post-2010, BBR) at ~90 000 degree-hours:

      pre-1960 1.3, 1961-1975 1.05, 1976-1990 0.8, 1991-2010 0.65, 2011 and later 0.45

  Unknown era: 0.85 with a wider plausible range.
- Floors change the envelope per m² of floor: 1 floor ×1.10 (more roof and
  ground per m²), 1.5 ×1.0, 2 ×0.92, 3+ ×0.88.
- Thermal capacity ``C`` (Wh/K per m²): light timber frame 60, medium
  (timber with masonry/slab parts) 90, heavy (concrete/brick) 130. Unknown:
  75 (Swedish houses are mostly timber, often with a concrete slab).
- Internal gains 4 W/m² (people, appliances, lighting; BBR-style figure).
- Useful solar gain at solar proxy 1 (clear sky, sun high): 6 W/m².
- Design outdoor temperature (DUT) from latitude and elevation, roughly
  the Swedish DVUT table: -16 °C at 56° N (Malmö), -19 at 59.3° (Stockholm),
  -23 at 64° (Umeå), -27 at 68° (Kiruna); -0.6 °C per 100 m elevation.
- Emitter conductance ``K_e`` such that the pump's curve at DUT just
  covers the design load: ``K_e = (H (T_target - DUT) - gains) /
  (T_supply(DUT) - T_target)``. The curve the user has set encodes how
  much emitter the house has.
- Floors and indoor sensors: one sensor on one floor of a multi-floor
  house represents the house poorly (stratification, sun on one side).
  ``sensor_margin`` (°C) is 0 for 1 floor, 0.1 for 1.5, 0.2 for 2 and 0.3
  for 3+, halved with two or more sensors averaged. The planner raises the
  comfort floor by it and the fit needs a better match to reach the same
  confidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .energy import EnergyModel
from .heatcurve import HeatCurve
from .house import FitBounds, HouseModel

ERA_LOSS_W_PER_K_M2 = {
    "pre1960": 1.3,
    "1961-1975": 1.05,
    "1976-1990": 0.8,
    "1991-2010": 0.65,
    "2011_later": 0.45,
    "2011-": 0.45,  # spelling before 0.2.0 (not a valid selector key)
}
DEFAULT_LOSS_W_PER_K_M2 = 0.85
CONSTRUCTION_WH_PER_K_M2 = {"light": 60.0, "medium": 90.0, "heavy": 130.0}
DEFAULT_CAPACITY_WH_PER_K_M2 = 75.0
INTERNAL_GAINS_W_PER_M2 = 4.0
SOLAR_GAIN_W_PER_M2 = 6.0
FLOOR_FACTOR = {1.0: 1.10, 1.5: 1.0, 2.0: 0.92, 3.0: 0.88}
SENSOR_MARGIN = {1.0: 0.0, 1.5: 0.1, 2.0: 0.2, 3.0: 0.3}


@dataclass(frozen=True)
class HouseProfile:
    floor_area_m2: float = 150.0
    floors: float = 1.5  # 1, 1.5, 2 or 3 (3 = three or more)
    target_temp: float = 21.0
    era: Optional[str] = None  # a key of ERA_LOSS_W_PER_K_M2
    construction: Optional[str] = None  # light / medium / heavy
    indoor_sensors: int = 1
    # From Home Assistant's core config unless overridden.
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    elevation_m: float = 0.0
    time_zone: Optional[str] = None


def _floors_key(floors: float) -> float:
    if floors >= 3:
        return 3.0
    if floors >= 2:
        return 2.0
    if floors > 1:
        return 1.5
    return 1.0


def design_outdoor_temp(latitude: Optional[float], elevation_m: float = 0.0) -> float:
    lat = 59.3 if latitude is None else latitude
    dut = -16.0 - 0.9 * (lat - 56.0) if lat > 56.0 else -16.0
    return max(-32.0, dut - 0.006 * max(0.0, elevation_m))


@dataclass(frozen=True)
class HousePrior:
    ua_kw_per_k: float
    capacity_kwh_per_k: float
    gains_kw: float
    solar_kw: float
    emitter_kw_per_k: float
    design_outdoor: float
    sensor_margin: float
    uncertainty: float  # relative 1-sigma of ua and capacity
    model: HouseModel
    bounds: FitBounds

    def energy_model(self, base: EnergyModel = EnergyModel()) -> EnergyModel:
        return EnergyModel(
            self.emitter_kw_per_k,
            base.carnot_eta,
            base.condenser_approach,
            base.evaporator_approach,
            base.brine_temp,
            base.cop_min,
            base.cop_max,
        )


def house_prior(profile: HouseProfile, curve: HeatCurve = HeatCurve()) -> HousePrior:
    area = max(30.0, float(profile.floor_area_m2))
    key = _floors_key(profile.floors)
    loss = ERA_LOSS_W_PER_K_M2.get(profile.era or "", DEFAULT_LOSS_W_PER_K_M2)
    uncertainty = 0.3 if profile.era in ERA_LOSS_W_PER_K_M2 else 0.45
    ua = loss * FLOOR_FACTOR[key] * area / 1000.0
    cap = CONSTRUCTION_WH_PER_K_M2.get(profile.construction or "", DEFAULT_CAPACITY_WH_PER_K_M2)
    if profile.construction not in CONSTRUCTION_WH_PER_K_M2:
        uncertainty = max(uncertainty, 0.4)
    capacity = cap * area / 1000.0
    gains = INTERNAL_GAINS_W_PER_M2 * area / 1000.0
    solar = SOLAR_GAIN_W_PER_M2 * area / 1000.0
    dut = design_outdoor_temp(profile.latitude, profile.elevation_m)
    target = profile.target_temp
    supply_dut = curve.supply(dut, 0.0)
    lift = max(5.0, supply_dut - target)
    emitter = max(0.05, (ua * (target - dut) - gains) / lift)
    margin = SENSOR_MARGIN[key] * (0.5 if profile.indoor_sensors >= 2 else 1.0)

    model = HouseModel(ua / capacity, emitter / capacity, solar / capacity, gains / capacity)
    spread = 1.0 + 3.0 * uncertainty  # ~ ±3 sigma, multiplicative

    def rng(value: float) -> Tuple[float, float]:
        return (value / (spread * spread), value * spread * spread)

    tau = 1.0 / (model.a + model.b)
    bounds = FitBounds(
        a=rng(model.a),
        b=rng(model.b),
        c=(0.0, max(0.05, model.c * 6.0)),
        d=(-0.5 * model.d, model.d * 4.0 + 0.02),
        time_constant_h=(tau / (spread * spread * 2.0), tau * spread * spread * 2.0),
    )
    return HousePrior(ua, capacity, gains, solar, emitter, dut, margin, uncertainty, model, bounds)
