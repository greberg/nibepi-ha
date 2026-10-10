"""Runtime: collect inputs, run the controllers every tick, publish leases.

All decisions are made by the pure modules in ``logic``; this file only
moves data between Home Assistant, MQTT and those modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import sun as sun_helper
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from .const import (
    COMPONENTS,
    CONF_NAME,
    CONF_PREFIX,
    CONF_SYSTEMS,
    DEFAULT_NAME,
    DOMAIN,
    FORECAST_REFRESH_SECONDS,
    ISSUE_TIBBER_MISSING,
    MANUFACTURER,
    OPT_INDOOR_ENTITY,
    OPT_INDOOR_FACTOR,
    OPT_PRICE_ENTITY,
    OPT_PRICE_GUARD,
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
    OPT_TIBBER_HOME,
    OWNER,
    PRICE_LEVEL_OPTIONS,
    PRICE_SOURCE_NONE,
    PRICE_SOURCE_TIBBER,
    REG_OUTDOOR,
    REGISTERS,
    SHARED_DEFAULTS,
    SYSTEM_DEFAULTS,
    TICK_SECONDS,
    price_source,
    signal_update,
    system_key,
)
from .logic import curve
from .logic.gateway import STATUS_TIMEOUT, gateway_online, status_hint
from .logic.indoor import compute_indoor
from .logic.inputs import (
    Decision,
    ForecastPoint,
    PriceSlot,
    Reading,
    state_timestamp,
    state_value,
    to_float,
)
from .logic.lease import MODE_ACTIVE, MODE_OFF, MODE_SHADOW, LeaseAction, LeaseManager
from .logic.price import (
    TIBBER_DOMAIN,
    TIBBER_SERVICE,
    PriceCache,
    PriceLevel,
    compute_level,
    compute_price,
    normalize_prices,
    normalize_tibber_response,
    tibber_refresh_due,
    tibber_request,
)
from .logic.weather import WeatherSettings, compute_weather

_LOGGER = logging.getLogger(__name__)

# No lease is published during the first ticks after start: MQTT register
# values are not retained and the forecast is still loading, so the first
# totals would be built from zeros. An adopted lease has a 15 min TTL.
STARTUP_GRACE = timedelta(seconds=150)

# tibber.get_prices may call Tibber's API; never wait longer than this.
TIBBER_TIMEOUT_SECONDS = 60
# The Tibber integration may still be loading at start: raise the repair
# issue only when the action is missing on this many attempts in a row.
TIBBER_MISSING_ATTEMPTS = 2

CARDINALS = {
    "N": 0.0, "NNE": 22.5, "NE": 45.0, "ENE": 67.5, "E": 90.0, "ESE": 112.5,
    "SE": 135.0, "SSE": 157.5, "S": 180.0, "SSW": 202.5, "SW": 225.0,
    "WSW": 247.5, "W": 270.0, "WNW": 292.5, "NW": 315.0, "NNW": 337.5,
}


@dataclass
class SystemRuntime:
    system: str
    registers: Dict[str, int]
    lease: LeaseManager
    mode: str = MODE_SHADOW
    enabled: Dict[str, bool] = field(default_factory=lambda: {c: True for c in COMPONENTS})
    decisions: Dict[str, Decision] = field(default_factory=dict)
    plan: Optional[curve.CurvePlan] = None
    last_tick: Optional[datetime] = None
    healthy: bool = True
    error: Optional[str] = None
    last_action: Optional[str] = None


class NibePiRuntime:
    """One per config entry (one gateway)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.prefix: str = entry.data[CONF_PREFIX]
        self.readings: Dict[int, Reading] = {}
        self.base_adjust: Dict[str, float] = {}
        self.gateway_state: Dict[str, Any] = {}
        self.status_payload: Optional[str] = None
        self.status_at: Optional[datetime] = None
        self.gateway_state_at: Optional[datetime] = None
        self.gateway_state_live_at: Optional[datetime] = None
        self.forecast: Optional[List[ForecastPoint]] = None
        self.forecast_fetched: Optional[datetime] = None
        self.price_cache = PriceCache()
        self.price_level: Optional[PriceLevel] = None
        self.price_status: Optional[str] = None
        self.tibber_slots: List[PriceSlot] = []
        self.tibber_home: Optional[str] = None
        self.tibber_fetched: Optional[datetime] = None
        self._tibber_attempt: Optional[datetime] = None
        self._tibber_task: Optional[asyncio.Task] = None
        self._tibber_missing = 0
        self._tibber_logged: Optional[str] = None
        self.started: Optional[datetime] = None
        self.systems: Dict[str, SystemRuntime] = {}
        for system in entry.data.get(CONF_SYSTEMS, ["s1"]):
            regs = REGISTERS[system]
            self.systems[system] = SystemRuntime(
                system=system,
                registers=regs,
                lease=LeaseManager(regs["offset"], owner=OWNER),
            )
        self._unsubs: List[Callable[[], None]] = []
        self._subscribed: set = set()

    # -- helpers ----------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.data.get(CONF_NAME, DEFAULT_NAME),
            manufacturer=MANUFACTURER,
            model="F1255 (NibePi gateway)",
        )

    def opt(self, key: str, system: Optional[str] = None) -> Any:
        options = self.entry.options
        if system is not None:
            full = system_key(key, system)
            if full in options:
                return options[full]
            return SYSTEM_DEFAULTS.get(key)
        if key in options:
            return options[key]
        return SHARED_DEFAULTS.get(key)

    @property
    def status_topic(self) -> str:
        return self.prefix + "status"

    @property
    def gateway_online(self) -> Optional[bool]:
        return gateway_online(
            self.status_payload, self.gateway_state_live_at, self.started, dt_util.utcnow()
        )

    @property
    def last_message_at(self) -> Optional[datetime]:
        stamps = [t for t in (self.status_at, self.gateway_state_at) if t is not None]
        return max(stamps) if stamps else None

    def _registers(self) -> List[int]:
        regs = {REG_OUTDOOR}
        for sys in self.systems.values():
            regs.add(sys.registers["offset"])
            regs.add(sys.registers["curve"])
            if not self.opt(OPT_INDOOR_ENTITY, sys.system):
                regs.add(sys.registers["indoor"])
        return sorted(regs)

    async def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        await mqtt.async_publish(self.hass, self.prefix + topic, payload, 0, retain)

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    # -- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        self.started = dt_util.utcnow()
        await self._async_subscribe()
        await self._async_subscribe_registers()
        await self._async_request_adjust()
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_tick_cb, timedelta(seconds=TICK_SECONDS))
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_forecast_cb, timedelta(seconds=FORECAST_REFRESH_SECONDS)
            )
        )
        self._unsubs.append(
            async_call_later(self.hass, STATUS_TIMEOUT.total_seconds() + 1, self._check_status)
        )
        self.entry.async_create_background_task(
            self.hass, self._async_refresh_forecast(), "nibepi forecast"
        )

    async def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        self._subscribed.clear()
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_TIBBER_MISSING)
        for sys in self.systems.values():
            action = sys.lease.release_action()
            if action is not None:
                await self._async_send(sys, action)

    async def async_options_updated(self) -> None:
        # The price source or Tibber home may have changed.
        self.price_cache = PriceCache()
        self.tibber_slots = []
        self.tibber_home = None
        self.tibber_fetched = None
        self._tibber_attempt = None
        await self._async_subscribe_registers()
        await self._async_refresh_forecast()
        await self.async_tick()

    async def _async_subscribe_registers(self) -> None:
        """Subscribe to and ask the gateway to poll every needed register."""
        for reg in self._registers():
            if reg in self._subscribed:
                continue
            self._unsubs.append(
                await mqtt.async_subscribe(self.hass, self.prefix + str(reg), self._make_register_cb(reg))
            )
            self._subscribed.add(reg)
            await self._publish("%d/add" % reg, "ha")
            await self._publish("%d/get" % reg, "ha")

    async def _async_subscribe(self) -> None:
        self._unsubs.append(await mqtt.async_subscribe(self.hass, self.status_topic, self._on_status))
        self._unsubs.append(
            await mqtt.async_subscribe(self.hass, self.prefix + "gateway/state", self._on_gateway_state)
        )
        for sys in self.systems.values():
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass,
                    "%slease/%d/state" % (self.prefix, sys.registers["offset"]),
                    self._make_lease_cb(sys),
                )
            )
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass, "%sadjust_%s" % (self.prefix, sys.system), self._make_adjust_cb(sys.system)
                )
            )

    async def _async_request_adjust(self) -> None:
        for system in self.systems:
            await self._publish("adjust_%s/get" % system, "ha")

    # -- MQTT callbacks -----------------------------------------------------

    def _make_register_cb(self, reg: int) -> Callable[[Any], None]:
        @callback
        def _cb(msg: Any) -> None:
            value = to_float(msg.payload)
            if value is not None:
                self.readings[reg] = Reading(value, dt_util.utcnow())

        return _cb

    def _make_adjust_cb(self, system: str) -> Callable[[Any], None]:
        @callback
        def _cb(msg: Any) -> None:
            value = to_float(msg.payload)
            if value is not None:
                self.base_adjust[system] = value

        return _cb

    def _make_lease_cb(self, sys: SystemRuntime) -> Callable[[Any], None]:
        @callback
        def _cb(msg: Any) -> None:
            data = _json(msg.payload)
            if data is None:
                return
            previous_error = sys.lease.remote.get("error")
            sys.lease.note_remote_state(data)
            error = data.get("error")
            if error and error != previous_error:
                _LOGGER.warning("Gateway rejected lease for %s: %s", sys.registers["offset"], error)
            self._notify()

        return _cb

    @callback
    def _on_status(self, msg: Any) -> None:
        self.status_payload = str(msg.payload).strip()
        self.status_at = dt_util.utcnow()
        self._notify()

    @callback
    def _on_gateway_state(self, msg: Any) -> None:
        data = _json(msg.payload)
        if data is not None:
            now = dt_util.utcnow()
            self.gateway_state = data
            self.gateway_state_at = now
            # The retained copy replayed on subscribe may be from a gateway
            # that has since died; only a live one shows it is running.
            if not getattr(msg, "retain", False):
                self.gateway_state_live_at = now
            self._notify()

    @callback
    def _check_status(self, _now: datetime) -> None:
        if self.status_payload is None and self.gateway_state_at is None:
            _LOGGER.warning(status_hint(self.status_topic))
        self._notify()

    # -- control ----------------------------------------------------------

    def set_mode(self, system: str, mode: str) -> None:
        sys = self.systems[system]
        if sys.mode == mode:
            return
        _LOGGER.info("NibePi %s mode %s -> %s", system, sys.mode, mode)
        sys.mode = mode
        if self.started is not None:
            self.entry.async_create_background_task(self.hass, self.async_tick(), "nibepi tick")

    def set_enabled(self, system: str, component: str, enabled: bool) -> None:
        self.systems[system].enabled[component] = enabled

    async def _async_tick_cb(self, _now: datetime) -> None:
        await self.async_tick()

    async def _async_forecast_cb(self, _now: datetime) -> None:
        await self._async_refresh_forecast()
        await self._async_request_adjust()

    async def async_tick(self) -> None:
        now = dt_util.utcnow()
        try:
            self._schedule_tibber_refresh(now)
        except Exception:  # noqa: BLE001 - keep the tick going on cached prices
            _LOGGER.exception("Could not schedule the Tibber price refresh")
        for sys in self.systems.values():
            try:
                self._compute(sys, now)
                sys.healthy = True
                sys.error = None
            except Exception as err:  # noqa: BLE001 - stop renewing, gateway falls back
                _LOGGER.exception("NibePi tick failed for %s", sys.system)
                sys.healthy = False
                sys.error = repr(err)
            sys.last_tick = now
            in_grace = self.started is not None and now - self.started < STARTUP_GRACE
            target = sys.plan.target if (sys.plan is not None and sys.healthy) else None
            action = sys.lease.decide(sys.mode, target, sys.healthy and not in_grace)
            if action is not None:
                await self._async_send(sys, action)
        self._notify()

    async def _async_send(self, sys: SystemRuntime, action: LeaseAction) -> None:
        reg = sys.registers["offset"]
        topic = "lease/%d/%s" % (reg, "set" if action.kind == "set" else "release")
        try:
            await self._publish(topic, json.dumps(action.payload(sys.lease.ttl, OWNER)))
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not publish %s%s", self.prefix, topic, exc_info=True)
            return
        sys.lease.record(action)
        sys.last_action = "%s %s" % (action.kind, "" if action.value is None else action.value)
        if not action.renew:
            _LOGGER.info("NibePi %s: lease %s %s", reg, action.kind, action.value)

    def _compute(self, sys: SystemRuntime, now: datetime) -> None:
        if sys.mode == MODE_OFF:
            sys.decisions = {}
            sys.plan = None
            return
        timeout = self.opt(OPT_SENSOR_TIMEOUT)
        indoor = self._indoor_reading(sys)
        setpoint = self._setpoint(sys)
        try:
            self.price_level = self._price_level(now)
        except Exception:  # noqa: BLE001 - bad price attributes: no level
            _LOGGER.exception("Could not compute the price level")
            self.price_level = None

        def indoor_part() -> Decision:
            return compute_indoor(indoor, setpoint, self.opt(OPT_INDOOR_FACTOR, sys.system), now, timeout)

        def price_part() -> Decision:
            offsets = {level: self.opt(key, sys.system) for level, key in PRICE_LEVEL_OPTIONS.items()}
            return compute_price(
                self.price_level,
                offsets,
                now,
                guard_enabled=bool(self.opt(OPT_PRICE_GUARD, sys.system)),
                temp_low=self.opt(OPT_PRICE_TEMP_LOW, sys.system),
                indoor=indoor,
                setpoint=setpoint,
                sensor_timeout_min=timeout,
            )

        def weather_part() -> Decision:
            hours = to_float(self.opt(OPT_WEATHER_HOURS, sys.system)) or 0.0
            return compute_weather(
                self.readings.get(REG_OUTDOOR),
                self._value(sys.registers["curve"]),
                self.forecast,
                self.forecast_fetched,
                now,
                self._weather_settings(hours),
                daylight=self._daylight(now + timedelta(hours=hours)),
                sensor_timeout_min=timeout,
            )

        parts = {"indoor": indoor_part, "price": price_part, "weather": weather_part}
        decisions: Dict[str, Decision] = {}
        for name, part in parts.items():
            if not sys.enabled.get(name, True):
                decisions[name] = Decision.zero("disabled", inputs_ok=True)
                continue
            try:
                decisions[name] = part()
            except Exception as err:  # noqa: BLE001 - a broken input counts as missing
                _LOGGER.exception("NibePi %s %s component failed", sys.system, name)
                decisions[name] = Decision.zero("error: %r" % err)

        if sys.mode == MODE_ACTIVE and sys.lease.held and sys.lease.value is not None:
            reference: Optional[float] = float(sys.lease.value)
        else:
            reference = self._value(sys.registers["offset"])
        sys.decisions = decisions
        sys.plan = curve.plan(decisions, reference, self.base_adjust.get(sys.system, 0.0))

    # -- inputs -----------------------------------------------------------

    def _value(self, reg: int) -> Optional[float]:
        reading = self.readings.get(reg)
        return reading.value if reading is not None else None

    def _indoor_reading(self, sys: SystemRuntime) -> Optional[Reading]:
        entity_id = self.opt(OPT_INDOOR_ENTITY, sys.system)
        if not entity_id:
            return self.readings.get(sys.registers["indoor"])
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        value = state_value(state.state)
        if value is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        if unit and unit != UnitOfTemperature.CELSIUS:
            try:
                value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
            except Exception:  # noqa: BLE001 - unknown unit
                return None
        # last_reported: Netatmo often reports an unchanged value, which
        # does not move last_updated.
        ts = state_timestamp(getattr(state, "last_reported", None), state.last_updated)
        return Reading(value, ts) if ts is not None else None

    def _setpoint(self, sys: SystemRuntime) -> Optional[float]:
        entity_id = self.opt(OPT_SETPOINT_ENTITY, sys.system)
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state is None:
                return None
            return state_value(state.state)
        return to_float(self.opt(OPT_SETPOINT, sys.system))

    def _price_level(self, now: datetime) -> Optional[PriceLevel]:
        source = price_source(dict(self.entry.options))
        if source == PRICE_SOURCE_NONE:
            self.price_status = "no price source configured"
            return None
        if source == PRICE_SOURCE_TIBBER:
            slots = self.tibber_slots
        else:
            entity_id = self.opt(OPT_PRICE_ENTITY)
            if not entity_id:
                self.price_status = "no price sensor configured"
                return None
            state = self.hass.states.get(entity_id)
            attributes = dict(state.attributes) if state is not None else {}
            slots = normalize_prices(attributes, _local_tz(), now)
            self.price_status = "ok" if slots else "no prices in %s" % entity_id
        slots = self.price_cache.update(slots, now)
        return compute_level(
            slots,
            now,
            time_window_h=self.opt(OPT_PRICE_TIME_WINDOW),
            min_spread=self.opt(OPT_PRICE_MIN_SPREAD),
            unit_factor=self.opt(OPT_PRICE_UNIT_FACTOR),
            very_cheap_below=self.opt(OPT_PRICE_VERY_CHEAP_BELOW),
            very_expensive_above=self.opt(OPT_PRICE_VERY_EXPENSIVE_ABOVE),
        )

    # -- Tibber prices ------------------------------------------------------

    def _schedule_tibber_refresh(self, now: datetime) -> None:
        """Start a background tibber.get_prices call when one is due."""
        if price_source(dict(self.entry.options)) != PRICE_SOURCE_TIBBER:
            self._set_tibber_issue(False)
            return
        if self._tibber_task is not None and not self._tibber_task.done():
            return
        if not tibber_refresh_due(now, _local_tz(), self._tibber_attempt, self.tibber_slots):
            return
        self._tibber_attempt = now
        self._tibber_task = self.entry.async_create_background_task(
            self.hass, self._async_refresh_tibber(now), "nibepi tibber prices"
        )

    def _set_tibber_issue(self, missing: bool) -> None:
        if missing:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_TIBBER_MISSING,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_TIBBER_MISSING,
            )
        else:
            self._tibber_missing = 0
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_TIBBER_MISSING)

    def _tibber_problem(self, message: str) -> None:
        """Show ``message`` on the price level sensor; log it once."""
        self.price_status = message
        if message != self._tibber_logged:
            _LOGGER.warning("Tibber prices: %s", message)
            self._tibber_logged = message

    async def _async_refresh_tibber(self, now: datetime) -> None:
        tz = _local_tz()
        try:
            if not self.hass.services.has_service(TIBBER_DOMAIN, TIBBER_SERVICE):
                raise ServiceNotFound(TIBBER_DOMAIN, TIBBER_SERVICE)
            response = await asyncio.wait_for(
                self.hass.services.async_call(
                    TIBBER_DOMAIN,
                    TIBBER_SERVICE,
                    tibber_request(now, tz),
                    blocking=True,
                    return_response=True,
                ),
                TIBBER_TIMEOUT_SECONDS,
            )
        except ServiceNotFound:
            self._tibber_missing += 1
            if self._tibber_missing >= TIBBER_MISSING_ATTEMPTS:
                self._set_tibber_issue(True)
            self._tibber_problem(
                "%s.%s is not available; set up the Tibber integration or choose "
                "another price source" % (TIBBER_DOMAIN, TIBBER_SERVICE)
            )
            return
        except asyncio.TimeoutError:
            self._set_tibber_issue(False)
            self._tibber_problem("%s.%s timed out" % (TIBBER_DOMAIN, TIBBER_SERVICE))
            return
        except Exception as err:  # noqa: BLE001 - keep the cached prices
            self._set_tibber_issue(False)
            self._tibber_problem(
                "%s.%s failed: %s" % (TIBBER_DOMAIN, TIBBER_SERVICE, str(err) or repr(err))
            )
            return
        self._set_tibber_issue(False)
        result = normalize_tibber_response(response, tz, self.opt(OPT_TIBBER_HOME))
        if result.warning and result.warning != self._tibber_logged:
            _LOGGER.warning("Tibber prices: %s", result.warning)
            self._tibber_logged = result.warning
        if not result.slots:
            self._tibber_problem(result.warning or "no prices for Tibber home %r" % result.home)
            return
        self.tibber_slots = result.slots
        self.tibber_home = result.home
        self.tibber_fetched = dt_util.utcnow()
        self.price_status = result.warning or "ok"
        if result.warning is None:
            self._tibber_logged = None
        self._notify()

    def _weather_settings(self, hours: float) -> WeatherSettings:
        return WeatherSettings(
            hours=hours,
            enable_up=bool(self.opt(OPT_WEATHER_UP)),
            enable_down=bool(self.opt(OPT_WEATHER_DOWN)),
            forecast_adjust=bool(self.opt(OPT_WEATHER_ADJUST)),
            wind_enable=bool(self.opt(OPT_WIND_ENABLE)),
            wind_factors={
                "n": self.opt(OPT_WIND_N),
                "s": self.opt(OPT_WIND_S),
                "w": self.opt(OPT_WIND_W),
                "e": self.opt(OPT_WIND_E),
            },
            sun_enable=bool(self.opt(OPT_SUN_ENABLE)),
            sun_factors={
                "clear": self.opt(OPT_SUN_CLEAR),
                "half_clear": self.opt(OPT_SUN_HALF_CLEAR),
            },
        )

    def _daylight(self, when: datetime) -> Optional[bool]:
        try:
            return bool(sun_helper.is_up(self.hass, when))
        except Exception:  # noqa: BLE001 - no location configured
            return None

    async def _async_refresh_forecast(self) -> None:
        entity_id = self.opt(OPT_WEATHER_ENTITY)
        if not entity_id:
            self.forecast = None
            self.forecast_fetched = None
            return
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - keep the cached forecast
            _LOGGER.warning("Could not fetch forecast from %s: %s", entity_id, err)
            return
        items = ((response or {}).get(entity_id) or {}).get("forecast") or []
        state = self.hass.states.get(entity_id)
        attrs = state.attributes if state is not None else {}
        points = _forecast_points(
            items,
            attrs.get("temperature_unit") or UnitOfTemperature.CELSIUS,
            attrs.get("wind_speed_unit") or UnitOfSpeed.METERS_PER_SECOND,
        )
        if points:
            self.forecast = points
            self.forecast_fetched = dt_util.utcnow()
        else:
            _LOGGER.warning("Forecast from %s had no usable hourly points", entity_id)


def _json(payload: Any) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _local_tz() -> Any:
    getter = getattr(dt_util, "get_default_time_zone", None)
    return getter() if getter is not None else dt_util.DEFAULT_TIME_ZONE


def _bearing(value: Any) -> Optional[float]:
    number = to_float(value)
    if number is not None:
        return number % 360 if number != 360 else 360.0
    if isinstance(value, str):
        return CARDINALS.get(value.strip().upper())
    return None


def _forecast_points(items: List[Dict[str, Any]], temp_unit: str, speed_unit: str) -> List[ForecastPoint]:
    points = []
    for item in items:
        when = dt_util.parse_datetime(str(item.get("datetime", "")))
        temp = to_float(item.get("temperature"))
        if when is None or temp is None:
            continue
        if temp_unit != UnitOfTemperature.CELSIUS:
            temp = TemperatureConverter.convert(temp, temp_unit, UnitOfTemperature.CELSIUS)
        speed = to_float(item.get("wind_speed"))
        gust = to_float(item.get("wind_gust_speed"))
        if speed_unit != UnitOfSpeed.METERS_PER_SECOND:
            if speed is not None:
                speed = SpeedConverter.convert(speed, speed_unit, UnitOfSpeed.METERS_PER_SECOND)
            if gust is not None:
                gust = SpeedConverter.convert(gust, speed_unit, UnitOfSpeed.METERS_PER_SECOND)
        points.append(
            ForecastPoint(
                time=dt_util.as_utc(when),
                temperature=temp,
                wind_speed=speed,
                wind_bearing=_bearing(item.get("wind_bearing")),
                wind_gust=gust,
                condition=item.get("condition"),
            )
        )
    return points
