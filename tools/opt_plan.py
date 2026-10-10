#!/usr/bin/env python3
"""Run the cost optimiser on a JSON fixture and print the plan.

    python3 tools/opt_plan.py tools/opt_example.json
    python3 tools/opt_plan.py fixture.json --json        # machine-readable
    python3 tools/opt_plan.py fixture.json --backtest    # closed loop vs. baseline

No Home Assistant needed. The fixture (all keys but ``prices`` optional)::

    {
      "now": "2026-01-15T00:00:00+01:00",  "tz": "Europe/Stockholm",
      "t_in": 21.0, "t_out": -5.0, "t_tank": 46.0, "t_brine": 1.5,
      "prices": [{"start": ISO, "end": ISO, "price": SEK/kWh}, ...]
                or a tibber.get_prices response {"prices": {"<home>": [...]}},
      "forecast": [{"time": ISO, "temperature": °C, "condition": "cloudy"}, ...],
      "profile": {"floor_area_m2": 150, "floors": 1.5, "target_temp": 21,
                  "era": "1976-1990", "construction": "light",
                  "latitude": 59.3, "longitude": 18.1},
      "curve": {"curve": 9},
      "house": {"a": .., "b": .., "c": .., "d": ..},   # overrides the prior
      "history": [{"ts": ISO, "t_in": .., "t_out": .., "t_supply": ..}, ...],
                                                         # fitted when present
      "energy": {"emitter_kw_per_k": .., "carnot_eta": ..},
      "comfort": {"below": 0.5, "above": 1.0, ...},
      "planner": {"max_down": 3, "max_up": 3, "base_offset": null, ...},
      "step_minutes": null,
      "draws": [{"start": ISO, "end": ISO, "temperature_drop": 8, "confidence": 0.8}],
      "vv_profile": [168 BT6 drops, Monday 00:00 local first],
      "tank": {...}, "tank_settings": {...}
    }

Exported Home Assistant history can be turned into ``history`` (10-min
means of the room sensor, BT1 40004 and BT2 40008) to try a real fit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, fields
from datetime import timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "custom_components", "nibepi"))

from logic.inputs import ForecastPoint  # noqa: E402
from logic.optimizer.backtest import backtest_heating  # noqa: E402
from logic.optimizer.energy import EnergyModel  # noqa: E402
from logic.optimizer.explain import explain_plan, summary  # noqa: E402
from logic.optimizer.heatcurve import HeatCurve  # noqa: E402
from logic.optimizer.horizon import build_horizon  # noqa: E402
from logic.optimizer.house import HouseModel, HouseSample, fit_house_model  # noqa: E402
from logic.optimizer.planner import ComfortSettings, PlannerSettings, neutral_offset, plan_heating  # noqa: E402
from logic.optimizer.profile import HouseProfile, house_prior  # noqa: E402
from logic.optimizer.tank import (  # noqa: E402
    DrawForecast,
    TankModel,
    TankSettings,
    draws_from_week_profile,
    plan_hot_water,
)
from logic.price import normalize_tibber_response, parse_time, slots_from_dicts  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def build(cls, data):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in names})


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("fixture")
    ap.add_argument("--json", action="store_true", help="print the plan as JSON")
    ap.add_argument("--backtest", action="store_true", help="closed-loop replay against the model")
    args = ap.parse_args(argv)
    fx = load(args.fixture)

    tz = ZoneInfo(fx["tz"]) if fx.get("tz") and ZoneInfo else timezone.utc
    now = parse_time(fx["now"], tz)
    raw = fx["prices"]
    if isinstance(raw, dict):
        prices = normalize_tibber_response(raw, tz, fx.get("tibber_home")).slots
    else:
        prices = slots_from_dicts(raw, tz)
    forecast = [
        ForecastPoint(parse_time(p["time"], tz), p.get("temperature"), condition=p.get("condition"))
        for p in fx.get("forecast", [])
    ]
    profile = build(HouseProfile, fx.get("profile"))
    curve = build(HeatCurve, fx.get("curve"))
    prior = house_prior(profile, curve)
    house = prior.model
    notes = []
    if fx.get("house"):
        house = build(HouseModel, fx["house"])
        notes.append("house model from the fixture")
    elif fx.get("history"):
        samples = [HouseSample(parse_time(h["ts"], tz), h["t_in"], h["t_out"], h["t_supply"], h.get("solar", 0.0))
                   for h in fx["history"]]
        fit = fit_house_model(samples, prior.model, prior.bounds, sensor_margin=prior.sensor_margin)
        notes.append("fit: %s, %s confidence %.2f, sim RMSE %.2f °C over %d h"
                     % (fit.reason, fit.level, fit.confidence, fit.sim_rmse, fit.samples))
        house = fit.model
    else:
        notes.append("house model from the profile prior (no history)")
    energy = build(EnergyModel, dict(asdict(prior.energy_model()), **fx.get("energy", {})))
    comfort = build(ComfortSettings, dict({"target": profile.target_temp, "sensor_margin": prior.sensor_margin},
                                          **fx.get("comfort", {})))
    slots = build_horizon(prices, now, fx.get("t_out"), forecast, step_minutes=fx.get("step_minutes"),
                          latitude=profile.latitude, longitude=profile.longitude, t_brine=fx.get("t_brine"))
    if not slots:
        print("no prices cover %s" % now.isoformat(), file=sys.stderr)
        return 1
    pset = dict(fx.get("planner", {}))
    if pset.get("base_offset") is None:
        mean_out = sum(s.t_out * s.hours for s in slots) / sum(s.hours for s in slots)
        pset["base_offset"] = neutral_offset(house, curve, mean_out, comfort.target)
    settings = build(PlannerSettings, pset)
    t_in = fx.get("t_in", comfort.target)

    if args.backtest:
        result = backtest_heating(slots, t_in, house, curve, energy, comfort, settings)
        print("backtest: %d replans, saves %.2f (%.1f %%), indoor %.2f-%.2f °C, slowest plan %.3f s"
              % (result.replans, result.savings, result.savings_pct, result.plan.min_t_in,
                 result.plan.max_t_in, result.max_plan_s))
        return 0

    plan = plan_heating(slots, t_in, house, curve, energy, comfort, settings,
                        current_offset=fx.get("current_offset", settings.base_offset))
    blocks = explain_plan(plan, comfort, tz)

    hw = None
    if "t_tank" in fx:
        draws = [DrawForecast(parse_time(d["start"], tz), parse_time(d["end"], tz), d.get("expected_kwh"),
                              d.get("temperature_drop"), d.get("confidence", 1.0)) for d in fx.get("draws", [])]
        if fx.get("vv_profile"):
            draws += draws_from_week_profile(fx["vv_profile"], slots[0].start, slots[-1].end, tz)
        hw = plan_hot_water(slots, fx["t_tank"], draws, build(TankModel, fx.get("tank")),
                            build(TankSettings, fx.get("tank_settings")), tz)

    if args.json:
        out = {
            "notes": notes,
            "base_offset": settings.base_offset,
            "savings": plan.savings,
            "elapsed_s": plan.elapsed_s,
            "steps": [dict(start=s.start.isoformat(), offset=s.offset, price=s.price, t_out=s.t_out,
                           t_in=round(s.t_in_end, 3), supply=round(s.supply, 2), kwh=round(s.electric_kwh, 4),
                           cost=round(s.energy_cost, 4)) for s in plan.plan.steps],
            "blocks": [dict(start=b.start.isoformat(), end=b.end.isoformat(), offset=b.offset,
                            reason=b.reason, text=b.text) for b in blocks],
        }
        if hw is not None:
            out["hot_water"] = {
                "savings": hw.savings,
                "min_temp": hw.min_temp,
                "blocks": [dict(start=b.start.isoformat(), end=b.end.isoformat(), action=b.action,
                                target=b.target, mode=b.mode, boost=b.boost, reason=b.reason)
                           for b in hw.blocks],
            }
        print(json.dumps(out, indent=2))
        return 0

    for note in notes:
        print("#", note)
    print("# house a=%.4f b=%.4f c=%.3f d=%.3f (tau %.0f h), base offset %+d, %d slots, planned in %.3f s"
          % (house.a, house.b, house.c, house.d, house.time_constant_h, settings.base_offset,
             len(slots), plan.elapsed_s))
    print("%-5s %7s %6s %4s %6s %6s %6s" % ("time", "price", "t_out", "off", "supply", "t_in", "kWh"))
    for s in plan.plan.steps:
        print("%-5s %7.2f %6.1f %+4d %6.1f %6.2f %6.2f" % (
            s.start.astimezone(tz).strftime("%H:%M"), s.price, s.t_out, s.offset, s.supply,
            s.t_in_end, s.electric_kwh))
    print()
    for b in blocks:
        print("-", b.text)
    print()
    print(summary(plan))
    if hw is not None:
        print()
        print("Hot water: saves %.2f, tank min %.1f °C (pump alone %.1f °C)"
              % (hw.savings, hw.min_temp, hw.baseline_min_temp))
        for b in hw.blocks:
            print("- %s-%s %s%s: %s" % (
                b.start.astimezone(tz).strftime("%H:%M"), b.end.astimezone(tz).strftime("%H:%M"), b.action,
                " to %.0f °C%s" % (b.target, " (temporary lux)" if b.boost else "") if b.target else "",
                b.reason))
    return 0


if __name__ == "__main__":
    sys.exit(main())
