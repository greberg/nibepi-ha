"""Gateway online (``<prefix>status``, retained, LWT; live ``gateway/state``)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NibePiConfigEntry
from .entity import NibePiEntity
from .logic.gateway import status_hint


async def async_setup_entry(
    hass: HomeAssistant, entry: NibePiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([GatewayOnline(entry.runtime_data)])


class GatewayOnline(NibePiEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "gateway_online")

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
        }
        if runtime.status_payload is None:
            attrs["hint"] = status_hint(runtime.status_topic)
        return attrs
