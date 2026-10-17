"""Button: run a legionella cycle now (hot water mode active only)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NibePiConfigEntry
from .entity import NibePiEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: NibePiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([LegionellaRunButton(entry.runtime_data)])


class LegionellaRunButton(NibePiEntity, ButtonEntity):
    """Starts a legionella cycle at once; refused unless hot water mode is
    active and the guard is on (the press then fails with the reason)."""

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "hw_legionella_run")

    async def async_press(self) -> None:
        await self.runtime.async_run_legionella()
