"""Nibe F1x55 heat curve: outdoor temperature and curve offset -> supply target.

The pump computes a supply (flow) target from the outdoor temperature BT1
with the heat curve number (47007 for S1, 1..15) and shifts it with the
curve offset (47011, -10..10). The official curves are slightly bent
lines that all pass close to 20 °C supply at +20 °C outdoor; at -30 °C
they span roughly 30 °C (curve 1) to 70 °C (curve 15). A straight line
through (20, 20) with ``slope = slope_per_curve * curve`` and
``slope_per_curve`` ≈ 0.07 matches the middle curves to a few degrees:

    supply = room + bias + gain * offset + slope * (room - outdoor)

The offset is meant as a parallel shift that moves the room temperature
about one degree per step; for a curve that is a shift of the room
reference, so its supply effect is ``gain = 1 + slope`` (≈ 1.6 °C per step
on curve 9). The result is clamped to the pump's min/max supply.

All of this is approximate; ``fit_heat_curve`` calibrates bias, slope and
gain against logged BT2 supply temperatures (40008), BT1 and the offset.
Only the planner's *relative* effect of the offset matters much, because
the house model is fitted on the supply temperature the curve gives.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional, Sequence, Tuple

from .linalg import NormalEquations

DEFAULT_CURVE = 9.0
ROOM_REFERENCE = 20.0
DEFAULT_SLOPE_PER_CURVE = 0.07
DEFAULT_MIN_SUPPLY = 20.0
DEFAULT_MAX_SUPPLY = 60.0
OFFSET_LIMIT = 10


@dataclass(frozen=True)
class HeatCurve:
    curve: float = DEFAULT_CURVE  # 47007 (S1) / 47006 (S2)
    slope_per_curve: float = DEFAULT_SLOPE_PER_CURVE
    offset_gain: Optional[float] = None  # supply °C per offset step; None: 1 + slope
    bias: float = 0.0  # calibrated shift of the whole curve, °C
    min_supply: float = DEFAULT_MIN_SUPPLY
    max_supply: float = DEFAULT_MAX_SUPPLY
    room: float = ROOM_REFERENCE

    @property
    def slope(self) -> float:
        return self.slope_per_curve * self.curve

    @property
    def gain(self) -> float:
        return self.offset_gain if self.offset_gain is not None else 1.0 + self.slope

    def supply(self, t_out: float, offset: float) -> float:
        value = self.room + self.bias + self.gain * offset + self.slope * (self.room - t_out)
        return max(self.min_supply, min(self.max_supply, value))

    def offset_for_supply(self, t_out: float, supply: float) -> float:
        """Inverse of ``supply`` (unclamped), e.g. for the neutral offset."""
        return (supply - self.room - self.bias - self.slope * (self.room - t_out)) / self.gain


@dataclass(frozen=True)
class CurveFit:
    curve: HeatCurve
    samples: int
    rmse: float
    ok: bool
    reason: str


def fit_heat_curve(
    samples: Sequence[Tuple[float, float, float]],
    base: HeatCurve = HeatCurve(),
    prior_weight: float = 5.0,
) -> CurveFit:
    """Calibrate ``bias``, slope and gain from (outdoor, offset, supply).

    Use averaged samples (e.g. hourly means of BT1, the offset and BT2)
    taken while the pump heats. Samples near the clamps or with outdoor
    above +15 °C are skipped (supply floats there). A ridge pull towards
    ``base`` with ``prior_weight`` pseudo-samples keeps the gain sane when
    the offset hardly varied in the data.
    """
    eq = NormalEquations(3)
    used = 0
    for t_out, offset, supply in samples:
        if t_out > 15.0:
            continue
        if supply <= base.min_supply + 1.0 or supply >= base.max_supply - 1.0:
            continue
        eq.add([1.0, base.room - t_out, offset], supply)
        used += 1
    if used < 10:
        return CurveFit(base, used, 0.0, False, "too few heating samples (%d)" % used)
    prior = [base.room + base.bias, base.slope, base.gain]
    # Pseudo-samples: an intercept is cheap to learn, slope and gain need spread.
    ridge = [prior_weight, prior_weight * 100.0, prior_weight * 4.0]
    theta = eq.solve(ridge, prior)
    if theta is None:
        return CurveFit(base, used, 0.0, False, "singular fit")
    rmse = (eq.residual_sum(theta) / used) ** 0.5
    intercept, slope, gain = theta
    if slope <= 0 or gain <= 0.2 or gain > 6.0:
        return CurveFit(base, used, rmse, False, "implausible slope %.2f / gain %.2f" % (slope, gain))
    fitted = replace(
        base,
        bias=intercept - base.room,
        slope_per_curve=slope / base.curve if base.curve else base.slope_per_curve,
        offset_gain=gain,
    )
    return CurveFit(fitted, used, rmse, True, "ok")
