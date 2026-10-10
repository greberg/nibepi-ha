"""Base entity: device, unique id, translation key, update signal."""

from __future__ import annotations

from typing import Optional

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import signal_update
from .runtime import NibePiRuntime


class NibePiEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime: NibePiRuntime, key: str, system: Optional[str] = None) -> None:
        self.runtime = runtime
        self.system = system
        entry_id = runtime.entry.entry_id
        self._attr_unique_id = "%s_%s_%s" % (entry_id, system, key) if system else "%s_%s" % (entry_id, key)
        self._attr_translation_key = key
        if system:
            self._attr_translation_placeholders = {"system": system.upper()}
        self._attr_device_info = runtime.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_update(self.runtime.entry.entry_id), self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
