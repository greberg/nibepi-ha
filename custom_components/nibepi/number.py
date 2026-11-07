"""Numbers: pump settings changed for good through the gateway's settings
channel (logic/settings.py), the hot water stop temperatures."""

from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NibePiConfigEntry
from .entity import NibePiEntity
from .logic.settings import SETTINGS, Setting, gateway_supports


async def async_setup_entry(
    hass: HomeAssistant, entry: NibePiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    async_add_entities(PumpSettingNumber(runtime, setting) for setting in SETTINGS)


class PumpSettingNumber(NibePiEntity, NumberEntity):
    """A pump setting: shows the pump's value (while a lease holds the
    register, the value the lease restores) and writes it for good."""

    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, runtime, setting: Setting) -> None:
        super().__init__(runtime, setting.key)
        self.setting = setting
        self._attr_native_min_value = setting.minimum
        self._attr_native_max_value = setting.maximum
        self._attr_native_step = setting.step

    @property
    def available(self) -> bool:
        return gateway_supports(self.runtime.gateway_state, self.setting.register) and self.native_value is not None

    @property
    def native_value(self) -> Optional[float]:
        return self.runtime.setting_now(self.setting)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        reg = self.setting.register
        lease = self.runtime.setting_lease(reg) or {}
        held = lease.get("active") is True or lease.get("restoring") is True
        result = self.runtime.setting_results.get(reg) or {}
        return {
            "register": reg,
            "leased": held,
            "pump_value": self.runtime._value(reg) if held else None,
            "last_result": {k: result.get(k) for k in ("ok", "value", "deferred", "error") if k in result} or None,
        }

    async def async_set_native_value(self, value: float) -> None:
        await self.runtime.async_write_setting(self.setting, value)
