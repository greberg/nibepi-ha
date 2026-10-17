"""The entity map the NibePi cards read from the Gateway entity.

The custom Lovelace cards (``frontend/nibepi-cards.js``) run in the browser,
where the entity registry has no unique ids and per-system entities share a
translation key. The Gateway binary sensor therefore carries an ``entities``
attribute: card key -> entity id, built here.

Keys:

- the integration's entities by translation key, ``<key>_<system>`` for a
  per-system entity (``planned_offset_s1``, ``price_level``), among them
  its own pump sensors (logic/pump.py: ``outdoor``, ``bt7_hw_top``,
  ``supply_s1``, ``bt50_s2``, ``bus_health``, ...);
- for a pump sensor key without an own entity (disabled), the gateway's
  MQTT-discovered sensor for the same register, when the gateway's
  discovery is on;
- ``indoor_<system>``: the indoor sensor the integration uses, the
  configured entity, else BT50.

Plain Python, no Home Assistant imports.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, Mapping, Optional

from .pump import pump_sensor_keys

# Card key -> register of the gateway's discovered sensors (unique id
# ``<device>_<register>``; ``bus_health`` is the Bus errors sensor).
gateway_sensor_keys = pump_sensor_keys


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
        if key in found:
            continue  # the integration's own sensor
        entity_id = gateway_entity(register)
        if entity_id:
            found[key] = entity_id
    for system in systems:
        indoor = indoor_entities.get(system) or found.get("bt50_" + system)
        if indoor:
            found["indoor_" + system] = indoor
    return dict(sorted(found.items()))
