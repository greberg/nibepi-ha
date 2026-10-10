"""Config and options flow for NibePi."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_NAME,
    CONF_PREFIX,
    CONF_SYSTEMS,
    DEFAULT_NAME,
    DEFAULT_PREFIX,
    DOMAIN,
    OPT_INDOOR_ENTITY,
    OPT_INDOOR_FACTOR,
    OPT_PRICE_ENTITY,
    OPT_PRICE_GUARD,
    OPT_PRICE_SOURCE,
    OPT_PRICE_MIN_SPREAD,
    OPT_PRICE_TEMP_LOW,
    OPT_PRICE_TIME_WINDOW,
    OPT_PRICE_UNIT_FACTOR,
    OPT_PRICE_VERY_CHEAP_BELOW,
    OPT_PRICE_VERY_EXPENSIVE_ABOVE,
    OPT_SENSOR_TIMEOUT,
    OPT_SETPOINT,
    OPT_SETPOINT_ENTITY,
    OPT_SUN_CLEAR,
    OPT_SUN_ENABLE,
    OPT_SUN_HALF_CLEAR,
    OPT_TIBBER_HOME,
    OPT_WEATHER_ADJUST,
    OPT_WEATHER_DOWN,
    OPT_WEATHER_ENTITY,
    OPT_WEATHER_HOURS,
    OPT_WEATHER_UP,
    OPT_WIND_E,
    OPT_WIND_ENABLE,
    OPT_WIND_N,
    OPT_WIND_S,
    OPT_WIND_W,
    PRICE_LEVEL_OPTIONS,
    PRICE_SOURCES,
    SHARED_DEFAULTS,
    SYSTEM_DEFAULTS,
    price_source,
    system_key,
)

CONF_USE_S2 = "use_s2"

# Shared options that may be left empty.
SHARED_OPTIONAL = (
    OPT_TIBBER_HOME,
    OPT_PRICE_ENTITY,
    OPT_PRICE_VERY_CHEAP_BELOW,
    OPT_PRICE_VERY_EXPENSIVE_ABOVE,
    OPT_WEATHER_ENTITY,
)
SYSTEM_OPTIONAL = (OPT_INDOOR_ENTITY, OPT_SETPOINT_ENTITY)


def _number(low: float, high: float, step: float) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=low, max=high, step=step, mode=NumberSelectorMode.BOX)
    )


OFFSET = _number(-10, 10, 0.5)
FACTOR = _number(0, 10, 0.1)
BOOL = BooleanSelector()
PRICE_SOURCE = SelectSelector(
    SelectSelectorConfig(
        options=list(PRICE_SOURCES), mode=SelectSelectorMode.LIST, translation_key="price_source"
    )
)


def _optional(key: str, current: Dict[str, Any]) -> vol.Optional:
    value = current.get(key)
    if value in (None, ""):
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": value})


def _required(key: str, current: Dict[str, Any], defaults: Dict[str, Any]) -> vol.Required:
    return vol.Required(key, default=current.get(key, defaults.get(key)))


class NibePiConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            prefix = str(user_input[CONF_PREFIX]).strip()
            if prefix and not prefix.endswith("/"):
                prefix += "/"
            if not prefix or "#" in prefix or "+" in prefix:
                errors[CONF_PREFIX] = "invalid_prefix"
            else:
                await self.async_set_unique_id(prefix)
                self._abort_if_unique_id_configured()
                systems: List[str] = ["s1", "s2"] if user_input.get(CONF_USE_S2) else ["s1"]
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_NAME: user_input[CONF_NAME], CONF_PREFIX: prefix, CONF_SYSTEMS: systems},
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_PREFIX, default=DEFAULT_PREFIX): str,
                vol.Required(CONF_USE_S2, default=False): BOOL,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "NibePiOptionsFlow":
        return NibePiOptionsFlow()


class NibePiOptionsFlow(OptionsFlow):
    """init (shared: price, weather, staleness) -> one step per system."""

    def __init__(self) -> None:
        self._options: Dict[str, Any] = {}
        self._pending: List[str] = []

    async def async_step_init(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options = dict(self.config_entry.options)
            for key in SHARED_OPTIONAL:
                if key not in user_input:
                    self._options.pop(key, None)
            self._options.update(user_input)
            self._pending = list(self.config_entry.data.get(CONF_SYSTEMS, ["s1"]))
            return await self._async_next_system()
        current = dict(self.config_entry.options)
        schema = vol.Schema(
            {
                _required(OPT_SENSOR_TIMEOUT, current, SHARED_DEFAULTS): _number(0, 1440, 1),
                vol.Required(OPT_PRICE_SOURCE, default=price_source(current)): PRICE_SOURCE,
                _optional(OPT_TIBBER_HOME, current): TextSelector(),
                _optional(OPT_PRICE_ENTITY, current): EntitySelector(EntitySelectorConfig(domain="sensor")),
                _required(OPT_PRICE_UNIT_FACTOR, current, SHARED_DEFAULTS): _number(0.001, 100000, 0.001),
                _required(OPT_PRICE_TIME_WINDOW, current, SHARED_DEFAULTS): _number(1, 48, 1),
                _required(OPT_PRICE_MIN_SPREAD, current, SHARED_DEFAULTS): _number(0, 1000, 1),
                _optional(OPT_PRICE_VERY_CHEAP_BELOW, current): _number(-1000, 10000, 0.1),
                _optional(OPT_PRICE_VERY_EXPENSIVE_ABOVE, current): _number(-1000, 10000, 0.1),
                _optional(OPT_WEATHER_ENTITY, current): EntitySelector(EntitySelectorConfig(domain="weather")),
                _required(OPT_WEATHER_ADJUST, current, SHARED_DEFAULTS): BOOL,
                _required(OPT_WEATHER_UP, current, SHARED_DEFAULTS): BOOL,
                _required(OPT_WEATHER_DOWN, current, SHARED_DEFAULTS): BOOL,
                _required(OPT_WIND_ENABLE, current, SHARED_DEFAULTS): BOOL,
                _required(OPT_WIND_N, current, SHARED_DEFAULTS): FACTOR,
                _required(OPT_WIND_S, current, SHARED_DEFAULTS): FACTOR,
                _required(OPT_WIND_W, current, SHARED_DEFAULTS): FACTOR,
                _required(OPT_WIND_E, current, SHARED_DEFAULTS): FACTOR,
                _required(OPT_SUN_ENABLE, current, SHARED_DEFAULTS): BOOL,
                _required(OPT_SUN_CLEAR, current, SHARED_DEFAULTS): OFFSET,
                _required(OPT_SUN_HALF_CLEAR, current, SHARED_DEFAULTS): OFFSET,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def _async_next_system(self) -> ConfigFlowResult:
        if not self._pending:
            return self.async_create_entry(data=self._options)
        system = self._pending[0]
        return await (self.async_step_system_s1() if system == "s1" else self.async_step_system_s2())

    async def async_step_system_s1(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        return await self._async_system_step("s1", user_input)

    async def async_step_system_s2(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        return await self._async_system_step("s2", user_input)

    async def _async_system_step(self, system: str, user_input: Optional[Dict[str, Any]]) -> ConfigFlowResult:
        if user_input is not None:
            for key in SYSTEM_OPTIONAL:
                if key not in user_input:
                    self._options.pop(system_key(key, system), None)
            for key, value in user_input.items():
                if key == OPT_WEATHER_HOURS:
                    value = int(value)
                self._options[system_key(key, system)] = value
            self._pending.pop(0)
            return await self._async_next_system()
        current = {
            key[: -len(system) - 1]: value
            for key, value in self._options.items()
            if key.endswith("_" + system)
        }
        fields: Dict[Any, Any] = {
            _optional(OPT_INDOOR_ENTITY, current): EntitySelector(
                EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
            _required(OPT_SETPOINT, current, SYSTEM_DEFAULTS): _number(5, 35, 0.1),
            _optional(OPT_SETPOINT_ENTITY, current): EntitySelector(
                EntitySelectorConfig(domain=["input_number", "number", "sensor"])
            ),
            _required(OPT_INDOOR_FACTOR, current, SYSTEM_DEFAULTS): FACTOR,
        }
        for key in PRICE_LEVEL_OPTIONS.values():
            fields[_required(key, current, SYSTEM_DEFAULTS)] = OFFSET
        fields[_required(OPT_PRICE_GUARD, current, SYSTEM_DEFAULTS)] = BOOL
        fields[_required(OPT_PRICE_TEMP_LOW, current, SYSTEM_DEFAULTS)] = _number(-10, 10, 0.1)
        fields[_required(OPT_WEATHER_HOURS, current, SYSTEM_DEFAULTS)] = _number(0, 48, 1)
        return self.async_show_form(
            step_id="system_%s" % system,
            data_schema=vol.Schema(fields),
            description_placeholders={"system": system.upper()},
        )
