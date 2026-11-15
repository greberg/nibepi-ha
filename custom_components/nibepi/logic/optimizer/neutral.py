"""Check a fitted house model against the offsets that actually held the room.

The fit judges itself on how well it replays the past (``sim_rmse``), and
a model whose parameters bent around each other can replay the past well
and still put the offset that holds the target several steps off: on
13 Nov a "medium" model said -5 holds 21 °C at 7 °C outside, while -1 to
-2 had held it for weeks, and -5/-6 let the room fall 1.2 °C overnight.
The planner then plans around that wrong neutral offset.

This check needs no model of its own. Over whole days (24 h, so day and
night, sun and the slab's lag average out) in which the indoor
temperature ended where it started (within ``MAX_DRIFT``), the mean
offset was what held the mean indoor temperature at the mean outdoor
temperature. The model's neutral offset for the same day is compared
with it; the mean difference over the days is the model's bias in
offset steps. Beyond ``MAX_BIAS`` the model may show, never steer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from .heatcurve import HeatCurve
from .house import HouseModel
from .solar import DEFAULT_CLOUD, solar_proxy

DAY_S = 86400
STEP_S = 600
MAX_DAYS = 14
MIN_DAYS = 3
MIN_COVERAGE = 0.8  # share of a day's bins with indoor, outdoor and offset
MAX_DRIFT = 0.3  # °C between the first and the last hour of a day
MAX_BIAS = 1.5  # offset steps
EDGE_BINS = 6  # an hour at either end for the drift


@dataclass(frozen=True)
class NeutralCheck:
    ok: bool  # False: the model is biased (or nothing to tell: see ``days``)
    days: int
    model: Optional[float] = None  # mean neutral offset by the model
    actual: Optional[float] = None  # mean offset that held the room
    bias: Optional[float] = None  # model - actual, offset steps

    def as_dict(self) -> Dict[str, Any]:
        def r(v: Optional[float]) -> Optional[float]:
            return round(v, 2) if v is not None else None

        return {"ok": self.ok, "days": self.days, "model": r(self.model), "actual": r(self.actual),
                "bias": r(self.bias)}


def model_neutral(house: HouseModel, curve: HeatCurve, t_in: float, t_out: float, solar: float = 0.0) -> float:
    """The offset (unrounded, unclamped) that holds ``t_in`` by the model."""
    return curve.offset_for_supply(t_out, house.steady_supply(t_in, t_out, solar))


def check_neutral(
    rows: Mapping[int, Mapping[str, float]],
    system: str,
    house: HouseModel,
    curve: HeatCurve,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    end: Optional[int] = None,
) -> NeutralCheck:
    """``rows``: the history bins (start -> column values). Passes (ok)
    with fewer than ``MIN_DAYS`` steady days: nothing to tell then."""
    t_in_key, offset_key = "%s_t_in" % system, "%s_offset" % system
    starts = sorted(rows)
    if not starts:
        return NeutralCheck(True, 0)
    last = (end if end is not None else starts[-1] + STEP_S)
    model_sum = actual_sum = 0.0
    days = 0
    for k in range(MAX_DAYS):
        hi = last - k * DAY_S
        lo = hi - DAY_S
        day = [(t, rows[t]) for t in range(lo - lo % STEP_S, hi, STEP_S)
               if t >= lo and t in rows and t_in_key in rows[t] and "t_out" in rows[t] and offset_key in rows[t]]
        if len(day) < MIN_COVERAGE * DAY_S / STEP_S:
            continue
        first = [v[t_in_key] for _, v in day[:EDGE_BINS]]
        final = [v[t_in_key] for _, v in day[-EDGE_BINS:]]
        if abs(sum(final) / len(final) - sum(first) / len(first)) > MAX_DRIFT:
            continue
        n = len(day)
        t_in = sum(v[t_in_key] for _, v in day) / n
        t_out = sum(v["t_out"] for _, v in day) / n
        offset = sum(v[offset_key] for _, v in day) / n
        solar = 0.0
        if latitude is not None and longitude is not None:
            solar = sum(
                solar_proxy(datetime.fromtimestamp(t + STEP_S / 2, timezone.utc), latitude, longitude, DEFAULT_CLOUD)
                for t, _ in day
            ) / n
        model_sum += model_neutral(house, curve, t_in, t_out, solar)
        actual_sum += offset
        days += 1
    if days < MIN_DAYS:
        return NeutralCheck(True, days)
    model, actual = model_sum / days, actual_sum / days
    bias = model - actual
    return NeutralCheck(abs(bias) <= MAX_BIAS, days, model, actual, bias)
