"""Combine controller offsets into the curve offset (47011 / 47010).

Ports ``curveComponent`` and ``curveAdjust`` from config_node.js:

- Each component is clamped to ±10; a non-finite component counts as 0.
- The total is the sum of the components plus the manual base adjustment
  (``config.home.adjust_sN``, published by the gateway on
  ``<prefix>adjust_sN``), itself clamped to ±10, and the total is clamped
  to ±10.
- A new value is only written when the total moves more than 0.75 away from
  the value the register holds now, and is then rounded with JavaScript
  ``Math.round`` (half towards +infinity) because the register is an s8.

Deliberate differences:

- config_node debounced component updates with a 5 s timer and then read
  the register; here the runtime calls ``plan`` once per tick with the last
  known register value, which is the same comparison without the timer.
- When the register value is unknown (no MQTT value yet) the rounded total
  is planned, where config_node would not have written anything until the
  read succeeded. The lease publisher still only writes in active mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

from .inputs import Decision, js_round, to_float

COMPONENT_LIMIT = 10.0
TOTAL_LIMIT = 10.0
WRITE_HYSTERESIS = 0.75


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def component_value(value: object) -> float:
    """``curveComponent``: non-finite counts as 0, clamped to ±10."""
    number = to_float(value)
    if number is None:
        return 0.0
    return clamp(number, -COMPONENT_LIMIT, COMPONENT_LIMIT)


@dataclass(frozen=True)
class CurvePlan:
    """Result of combining the components for one system."""

    total: float  # clamped, unrounded sum
    target: int  # value the register should hold after this tick
    write: bool  # True when target differs from the current register value
    components: Dict[str, float] = field(default_factory=dict)
    reasons: Dict[str, str] = field(default_factory=dict)
    inputs_ok: Dict[str, bool] = field(default_factory=dict)


def combine(
    decisions: Mapping[str, Decision], base_adjust: Optional[float] = 0.0
) -> float:
    """Sum of clamped components plus the base adjustment, clamped to ±10."""
    total = 0.0
    for decision in decisions.values():
        total += component_value(decision.offset)
    if base_adjust is not None:
        total += component_value(base_adjust)
    return clamp(total, -TOTAL_LIMIT, TOTAL_LIMIT)


def target_value(
    total: float,
    current: Optional[float],
    hysteresis: float = WRITE_HYSTERESIS,
) -> int:
    """The register value after applying the write threshold.

    The total must move more than ``hysteresis`` away from ``current``
    before a new rounded value is chosen; otherwise the current value is
    kept. Unknown ``current`` gives the rounded total.
    """
    if current is None:
        return js_round(total)
    if total > current + hysteresis or total < current - hysteresis:
        return js_round(total)
    return int(js_round(current))


def plan(
    decisions: Mapping[str, Decision],
    current: Optional[float],
    base_adjust: Optional[float] = 0.0,
    hysteresis: float = WRITE_HYSTERESIS,
) -> CurvePlan:
    total = combine(decisions, base_adjust)
    target = target_value(total, current, hysteresis)
    write = current is None or target != current
    return CurvePlan(
        total=total,
        target=target,
        write=write,
        components={k: component_value(d.offset) for k, d in decisions.items()},
        reasons={k: d.reason for k, d in decisions.items()},
        inputs_ok={k: d.inputs_ok for k, d in decisions.items()},
    )
