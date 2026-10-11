"""The ``nibepi.get_dashboard`` action: the dashboard with this entry's
entity ids, found in the entity registry by unique id.

The cards come from ``logic/dashboard.py``; this module only resolves the
entity ids and dumps the YAML with Home Assistant's dumper.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util.yaml import dump

from .const import DOMAIN, OPT_INDOOR_ENTITY
from .logic import dashboard
from .runtime import NibePiRuntime


class _Block(str):
    """A multi-line string (Jinja, JavaScript), dumped as a literal block."""


def _block_representer(dumper: Any, data: str) -> Any:
    # PyYAML falls back to a quoted string when a block cannot hold it.
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="|")


try:
    from homeassistant.util.yaml.dumper import add_representer

    add_representer(_Block, _block_representer)
    _BLOCKS = True
except ImportError:  # pragma: no cover - older or changed dumper module
    _BLOCKS = False


def _blocks(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _blocks(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_blocks(item) for item in value]
    if _BLOCKS and isinstance(value, str) and "\n" in value:
        return _Block(value)
    return value


def to_yaml(data: Dict[str, Any]) -> str:
    return dump(_blocks(data))


def resolve_entities(
    hass: HomeAssistant, runtime: NibePiRuntime, bus_health: Optional[str] = None
) -> Dict[str, str]:
    """Dashboard key -> entity id of every entity that exists and is enabled."""
    registry = er.async_get(hass)
    systems = list(runtime.systems)

    def usable(entity_id: Optional[str]) -> bool:
        if not entity_id:
            return False
        entry = registry.async_get(entity_id)
        if entry is not None:
            return entry.disabled_by is None
        return hass.states.get(entity_id) is not None

    found: Dict[str, str] = {}
    for key, (domain, unique_id) in dashboard.integration_unique_ids(runtime.entry.entry_id, systems).items():
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        if entity_id and usable(entity_id):
            found[key] = entity_id
    # The gateway's register sensors, through its MQTT discovery.
    for key, register in dashboard.gateway_registers(systems).items():
        entity_id = runtime.optimizer.gateway_entity(register)
        if entity_id and usable(entity_id):
            found[key] = entity_id
    # The indoor sensor the integration uses: the configured one, else BT50.
    for system in systems:
        indoor = runtime.opt(OPT_INDOOR_ENTITY, system)
        if not (isinstance(indoor, str) and usable(indoor)):
            indoor = found.get("bt50_" + system)
        if indoor:
            found["indoor_" + system] = indoor
    # The gateway announces its bus health sensor through discovery; an
    # explicit entity (an older gateway, a hand-made MQTT sensor) wins.
    if not bus_health:
        bus_health = runtime.optimizer.gateway_entity("bus_health")
    if bus_health and usable(bus_health):
        found["bus_health"] = bus_health
    return found


def dashboard_response(
    hass: HomeAssistant, runtime: NibePiRuntime, bus_health: Optional[str] = None
) -> Dict[str, Any]:
    entities = resolve_entities(hass, runtime, bus_health)
    options = {"title": runtime.entry.title or "NibePi"}
    wanted = set(dashboard.integration_unique_ids(runtime.entry.entry_id, runtime.systems))
    wanted.update(dashboard.gateway_registers(runtime.systems))
    wanted.update("indoor_" + system for system in runtime.systems)
    wanted.add("bus_health")
    apex = dashboard.build_apexcharts(entities, options)
    return {
        "yaml": to_yaml(dashboard.build_dashboard(entities, options)),
        "apexcharts_yaml": to_yaml(apex) if apex is not None else None,
        "entities": dict(sorted(entities.items())),
        "missing": sorted(wanted - set(entities)),
    }
