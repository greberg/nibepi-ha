"""Is the NibePi gateway online?

Inputs, from the MQTT contract in docs/ha-migration.md of the workspace:

- ``<prefix>status``: ``online`` / ``offline``, retained, and the gateway's
  last will. This is the primary signal.
- ``<prefix>gateway/state``: retained, republished once a minute. A copy
  delivered live (not the retained replay on subscribe) is evidence that
  the gateway is running right now, even when the status topic says
  ``offline`` (an old last will) or never arrived.

Before anything arrived the answer is unknown (None) for
``STATUS_TIMEOUT`` after setup, and offline (False) after that: the gateway
is not running, or not on this broker or topic prefix.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

STATUS_TIMEOUT = timedelta(seconds=120)
# gateway/state is published once a minute; allow one missed publish.
STATE_FRESH = timedelta(seconds=180)


def state_is_fresh(state_live_at: Optional[datetime], now: datetime) -> bool:
    return state_live_at is not None and timedelta(0) <= now - state_live_at <= STATE_FRESH


def gateway_online(
    status: Optional[str],
    state_live_at: Optional[datetime],
    started: Optional[datetime],
    now: datetime,
) -> Optional[bool]:
    """``status`` is the last ``status`` payload (None: none received),
    ``state_live_at`` when a live ``gateway/state`` last arrived."""
    if state_is_fresh(state_live_at, now):
        return True
    if status is not None:
        return status.strip().lower() == "online"
    if started is not None and now - started >= STATUS_TIMEOUT:
        return False
    return None


def status_hint(topic: str) -> str:
    return (
        "No message on %s: is the NibePi gateway running and connected to this "
        "MQTT broker with the same topic prefix?" % topic
    )
