"""Feed a room temperature to the pump's virtual RMU40 room unit.

New in the HA component; NibePi's Node-RED version wrote the RMU40
register itself. The gateway emulates an RMU40 per climate system and
holds its room temperature register (10060 for S1, 10160 for S2) through
the same lease mechanism as the curve offset (``lease.py``):

- ``<prefix>lease/<reg>/set`` ``{"value": 21.4, "ttl": 900, "owner": "ha"}``,
  °C with one decimal, lease limits 5..35. The gateway writes a changed
  value at most once per 60 s; a renewal with the same value only extends.
- On release or expiry the gateway writes the system's room setpoint, so
  room temperature == setpoint and the pump's room correction is zero.
- A rejection comes back in ``lease/<reg>/state`` with ``error`` set, e.g.
  when RMU40 is not enabled for the system in the pump (menu 5.2).

Rules implemented by ``rmu_decide``:

- Feed on and the room temperature valid (not unavailable/unknown, within
  -30..50 °C, fresh per ``inputs.usable``): set the value rounded to 0.1
  (clamped to 5..35) when it changed by 0.1 or more, otherwise renew every
  5 minutes.
- Feed off, or the temperature stale or invalid: release (once). Unlike
  the curve offset, a bad input releases at once instead of letting the
  lease run out: a stale room temperature would keep steering the pump.
- The curve-offset mode (off/shadow/active) does not apply: the feed has
  its own switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Tuple

from .inputs import Reading, usable
from .lease import MODE_ACTIVE, MODE_OFF, LeaseAction, LeaseManager

RMU_LOW = 5.0
RMU_HIGH = 35.0
RMU_DECIMALS = 1
VALID_LOW = -30.0
VALID_HIGH = 50.0


def rmu_lease_manager(
    register: int, owner: str = "ha", clock: Optional[Callable[[], datetime]] = None
) -> LeaseManager:
    return LeaseManager(register, owner=owner, clock=clock, low=RMU_LOW, high=RMU_HIGH, decimals=RMU_DECIMALS)


@dataclass(frozen=True)
class RoomTemperature:
    value: Optional[float]  # None: do not feed
    reason: str


def room_temperature(
    reading: Optional[Reading], now: datetime, sensor_timeout_min: Optional[float] = None
) -> RoomTemperature:
    problem = usable(reading, now, sensor_timeout_min)
    if problem is not None:
        return RoomTemperature(None, "room temperature %s" % problem)
    assert reading is not None
    value = float(reading.value)
    if not VALID_LOW <= value <= VALID_HIGH:
        return RoomTemperature(None, "room temperature implausible (%.1f)" % value)
    return RoomTemperature(value, "room temperature %.1f" % value)


def rmu_decide(
    lease: LeaseManager,
    enabled: bool,
    reading: Optional[Reading],
    now: datetime,
    sensor_timeout_min: Optional[float] = None,
) -> Tuple[Optional[LeaseAction], RoomTemperature]:
    """The lease action for this tick (None: nothing to send), and why."""
    if not enabled:
        room = RoomTemperature(None, "feed off")
    else:
        room = room_temperature(reading, now, sensor_timeout_min)
    mode = MODE_ACTIVE if room.value is not None else MODE_OFF
    return lease.decide(mode, room.value, True), room
