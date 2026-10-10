"""Heat delivered and electricity used for a given supply temperature.

Heat to the house is proportional to how far the supply is above the
room::

    P_heat = K_e * max(0, T_s - T_in)        kW, K_e in kW/K

which is the house model's ``b`` term times the thermal capacity
(``C = K_e / b``). The compressor's coefficient of performance is
approximated as a fraction of the Carnot COP between the condenser
(a little above the supply) and the evaporator (a little below the brine
coming in from the borehole, BT10 40015)::

    COP = eta * T_cond / (T_cond - T_evap),   temperatures in kelvin
    T_cond = T_s + condenser_approach,  T_evap = T_brine - evaporator_approach

``eta`` ≈ 0.6 gives the F1255's published ~4.5 at B0/W35 and ~3.5 at
B0/W45. It is clamped to [cop_min, cop_max]. Without BT10 a constant
brine temperature (2 °C, typical in winter for a borehole) is used.

Calibration (``fit_emitter``, ``fit_carnot_eta``) uses the pump's own
heat meters (44300/44308 heating, 44298/44306 hot water, kWh) and an
electricity figure: the compressor input power the pump reports (43141,
W) or the user's meter after separating the heat pump's share
(``meter.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

KELVIN = 273.15


@dataclass(frozen=True)
class EnergyModel:
    emitter_kw_per_k: float = 0.3
    carnot_eta: float = 0.6
    condenser_approach: float = 3.0
    evaporator_approach: float = 6.0
    brine_temp: float = 2.0
    cop_min: float = 1.5
    cop_max: float = 7.0

    def cop(self, t_supply: float, t_brine: Optional[float] = None) -> float:
        brine = self.brine_temp if t_brine is None else t_brine
        cond = t_supply + self.condenser_approach + KELVIN
        evap = brine - self.evaporator_approach + KELVIN
        lift = cond - evap
        if lift <= 1.0:
            return self.cop_max
        return max(self.cop_min, min(self.cop_max, self.carnot_eta * cond / lift))

    def heat_kw(self, t_supply: float, t_in: float) -> float:
        return self.emitter_kw_per_k * max(0.0, t_supply - t_in)

    def electric_kw(self, t_supply: float, t_in: float, t_brine: Optional[float] = None) -> float:
        heat = self.heat_kw(t_supply, t_in)
        return heat / self.cop(t_supply, t_brine) if heat > 0 else 0.0


@dataclass(frozen=True)
class EnergyFit:
    value: float
    samples: int
    ok: bool
    reason: str


def fit_emitter(
    rows: Sequence[Tuple[float, float, float, float]], prior: float, prior_weight: float = 24.0
) -> EnergyFit:
    """K_e from (heat_kwh, hours, mean supply, mean indoor) rows.

    Least squares through the origin of heat power on (T_s - T_in), with
    ``prior_weight`` pseudo-rows at ΔT 15 K worth of the prior. Rows where
    the supply is not above the room (pump idle) are skipped.
    """
    sxx = prior_weight * 15.0 * 15.0
    sxy = sxx * prior
    used = 0
    for heat_kwh, hours, supply, t_in in rows:
        dt = supply - t_in
        if hours <= 0 or dt <= 1.0 or heat_kwh < 0:
            continue
        q = heat_kwh / hours
        sxx += dt * dt
        sxy += dt * q
        used += 1
    value = sxy / sxx
    if used < 12:
        return EnergyFit(prior, used, False, "too few heating hours (%d)" % used)
    if not 0.02 <= value <= 3.0:
        return EnergyFit(prior, used, False, "implausible K_e %.3f kW/K" % value)
    return EnergyFit(value, used, True, "ok")


def fit_carnot_eta(
    rows: Sequence[Tuple[float, float, float, Optional[float]]],
    model: EnergyModel = EnergyModel(),
) -> EnergyFit:
    """Carnot fraction from (heat_kwh, electric_kwh, supply, brine) rows.

    ``eta = Σ heat / Σ (electric × Carnot COP)``, which weights hours by
    energy. Rows with little electricity (< 0.1 kWh) are skipped.
    """
    num = 0.0
    den = 0.0
    used = 0
    for heat, electric, supply, brine in rows:
        if electric < 0.1 or heat <= 0:
            continue
        unit = EnergyModel(1.0, 1.0, model.condenser_approach, model.evaporator_approach,
                           model.brine_temp, 0.0, 1e9)
        carnot = unit.cop(supply, brine)
        num += heat
        den += electric * carnot
        used += 1
    if used < 12 or den <= 0:
        return EnergyFit(model.carnot_eta, used, False, "too few rows (%d)" % used)
    eta = num / den
    if not 0.3 <= eta <= 0.85:
        return EnergyFit(model.carnot_eta, used, False, "implausible eta %.2f" % eta)
    return EnergyFit(eta, used, True, "ok")
