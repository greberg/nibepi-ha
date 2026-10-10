"""Mode select per system: off / shadow / active (restored on restart)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NibePiConfigEntry
from .entity import NibePiEntity
from .logic.lease import MODE_SHADOW, MODES


async def async_setup_entry(
    hass: HomeAssistant, entry: NibePiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    async_add_entities(ModeSelect(runtime, system) for system in runtime.systems)


class ModeSelect(NibePiEntity, SelectEntity, RestoreEntity):
    _attr_options = list(MODES)
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime, system: str) -> None:
        super().__init__(runtime, "mode", system)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        mode = last.state if last is not None and last.state in MODES else MODE_SHADOW
        self.runtime.set_mode(self.system, mode)

    @property
    def current_option(self) -> str:
        return self.runtime.systems[self.system].mode

    async def async_select_option(self, option: str) -> None:
        if option not in MODES:
            raise ValueError("Unknown mode %s" % option)
        self.runtime.set_mode(self.system, option)
        self.async_write_ha_state()
