"""Switches: Away mode; per system the indoor, price and weather components
and the RMU40 feed; for hot water its sub-features."""

from __future__ import annotations

from typing import Any, Dict

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF, STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import NibePiConfigEntry
from .const import COMPONENTS
from .entity import NibePiEntity
from .logic.hwcontrol import FEATURE_DEFAULTS, FEATURES

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
    entities.extend(HotWaterFeatureSwitch(runtime, feature) for feature in FEATURES)
    entities.append(AwaySwitch(runtime))
    async_add_entities(entities)


class AwaySwitch(NibePiEntity, SwitchEntity):
    """Away (vacation) mode for every climate system and hot water.

    On starts Away without an end (what an alarm automation toggles);
    ``nibepi.set_away`` can give a return time. The state lives in the
    runtime's store, so it survives restarts and the end is kept.
    """

    def __init__(self, runtime) -> None:
        super().__init__(runtime, "away")

    @property
    def is_on(self) -> bool:
        return self.runtime.away.enabled

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        runtime = self.runtime
        away = runtime.away
        settings = runtime.away_settings()
        now = dt_util.utcnow()
        return {
            "until": away.until.isoformat() if away.until is not None else None,
            "since": away.since.isoformat() if away.since is not None else None,
            "setback": settings.setback_for(away),
            "setback_override": away.setback,
            "min_indoor": settings.min_indoor,
            "hot_water": settings.hot_water,
            "preheat": settings.preheat,
            "return_lead_h": settings.return_lead_h,
            # Whether the rules and hot water are in their away phase now
            # (false during the return lead before ``until``).
            "rules_away": runtime.away_rules(now),
            "targets": {
                system: runtime.away_setpoint(sys) if away.enabled else runtime._setpoint(sys)
                for system, sys in runtime.systems.items()
            },
            "modes": {system: sys.mode for system, sys in runtime.systems.items()},
            "hw_mode": runtime.hotwater.mode,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.runtime.async_turn_away_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.runtime.async_set_away(False)


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


class HotWaterFeatureSwitch(NibePiEntity, SwitchEntity, RestoreEntity):
    """A hot water sub-feature; defaults are Node-RED's, learning on."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime, feature: str) -> None:
        super().__init__(runtime, "hw_%s" % feature)
        self.feature = feature

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in (STATE_ON, STATE_OFF):
            enabled = last.state == STATE_ON
        else:
            enabled = FEATURE_DEFAULTS[self.feature]
        self.runtime.set_hw_feature(self.feature, enabled)

    @property
    def is_on(self) -> bool:
        return bool(self.runtime.hotwater.features.get(self.feature, FEATURE_DEFAULTS[self.feature]))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.runtime.set_hw_feature(self.feature, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.runtime.set_hw_feature(self.feature, False)
        self.async_write_ha_state()
