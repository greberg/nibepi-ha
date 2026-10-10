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
# supply: BT2 (S1) / EP21-BT2 (S2), for the optimiser's house model.
REGISTERS = {
    "s1": {"offset": 47011, "curve": 47007, "indoor": 40033, "rmu": 10060, "supply": 40008},  # BT50
    "s2": {"offset": 47010, "curve": 47006, "indoor": 40032, "rmu": 10160, "supply": 40007},
}

# Cost optimiser inputs (besides BT1, BT2, the offsets and the hot water
# registers): brine in/out, compressor input power, electric addition,
# compressor frequency and the heat meters for heating and hot water.
REG_BRINE_IN = 40015  # BT10
REG_BRINE_OUT = 40016  # BT11
REG_COMPRESSOR_POWER = 43141  # W
REG_ADDITION_POWER = 43084  # kW
REG_COMPRESSOR_FREQ = 43136  # Hz
REG_HEAT_METER = 44300  # kWh, heating (compressor + addition)
REG_HW_METER = 44298  # kWh, hot water (compressor + addition)
OPTIMIZER_REGISTERS = (
    REG_BRINE_IN,
    REG_BRINE_OUT,
    REG_COMPRESSOR_POWER,
    REG_ADDITION_POWER,
    REG_COMPRESSOR_FREQ,
    REG_HEAT_METER,
    REG_HW_METER,
)

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

# Options: hot water (shared). Defaults are Node-RED's (config_node.js,
# default.json); see docs/hotwater.md for the exceptions.
OPT_HW_LEARNING_RATE = "hw_learning_rate"
OPT_HW_MIN_DROP = "hw_min_drop"
OPT_HW_DAY_SMALL = "hw_day_small"
OPT_HW_DAY_MEDIUM = "hw_day_medium"
OPT_HW_DAY_LARGE = "hw_day_large"
OPT_HW_PEAK = "hw_peak_threshold"
OPT_HW_PREHEAT = "hw_preheat_hours"
OPT_HW_USE_PRICE = "hw_use_price"
OPT_HW_PRICE_WINDOW = "hw_price_window"
OPT_HW_MIN_TEMP_ENABLE = "hw_min_temp_enable"
OPT_HW_MIN_TEMP = "hw_min_temp"
OPT_HW_AUTOLUX_DIFF = "hw_autolux_diff"
OPT_HW_AUTOLUX_TIME = "hw_autolux_time"

HW_COMFORT_OPTIONS = {
    "VERY_CHEAP": "hw_comfort_very_cheap",
    "CHEAP": "hw_comfort_cheap",
    "NORMAL": "hw_comfort_normal",
    "EXPENSIVE": "hw_comfort_expensive",
    "VERY_EXPENSIVE": "hw_comfort_very_expensive",
}

HW_DEFAULTS: Dict[str, Any] = {
    OPT_HW_LEARNING_RATE: 10,
    OPT_HW_MIN_DROP: 3.0,
    OPT_HW_DAY_SMALL: 1.0,
    OPT_HW_DAY_MEDIUM: 3.0,
    OPT_HW_DAY_LARGE: 6.0,
    OPT_HW_PEAK: 3.0,
    OPT_HW_PREHEAT: 2,
    OPT_HW_USE_PRICE: False,
    OPT_HW_PRICE_WINDOW: 7,
    OPT_HW_MIN_TEMP_ENABLE: True,
    OPT_HW_MIN_TEMP: 35.0,
    OPT_HW_AUTOLUX_DIFF: 20.0,
    OPT_HW_AUTOLUX_TIME: 10,
    "hw_comfort_very_cheap": "2",
    "hw_comfort_cheap": "1",
    "hw_comfort_normal": "0",
    "hw_comfort_expensive": "0",
    "hw_comfort_very_expensive": "0",
}

# The learned hot water profile (homeassistant.helpers.storage).
VV_STORE_VERSION = 1
VV_STORE_SAVE_DELAY = 30  # seconds; a learned draw or a new day saves
SERVICE_IMPORT_VV_PROFILE = "import_vv_profile"
ATTR_PATH = "path"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
VV_IMPORT_MAX_BYTES = 1_000_000


def vv_store_key(entry_id: str) -> str:
    return "%s_vv_profile_%s" % (DOMAIN, entry_id)


# The optimiser's history, fitted models and savings (helpers.storage).
OPT_STORE_VERSION = 1
OPT_STORE_SAVE_DELAY = 900  # seconds; a new 10-minute bin saves at most this late


def opt_store_key(entry_id: str) -> str:
    return "%s_optimizer_%s" % (DOMAIN, entry_id)


# Options: house profile and cost optimiser (shared). See docs/optimizer.md
# in the workspace; the comfort band is per system (below).
OPT_HOUSE_AREA = "house_area"
OPT_HOUSE_FLOORS = "house_floors"
OPT_HOUSE_ERA = "house_era"
OPT_HOUSE_CONSTRUCTION = "house_construction"
OPT_HOUSE_EMITTER = "house_emitter"
OPT_HOUSE_SENSORS = "house_sensors"
OPT_METER_ENTITY = "meter_entity"
OPT_OPT_MIN_LEVEL = "optimizer_min_level"
OPT_HW_OPT_FLOOR = "hw_optimizer_floor"

UNKNOWN = "unknown"
HOUSE_FLOORS = ("1", "1.5", "2", "3")
HOUSE_ERAS = (UNKNOWN, "pre1960", "1961-1975", "1976-1990", "1991-2010", "2011-")
HOUSE_CONSTRUCTIONS = (UNKNOWN, "light", "medium", "heavy")
HOUSE_EMITTERS = ("radiators", "floor", "mixed")
CONFIDENCE_LEVELS = ("low", "medium", "high")

OPTIMIZER_DEFAULTS: Dict[str, Any] = {
    OPT_HOUSE_AREA: 150.0,
    OPT_HOUSE_FLOORS: "1.5",
    OPT_HOUSE_ERA: UNKNOWN,
    OPT_HOUSE_CONSTRUCTION: UNKNOWN,
    OPT_HOUSE_EMITTER: "radiators",
    OPT_HOUSE_SENSORS: 1,
    OPT_OPT_MIN_LEVEL: "high",
    OPT_HW_OPT_FLOOR: 40.0,
}

# Options: comfort band per system, around the setpoint (the optimiser's
# target indoor temperature).
OPT_COMFORT_BELOW = "comfort_below"
OPT_COMFORT_ABOVE = "comfort_above"
OPT_COMFORT_FLOOR = "comfort_floor_below"


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
    **HW_DEFAULTS,
    **OPTIMIZER_DEFAULTS,
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
    OPT_COMFORT_BELOW: 0.5,
    OPT_COMFORT_ABOVE: 1.0,
    OPT_COMFORT_FLOOR: 1.5,
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
