"""Selects (restored): per system and for hot water the mode, off / shadow /
active; per system the heating strategy, rules / optimiser."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import NibePiConfigEntry
from .entity import NibePiEntity
from .logic.lease import MODE_SHADOW, MODES
from .logic.optstrategy import STRATEGIES, STRATEGY_RULES


async def async_setup_entry(
    hass: HomeAssistant, entry: NibePiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    entities: list = [ModeSelect(runtime, system) for system in runtime.systems]
    entities.extend(StrategySelect(runtime, system) for system in runtime.systems)
    entities.append(HotWaterModeSelect(runtime))
    async_add_entities(entities)


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


class HotWaterModeSelect(NibePiEntity, SelectEntity, RestoreEntity):
    """Hot water control mode, separate from the curve-offset modes."""

    _attr_options = list(MODES)
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:water-boiler"

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "hw_mode")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        mode = last.state if last is not None and last.state in MODES else MODE_SHADOW
        self.runtime.set_hw_mode(mode)

    @property
    def current_option(self) -> str:
        return self.runtime.hotwater.mode

    async def async_select_option(self, option: str) -> None:
        if option not in MODES:
            raise ValueError("Unknown mode %s" % option)
        self.runtime.set_hw_mode(option)
        self.async_write_ha_state()


class StrategySelect(NibePiEntity, SelectEntity, RestoreEntity):
    """Rules (Phase 1 sum, default) or the cost optimiser's plan.

    The mode still decides whether anything is written: optimiser in
    shadow mode only computes and shows.
    """

    _attr_options = list(STRATEGIES)
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:strategy"

    def __init__(self, runtime, system: str) -> None:
        super().__init__(runtime, "strategy", system)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        strategy = last.state if last is not None and last.state in STRATEGIES else STRATEGY_RULES
        self.runtime.optimizer.set_strategy(self.system, strategy)

    @property
    def current_option(self) -> str:
        return self.runtime.optimizer.strategy.get(self.system, STRATEGY_RULES)

    async def async_select_option(self, option: str) -> None:
        if option not in STRATEGIES:
            raise ValueError("Unknown strategy %s" % option)
        self.runtime.optimizer.set_strategy(self.system, option)
        self.async_write_ha_state()
