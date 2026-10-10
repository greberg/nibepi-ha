"""Per-system switches: the indoor, price and weather components, and the RMU40 feed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NibePiConfigEntry
from .const import COMPONENTS
from .entity import NibePiEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: NibePiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    entities: list = [
        ComponentSwitch(runtime, system, component)
        for system in runtime.systems
        for component in COMPONENTS
    ]
    entities.extend(RmuFeedSwitch(runtime, system) for system in runtime.systems)
    async_add_entities(entities)


class ComponentSwitch(NibePiEntity, SwitchEntity, RestoreEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime, system: str, component: str) -> None:
        super().__init__(runtime, "%s_enabled" % component, system)
        self.component = component

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        enabled = not (last is not None and last.state == STATE_OFF)
        self.runtime.set_enabled(self.system, self.component, enabled)

    @property
    def is_on(self) -> bool:
        return self.runtime.systems[self.system].enabled.get(self.component, True)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.runtime.set_enabled(self.system, self.component, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.runtime.set_enabled(self.system, self.component, False)
        self.async_write_ha_state()


class RmuFeedSwitch(NibePiEntity, SwitchEntity, RestoreEntity):
    """Send the room temperature to the pump's virtual RMU40 (default off)."""

    _attr_icon = "mdi:home-thermometer"

    def __init__(self, runtime, system: str) -> None:
        super().__init__(runtime, "rmu_feed", system)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        self.runtime.set_rmu_enabled(self.system, last is not None and last.state == STATE_ON)

    @property
    def is_on(self) -> bool:
        return self.runtime.systems[self.system].rmu_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.runtime.set_rmu_enabled(self.system, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.runtime.set_rmu_enabled(self.system, False)
        self.async_write_ha_state()
