"""Explain a heating plan in blocks of equal offset.

Each block gets a reason code and a sentence (a ``texts.Msg``):

- ``cheap``: above the base offset while the price is below the horizon
  average, storing heat (names the next pricier block when there is one);
- ``expensive``: below the base offset while the price is above average,
  coasting on stored heat;
- ``comfort_low`` / ``comfort_high``: up because the indoor temperature
  is near the lower band edge, or down because it is above the band;
- ``forecast``: up ahead of colder weather, or down for milder weather or
  sun, when the price does not explain it;
- ``neutral``: at the base offset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import List, Sequence

from ..texts import Msg, msg
from .planner import ComfortSettings, Plan, PlanStep

CHEAP_RATIO = 0.95
EXPENSIVE_RATIO = 1.05


@dataclass(frozen=True)
class Block:
    start: datetime
    end: datetime
    offset: int
    delta: int  # offset - base
    price: float  # time-weighted mean
    t_in_end: float
    reason: str
    text: Msg


def _mean(steps: Sequence[PlanStep], attr: str) -> float:
    total = sum((s.end - s.start).total_seconds() for s in steps) or 1.0
    return sum(getattr(s, attr) * (s.end - s.start).total_seconds() for s in steps) / total


def explain_plan(
    plan: Plan, comfort: ComfortSettings, tz: tzinfo = timezone.utc, unit: str = "SEK/kWh"
) -> List[Block]:
    steps = plan.plan.steps
    if not steps:
        return []
    base = plan.settings.base_offset
    avg_price = _mean(steps, "price")
    avg_out = _mean(steps, "t_out")
    groups: List[List[PlanStep]] = []
    for step in steps:
        if groups and groups[-1][-1].offset == step.offset:
            groups[-1].append(step)
        else:
            groups.append([step])

    def hhmm(when: datetime) -> str:
        return when.astimezone(tz).strftime("%H:%M")

    blocks: List[Block] = []
    for gi, group in enumerate(groups):
        offset = group[0].offset
        delta = offset - base
        price = _mean(group, "price")
        t_out = _mean(group, "t_out")
        solar = _mean(group, "solar")
        t_end = group[-1].t_in_end
        t_min = min(s.t_in_end for s in group)
        span = "%s-%s" % (hhmm(group[0].start), hhmm(group[-1].end))
        later = [g for g in groups[gi + 1:] if _mean(g, "price") > price * 1.15]
        price_txt = msg("explain.price", price=price, unit=unit, avg=avg_price)
        if delta > 0:
            if price <= avg_price * CHEAP_RATIO:
                reason = "cheap"
                if later:
                    why = msg("explain.cheap_before", price=price_txt, later=hhmm(later[0][0].start))
                else:
                    why = msg("explain.cheap", price=price_txt)
            elif t_min <= comfort.lower + 0.2:
                reason = "comfort_low"
                why = msg("explain.comfort_low", lower=comfort.lower)
            elif t_out < avg_out - 1.0:
                reason = "forecast"
                why = msg("explain.colder", t_out=t_out)
            else:
                reason = "cheap"
                why = msg("explain.storing", price=price_txt)
        elif delta < 0:
            if price >= avg_price * EXPENSIVE_RATIO:
                reason = "expensive"
                why = msg("explain.expensive", price=price_txt)
            elif t_end > comfort.upper - 0.1:
                reason = "comfort_high"
                why = msg("explain.comfort_high", upper=comfort.upper)
            elif t_out > avg_out + 1.0 or solar > 0.2:
                reason = "forecast"
                why = msg("explain.milder", t_out=t_out)
            else:
                reason = "expensive"
                why = msg("explain.saving", price=price_txt)
        else:
            reason = "neutral"
            why = msg("explain.neutral")
        text = msg("explain.line", span=span, offset=int(offset), why=why, t_end=t_end, at=hhmm(group[-1].end))
        blocks.append(Block(group[0].start, group[-1].end, offset, delta, price, t_end, reason, text))
    return blocks


def summary(plan: Plan, unit: str = "SEK") -> Msg:
    p, b = plan.plan, plan.baseline
    return msg(
        "explain.summary", kwh=p.electric_kwh, cost=p.energy_cost, unit=unit, base=int(plan.settings.base_offset),
        base_kwh=b.electric_kwh, base_cost=b.energy_cost, savings=plan.savings, t_min=p.min_t_in, t_max=p.max_t_in,
        changes=int(p.changes),
    )
