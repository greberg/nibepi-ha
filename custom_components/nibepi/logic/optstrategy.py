"""Heating strategy: the rules (Phase 1) or the cost optimiser's plan.

Per climate system the user picks a strategy:

- ``rules`` (default): the curve offset is the Phase 1 sum of the indoor,
  price and weather components plus the base adjustment, as before.
- ``optimiser``: while the gate (``optimizer.gate``) lets it, the planned
  offset for the current slot **replaces** that sum: the plan already
  accounts for price, forecast and indoor temperature, and its offsets are
  absolute register values. Below the hard comfort floor the gate gives
  ``base + max_up`` whatever the price or the model confidence. When the
  gate refuses, the rules sum is used and the reason is shown.

The off/shadow/active mode and the lease still decide whether anything is
written; ``optimiser`` in shadow mode only computes and shows.

Also here: the planned offset at a moment, how much of a plan's savings
falls into a time span (for "expected savings today"), and the hot water
plan's charge blocks as heating windows for the hot water controller.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo
from typing import List, Optional, Sequence

from .curve import TOTAL_LIMIT, CurvePlan, clamp, target_value
from .optimizer.gate import GateDecision
from .vvplan import HeatWindow

STRATEGY_RULES = "rules"
STRATEGY_OPTIMISER = "optimiser"
STRATEGIES = (STRATEGY_RULES, STRATEGY_OPTIMISER)

SOURCE_RULES = "rules"
SOURCE_OPTIMISER = "optimiser"
SOURCE_FLOOR = "comfort_floor"


@dataclass(frozen=True)
class StrategyChoice:
    strategy: str
    source: str  # rules / optimiser / comfort_floor
    total: float  # replaces the rules total in the curve plan
    reason: str


def choose_total(strategy: str, rules_total: float, gate: Optional[GateDecision]) -> StrategyChoice:
    """The total curve offset for this tick, and why."""
    if strategy != STRATEGY_OPTIMISER:
        return StrategyChoice(STRATEGY_RULES, SOURCE_RULES, rules_total, "rules")
    if gate is None:
        return StrategyChoice(strategy, SOURCE_RULES, rules_total, "optimiser not ready: no plan yet")
    if gate.use_optimizer and gate.offset is not None:
        source = SOURCE_OPTIMISER if gate.reason == "optimiser" else SOURCE_FLOOR
        total = clamp(float(gate.offset), -TOTAL_LIMIT, TOTAL_LIMIT)
        return StrategyChoice(strategy, source, total, gate.reason)
    return StrategyChoice(strategy, SOURCE_RULES, rules_total, "optimiser not used: %s" % gate.reason)


def apply_choice(rules: CurvePlan, choice: StrategyChoice, current: Optional[float]) -> CurvePlan:
    """The rules plan with the chosen total (same write threshold)."""
    if choice.source == SOURCE_RULES:
        return rules
    target = target_value(choice.total, current)
    return replace(rules, total=choice.total, target=target, write=current is None or target != current)


def offset_at(steps: Sequence, when: datetime) -> Optional[int]:
    """The planned offset of the step (``start``/``end``/``offset``) at ``when``."""
    for step in steps:
        if step.start <= when < step.end:
            return int(step.offset)
    return None


def hours_left(steps: Sequence, when: datetime) -> float:
    if not steps:
        return 0.0
    return max(0.0, (steps[-1].end - when).total_seconds() / 3600.0)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def savings_between(plan, start: datetime, end: datetime) -> float:
    """The part of ``plan.savings`` that falls in [start, end).

    The savings of a plan are only meaningful as a whole (heat moved
    between hours, the heat left at the end counted); they are spread over
    time in proportion to the baseline's energy cost.
    """
    steps = plan.baseline.steps
    total = sum(s.energy_cost for s in steps)
    if total <= 0 or end <= start:
        return 0.0
    s0, s1 = start.timestamp(), end.timestamp()
    part = 0.0
    for step in steps:
        t0, t1 = step.start.timestamp(), step.end.timestamp()
        if t1 > t0:
            part += step.energy_cost * _overlap(t0, t1, s0, s1) / (t1 - t0)
    return plan.savings * part / total


def time_share(start: datetime, end: datetime, span_start: datetime, span_end: datetime) -> float:
    """Share of [span_start, span_end) that lies in [start, end)."""
    length = (span_end - span_start).total_seconds()
    if length <= 0:
        return 0.0
    return _overlap(span_start.timestamp(), span_end.timestamp(), start.timestamp(), end.timestamp()) / length


def hw_windows(blocks: Sequence, now: datetime, tz: tzinfo) -> List[HeatWindow]:
    """The optimiser's charge blocks as the hot water controller's windows.

    Each ``charge`` block that has not ended becomes a window over the
    block with the block's target (the pump's normal or luxury stop
    temperature) and its comfort mode (economy/normal/luxury -> day mode
    1/2/3). Inside it the controller starts a charge with temporary lux
    and ends it when BT7 reaches the target, as for a VV-AI window.
    """
    out: List[HeatWindow] = []
    for block in blocks:
        if block.action != "charge" or block.end <= now or block.target is None:
            continue
        mode = max(1, min(3, int(block.mode) + 1))
        out.append(
            HeatWindow(
                start=block.start,
                end=block.end,
                peak=block.end,
                day=(block.start.astimezone(tz) - timedelta(hours=2)).date(),
                day_mode=mode,
                target=float(block.target),
            )
        )
    out.sort(key=lambda w: w.start)
    return out
