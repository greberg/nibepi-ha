"""Which gateway registers to ask for again.

The integration asks the gateway to poll each register it needs with
``<prefix><reg>/add`` and for a first value with ``<prefix><reg>/get``.
Register values on MQTT are not retained, so a request sent while the
gateway was not running (Home Assistant set up first, gateway restarted
with an older config) is lost and the value never arrives. The runtime
therefore repeats the request for registers without a fresh value, at most
every ``retry_every`` per register, and for all of them when the gateway
comes online.

Plain Python, no Home Assistant imports.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, List, Mapping, Optional

STALE_AFTER = timedelta(minutes=30)
RETRY_EVERY = timedelta(minutes=10)


def registers_to_request(
    registers: Iterable[int],
    last_value: Mapping[int, datetime],
    last_request: Mapping[int, datetime],
    now: datetime,
    stale_after: timedelta = STALE_AFTER,
    retry_every: timedelta = RETRY_EVERY,
) -> List[int]:
    """Registers with no value, or none newer than ``stale_after``, that were
    not requested within ``retry_every``; sorted."""
    due: List[int] = []
    for reg in sorted(set(registers)):
        seen: Optional[datetime] = last_value.get(reg)
        if seen is not None and now - seen <= stale_after:
            continue
        asked: Optional[datetime] = last_request.get(reg)
        if asked is not None and now - asked < retry_every:
            continue
        due.append(reg)
    return due


def came_online(previous: Optional[str], current: Optional[str]) -> bool:
    """True when the gateway's status topic changes to ``online``."""
    def norm(value: Optional[str]) -> str:
        return (value or "").strip().lower()

    return norm(current) == "online" and norm(previous) != "online"
