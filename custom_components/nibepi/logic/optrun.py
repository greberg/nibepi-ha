"""Run the cost optimiser on the sample log: fit the models, make plans.

Everything here is pure and CPU-bound; the integration calls it in an
executor job with plain snapshots (dicts, dataclasses), and gets back
plain results: dicts that can be persisted, and dataclasses.

Fitting (nightly and after start) per climate system:

- ``fit_house_model`` on the 10-minute samples (indoor, BT1, BT2, solar
  proxy) against the house profile's prior;
- ``fit_heat_curve`` on hourly (BT1, offset, BT2) while the pump heats;
- ``fit_emitter`` on the heating heat meter (44300) increments, only with
  one climate system (the meter is shared);

and once: ``fit_carnot_eta`` (heat meter vs. compressor power and
addition) and ``fit_hp_share`` (the user's meter vs. the heat pump's
activity), the latter for calibration and diagnostics only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .opthistory import column, house_samples, hourly
from .optimizer.energy import EnergyModel, fit_carnot_eta, fit_emitter
from .optimizer.heatcurve import DEFAULT_CURVE, HeatCurve, fit_heat_curve
from .optimizer.house import HouseFit, HouseModel, fit_house_model
from .optimizer.meter import fit_hp_share
from .optimizer.profile import HousePrior, HouseProfile, house_prior

HEATING_SHARE = 0.5  # an hour counts as heating when prio was 30 this much
HW_SHARE = 0.1  # ... and as free of hot water charging below this


@dataclass(frozen=True)
class SystemSetup:
    """What the optimiser needs to know about one climate system."""

    system: str
    profile: HouseProfile
    curve: float = DEFAULT_CURVE  # 47007 / 47006


def curve_number(value: Optional[float]) -> float:
    """The pump's heat curve, 1..15; own curve (0) or unknown: the default."""
    if value is None or not 1 <= value <= 15:
        return DEFAULT_CURVE
    return float(value)


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------


def house_fit_dict(fit: HouseFit) -> Dict[str, Any]:
    m = fit.model
    return {
        "ok": fit.ok,
        "reason": fit.reason,
        "level": fit.level if fit.ok else "low",
        "confidence": round(fit.confidence, 3),
        "a": m.a,
        "b": m.b,
        "c": m.c,
        "d": m.d,
        "hours": fit.samples,
        "r2": round(fit.r2, 3),
        "rmse_rate": round(fit.rmse_rate, 4),
        "sim_rmse": round(fit.sim_rmse, 3),
        "rel_se_b": round(fit.rel_se_b, 3) if fit.rel_se_b != float("inf") else None,
        "rejected": {k: v for k, v in fit.details.items() if v is not None} if not fit.ok else None,
    }


def house_fit_from(data: Any, prior: HouseModel) -> HouseFit:
    """A stored fit, or the prior (not ok) when there is none or it is broken."""
    if not isinstance(data, Mapping):
        return HouseFit(prior, False, "not fitted yet")
    try:
        ok = bool(data.get("ok"))
        model = HouseModel(float(data["a"]), float(data["b"]), float(data["c"]), float(data["d"])) if ok else prior
        rel = data.get("rel_se_b")
        return HouseFit(
            model,
            ok,
            str(data.get("reason", "")),
            int(data.get("hours") or 0),
            float(data.get("r2") or 0.0),
            float(data.get("rmse_rate") or 0.0),
            float(data.get("sim_rmse") or 0.0),
            float(rel) if rel is not None else float("inf"),
            float(data.get("confidence") or 0.0),
            str(data.get("level") or "low"),
        )
    except (KeyError, TypeError, ValueError):
        return HouseFit(prior, False, "stored fit unreadable")


def _energy_fit_dict(fit: Any) -> Dict[str, Any]:
    return {"value": fit.value, "ok": fit.ok, "reason": fit.reason, "hours": fit.samples}


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------


def _hour_map(rows: Mapping[int, Mapping[str, float]], name: str) -> Dict[int, float]:
    return {hour: values[name] for hour, values in hourly(rows, [name])}


def fit_system(
    rows: Mapping[int, Mapping[str, float]], setup: SystemSetup, single_system: bool
) -> Dict[str, Any]:
    system = setup.system
    profile = setup.profile
    base_curve = HeatCurve(curve=curve_number(setup.curve))
    prior = house_prior(profile, base_curve)
    samples = house_samples(rows, system, profile.latitude, profile.longitude)
    fit = fit_house_model(samples, prior.model, prior.bounds, sensor_margin=prior.sensor_margin)

    t_in, supply, offset = column("t_in", system), column("supply", system), column("offset", system)
    heating = _hour_map(rows, "prio_heat")
    hw = _hour_map(rows, "prio_hw")

    curve_rows: List[Tuple[float, float, float]] = []
    for hour, v in hourly(rows, ["t_out", offset, supply]):
        if heating.get(hour, 1.0) < HEATING_SHARE:
            continue
        curve_rows.append((v["t_out"], v[offset], v[supply]))
    curve_fit = fit_heat_curve(curve_rows, base_curve)

    if single_system:
        emitter_rows = [
            (v["heat_kwh"], 1.0, v[supply], v[t_in])
            for hour, v in hourly(rows, ["heat_kwh", supply, t_in])
            if hw.get(hour, 0.0) < HW_SHARE
        ]
        emitter = _energy_fit_dict(fit_emitter(emitter_rows, prior.emitter_kw_per_k))
    else:
        emitter = {
            "value": prior.emitter_kw_per_k,
            "ok": False,
            "reason": "the heat meter is shared by the climate systems",
            "hours": 0,
        }
    c = curve_fit.curve
    return {
        "house": house_fit_dict(fit),
        "curve": {
            "curve": c.curve,
            "bias": round(c.bias, 3),
            "slope_per_curve": round(c.slope_per_curve, 5),
            "offset_gain": round(c.offset_gain, 4) if c.offset_gain is not None else None,
            "ok": curve_fit.ok,
            "reason": curve_fit.reason,
            "hours": curve_fit.samples,
            "rmse": round(curve_fit.rmse, 3),
        },
        "emitter": emitter,
        "prior": {
            "ua_kw_per_k": round(prior.ua_kw_per_k, 4),
            "capacity_kwh_per_k": round(prior.capacity_kwh_per_k, 3),
            "emitter_kw_per_k": round(prior.emitter_kw_per_k, 4),
            "design_outdoor": round(prior.design_outdoor, 1),
            "sensor_margin": prior.sensor_margin,
            "a": prior.model.a,
            "b": prior.model.b,
            "c": prior.model.c,
            "d": prior.model.d,
        },
    }


def fit_shared(rows: Mapping[int, Mapping[str, float]], supply_system: str) -> Dict[str, Any]:
    supply = column("supply", supply_system)
    hw = _hour_map(rows, "prio_hw")
    add = _hour_map(rows, "add_kw")
    brine = _hour_map(rows, "bt10")
    eta_rows = []
    for hour, v in hourly(rows, ["heat_kwh", "power_w", supply]):
        if hw.get(hour, 0.0) >= HW_SHARE:
            continue
        electric = v["power_w"] / 1000.0 + max(0.0, add.get(hour, 0.0))
        eta_rows.append((v["heat_kwh"], electric, v[supply], brine.get(hour)))
    eta = _energy_fit_dict(fit_carnot_eta(eta_rows))

    share_rows = [
        (v["meter_kwh"], v["power_w"] / 1000.0 + max(0.0, add.get(hour, 0.0)), 1.0)
        for hour, v in hourly(rows, ["meter_kwh", "power_w"])
    ]
    signal = "compressor power + addition (kW)"
    expect: Optional[Tuple[float, float]] = (0.8, 1.4)
    if len(share_rows) < 48:
        share_rows = [(v["meter_kwh"], v["freq_hz"], 1.0) for _, v in hourly(rows, ["meter_kwh", "freq_hz"])]
        signal = "compressor frequency (Hz)"
        expect = None
    share = fit_hp_share(share_rows, expect)
    return {
        "eta": eta,
        "hp_share": {
            "base_kw": round(share.base_kw, 3),
            "beta": round(share.beta, 4),
            "r2": round(share.r2, 3),
            "hours": share.samples,
            "ok": share.ok,
            "reason": share.reason,
            "signal": signal,
        },
    }


def fit_all(
    rows: Mapping[int, Mapping[str, float]], setups: Sequence[SystemSetup], now: datetime
) -> Dict[str, Any]:
    """All fits, as a dict to persist and to build the models from."""
    systems = {s.system: fit_system(rows, s, len(setups) == 1) for s in setups}
    shared = fit_shared(rows, setups[0].system) if setups else {}
    data_hours = len(rows) * 10 / 60.0
    return {"fitted_at": now.isoformat(), "data_hours": round(data_hours, 1), "systems": systems, **shared}


# --------------------------------------------------------------------------
# Fits -> the models the planner uses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Models:
    prior: HousePrior
    fit: HouseFit
    curve: HeatCurve
    energy: EnergyModel

    @property
    def house(self) -> HouseModel:
        return self.fit.model

    @property
    def capacity_kwh_per_k(self) -> float:
        return self.energy.emitter_kw_per_k / self.house.b

    @property
    def heat_loss_w_per_k(self) -> float:
        return 1000.0 * self.house.a * self.capacity_kwh_per_k


def models_for(setup: SystemSetup, fits: Optional[Mapping[str, Any]]) -> Models:
    """The planner's models: fitted where the fit was accepted, else priors."""
    base_curve = HeatCurve(curve=curve_number(setup.curve))
    prior = house_prior(setup.profile, base_curve)
    data = (fits or {}).get("systems", {}).get(setup.system) if isinstance(fits, Mapping) else None
    data = data if isinstance(data, Mapping) else {}
    fit = house_fit_from(data.get("house"), prior.model)
    curve = base_curve
    stored = data.get("curve")
    if isinstance(stored, Mapping) and stored.get("ok") and stored.get("curve") == base_curve.curve:
        try:
            curve = replace(
                base_curve,
                bias=float(stored["bias"]),
                slope_per_curve=float(stored["slope_per_curve"]),
                offset_gain=float(stored["offset_gain"]) if stored.get("offset_gain") is not None else None,
            )
        except (KeyError, TypeError, ValueError):
            curve = base_curve
    energy = prior.energy_model()
    emitter = data.get("emitter")
    if isinstance(emitter, Mapping) and emitter.get("ok"):
        energy = replace(energy, emitter_kw_per_k=float(emitter["value"]))
    eta = (fits or {}).get("eta") if isinstance(fits, Mapping) else None
    if isinstance(eta, Mapping) and eta.get("ok"):
        energy = replace(energy, carnot_eta=float(eta["value"]))
    return Models(prior, fit, curve, energy)


def models_summary(models: Models) -> Dict[str, Any]:
    """Compact figures for the House model sensor and diagnostics."""
    house = models.house
    fit = models.fit
    return {
        "a": round(house.a, 5),
        "b": round(house.b, 5),
        "c": round(house.c, 4),
        "d": round(house.d, 4),
        "time_constant_h": round(house.time_constant_h, 1),
        "heat_loss_w_per_k": round(models.heat_loss_w_per_k, 1),
        "capacity_kwh_per_k": round(models.capacity_kwh_per_k, 2),
        "emitter_kw_per_k": round(models.energy.emitter_kw_per_k, 4),
        "carnot_eta": round(models.energy.carnot_eta, 3),
        "curve": asdict(models.curve),
        "fit_ok": fit.ok,
        "fit_reason": fit.reason,
        "confidence": round(fit.confidence, 3),
        "level": fit.level if fit.ok else "low",
        "sim_rmse": round(fit.sim_rmse, 3),
        "rel_se_b": round(fit.rel_se_b, 3) if fit.rel_se_b != float("inf") else None,
        "data_hours": fit.samples,
    }
