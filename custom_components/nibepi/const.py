"""Constants for the NibePi integration."""

from __future__ import annotations

from typing import Any, Dict

DOMAIN = "nibepi"
MANUFACTURER = "NIBE"
OWNER = "ha"

PLATFORMS = ["binary_sensor", "select", "sensor", "switch"]

# Config entry data
CONF_PREFIX = "prefix"
CONF_SYSTEMS = "systems"
CONF_NAME = "name"
DEFAULT_PREFIX = "nibe/modbus/"
DEFAULT_NAME = "Nibe F1255"
SYSTEMS = ("s1", "s2")

# Registers (F series, node-red-contrib-nibepi dataregister.json)
REG_OUTDOOR = 40004  # BT1
# rmu: room temperature of the gateway's virtual RMU40 (leased, °C).
REGISTERS = {
    "s1": {"offset": 47011, "curve": 47007, "indoor": 40033, "rmu": 10060},  # BT50
    "s2": {"offset": 47010, "curve": 47006, "indoor": 40032, "rmu": 10160},
}

COMPONENTS = ("indoor", "price", "weather")

TICK_SECONDS = 60
FORECAST_REFRESH_SECONDS = 30 * 60

# Options: shared
OPT_SENSOR_TIMEOUT = "sensor_timeout"
OPT_PRICE_SOURCE = "price_source"
OPT_TIBBER_HOME = "tibber_home"
# Informational: tibber.get_prices always uses Tibber's first config entry.
OPT_TIBBER_ENTRY = "tibber_entry"
OPT_PRICE_ENTITY = "price_entity"
OPT_PRICE_UNIT_FACTOR = "price_unit_factor"
OPT_PRICE_TIME_WINDOW = "price_time_window"
OPT_PRICE_MIN_SPREAD = "price_min_spread"
OPT_PRICE_VERY_CHEAP_BELOW = "price_very_cheap_below"
OPT_PRICE_VERY_EXPENSIVE_ABOVE = "price_very_expensive_above"
OPT_WEATHER_ENTITY = "weather_entity"
OPT_WEATHER_ADJUST = "weather_forecast_adjust"
OPT_WEATHER_UP = "weather_enable_up"
OPT_WEATHER_DOWN = "weather_enable_down"
OPT_WIND_ENABLE = "wind_enable"
OPT_WIND_N = "wind_factor_n"
OPT_WIND_S = "wind_factor_s"
OPT_WIND_W = "wind_factor_w"
OPT_WIND_E = "wind_factor_e"
OPT_SUN_ENABLE = "sun_enable"
OPT_SUN_CLEAR = "sun_clear"
OPT_SUN_HALF_CLEAR = "sun_half_clear"

# Options: per system, stored as "<key>_<system>"
OPT_INDOOR_ENTITY = "indoor_entity"
OPT_RMU_ENTITY = "rmu_entity"  # empty: the indoor entity
OPT_SETPOINT = "setpoint"
OPT_SETPOINT_ENTITY = "setpoint_entity"
OPT_INDOOR_FACTOR = "indoor_factor"
OPT_PRICE_GUARD = "price_guard"
OPT_PRICE_TEMP_LOW = "price_temp_low"
OPT_WEATHER_HOURS = "weather_hours"
PRICE_SOURCE_TIBBER = "tibber_action"
PRICE_SOURCE_ENTITY = "entity_attributes"
PRICE_SOURCE_NONE = "none"
PRICE_SOURCES = (PRICE_SOURCE_TIBBER, PRICE_SOURCE_ENTITY, PRICE_SOURCE_NONE)

ISSUE_TIBBER_MISSING = "tibber_missing"

PRICE_LEVEL_OPTIONS = {
    "VERY_CHEAP": "price_very_cheap",
    "CHEAP": "price_cheap",
    "NORMAL": "price_normal",
    "EXPENSIVE": "price_expensive",
    "VERY_EXPENSIVE": "price_very_expensive",
}

SHARED_DEFAULTS: Dict[str, Any] = {
    OPT_SENSOR_TIMEOUT: 60,
    OPT_PRICE_UNIT_FACTOR: 100.0,
    OPT_PRICE_TIME_WINDOW: 12,
    OPT_PRICE_MIN_SPREAD: 20.0,
    OPT_WEATHER_ADJUST: False,
    OPT_WEATHER_UP: True,
    OPT_WEATHER_DOWN: True,
    OPT_WIND_ENABLE: False,
    OPT_WIND_N: 0.0,
    OPT_WIND_S: 0.0,
    OPT_WIND_W: 0.0,
    OPT_WIND_E: 0.0,
    OPT_SUN_ENABLE: False,
    OPT_SUN_CLEAR: 0.0,
    OPT_SUN_HALF_CLEAR: 0.0,
}

SYSTEM_DEFAULTS: Dict[str, Any] = {
    OPT_SETPOINT: 20.0,
    OPT_INDOOR_FACTOR: 2.0,
    OPT_PRICE_GUARD: False,
    OPT_PRICE_TEMP_LOW: 0.0,
    OPT_WEATHER_HOURS: 6,
    "price_very_cheap": 0.0,
    "price_cheap": 0.0,
    "price_normal": 0.0,
    "price_expensive": 0.0,
    "price_very_expensive": 0.0,
}


def price_source(options: Dict[str, Any]) -> str:
    """The configured price source.

    Entries set up before the option existed keep reading their price
    entity; everything else defaults to the Tibber action.
    """
    source = options.get(OPT_PRICE_SOURCE)
    if source in PRICE_SOURCES:
        return str(source)
    return PRICE_SOURCE_ENTITY if options.get(OPT_PRICE_ENTITY) else PRICE_SOURCE_TIBBER


def system_key(key: str, system: str) -> str:
    return "%s_%s" % (key, system)


def signal_update(entry_id: str) -> str:
    return "%s_%s_update" % (DOMAIN, entry_id)
