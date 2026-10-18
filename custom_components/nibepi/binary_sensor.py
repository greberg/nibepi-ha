"""Gateway online (``<prefix>status``, retained, LWT; live ``gateway/state``).

Its ``entities`` attribute is the map the NibePi cards resolve entities
with (logic/cards.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import NibePiConfigEntry
from .const import OPT_INDOOR_ENTITY
from .entity import NibePiEntity
from .logic.cards import card_entities
from .logic.gateway import status_hint


async def async_setup_entry(
    hass: HomeAssistant, entry: NibePiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([GatewayOnline(entry.runtime_data)])


class GatewayOnline(NibePiEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _unrecorded_attributes = frozenset({"entities", "systems"})
    # The gateway's sensors are looked up in the entity registry at most
    # this often (seconds); the integration's own entities are always live.
    GATEWAY_LOOKUP_EVERY = 60

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "gateway_online")
        self._gateway_ids: Dict[Any, Optional[str]] = {}
        self._gateway_ids_at: Optional[datetime] = None

    def _gateway_entity(self, register: Any) -> Optional[str]:
        """The gateway's discovered sensor for ``register``, if it has a state
        (a disabled entity has none)."""
        if register not in self._gateway_ids:
            entity_id = self.runtime.optimizer.gateway_entity(register)
            self._gateway_ids[register] = entity_id if entity_id and self.hass.states.get(entity_id) else None
        return self._gateway_ids[register]

    def _card_entities(self) -> Dict[str, str]:
        runtime = self.runtime
        now = dt_util.utcnow()
        if self._gateway_ids_at is None or (now - self._gateway_ids_at).total_seconds() >= self.GATEWAY_LOOKUP_EVERY:
            self._gateway_ids = {}
            self._gateway_ids_at = now
        indoor = {}
        for system in runtime.systems:
            configured = runtime.opt(OPT_INDOOR_ENTITY, system)
            indoor[system] = configured if isinstance(configured, str) and configured else None
        return card_entities(runtime.entity_ids, self._gateway_entity, runtime.systems, indoor)

    @property
    def is_on(self) -> Optional[bool]:
        return self.runtime.gateway_online

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        runtime = self.runtime
        state = runtime.gateway_state
        last = runtime.last_message_at
        attrs: Dict[str, Any] = {
            "status_topic": runtime.status_topic,
            "last_status": runtime.status_payload,
            "last_message_at": last.isoformat() if last is not None else None,
            "version": state.get("version"),
            "core": state.get("core"),
            "raw_set": state.get("raw_set"),
            "last_data": state.get("lastData"),
            "leases": state.get("leases"),
            "systems": list(runtime.systems),
            "entities": self._card_entities(),
        }
        if runtime.status_payload is None:
            attrs["hint"] = status_hint(runtime.status_topic)
        return self.localised(attrs)
