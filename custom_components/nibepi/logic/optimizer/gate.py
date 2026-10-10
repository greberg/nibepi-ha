"""Whether the optimiser's offset may replace the Phase 1 price logic.

The plan is only a target: the runtime still sends it through the curve
offset lease (TTL, renewals, gateway fall-back), and the mode switch
(off/shadow/active) still applies. On top of that:

- The house model must be fitted with at least ``min_level`` confidence
  (default high); otherwise use the ported Phase 1 price-level offset.
- The horizon must cover at least ``min_horizon_h`` hours of prices.
- The indoor reading must be usable (the runtime passes the reason).
- Hard comfort floor: below the floor the optimiser's own value is
  overridden by ``base + max_up`` whatever the price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .house import HouseFit

LEVELS = ("low", "medium", "high")


@dataclass(frozen=True)
class GateDecision:
    use_optimizer: bool
    offset: Optional[int]
    reason: str


def gate(
    fit: Optional[HouseFit],
    planned_offset: Optional[int],
    horizon_h: float,
    t_in: Optional[float],
    floor: float,
    base_offset: int,
    max_up: int,
    indoor_problem: Optional[str] = None,
    min_level: str = "high",
    min_horizon_h: float = 6.0,
) -> GateDecision:
    if indoor_problem is not None or t_in is None:
        return GateDecision(False, None, "indoor sensor %s" % (indoor_problem or "missing"))
    if t_in < floor:
        return GateDecision(True, base_offset + max_up, "below comfort floor %.1f °C: heating" % floor)
    if fit is None or not fit.ok:
        return GateDecision(False, None, "house model not fitted (%s)" % (fit.reason if fit else "no data"))
    if LEVELS.index(fit.level) < LEVELS.index(min_level):
        return GateDecision(False, None, "model confidence %s (%.2f) below %s" % (fit.level, fit.confidence, min_level))
    if horizon_h < min_horizon_h:
        return GateDecision(False, None, "only %.1f h of prices" % horizon_h)
    if planned_offset is None:
        return GateDecision(False, None, "no plan")
    return GateDecision(True, planned_offset, "optimiser")
