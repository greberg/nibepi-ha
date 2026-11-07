"""Learning steps: deliberate curve offset steps so the house model can learn.

The house fit (``optimizer/house.py``) tells the emitters' gain ``b`` apart
from the losses and internal gains only through offset movement: the
supply otherwise follows the outdoor temperature through the curve, and
``a``, ``b`` and ``d`` move together. A rules strategy that holds the
offset at -1 day after day gives the fit nothing to separate them with,
so the model never reaches the confidence the gate needs, and the
optimiser never steers: the system stays passive for good.

While the optimiser strategy is chosen and the model is below the gate's
level, a step is added to the rules total at night: ``STEP`` for
``SLOT_HOURS``, then the opposite step for as long, starting at
``START_HOUR`` local time. The order alternates night by night, so the
step is not confused with the time of night. The night is used because
it is free of sun and most household gains, and the pair costs about
nothing: what the warm half adds, the cool half saves.

Comfort comes first: a warming step is skipped when the room is already
near the band's upper edge, a cooling step when it is near the lower
edge, and a skipped slot stays skipped until it ends (no flapping at the
threshold). Without an indoor temperature no step is taken.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from .optimizer.gate import LEVELS
from .texts import Msg, msg

STEP = 2.0
SLOT_HOURS = 3
START_HOUR = 23
# Margins (°C) to the comfort band edges: no step that heads past them.
WARM_MARGIN = 0.3
COLD_MARGIN = 0.3


@dataclass(frozen=True)
class LearnStep:
    offset: float  # added to the rules total (0 when skipped)
    reason: Msg
    slot_start: datetime  # local start of the slot


def learning_needed(fit_ok: bool, level: str, min_level: str) -> bool:
    """True while the house model is below the level the gate needs."""
    if not fit_ok:
        return True
    try:
        return LEVELS.index(level) < LEVELS.index(min_level)
    except ValueError:
        return True


def slot_at(local: datetime, step: float = STEP) -> Optional[Tuple[datetime, float]]:
    """(slot start, signed step) of the learning slot at ``local``, else None."""
    since = (local.hour - START_HOUR) % 24
    if since >= 2 * SLOT_HOURS:
        return None
    window = local.replace(minute=0, second=0, microsecond=0) - timedelta(hours=since)
    first = since < SLOT_HOURS
    warm_first = window.date().toordinal() % 2 == 0
    up = first == warm_first
    start = window if first else window + timedelta(hours=SLOT_HOURS)
    return start, (step if up else -step)


class LearnSteps:
    """Per system: the step at this moment, with skipped slots remembered."""

    def __init__(self, step: float = STEP) -> None:
        self.step = step
        self._skipped: Dict[str, Tuple[datetime, Msg]] = {}

    def at(self, system: str, local: datetime, t_in: Optional[float], lower: float,
           upper: float) -> Optional[LearnStep]:
        slot = slot_at(local, self.step)
        if slot is None:
            self._skipped.pop(system, None)
            return None
        start, offset = slot
        skipped = self._skipped.get(system)
        if skipped is not None and skipped[0] == start:
            return LearnStep(0.0, skipped[1], start)
        why = None
        if t_in is None:
            why = msg("learn.no_indoor")
        elif offset > 0 and t_in >= upper - WARM_MARGIN:
            why = msg("learn.skip_warm", t_in=t_in)
        elif offset < 0 and t_in <= lower + COLD_MARGIN:
            why = msg("learn.skip_cold", t_in=t_in)
        if why is not None:
            self._skipped[system] = (start, why)
            return LearnStep(0.0, why, start)
        end = start + timedelta(hours=SLOT_HOURS)
        return LearnStep(offset, msg("learn.step", offset=offset, end=end.strftime("%H:%M")), start)
