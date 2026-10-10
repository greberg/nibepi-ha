"""Explain a heating plan in blocks of equal offset.

Each block gets a reason code and an English sentence:

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
    text: str


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
        price_txt = "%.2f %s vs %.2f average" % (price, unit, avg_price)
        if delta > 0:
            if price <= avg_price * CHEAP_RATIO:
                reason = "cheap"
                text = "%s offset %+d: cheap (%s), storing heat" % (span, offset, price_txt)
                if later:
                    text += " before the dearer %s" % hhmm(later[0][0].start)
            elif t_min <= comfort.lower + 0.2:
                reason = "comfort_low"
                text = "%s offset %+d: keeping indoor above %.1f °C" % (span, offset, comfort.lower)
            elif t_out < avg_out - 1.0:
                reason = "forecast"
                text = "%s offset %+d: colder outside (%.1f °C) ahead" % (span, offset, t_out)
            else:
                reason = "cheap"
                text = "%s offset %+d: storing heat (%s)" % (span, offset, price_txt)
        elif delta < 0:
            if price >= avg_price * EXPENSIVE_RATIO:
                reason = "expensive"
                text = "%s offset %+d: expensive (%s), coasting on stored heat" % (span, offset, price_txt)
            elif t_end > comfort.upper - 0.1:
                reason = "comfort_high"
                text = "%s offset %+d: indoor above %.1f °C" % (span, offset, comfort.upper)
            elif t_out > avg_out + 1.0 or solar > 0.2:
                reason = "forecast"
                text = "%s offset %+d: milder or sunny (%.1f °C outside)" % (span, offset, t_out)
            else:
                reason = "expensive"
                text = "%s offset %+d: saving ahead of later heating (%s)" % (span, offset, price_txt)
        else:
            reason = "neutral"
            text = "%s offset %+d: normal operation" % (span, offset)
        text += ", indoor %.1f °C at %s" % (t_end, hhmm(group[-1].end))
        blocks.append(Block(group[0].start, group[-1].end, offset, delta, price, t_end, reason, text))
    return blocks


def summary(plan: Plan, unit: str = "SEK") -> str:
    p, b = plan.plan, plan.baseline
    return (
        "Planned %.1f kWh for %.2f %s, base offset %+d alone %.1f kWh for %.2f %s: "
        "saves %.2f %s (heat left in the house counted). Indoor %.1f-%.1f °C, %d changes."
        % (p.electric_kwh, p.energy_cost, unit, plan.settings.base_offset, b.electric_kwh, b.energy_cost,
           unit, plan.savings, unit, p.min_t_in, p.max_t_in, p.changes)
    )
