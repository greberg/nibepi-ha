"""The entity map the NibePi cards read from the Gateway entity.

The custom Lovelace cards (``frontend/nibepi-cards.js``) run in the browser,
where the entity registry has no unique ids and per-system entities share a
translation key. The Gateway binary sensor therefore carries an ``entities``
attribute: card key -> entity id, built here.

Keys:

- the integration's entities by translation key, ``<key>_<system>`` for a
  per-system entity (``planned_offset_s1``, ``price_level``);
- the gateway's MQTT-discovered sensors in ``GATEWAY_SENSORS`` and, per
  system, ``SYSTEM_SENSORS`` (``bt50_s1``, ``supply_s2``);
- ``indoor_<system>``: the indoor sensor the integration uses, the
  configured entity, else BT50.

Plain Python, no Home Assistant imports.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Mapping, Optional

# The gateway's discovered sensors: key -> register (unique id
# ``<device>_<register>``; ``bus_health`` is the Bus errors sensor).
GATEWAY_SENSORS: Dict[str, Any] = {
    "outdoor": 40004,  # BT1
    "bt7_hw_top": 40013,
    "bt6_hw_load": 40014,
    "bt3_return": 40012,
    "degree_minutes": 43005,
    "compressor_frequency": 43136,
    "compressor_power": 43141,
    "priority": 43086,
    "bus_health": "bus_health",
}
SYSTEM_SENSORS: Dict[str, Dict[str, int]] = {
    "bt50": {"s1": 40033, "s2": 40032},
    "supply": {"s1": 40008, "s2": 40007},  # BT2 / EP21-BT2
}


def gateway_sensor_keys(systems: Iterable[str]) -> Dict[str, Any]:
    """Card key -> register of the gateway's discovered sensors."""
    found = dict(GATEWAY_SENSORS)
    for system in systems:
        for name, regs in SYSTEM_SENSORS.items():
            if system in regs:
                found["%s_%s" % (name, system)] = regs[system]
    return found


def card_entities(
    own: Mapping[str, str],
    gateway_entity: Callable[[Any], Optional[str]],
    systems: Iterable[str],
    indoor_entities: Mapping[str, Optional[str]],
) -> Dict[str, str]:
    """``own``: the integration's entities (card key -> entity id);
    ``gateway_entity``: register -> entity id of the gateway's sensor or
    None; ``indoor_entities``: system -> configured indoor entity or None."""
    systems = list(systems)
    found: Dict[str, str] = {k: v for k, v in own.items() if v}
    for key, register in gateway_sensor_keys(systems).items():
        entity_id = gateway_entity(register)
        if entity_id:
            found[key] = entity_id
    for system in systems:
        indoor = indoor_entities.get(system) or found.get("bt50_" + system)
        if indoor:
            found["indoor_" + system] = indoor
    return dict(sorted(found.items()))
