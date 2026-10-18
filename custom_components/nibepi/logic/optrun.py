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

and once: ``fit_carnot_eta`` (heat meter vs. the heat pump's
electricity) and, depending on what the user's meter measures
(``meter_scope``): with ``heat_pump`` the meter is the electricity and
calibrates the efficiency directly; with ``whole_house`` the efficiency
uses the compressor power and addition, and ``fit_hp_share`` (the meter
vs. the heat pump's activity) is kept for diagnostics only.

Planning (every 15 minutes, on new prices and on a mode change):
``plan_system`` builds the horizon from the prices (currency/kWh), the
forecast and BT1, runs ``plan_heating`` from the measured indoor
temperature and explains it. A house with an emitter lag (floor heating)
is planned on 30-minute slots with offsets held for an hour, from the
slab temperature filtered out of the last day's BT2 and indoor history,
pulled towards BT3 when the fit uses it (``slab_estimate``); the floor
loop's flow is calibrated from the heat meter and BT2 - BT3
(``fit_loop``); ``plan_tank`` plans the hot water charges
against the VV learning profile's expected draws.

Away mode with a known return (``HeatingInputs.comfort_after`` from
``comfort_change``): the planner gets one comfort band per slot, the
lowered one now and the normal one from the return, so it heats the house
back in time; the baseline holds the base offset of each band, and the
offsets may go as far above the lowered base as the normal base needs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .inputs import ForecastPoint, PriceSlot
from .opthistory import column, house_samples, hourly
from .optimizer.energy import EnergyModel, fit_carnot_eta, fit_emitter
from .optimizer.explain import Block, explain_plan, summary
from .optimizer.heatcurve import DEFAULT_CURVE, HeatCurve, fit_heat_curve
from .optimizer.horizon import build_horizon
from .optimizer.house import (
    PUMP_ON_SHARE,
    HouseFit,
    HouseModel,
    fit_house_model,
    slab_filter,
    slab_observable,
    slab_temp,
    slab_track,
)
from .optimizer.meter import fit_hp_share
from .optimizer.planner import (
    LAG_PLANNER_MIN_H,
    ComfortSettings,
    Plan,
    PlannerSettings,
    comfort_schedule,
    neutral_offset,
    plan_heating,
)
from .optimizer.profile import HousePrior, HouseProfile, house_prior
from .optimizer.tank import (
    HotWaterPlan,
    PinnedCharge,
    TankModel,
    TankSettings,
    draws_from_week_profile,
    plan_hot_water,
)
from .texts import Msg, code_of, msg

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
        "tau_f": m.tau_f,
        "tau_d": m.tau_d,
        "hours": fit.samples,
        "r2": round(fit.r2, 3),
        "rmse_rate": round(fit.rmse_rate, 4),
        "sim_rmse": round(fit.sim_rmse, 3),
        "rel_se_b": round(fit.rel_se_b, 3) if fit.rel_se_b != float("inf") else None,
        "rejected": {k: v for k, v in fit.details.items() if v is not None} if not fit.ok else None,
        "lag_grid": {str(k): v for k, v in fit.details.get("lag_grid", {}).items()} or None,
        "kappa": round(m.kappa, 4) if m.kappa is not None else None,
        "slab_source": fit.details.get("slab_source"),
        "bt2_sim_rmse": fit.details.get("bt2_sim_rmse"),
        "bt3": fit.details.get("bt3"),
    }


def house_fit_from(data: Any, prior: HouseModel) -> HouseFit:
    """A stored fit, or the prior (not ok) when there is none or it is broken.

    A fit stored before the emitter lag existed (0.2.0) keeps its a..d and
    takes the prior's lag until the next fit; so does one stored for
    another emitter type (lagged vs one node). One from before BT3 was
    used (0.2.1) has no ``kappa``: the slab is then filtered from BT2.
    """
    if not isinstance(data, Mapping):
        return HouseFit(prior, False, "not fitted yet")
    try:
        ok = bool(data.get("ok"))
        tau_f, tau_d = prior.tau_f, prior.tau_d
        kappa = None
        if data.get("tau_f") is not None and (float(data["tau_f"]) > 0) == prior.lagged:
            tau_f, tau_d = float(data["tau_f"]), float(data.get("tau_d") or 0.0)
            kappa = float(data["kappa"]) if prior.lagged and data.get("kappa") is not None else None
        model = (
            HouseModel(float(data["a"]), float(data["b"]), float(data["c"]), float(data["d"]), tau_f, tau_d, kappa)
            if ok
            else prior
        )
        details = {k: data[k] for k in ("slab_source", "bt3", "bt2_sim_rmse") if data.get(k) is not None}
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
            details,
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
        # The planner's energy model: the slab's for a lagged house, else the supply's.
        lagged = fit.model.tau_f >= LAG_PLANNER_MIN_H
        slab = _slab_by_bin(samples, fit.model) if lagged else {}
        emitter_rows = []
        for hour, v in hourly(rows, ["heat_kwh", supply, t_in]):
            if hw.get(hour, 0.0) >= HW_SHARE:
                continue
            drive = v[supply]
            if lagged:
                # Heat = K_e ((T_f - T_in) + tau_d dT_f/dt): the slab's mean
                # plus what it stored over the hour, in place of the supply.
                inside = [slab.get(hour + i * 600) for i in range(6)]
                start, end = slab.get(hour), slab.get(hour + 3600)
                if None in inside or start is None or end is None:
                    continue
                drive = sum(inside) / 6.0 + fit.model.drain_h * (end - start)  # type: ignore[arg-type,operator]
            emitter_rows.append((v["heat_kwh"], 1.0, drive, v[t_in]))
        emitter = _energy_fit_dict(fit_emitter(emitter_rows, prior.emitter_kw_per_k))
    else:
        emitter = {
            "value": prior.emitter_kw_per_k,
            "ok": False,
            "reason": "the heat meter is shared by the climate systems",
            "hours": 0,
        }
    if single_system:
        loop = fit_loop(rows, supply, hw, fit.model, float(emitter["value"]) if emitter.get("ok") else None)
    else:
        loop = {"ok": False, "reason": "the heat meter is shared by the climate systems", "hours": 0}
    c = curve_fit.curve
    return {
        "house": house_fit_dict(fit),
        "loop": loop,
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
            "tau_f": prior.model.tau_f,
        },
    }


MIN_LOOP_HOURS = 24
MIN_LOOP_DT = 1.0  # K of BT2 - BT3 in the hour
WATER_KJ_PER_KG_K = 4.18


def fit_loop(
    rows: Mapping[int, Mapping[str, float]],
    supply: str,
    hw: Mapping[int, float],
    model: HouseModel,
    emitter_kw_per_k: Optional[float],
) -> Dict[str, Any]:
    """The floor loop's water flow from the heat meter (44300) and BT2 - BT3.

    Heat into the floor is flow x c_p x (BT2 - BT3), the flow roughly
    constant, so the ratio of the meter's increments to BT2 - BT3 over
    heating hours (no hot water, circulation pump on) is the flow's
    conductance in kW/K. With the slab's BT3 offset (``kappa``) and the
    fitted emitter conductance it also gives the coasting ratio
    tau_d / tau_f independently of the indoor fit: the water's
    conductance to the slab over the slab's to the room
    (``drain_ratio``), a check of tau_d.
    """
    pump = _hour_map(rows, "pump_on")
    heat = gap = 0.0
    hours = 0
    for hour, v in hourly(rows, ["heat_kwh", supply, "bt3"]):
        if hw.get(hour, 0.0) >= HW_SHARE or pump.get(hour, 1.0) < PUMP_ON_SHARE:
            continue
        dt = v[supply] - v["bt3"]
        if dt < MIN_LOOP_DT:
            continue
        heat += v["heat_kwh"]
        gap += dt
        hours += 1
    if hours < MIN_LOOP_HOURS:
        return {"ok": False, "reason": "too few heating hours with BT3 (%d)" % hours, "hours": hours}
    kw = heat / gap
    out: Dict[str, Any] = {
        "ok": True,
        "reason": "ok",
        "hours": hours,
        "kw_per_k": round(kw, 3),
        "flow_l_min": round(kw / WATER_KJ_PER_KG_K * 60.0, 1),
        "drain_ratio": None,
    }
    if model.kappa is not None and emitter_kw_per_k:
        out["drain_ratio"] = round(kw / ((1.0 + model.kappa) * emitter_kw_per_k), 2)
    return out


def _slab_by_bin(samples, model: HouseModel) -> Dict[int, float]:
    """Filtered slab temperature per 10-minute bin start."""
    ordered = sorted(samples, key=lambda x: x.ts.timestamp())
    values = slab_filter(ordered, model.tau_f, model.drain_h)
    return {int(x.ts.timestamp()): v for x, v in zip(ordered, values) if v is not None}


SLAB_RECENT = 12  # samples (2 h): the slab counts as seen in BT3 with an observation this recent


@dataclass(frozen=True)
class SlabEstimate:
    state: float  # T_f, the planner's slab state (settles at the supply)
    temp: float  # the slab's own temperature, what BT3 sees
    source: str  # "bt3": tracked with BT3; "bt2": filtered from BT2 only


def slab_estimate(
    rows: Mapping[int, Mapping[str, float]], system: str, model: HouseModel, now: datetime,
    max_age_s: float = 1800.0,
) -> Optional[SlabEstimate]:
    """The slab now, from the logged BT2, indoor and (when the fit uses it) BT3.

    With a BT3 fit (``model.kappa``) the slab is filtered from BT2 and
    pulled towards BT3 wherever the floor water circulates
    (``slab_track``); otherwise filtered from BT2 alone. None without a
    lag, without enough recent history (BT2 alone needs a warm-up of
    1.5 tau_f, BT3 one observation), or when the last bin is older than
    ``max_age_s``.
    """
    if not model.lagged:
        return None
    samples = sorted(house_samples(rows, system), key=lambda x: x.ts.timestamp())
    if not samples or now.timestamp() - samples[-1].ts.timestamp() > max_age_s:
        return None
    tau_f, tau_d = model.tau_f, model.drain_h
    source = "bt2"
    if model.kappa is not None:
        values = slab_track(samples, tau_f, tau_d, model.kappa)
        if any(slab_observable(samples)[-SLAB_RECENT:]):
            source = "bt3"
    else:
        values = slab_filter(samples, tau_f, tau_d)
    value = values[-1]
    if value is None:
        return None
    temp = slab_temp(value, samples[-1].t_in, tau_f, tau_d)
    return SlabEstimate(round(value, 2), round(temp, 2), source)


def slab_now(
    rows: Mapping[int, Mapping[str, float]], system: str, model: HouseModel, now: datetime,
    max_age_s: float = 1800.0,
) -> Optional[float]:
    """The planner's slab state now (``slab_estimate``), or None."""
    est = slab_estimate(rows, system, model, now, max_age_s)
    return est.state if est is not None else None


METER_HEAT_PUMP = "heat_pump"
METER_WHOLE_HOUSE = "whole_house"
MIN_ETA_HOURS = 12


def fit_shared(
    rows: Mapping[int, Mapping[str, float]], supply_system: str, meter_scope: str = METER_WHOLE_HOUSE
) -> Dict[str, Any]:
    """The compressor's efficiency and how the user's meter relates to the pump.

    ``meter_scope`` ``heat_pump``: the meter's kWh per hour is the heat
    pump's electricity. The Carnot fraction is calibrated directly against
    it (falling back to the compressor power estimate while the meter has
    fewer than 12 usable hours), no share is regressed (beta = 1), and the
    ratio of the meter to the pump's own estimate (43141 + 43084) is kept
    as a check. ``whole_house``: as before, the efficiency comes from the
    pump's estimate and the share is regressed from the house total.
    """
    supply = column("supply", supply_system)
    hw = _hour_map(rows, "prio_hw")
    add = _hour_map(rows, "add_kw")
    brine = _hour_map(rows, "bt10")
    estimate = {hour: v["power_w"] / 1000.0 + max(0.0, add.get(hour, 0.0))
                for hour, v in hourly(rows, ["power_w"])}

    def eta_rows(electric: Mapping[int, float]):
        return [
            (v["heat_kwh"], electric[hour], v[supply], brine.get(hour))
            for hour, v in hourly(rows, ["heat_kwh", supply])
            if hour in electric and hw.get(hour, 0.0) < HW_SHARE
        ]

    if meter_scope == METER_HEAT_PUMP:
        metered = _hour_map(rows, "meter_kwh")
        rows_eta = eta_rows(metered)
        source = "meter"
        if len(rows_eta) < MIN_ETA_HOURS:
            rows_eta = eta_rows(estimate)
            source = "compressor power (meter has too few hours)"
        eta = dict(_energy_fit_dict(fit_carnot_eta(rows_eta)), electricity=source)
        both = [(metered[h], estimate[h]) for h in metered if h in estimate]
        est_sum = sum(e for _, e in both)
        ratio = sum(m for m, _ in both) / est_sum if both and est_sum > 0 else None
        return {
            "eta": eta,
            "hp_share": {
                "scope": METER_HEAT_PUMP,
                "base_kw": 0.0,
                "beta": 1.0,
                "r2": None,
                "hours": len(metered),
                "ok": bool(metered),
                "reason": "the meter measures the heat pump only" if metered else "no meter hours yet",
                "signal": "meter",
                "pump_estimate_ratio": round(ratio, 3) if ratio is not None else None,
            },
        }

    eta = dict(_energy_fit_dict(fit_carnot_eta(eta_rows(estimate))), electricity="compressor power")
    share_rows = [
        (v["meter_kwh"], estimate[hour], 1.0)
        for hour, v in hourly(rows, ["meter_kwh"])
        if hour in estimate
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
            "scope": METER_WHOLE_HOUSE,
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
    rows: Mapping[int, Mapping[str, float]],
    setups: Sequence[SystemSetup],
    now: datetime,
    meter_scope: str = METER_WHOLE_HOUSE,
) -> Dict[str, Any]:
    """All fits, as a dict to persist and to build the models from."""
    systems = {s.system: fit_system(rows, s, len(setups) == 1) for s in setups}
    shared = fit_shared(rows, setups[0].system, meter_scope) if setups else {}
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
    emitter: str = "radiators"

    @property
    def house(self) -> HouseModel:
        return self.fit.model

    @property
    def capacity_kwh_per_k(self) -> float:
        return self.energy.emitter_kw_per_k / self.house.b

    @property
    def heat_loss_w_per_k(self) -> float:
        return 1000.0 * self.house.a * self.capacity_kwh_per_k

    @property
    def lag_planner(self) -> bool:
        return self.house.tau_f >= LAG_PLANNER_MIN_H

    @property
    def slab_capacity_kwh_per_k(self) -> float:
        return self.energy.emitter_kw_per_k * self.house.drain_h if self.house.lagged else 0.0


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
    return Models(prior, fit, curve, energy, setup.profile.emitter)


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
        "emitter": models.emitter,
        "emitter_lag_h": round(house.tau_f, 2),
        "slab_coast_h": round(house.drain_h, 1) if house.lagged else None,
        "slab_coast_fitted": bool((fit.details.get("bt3") or {}).get("tau_d_fitted"))
        if house.kappa is not None else False,
        "slab_coupling_per_h": round(house.slab_coupling, 5) if house.lagged else None,
        "bt3_used": house.kappa is not None,
        "return_kappa": round(house.kappa, 3) if house.kappa is not None else None,
        "slab_capacity_kwh_per_k": round(models.slab_capacity_kwh_per_k, 2) if house.lagged else None,
        "planner": "lagged" if models.lag_planner else "one_node",
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


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

PLAN_MAX_DOWN = 3
PLAN_MAX_UP = 3
# Lagged houses (floor heating): 30-minute slots, offsets held for an hour
# and a higher switching cost; with hours of lag, faster changes only
# wear the compressor. The current slot's offset still applies every 15
# minutes when the plan is refreshed.
LAG_STEP_MINUTES = 30
LAG_HOLD_SLOTS = 2
LAG_CHANGE_COST = 0.3
LAG_STEP_COST = 0.05


@dataclass(frozen=True)
class HeatingInputs:
    system: str
    models: Models
    comfort: ComfortSettings
    prices: Sequence[PriceSlot]  # currency/kWh
    now: datetime
    tz: tzinfo
    t_in: Optional[float]
    t_out: Optional[float]
    forecast: Sequence[ForecastPoint] = ()
    t_brine: Optional[float] = None
    current_offset: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    currency: str = "SEK"
    # The log's recent bins (a day), for the slab temperature of a lagged house.
    history: Optional[Mapping[int, Mapping[str, float]]] = None
    # Away mode's return: ``comfort`` (lowered) until ``comfort_change -
    # comfort_ramp_h``, ramping to ``comfort_after`` (normal) at the change.
    comfort_after: Optional[ComfortSettings] = None
    comfort_change: Optional[datetime] = None
    comfort_ramp_h: float = 0.0
    # Why ``t_in`` is None (``indoor_problem``), and the price source's
    # status (for the problem when no prices cover now).
    t_in_problem: Optional[str] = None
    price_status: Optional[str] = None


@dataclass
class HeatingResult:
    system: str
    planned_at: datetime
    base_offset: int
    plan: Optional[Plan] = None
    slab: Optional[float] = None  # lagged house: slab state (T_f) the plan started from
    slab_temp: Optional[float] = None  # ... as the slab's own temperature (comparable with BT3)
    slab_source: Optional[str] = None  # bt3, bt2, or settled (no history: steady at the current offset)
    blocks: List[Block] = field(default_factory=list)
    summary: str = ""
    problem: Optional[str] = None
    # The comfort band of each plan step (all the same unless Away returns).
    comforts: List[ComfortSettings] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.plan is not None and self.problem is None


def comfort_settings(target: float, below: float, above: float, floor_below: float, margin: float) -> ComfortSettings:
    return ComfortSettings(
        target=target, below=below, above=above, hard_floor_below=floor_below, sensor_margin=margin
    )


def base_offset(models: Models, t_out: float, target: float) -> int:
    return neutral_offset(models.house, models.curve, t_out, target)


# Problems from a missing or unusable input (``problem_input`` says which):
# opt_runtime replans as soon as the input is back, not on the quarter hour.
INPUT_PROBLEMS = frozenset({
    "opt.no_indoor", "opt.indoor_not_found", "opt.indoor_unavailable", "opt.indoor_unknown",
    "opt.indoor_not_numeric", "opt.indoor_bad_unit", "opt.indoor_missing", "opt.indoor_stale",
    "opt.indoor_implausible", "opt.no_prices", "opt.prices_none", "opt.prices_end", "opt.prices_from",
    "opt.no_prices_why", "opt.no_bt6", "opt.no_stops",
})
INDOOR_REASONS = ("not_found", "unavailable", "unknown", "not_numeric", "bad_unit", "missing", "stale", "implausible")


# Between a plan and a retry for a missing input; the runtime ticks once
# a minute, so this is at most one retry per tick.
RETRY_MIN_GAP = timedelta(seconds=30)
# Retry every tick after start only this long; then only when an input is
# back (a setup that never gets prices would otherwise plan every minute).
STARTUP_RETRY = timedelta(minutes=20)


def input_problem(problem: Any) -> bool:
    """True when a plan's ``problem`` is a missing or unusable input."""
    return code_of(problem) in INPUT_PROBLEMS


def problem_input(problem: Any) -> Optional[str]:
    """The input a plan's problem is about: "indoor", "prices", "bt6",
    "stops", or None for another problem."""
    code = code_of(problem)
    if code not in INPUT_PROBLEMS:
        return None
    if code == "opt.no_indoor" or code.startswith("opt.indoor_"):
        return "indoor"
    if code == "opt.no_bt6":
        return "bt6"
    if code == "opt.no_stops":
        return "stops"
    return "prices"


def should_replan(
    now: datetime,
    last_plan_at: Optional[datetime],
    cleared: Sequence[bool],
    planned_ok: bool,
    min_gap: timedelta = RETRY_MIN_GAP,
    since_start: Optional[timedelta] = None,
) -> bool:
    """Whether to plan now, off the quarter-hour schedule.

    ``cleared`` has one item per current plan that failed on an input:
    whether that input is usable again. Retry when one is; until the first
    plan without such a problem since start (``planned_ok``), retry on
    every tick anyway, for the first ``STARTUP_RETRY`` after start
    (``since_start``; None counts as within it). Never sooner than
    ``min_gap`` after the last plan.
    """
    if not cleared or last_plan_at is None or now - last_plan_at < min_gap:
        return False
    early = since_start is None or since_start < STARTUP_RETRY
    return any(cleared) or (not planned_ok and early)


def indoor_problem(source: str, reason: str, details: Optional[Mapping[str, Any]] = None) -> Msg:
    """Why the indoor temperature from ``source`` (an entity id, BT50)
    cannot be used: a reason of ``inputs.unusable`` or
    ``inputs.entity_absence`` (or "bad_unit") with its details."""
    params: Dict[str, Any] = dict(details or {})
    params["source"] = source
    if reason not in INDOOR_REASONS:
        return msg("opt.no_indoor")
    if reason == "stale" and (params.get("minutes") is None or params.get("timeout") is None):
        reason = "missing"
    if reason == "stale":
        params["minutes"] = int(params["minutes"])
    return msg("opt.indoor_%s" % reason, **params)


def prices_problem(
    prices: Sequence[PriceSlot], now: datetime, tz: tzinfo, status: Optional[str] = None
) -> Msg:
    """Why no prices cover ``now``: the price source's problem when it has
    one (``status``, a text other than "ok"), none at all, prices that end
    before now or start after it, or a gap at now."""
    if isinstance(status, Msg) and status.code != "price.status_ok":
        return msg("opt.no_prices_why", why=status)
    if not prices:
        return msg("opt.prices_none")

    def hm(when: datetime) -> str:
        return when.astimezone(tz).strftime("%H:%M")

    last_end = max(slot.end for slot in prices)
    if last_end <= now:
        return msg("opt.prices_end", end=hm(last_end))
    first_start = min(slot.start for slot in prices if slot.end > now)
    if first_start > now:
        return msg("opt.prices_from", start=hm(first_start))
    return msg("opt.no_prices")


def plan_system(inputs: HeatingInputs) -> HeatingResult:
    """The heating plan for one system, or the reason there is none."""
    models = inputs.models
    target = inputs.comfort.target
    t_out_now = inputs.t_out if inputs.t_out is not None else 0.0
    result = HeatingResult(inputs.system, inputs.now, base_offset(models, t_out_now, target))
    if inputs.t_in is None:
        result.problem = inputs.t_in_problem or msg("opt.no_indoor")
        return result
    lagged = models.lag_planner
    slots = build_horizon(
        inputs.prices,
        inputs.now,
        inputs.t_out,
        inputs.forecast,
        step_minutes=LAG_STEP_MINUTES if lagged else None,
        latitude=inputs.latitude,
        longitude=inputs.longitude,
        t_brine=inputs.t_brine,
    )
    if not slots:
        result.problem = prices_problem(inputs.prices, inputs.now, inputs.tz, inputs.price_status)
        return result
    hours = sum(s.hours for s in slots)
    mean_out = sum(s.t_out * s.hours for s in slots) / hours
    result.base_offset = base_offset(models, mean_out, target)
    comforts = None
    baseline = None
    max_up = PLAN_MAX_UP
    after = inputs.comfort_after
    change = inputs.comfort_change
    if after is not None and change is not None and change - timedelta(hours=inputs.comfort_ramp_h) < slots[-1].end:
        comforts = comfort_schedule(slots, inputs.comfort, after, change, inputs.comfort_ramp_h)
        base_after = base_offset(models, mean_out, after.target)
        baseline = [base_after if s.end >= change else result.base_offset for s in slots]
        max_up = PLAN_MAX_UP + max(0, base_after - result.base_offset)
    if lagged:
        settings = PlannerSettings(
            base_offset=result.base_offset, max_down=PLAN_MAX_DOWN, max_up=max_up,
            change_cost=LAG_CHANGE_COST, step_cost=LAG_STEP_COST, hold_slots=LAG_HOLD_SLOTS,
        )
        est = slab_estimate(inputs.history or {}, inputs.system, models.house, inputs.now)
        if est is not None:
            result.slab, result.slab_temp, result.slab_source = est.state, est.temp, est.source
    else:
        settings = PlannerSettings(base_offset=result.base_offset, max_down=PLAN_MAX_DOWN, max_up=max_up)
    plan = plan_heating(
        slots, inputs.t_in, models.house, models.curve, models.energy, inputs.comfort, settings,
        current_offset=inputs.current_offset, t_f0=result.slab, comforts=comforts, baseline_offsets=baseline,
    )
    result.comforts = list(comforts) if comforts is not None else [inputs.comfort] * len(slots)
    if lagged and result.slab is None and plan.plan.steps:
        result.slab = plan.plan.steps[0].slab_start
        if result.slab is not None:
            house = models.house
            result.slab_temp = round(slab_temp(result.slab, inputs.t_in, house.tau_f, house.drain_h), 2)
            result.slab_source = "settled"
    result.plan = plan
    result.blocks = explain_plan(plan, inputs.comfort, inputs.tz, "%s/kWh" % inputs.currency)
    result.summary = summary(plan, inputs.currency)
    return result


@dataclass(frozen=True)
class TankInputs:
    prices: Sequence[PriceSlot]  # currency/kWh
    now: datetime
    tz: tzinfo
    bt6: Optional[float]
    economy_stop: Optional[float]
    normal_start: Optional[float]
    normal_stop: Optional[float]
    lux_stop: Optional[float]
    floor: float
    profile: Sequence[float]  # VV learning's 168 hour-of-week BT6 drops
    confidence: float = 0.7
    t_brine: Optional[float] = None
    pump_starts_itself: bool = False
    # A planned (or running) legionella cycle: (start, end, temperature).
    legionella: Optional[Tuple[datetime, datetime, float]] = None
    # The price source's status (for the problem when no prices cover now).
    price_status: Optional[str] = None


@dataclass
class TankResult:
    planned_at: datetime
    plan: Optional[HotWaterPlan] = None
    problem: Optional[str] = None


def plan_tank(inputs: TankInputs) -> TankResult:
    """The hot water charge plan, or the reason there is none.

    The optimiser's charges run through the hot water controller's VV
    path, which holds the hot water period (47134) at 0: the pump does not
    charge by itself then, so the economy start is modelled away unless
    ``pump_starts_itself``. The controller's BT7 min-temp floor stays as
    the backstop.
    """
    result = TankResult(inputs.now)
    if inputs.bt6 is None:
        result.problem = msg("opt.no_bt6")
        return result
    if None in (inputs.economy_stop, inputs.normal_stop, inputs.lux_stop):
        result.problem = msg("opt.no_stops")
        return result
    slots = build_horizon(inputs.prices, inputs.now, None, None, t_brine=inputs.t_brine)
    if not slots:
        result.problem = prices_problem(inputs.prices, inputs.now, inputs.tz, inputs.price_status)
        return result
    normal_stop = float(inputs.normal_stop)  # type: ignore[arg-type]
    normal_start = inputs.normal_start if inputs.normal_start is not None else normal_stop - 5.0
    settings = TankSettings(
        floor=inputs.floor,
        economy_start=(min(float(inputs.economy_stop) - 7.0, inputs.floor - 2.0)  # type: ignore[arg-type]
                       if inputs.pump_starts_itself else -50.0),
        economy_stop=float(inputs.economy_stop),  # type: ignore[arg-type]
        normal_start=float(normal_start),
        normal_stop=normal_stop,
        lux_stop=float(inputs.lux_stop),  # type: ignore[arg-type]
    )
    draws = draws_from_week_profile(
        list(inputs.profile), slots[0].start, slots[-1].end, inputs.tz, inputs.confidence
    )
    pinned = [PinnedCharge(*inputs.legionella)] if inputs.legionella is not None else []
    result.plan = plan_hot_water(slots, inputs.bt6, draws, TankModel(), settings, inputs.tz, pinned)
    return result


def profile_confidence(learned: int, imported: bool) -> float:
    """How far to trust VV learning's profile as a draw forecast (0.5..0.8)."""
    if imported:
        return 0.8
    return 0.5 + 0.3 * min(1.0, max(0, learned) / 60.0)


def scale_prices(slots: Sequence[PriceSlot], unit_factor: Any) -> List[PriceSlot]:
    """Source prices -> currency/kWh (the option converts to öre: /100)."""
    try:
        factor = float(unit_factor) / 100.0
    except (TypeError, ValueError):
        factor = 1.0
    if factor <= 0:
        factor = 1.0
    return [PriceSlot(s.start, s.end, s.price * factor) for s in slots]
