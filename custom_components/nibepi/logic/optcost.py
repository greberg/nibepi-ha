"""The heat pump's electricity cost today: measured against the plans.

With a meter that measures the heat pump only (``meter_scope``
``heat_pump``) each finished 10-minute bin's kWh is priced at the price
slot it falls in and added to the day. Alongside, what the optimiser's
plans predicted for the same hours is banked when a plan is replaced
(heating and hot water, plus the heating baseline: the base offset held).

In shadow mode the pump follows the rules, not the plan, so the gap
between measured and predicted mixes model error with the difference
between the strategies; with the optimiser active it is the model's
error. The tally is a plain dict so it can be persisted as is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

PLANNED_KEYS = ("heating", "hot_water", "baseline")


def price_at(slots: Sequence[Any], when: datetime) -> Optional[float]:
    """The price of the slot (``start``/``end``/``price``) holding ``when``."""
    for slot in slots:
        if slot.start <= when < slot.end:
            return float(slot.price)
    return None


def energy_between(steps: Sequence[Any], start: datetime, end: datetime) -> Tuple[float, float]:
    """(kWh, cost) of plan steps (``electric_kwh``, ``price``) in [start, end), pro rata."""
    if end <= start:
        return 0.0, 0.0
    s0, s1 = start.timestamp(), end.timestamp()
    kwh = cost = 0.0
    for step in steps:
        t0, t1 = step.start.timestamp(), step.end.timestamp()
        part = max(0.0, min(t1, s1) - max(t0, s0))
        if part <= 0 or t1 <= t0:
            continue
        share = step.electric_kwh * part / (t1 - t0)
        kwh += share
        cost += share * step.price
    return kwh, cost


def new_day(day: str, previous: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    tally: Dict[str, Any] = {
        "day": day,
        "kwh": 0.0,
        "cost": 0.0,
        "unpriced_kwh": 0.0,
        "bins": 0,
        "planned": {key: [0.0, 0.0] for key in PLANNED_KEYS},
    }
    if previous and previous.get("day") and previous.get("day") != day:
        tally["yesterday"] = summary(previous)
    return tally


def roll(tally: Any, day: str) -> Dict[str, Any]:
    """The tally for ``day``: the same one, or a new one remembering the last."""
    if isinstance(tally, dict) and tally.get("day") == day and isinstance(tally.get("planned"), dict):
        return tally
    return new_day(day, tally if isinstance(tally, Mapping) else None)


def add_measured(tally: Dict[str, Any], kwh: float, price: Optional[float]) -> None:
    if kwh is None or kwh < 0:
        return
    tally["kwh"] = tally.get("kwh", 0.0) + kwh
    tally["bins"] = tally.get("bins", 0) + 1
    if price is None:
        tally["unpriced_kwh"] = tally.get("unpriced_kwh", 0.0) + kwh
    else:
        tally["cost"] = tally.get("cost", 0.0) + kwh * price


def add_planned(tally: Dict[str, Any], key: str, kwh: float, cost: float) -> None:
    planned = tally.setdefault("planned", {})
    value = planned.get(key) or [0.0, 0.0]
    planned[key] = [value[0] + kwh, value[1] + cost]


def summary(tally: Mapping[str, Any]) -> Dict[str, Any]:
    """Rounded figures for the sensor: measured, predicted and the gap."""
    planned = tally.get("planned") or {}

    def pair(key: str) -> Tuple[float, float]:
        value = planned.get(key) or [0.0, 0.0]
        return float(value[0]), float(value[1])

    heat_kwh, heat_cost = pair("heating")
    hw_kwh, hw_cost = pair("hot_water")
    base_kwh, base_cost = pair("baseline")
    predicted_kwh = heat_kwh + hw_kwh
    predicted_cost = heat_cost + hw_cost
    kwh = float(tally.get("kwh") or 0.0)
    cost = float(tally.get("cost") or 0.0)
    return {
        "day": tally.get("day"),
        "kwh": round(kwh, 2),
        "cost": round(cost, 2),
        "unpriced_kwh": round(float(tally.get("unpriced_kwh") or 0.0), 2),
        "hours_measured": round(int(tally.get("bins") or 0) / 6.0, 1),
        "predicted_kwh": round(predicted_kwh, 2),
        "predicted_cost": round(predicted_cost, 2),
        "predicted_heating_cost": round(heat_cost, 2),
        "predicted_hot_water_cost": round(hw_cost, 2),
        "baseline_heating_cost": round(base_cost, 2),
        "baseline_heating_kwh": round(base_kwh, 2),
        "cost_vs_predicted_pct": round(100.0 * (cost - predicted_cost) / predicted_cost, 1)
        if predicted_cost > 0
        else None,
    }
