"""The integration's own read-only pump sensors.

Key pump values (BT1, BT7, BT6, BT2, BT3, BT50, degree minutes, the
compressor and the priority) become sensors of the integration, fed from
the register values it already receives for control, so they do not
depend on the gateway's MQTT discovery (``mqtt.discovery``, off by
default).

A sensor's key is its translation key and, with ``_<system>`` for a
per-system one, its card key (logic/cards.py).

Plain Python, no Home Assistant imports.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional

# Shared sensors: key -> register.
PUMP_SENSORS: Dict[str, int] = {
    "outdoor": 40004,  # BT1
    "bt7_hw_top": 40013,
    "bt6_hw_load": 40014,
    "bt3_return": 40012,
    "degree_minutes": 43005,
    "compressor_frequency": 43136,
    "compressor_power": 43141,  # W
    "priority": 43086,
}
# Per-system sensors: key -> {system: register}.
SYSTEM_PUMP_SENSORS: Dict[str, Dict[str, int]] = {
    "supply": {"s1": 40008, "s2": 40007},  # BT2 / EP21-BT2
    "bt50": {"s1": 40033, "s2": 40032},  # room temperature
}
# The Bus errors sensor (``<prefix>bus_health``, not a register).
BUS_HEALTH = "bus_health"

# A value older than this (or none at all) makes the sensor unavailable.
# The gateway publishes every polled register on each poll and the runtime
# asks again for one without a value for 30 min (logic/registers.py), so a
# working gateway never gets near it.
STALE_AFTER = timedelta(minutes=60)

# 43086: what the compressor works for.
PRIORITY_STATES: Dict[int, str] = {
    10: "off",
    20: "hot_water",
    30: "heating",
    40: "pool",
    41: "pool_2",
    50: "transfer",
    60: "cooling",
}


def pump_registers(systems: Iterable[str]) -> List[int]:
    """Every register an own sensor of ``systems`` shows; sorted."""
    regs = set(PUMP_SENSORS.values())
    for system in systems:
        for by_system in SYSTEM_PUMP_SENSORS.values():
            if system in by_system:
                regs.add(by_system[system])
    return sorted(regs)


def pump_sensor_keys(systems: Iterable[str]) -> Dict[str, Any]:
    """Card key -> register (``bus_health`` for the Bus errors sensor)."""
    found: Dict[str, Any] = dict(PUMP_SENSORS)
    for system in systems:
        for key, by_system in SYSTEM_PUMP_SENSORS.items():
            if system in by_system:
                found["%s_%s" % (key, system)] = by_system[system]
    found[BUS_HEALTH] = BUS_HEALTH
    return found


def own_unique_id(entry_id: str, key: str, system: Optional[str] = None) -> str:
    """The unique id of an integration entity (entity.py's format)."""
    return "%s_%s_%s" % (entry_id, system, key) if system else "%s_%s" % (entry_id, key)


def is_fresh(received: Optional[datetime], now: datetime, stale_after: timedelta = STALE_AFTER) -> bool:
    """A value arrived, and not longer than ``stale_after`` ago."""
    return received is not None and now - received <= stale_after


def priority_state(value: Optional[float]) -> Optional[str]:
    """43086 as an enum state; None for a value without a name."""
    if value is None:
        return None
    try:
        return PRIORITY_STATES.get(int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def parse_bus_health(payload: Any) -> Optional[Dict[str, Any]]:
    """``<prefix>bus_health`` -> {errors, ok, chksum, no_respond}, or None.

    The gateway publishes ``{timestamp, ok, chksum, no_respond, errors}``
    once a minute; ``errors`` is CHKSUM frames plus reads without a reply.
    """
    try:
        data = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else payload
    except (TypeError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None

    def count(name: str) -> Optional[int]:
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)

    errors = count("errors")
    if errors is None:
        chksum, no_respond = count("chksum"), count("no_respond")
        if chksum is None or no_respond is None:
            return None
        errors = chksum + no_respond
    return {"errors": errors, "ok": count("ok"), "chksum": count("chksum"), "no_respond": count("no_respond")}

