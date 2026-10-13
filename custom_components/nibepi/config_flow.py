"""Config and options flow for NibePi.

Setup:   user (name, prefix, S2) -> price -> [tibber_warning] ->
         [tibber_home | price_entity] -> system_s1 -> [system_s2] -> house
Options: init (shared) -> [tibber_warning] -> [tibber_home | price_entity]
         -> system_s1 -> [system_s2] -> house -> hotwater -> away

Setup asks only for what is needed to get going (price source, room
sensors, setpoint); the options flow has everything. Both store the same
option keys, and every key is optional: entries made before a key existed
fall back to the defaults in const.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util import dt as dt_util

from .const import (
    AWAY_HOT_WATER_CHOICES,
    CONF_NAME,
    CONF_PREFIX,
    CONF_SYSTEMS,
    CONFIDENCE_LEVELS,
    DEFAULT_NAME,
    DEFAULT_PREFIX,
    DOMAIN,
    HOUSE_CONSTRUCTIONS,
    HOUSE_EMITTERS,
    HOUSE_ERAS,
    HOUSE_FLOORS,
    HW_COMFORT_OPTIONS,
    METER_SCOPES,
    OPT_AWAY_CURVE_OFFSET,
    OPT_AWAY_HOT_WATER,
    OPT_AWAY_MIN_INDOOR,
    OPT_AWAY_PREHEAT,
    OPT_AWAY_RETURN_LEAD,
    OPT_AWAY_SETBACK,
    OPT_HW_AUTOLUX_DIFF,
    OPT_HW_AUTOLUX_TIME,
    OPT_HW_DAY_LARGE,
    OPT_HW_DAY_MEDIUM,
    OPT_HW_DAY_SMALL,
    OPT_HW_LEARNING_RATE,
    OPT_HW_MIN_DROP,
    OPT_HW_MIN_TEMP,
    OPT_HW_MIN_TEMP_ENABLE,
    OPT_HW_OPT_FLOOR,
    OPT_HW_PEAK,
    OPT_HW_PREHEAT,
    OPT_HW_PRICE_WINDOW,
    OPT_HW_USE_PRICE,
    OPT_COMFORT_ABOVE,
    OPT_COMFORT_BELOW,
    OPT_COMFORT_FLOOR,
    OPT_HOUSE_AREA,
    OPT_HOUSE_CONSTRUCTION,
    OPT_HOUSE_EMITTER,
    OPT_HOUSE_ERA,
    OPT_HOUSE_FLOORS,
    OPT_HOUSE_SENSORS,
    OPT_INDOOR_ENTITY,
    OPT_INDOOR_FACTOR,
    OPT_METER_ENTITY,
    OPT_METER_SCOPE,
    OPT_OPT_MIN_LEVEL,
    OPT_PRICE_ENTITY,
    OPT_PRICE_GUARD,
    OPT_PRICE_MIN_SPREAD,
    OPT_PRICE_SOURCE,
    OPT_PRICE_TEMP_LOW,
    OPT_PRICE_TIME_WINDOW,
    OPT_PRICE_UNIT_FACTOR,
    OPT_PRICE_VERY_CHEAP_BELOW,
    OPT_PRICE_VERY_EXPENSIVE_ABOVE,
    OPT_RMU_ENTITY,
    OPT_SENSOR_TIMEOUT,
    OPT_SETPOINT,
    OPT_SETPOINT_ENTITY,
    OPT_SUN_CLEAR,
    OPT_SUN_ENABLE,
    OPT_SUN_HALF_CLEAR,
    OPT_TIBBER_ENTRY,
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
    PRICE_SOURCE_ENTITY,
    PRICE_SOURCE_NONE,
    PRICE_SOURCE_TIBBER,
    SHARED_DEFAULTS,
    SYSTEM_DEFAULTS,
    price_source,
    system_key,
)
from .logic.hotwater import COMFORT_CHOICES
from .logic.opthistory import meter_kind
from .logic.price import TIBBER_DOMAIN, TIBBER_SERVICE, tibber_homes, tibber_request

_LOGGER = logging.getLogger(__name__)

CONF_USE_S2 = "use_s2"
# Price source selector values: one per Tibber config entry, plus the
# generic sources.
TIBBER_OPTION = "tibber:"
TIBBER_HOMES_TIMEOUT = 30

# Shared options that may be left empty.
SHARED_OPTIONAL = (
    OPT_PRICE_VERY_CHEAP_BELOW,
    OPT_PRICE_VERY_EXPENSIVE_ABOVE,
    OPT_WEATHER_ENTITY,
)
SYSTEM_OPTIONAL = (OPT_INDOOR_ENTITY, OPT_RMU_ENTITY, OPT_SETPOINT_ENTITY)


def _number(low: float, high: float, step: float) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=low, max=high, step=step, mode=NumberSelectorMode.BOX)
    )


OFFSET = _number(-10, 10, 0.5)
FACTOR = _number(0, 10, 0.1)
BOOL = BooleanSelector()
HW_COMFORT = SelectSelector(
    SelectSelectorConfig(
        options=list(COMFORT_CHOICES), mode=SelectSelectorMode.DROPDOWN, translation_key="hw_comfort"
    )
)
# Whole numbers in the hot water step.
HW_INT_OPTIONS = (OPT_HW_LEARNING_RATE, OPT_HW_PREHEAT, OPT_HW_PRICE_WINDOW, OPT_HW_AUTOLUX_TIME)
AWAY_INT_OPTIONS = (OPT_AWAY_CURVE_OFFSET, OPT_AWAY_RETURN_LEAD)
TEMPERATURE_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="temperature"))
METER_SENSOR = EntitySelector(EntitySelectorConfig(domain="sensor", device_class=["power", "energy"]))


def _choice(options: Any, key: str) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(options=list(options), mode=SelectSelectorMode.DROPDOWN, translation_key=key)
    )


def _optional(key: str, current: Dict[str, Any], suggested: Any = None) -> vol.Optional:
    value = current.get(key)
    if value in (None, ""):
        value = suggested
    if value in (None, ""):
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": value})


def _required(key: str, current: Dict[str, Any], defaults: Dict[str, Any]) -> vol.Required:
    return vol.Required(key, default=current.get(key, defaults.get(key)))


class _NibePiSteps:
    """Price and per-system steps shared by the config and options flow.

    The subclass sets ``_options`` and ``_pending`` (systems still to ask)
    and implements ``_async_after_house`` and sets ``_full_system_step``.
    """

    _options: Dict[str, Any]
    _pending: List[str]
    _full_system_step = False
    _allow_generic_tibber = False
    _homes: Optional[List[str]] = None
    _homes_error: Optional[str] = None

    # -- price source ------------------------------------------------------

    def _tibber_entries(self, loaded_only: bool = True) -> List[ConfigEntry]:
        entries = self.hass.config_entries.async_entries(TIBBER_DOMAIN)  # type: ignore[attr-defined]
        if loaded_only:
            entries = [e for e in entries if e.state is ConfigEntryState.LOADED]
        return entries

    def _source_value(self) -> str:
        """The selector value for the stored price source.

        A new entry gets the first loaded Tibber entry, or none. An existing
        Tibber entry whose integration is not loaded keeps a generic Tibber
        choice instead of silently switching source.
        """
        source = price_source(self._options)
        if source != PRICE_SOURCE_TIBBER:
            return source
        entries = self._tibber_entries()
        ids = [e.entry_id for e in entries]
        wanted = self._options.get(OPT_TIBBER_ENTRY)
        if wanted in ids:
            return TIBBER_OPTION + str(wanted)
        if ids:
            return TIBBER_OPTION + ids[0]
        return PRICE_SOURCE_TIBBER if self._allow_generic_tibber else PRICE_SOURCE_NONE

    def _source_selector(self) -> SelectSelector:
        options = [
            SelectOptionDict(value=TIBBER_OPTION + e.entry_id, label="Tibber: %s" % e.title)
            for e in self._tibber_entries()
        ]
        if not options and self._allow_generic_tibber and price_source(self._options) == PRICE_SOURCE_TIBBER:
            options.append(
                SelectOptionDict(value=PRICE_SOURCE_TIBBER, label="Tibber integration (tibber.get_prices)")
            )
        options.append(SelectOptionDict(value=PRICE_SOURCE_ENTITY, label="Price sensor entity (attributes)"))
        options.append(SelectOptionDict(value=PRICE_SOURCE_NONE, label="None"))
        return SelectSelector(
            SelectSelectorConfig(
                options=options, mode=SelectSelectorMode.DROPDOWN, translation_key="price_source"
            )
        )

    def _apply_source(self, value: str) -> None:
        if value.startswith(TIBBER_OPTION):
            self._options[OPT_PRICE_SOURCE] = PRICE_SOURCE_TIBBER
            self._options[OPT_TIBBER_ENTRY] = value[len(TIBBER_OPTION):]
        elif value in (PRICE_SOURCE_TIBBER, PRICE_SOURCE_ENTITY):
            self._options[OPT_PRICE_SOURCE] = value
        else:
            self._options[OPT_PRICE_SOURCE] = PRICE_SOURCE_NONE

    async def _async_after_source(self) -> ConfigFlowResult:
        source = self._options.get(OPT_PRICE_SOURCE)
        if source == PRICE_SOURCE_TIBBER:
            entries = self._tibber_entries(loaded_only=False)
            chosen = self._options.get(OPT_TIBBER_ENTRY)
            if entries and chosen and entries[0].entry_id != chosen:
                return await self.async_step_tibber_warning()
            return await self.async_step_tibber_home()
        if source == PRICE_SOURCE_ENTITY:
            return await self.async_step_price_entity()
        return await self._async_next_system()

    async def async_step_tibber_warning(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        """tibber.get_prices always uses Tibber's first config entry."""
        if user_input is not None:
            return await self.async_step_tibber_home()
        entries = self._tibber_entries(loaded_only=False)
        chosen = self._options.get(OPT_TIBBER_ENTRY)
        chosen_title = next((e.title for e in entries if e.entry_id == chosen), str(chosen))
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="tibber_warning",
            data_schema=vol.Schema({}),
            description_placeholders={"chosen": chosen_title, "first": entries[0].title},
        )

    async def _async_list_homes(self) -> None:
        self._homes = None
        self._homes_error = None
        hass = self.hass  # type: ignore[attr-defined]
        try:
            if not hass.services.has_service(TIBBER_DOMAIN, TIBBER_SERVICE):
                raise RuntimeError("%s.%s is not available" % (TIBBER_DOMAIN, TIBBER_SERVICE))
            tz = dt_util.get_default_time_zone()
            response = await asyncio.wait_for(
                hass.services.async_call(
                    TIBBER_DOMAIN,
                    TIBBER_SERVICE,
                    tibber_request(dt_util.utcnow(), tz),
                    blocking=True,
                    return_response=True,
                ),
                TIBBER_HOMES_TIMEOUT,
            )
        except Exception as err:  # noqa: BLE001 - fall back to a text field
            _LOGGER.info("Could not list Tibber homes: %s", str(err) or repr(err))
            self._homes_error = str(err) or repr(err)
            return
        homes = tibber_homes(response)
        if homes:
            self._homes = homes
        else:
            self._homes_error = "no homes in the response"

    async def async_step_tibber_home(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        if user_input is not None:
            home = str(user_input.get(OPT_TIBBER_HOME) or "").strip()
            if home:
                self._options[OPT_TIBBER_HOME] = home
            else:
                self._options.pop(OPT_TIBBER_HOME, None)
            return await self._async_next_system()
        await self._async_list_homes()
        if self._homes is not None and len(self._homes) == 1:
            # The only home: nothing to choose.
            self._options.pop(OPT_TIBBER_HOME, None)
            return await self._async_next_system()
        current = self._options.get(OPT_TIBBER_HOME)
        errors: Dict[str, str] = {}
        if self._homes:
            default = current if current in self._homes else self._homes[0]
            schema = vol.Schema(
                {
                    vol.Required(OPT_TIBBER_HOME, default=default): SelectSelector(
                        SelectSelectorConfig(options=list(self._homes), mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            )
        else:
            errors["base"] = "tibber_homes_failed"
            schema = vol.Schema({_optional(OPT_TIBBER_HOME, self._options): TextSelector()})
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="tibber_home",
            data_schema=schema,
            errors=errors,
            description_placeholders={"error": self._homes_error or ""},
        )

    async def async_step_price_entity(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        if user_input is not None:
            self._options[OPT_PRICE_ENTITY] = user_input[OPT_PRICE_ENTITY]
            return await self._async_next_system()
        current = self._options.get(OPT_PRICE_ENTITY)
        key = (
            vol.Required(OPT_PRICE_ENTITY, default=current)
            if current
            else vol.Required(OPT_PRICE_ENTITY)
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="price_entity",
            data_schema=vol.Schema({key: EntitySelector(EntitySelectorConfig(domain="sensor"))}),
        )

    # -- per system -------------------------------------------------------

    async def _async_next_system(self) -> ConfigFlowResult:
        if not self._pending:
            return await self._async_finish()
        system = self._pending[0]
        return await (self.async_step_system_s1() if system == "s1" else self.async_step_system_s2())

    async def async_step_system_s1(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        return await self._async_system_step("s1", user_input)

    async def async_step_system_s2(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        return await self._async_system_step("s2", user_input)

    async def _async_system_step(self, system: str, user_input: Optional[Dict[str, Any]]) -> ConfigFlowResult:
        errors: Dict[str, str] = {}
        if user_input is not None:
            below = user_input.get(OPT_COMFORT_BELOW, SYSTEM_DEFAULTS[OPT_COMFORT_BELOW])
            floor = user_input.get(OPT_COMFORT_FLOOR, SYSTEM_DEFAULTS[OPT_COMFORT_FLOOR])
            if float(floor) <= float(below):
                errors[OPT_COMFORT_FLOOR] = "comfort_floor"
        if user_input is not None and not errors:
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
        if user_input is not None:
            current.update(user_input)
        fields: Dict[Any, Any] = {
            _optional(OPT_INDOOR_ENTITY, current): TEMPERATURE_SENSOR,
            _optional(OPT_RMU_ENTITY, current, current.get(OPT_INDOOR_ENTITY)): TEMPERATURE_SENSOR,
            _required(OPT_SETPOINT, current, SYSTEM_DEFAULTS): _number(5, 35, 0.1),
        }
        if self._full_system_step:
            fields[_optional(OPT_SETPOINT_ENTITY, current)] = EntitySelector(
                EntitySelectorConfig(domain=["input_number", "number", "sensor"])
            )
            fields[_required(OPT_INDOOR_FACTOR, current, SYSTEM_DEFAULTS)] = FACTOR
            for key in PRICE_LEVEL_OPTIONS.values():
                fields[_required(key, current, SYSTEM_DEFAULTS)] = OFFSET
            fields[_required(OPT_PRICE_GUARD, current, SYSTEM_DEFAULTS)] = BOOL
            fields[_required(OPT_PRICE_TEMP_LOW, current, SYSTEM_DEFAULTS)] = _number(-10, 10, 0.1)
            fields[_required(OPT_WEATHER_HOURS, current, SYSTEM_DEFAULTS)] = _number(0, 48, 1)
            fields[_required(OPT_COMFORT_BELOW, current, SYSTEM_DEFAULTS)] = _number(0, 3, 0.1)
            fields[_required(OPT_COMFORT_ABOVE, current, SYSTEM_DEFAULTS)] = _number(0, 5, 0.1)
            fields[_required(OPT_COMFORT_FLOOR, current, SYSTEM_DEFAULTS)] = _number(0.2, 6, 0.1)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="system_%s" % system,
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"system": system.upper()},
        )

    # -- house profile and optimiser ----------------------------------------

    def _location_placeholders(self) -> Dict[str, str]:
        config = self.hass.config  # type: ignore[attr-defined]
        return {
            "latitude": "%.3f" % config.latitude,
            "longitude": "%.3f" % config.longitude,
            "elevation": "%d" % int(config.elevation or 0),
            "time_zone": str(config.time_zone),
        }

    def _meter_error(self, entity_id: str) -> Optional[str]:
        """None when the meter's unit and class are usable (or not known yet)."""
        state = self.hass.states.get(entity_id)  # type: ignore[attr-defined]
        if state is None:
            return None  # not loaded yet; the runtime checks again
        attrs = state.attributes
        kind = meter_kind(
            attrs.get("unit_of_measurement"), attrs.get("device_class"), attrs.get("state_class")
        )
        return None if kind is not None else "meter_unit"

    async def async_step_house(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        """House profile, meter and the optimiser's confidence gate."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            meter = user_input.get(OPT_METER_ENTITY)
            if meter:
                error = self._meter_error(meter)
                if error:
                    errors[OPT_METER_ENTITY] = error
            if not errors:
                if not meter:
                    self._options.pop(OPT_METER_ENTITY, None)
                for key, value in user_input.items():
                    self._options[key] = int(value) if key == OPT_HOUSE_SENSORS else value
                return await self._async_after_house()
        current = dict(self._options)
        if user_input is not None:
            current.update(user_input)
        fields: Dict[Any, Any] = {
            _required(OPT_HOUSE_AREA, current, SHARED_DEFAULTS): _number(30, 1000, 1),
            _required(OPT_HOUSE_FLOORS, current, SHARED_DEFAULTS): _choice(HOUSE_FLOORS, "house_floors"),
            _required(OPT_HOUSE_ERA, current, SHARED_DEFAULTS): _choice(HOUSE_ERAS, "house_era"),
            _required(OPT_HOUSE_CONSTRUCTION, current, SHARED_DEFAULTS): _choice(
                HOUSE_CONSTRUCTIONS, "house_construction"
            ),
            _required(OPT_HOUSE_EMITTER, current, SHARED_DEFAULTS): _choice(HOUSE_EMITTERS, "house_emitter"),
            _required(OPT_HOUSE_SENSORS, current, SHARED_DEFAULTS): _number(1, 10, 1),
            _optional(OPT_METER_ENTITY, current): METER_SENSOR,
            _required(OPT_METER_SCOPE, current, SHARED_DEFAULTS): _choice(METER_SCOPES, "meter_scope"),
            _required(OPT_OPT_MIN_LEVEL, current, SHARED_DEFAULTS): _choice(CONFIDENCE_LEVELS, "confidence_level"),
        }
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="house",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders=self._location_placeholders(),
        )

    async def _async_after_house(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def _async_finish(self) -> ConfigFlowResult:
        return await self.async_step_house()


class NibePiConfigFlow(_NibePiSteps, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._options = {}
        self._pending = []
        self._data: Dict[str, Any] = {}

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
                self._data = {CONF_NAME: user_input[CONF_NAME], CONF_PREFIX: prefix, CONF_SYSTEMS: systems}
                return await self.async_step_price()
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_PREFIX, default=DEFAULT_PREFIX): str,
                vol.Required(CONF_USE_S2, default=False): BOOL,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_price(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        if user_input is not None:
            self._apply_source(user_input[OPT_PRICE_SOURCE])
            self._pending = list(self._data[CONF_SYSTEMS])
            return await self._async_after_source()
        schema = vol.Schema(
            {vol.Required(OPT_PRICE_SOURCE, default=self._source_value()): self._source_selector()}
        )
        return self.async_show_form(step_id="price", data_schema=schema)

    async def _async_after_house(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._data[CONF_NAME], data=self._data, options=dict(self._options)
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "NibePiOptionsFlow":
        return NibePiOptionsFlow()


class NibePiOptionsFlow(_NibePiSteps, OptionsFlow):
    """init (shared: price, weather, staleness) -> price details -> one step per system."""

    _full_system_step = True
    _allow_generic_tibber = True

    def __init__(self) -> None:
        self._options = {}
        self._pending = []

    async def async_step_init(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        if not self._options:
            self._options = dict(self.config_entry.options)
        if user_input is not None:
            for key in SHARED_OPTIONAL:
                if key not in user_input:
                    self._options.pop(key, None)
            user_input = dict(user_input)
            self._apply_source(user_input.pop(OPT_PRICE_SOURCE))
            self._options.update(user_input)
            self._pending = list(self.config_entry.data.get(CONF_SYSTEMS, ["s1"]))
            return await self._async_after_source()
        current = self._options
        schema = vol.Schema(
            {
                _required(OPT_SENSOR_TIMEOUT, current, SHARED_DEFAULTS): _number(0, 1440, 1),
                vol.Required(OPT_PRICE_SOURCE, default=self._source_value()): self._source_selector(),
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

    async def _async_after_house(self) -> ConfigFlowResult:
        return await self.async_step_hotwater()

    async def async_step_hotwater(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        """Hot water: VV learning, VV-AI plan, automatic luxury, comfort mode."""
        if user_input is not None:
            for key, value in user_input.items():
                self._options[key] = int(value) if key in HW_INT_OPTIONS else value
            return await self.async_step_away()
        current = self._options
        fields: Dict[Any, Any] = {
            _required(OPT_HW_LEARNING_RATE, current, SHARED_DEFAULTS): _number(1, 10, 1),
            _required(OPT_HW_MIN_DROP, current, SHARED_DEFAULTS): _number(1, 12, 0.5),
            _required(OPT_HW_DAY_SMALL, current, SHARED_DEFAULTS): _number(0.5, 15, 0.5),
            _required(OPT_HW_DAY_MEDIUM, current, SHARED_DEFAULTS): _number(0.5, 25, 0.5),
            _required(OPT_HW_DAY_LARGE, current, SHARED_DEFAULTS): _number(0.5, 40, 0.5),
            _required(OPT_HW_PEAK, current, SHARED_DEFAULTS): _number(0.5, 7, 0.5),
            _required(OPT_HW_PREHEAT, current, SHARED_DEFAULTS): _number(0, 12, 1),
            _required(OPT_HW_USE_PRICE, current, SHARED_DEFAULTS): BOOL,
            _required(OPT_HW_PRICE_WINDOW, current, SHARED_DEFAULTS): _number(1, 24, 1),
            _required(OPT_HW_MIN_TEMP_ENABLE, current, SHARED_DEFAULTS): BOOL,
            _required(OPT_HW_MIN_TEMP, current, SHARED_DEFAULTS): _number(20, 60, 0.5),
            _required(OPT_HW_AUTOLUX_DIFF, current, SHARED_DEFAULTS): _number(5, 50, 1),
            _required(OPT_HW_AUTOLUX_TIME, current, SHARED_DEFAULTS): _number(1, 50, 1),
            _required(OPT_HW_OPT_FLOOR, current, SHARED_DEFAULTS): _number(30, 60, 0.5),
        }
        for key in HW_COMFORT_OPTIONS.values():
            fields[_required(key, current, SHARED_DEFAULTS)] = HW_COMFORT
        return self.async_show_form(step_id="hotwater", data_schema=vol.Schema(fields))

    async def async_step_away(self, user_input: Optional[Dict[str, Any]] = None) -> ConfigFlowResult:
        """Away (vacation) mode: setback, absolute floor, hot water, return."""
        if user_input is not None:
            for key, value in user_input.items():
                self._options[key] = int(value) if key in AWAY_INT_OPTIONS else value
            return self.async_create_entry(data=self._options)
        current = self._options
        fields: Dict[Any, Any] = {
            _required(OPT_AWAY_SETBACK, current, SHARED_DEFAULTS): _number(0, 10, 0.5),
            _required(OPT_AWAY_MIN_INDOOR, current, SHARED_DEFAULTS): _number(5, 25, 0.5),
            _required(OPT_AWAY_CURVE_OFFSET, current, SHARED_DEFAULTS): _number(-10, 0, 1),
            _required(OPT_AWAY_HOT_WATER, current, SHARED_DEFAULTS): _choice(
                AWAY_HOT_WATER_CHOICES, "away_hot_water"
            ),
            _required(OPT_AWAY_PREHEAT, current, SHARED_DEFAULTS): BOOL,
            _required(OPT_AWAY_RETURN_LEAD, current, SHARED_DEFAULTS): _number(0, 24, 1),
        }
        return self.async_show_form(step_id="away", data_schema=vol.Schema(fields))
