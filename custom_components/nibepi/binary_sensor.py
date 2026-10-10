"""Gateway online (``<prefix>status``, retained, LWT)."""

from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NibePiConfigEntry
from .entity import NibePiEntity


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
        state = self.runtime.gateway_state
        return {
            "version": state.get("version"),
            "core": state.get("core"),
            "raw_set": state.get("raw_set"),
            "last_data": state.get("lastData"),
            "leases": state.get("leases"),
        }
