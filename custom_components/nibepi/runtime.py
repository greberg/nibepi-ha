"""Runtime: collect inputs, run the controllers every tick, publish leases.

All decisions are made by the pure modules in ``logic``; this file only
moves data between Home Assistant, MQTT and those modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import sun as sun_helper
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from .const import (
    AWAY_STORE_VERSION,
    COMPONENTS,
    CONF_NAME,
    CONF_PREFIX,
    CONF_SYSTEMS,
    DEFAULT_NAME,
    DOMAIN,
    FORECAST_REFRESH_SECONDS,
    HW_COMFORT_OPTIONS,
    ISSUE_LEGIONELLA_FAILED,
    ISSUE_TIBBER_MISSING,
    LEGIONELLA_HISTORY_DAYS,
    MANUFACTURER,
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
    OPT_HW_PEAK,
    OPT_HW_PREHEAT,
    OPT_HW_PRICE_WINDOW,
    OPT_HW_USE_PRICE,
    OPT_HW_LEGIONELLA,
    OPT_HW_LEGIONELLA_HOLD,
    OPT_HW_LEGIONELLA_INTERVAL,
    OPT_HW_LEGIONELLA_MAX,
    OPT_HW_LEGIONELLA_START,
    OPT_HW_LEGIONELLA_TEMP,
    OPT_HW_LEGIONELLA_WINDOW,
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
    OPT_RMU_ENTITY,
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
    OPTIMIZER_REGISTERS,
    OWNER,
    PRICE_LEVEL_OPTIONS,
    PRICE_SOURCE_NONE,
    PRICE_SOURCE_TIBBER,
    REG_OUTDOOR,
    REGISTERS,
    SHARED_DEFAULTS,
    SYSTEM_DEFAULTS,
    TICK_SECONDS,
    VV_IMPORT_MAX_BYTES,
    VV_STORE_SAVE_DELAY,
    VV_STORE_VERSION,
    away_store_key,
    price_source,
    signal_reading,
    signal_update,
    system_key,
    vv_store_key,
)
from .logic import curve
from .logic.away import (
    AwaySettings,
    AwayState,
    away_component,
    away_setpoint,
    indoor_active,
    rules_away,
)
from .logic.away import start as away_start
from .logic.gateway import STATUS_TIMEOUT, device_sw_version, gateway_online, gateway_version, status_hint
from .logic.registers import came_online, registers_to_request
from .logic.hwcontrol import (
    FEATURE_DEFAULTS,
    INPUT_REGISTERS,
    REG_BT7,
    HotWaterController,
    HotWaterDecision,
    HotWaterInputs,
    HotWaterLeases,
    HotWaterSettings,
)
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
from .logic.rmu import RoomTemperature, rmu_decide, rmu_lease_manager
from .logic.vvimport import convert_nodered_store, read_json_file
from .logic.legionella import (
    RESULT_CANCELLED,
    RESULT_DONE,
    LegionellaSettings,
    LegionellaState,
    last_done_from_history,
)
from .logic.legionella import TOLERANCE as LEGIONELLA_TOLERANCE
from .logic.opthistory import parse_states
from .logic.pump import BUS_HEALTH, parse_bus_health, pump_registers
from .logic.vvlearn import VvProfile
from .logic.vvplan import PlanSettings
from .logic.weather import WeatherSettings, compute_weather
from .opt_runtime import OptimizerRuntime

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
    rmu_lease: LeaseManager
    mode: str = MODE_SHADOW
    enabled: Dict[str, bool] = field(default_factory=lambda: {c: True for c in COMPONENTS})
    decisions: Dict[str, Decision] = field(default_factory=dict)
    plan: Optional[curve.CurvePlan] = None  # what goes to the lease
    rules_plan: Optional[curve.CurvePlan] = None  # the Phase 1 sum
    last_tick: Optional[datetime] = None
    healthy: bool = True
    error: Optional[str] = None
    last_action: Optional[str] = None
    # RMU40 room temperature feed (own switch, independent of mode).
    rmu_enabled: bool = False
    rmu_room: Optional[RoomTemperature] = None
    rmu_entity: Optional[str] = None
    rmu_last_action: Optional[str] = None
    rmu_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class HotWaterRuntime:
    """Hot water control (shared by the climate systems)."""

    controller: HotWaterController
    leases: HotWaterLeases
    mode: str = MODE_SHADOW
    features: Dict[str, bool] = field(default_factory=lambda: dict(FEATURE_DEFAULTS))
    decision: Optional[HotWaterDecision] = None
    last_tick: Optional[datetime] = None
    healthy: bool = True
    error: Optional[str] = None
    last_actions: Dict[int, str] = field(default_factory=dict)
    store_loaded: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class NibePiRuntime:
    """One per config entry (one gateway)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.prefix: str = entry.data[CONF_PREFIX]
        self.readings: Dict[int, Reading] = {}
        self.base_adjust: Dict[str, float] = {}
        self.gateway_state: Dict[str, Any] = {}
        # The integration's own version (manifest), set before the platforms.
        self.integration_version: Optional[str] = None
        self._sw_version: Optional[str] = None
        self.status_payload: Optional[str] = None
        self.status_at: Optional[datetime] = None
        self.gateway_state_at: Optional[datetime] = None
        self.gateway_state_live_at: Optional[datetime] = None
        # The gateway's <prefix>bus_health (logic/pump.py) and when it came.
        self.bus_health: Optional[Dict[str, Any]] = None
        self.bus_health_at: Optional[datetime] = None
        self.forecast: Optional[List[ForecastPoint]] = None
        self.forecast_fetched: Optional[datetime] = None
        self.price_cache = PriceCache()
        self.price_level: Optional[PriceLevel] = None
        self.price_level_at: Optional[datetime] = None
        self.price_slots: List[PriceSlot] = []
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
                rmu_lease=rmu_lease_manager(regs["rmu"], owner=OWNER),
            )
        self.hotwater = HotWaterRuntime(HotWaterController(), HotWaterLeases(owner=OWNER))
        self.vv_store: Store = Store(hass, VV_STORE_VERSION, vv_store_key(entry.entry_id))
        # Away (vacation) mode: one state for all systems and hot water.
        self.away = AwayState()
        self.away_store: Store = Store(hass, AWAY_STORE_VERSION, away_store_key(entry.entry_id))
        self._away_end: Optional[Callable[[], None]] = None
        self._legionella_history_task: Optional[asyncio.Task] = None
        self._legionella_history_tries = 0
        self.optimizer = OptimizerRuntime(self)
        # Card key (translation key, "_<system>" per system) -> entity id of
        # each added entity, for the cards (logic/cards.py).
        self.entity_ids: Dict[str, str] = {}
        self._unsubs: List[Callable[[], None]] = []
        self._subscribed: set = set()
        # Register -> when /add + /get was last sent (logic/registers.py).
        self._requested: Dict[int, datetime] = {}

    # -- helpers ----------------------------------------------------------

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name=self.entry.data.get(CONF_NAME, DEFAULT_NAME),
            manufacturer=MANUFACTURER,
            model="F1255 (NibePi gateway)",
            sw_version=self.sw_version,
        )

    @property
    def sw_version(self) -> Optional[str]:
        """The device page's firmware line: gateway and integration versions."""
        version, build = gateway_version(self.gateway_state)
        return device_sw_version(version, build, self.integration_version)

    async def async_load_integration_version(self) -> None:
        try:
            integration = await async_get_integration(self.hass, DOMAIN)
            self.integration_version = str(integration.version) if integration.version else None
        except Exception:  # noqa: BLE001 - the version is only shown
            _LOGGER.debug("NibePi: integration version unknown", exc_info=True)
        self._sw_version = self.sw_version

    def _update_device_version(self) -> None:
        """Show a new gateway version on the device page."""
        sw_version = self.sw_version
        if sw_version == self._sw_version:
            return
        self._sw_version = sw_version
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, self.entry.entry_id)})
        if device is not None and device.sw_version != sw_version:
            registry.async_update_device(device.id, sw_version=sw_version)

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
            regs.add(sys.registers["supply"])
            if not self.opt(OPT_INDOOR_ENTITY, sys.system):
                regs.add(sys.registers["indoor"])
        # Hot water: BT6, BT7, prio, comfort mode, period, temporary lux and
        # the start/stop temperatures.
        regs.update(INPUT_REGISTERS)
        # Cost optimiser: brine, compressor power and frequency, addition,
        # heat meters, BT3 and the heating medium pump.
        regs.update(OPTIMIZER_REGISTERS)
        # The own pump sensors (BT1, BT7, BT6, BT2, BT3, BT50 of each
        # system, degree minutes, compressor, priority).
        regs.update(pump_registers(self.systems))
        return sorted(regs)

    async def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        await mqtt.async_publish(self.hass, self.prefix + topic, payload, 0, retain)

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    # -- lifecycle --------------------------------------------------------

    async def async_start(self) -> None:
        self.started = dt_util.utcnow()
        await self._async_load_vv_store()
        await self._async_subscribe()
        await self._async_subscribe_registers()
        await self._async_request_adjust()
        await self.optimizer.async_start()
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
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_rerequest_cb, timedelta(minutes=5))
        )
        self.entry.async_create_background_task(
            self.hass, self._async_refresh_forecast(), "nibepi forecast"
        )

    async def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        self._cancel_away_end()
        self._subscribed.clear()
        self._requested.clear()
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_TIBBER_MISSING)
        for sys in self.systems.values():
            action = sys.lease.release_action()
            if action is not None:
                await self._async_send(sys, action)
            action = sys.rmu_lease.release_action()
            if action is not None:
                await self._async_send(sys, action, sys.rmu_lease)
        for lease, action in self.hotwater.leases.release_all():
            await self._async_publish_lease(lease, action)
        if self.hotwater.store_loaded:
            await self.vv_store.async_save(self._vv_store_data())
        await self.optimizer.async_stop()

    async def async_options_updated(self) -> None:
        # The price source or Tibber home may have changed.
        self.price_cache = PriceCache()
        self.tibber_slots = []
        self.tibber_home = None
        self.tibber_fetched = None
        self._tibber_attempt = None
        await self._async_subscribe_registers()
        await self._async_refresh_forecast()
        await self.optimizer.async_options_updated()
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
            await self._async_request_register(reg)

    async def _async_request_register(self, reg: int) -> None:
        """Ask the gateway to poll ``reg`` and send its value now."""
        self._requested[reg] = dt_util.utcnow()
        await self._publish("%d/add" % reg, "ha")
        await self._publish("%d/get" % reg, "ha")

    async def _async_rerequest(self, registers: Optional[List[int]] = None) -> None:
        """Repeat /add + /get: values are not retained, so a request sent while
        the gateway was down is lost (logic/registers.py)."""
        if registers is None:
            now = dt_util.utcnow()
            seen = {reg: reading.ts for reg, reading in self.readings.items()}
            registers = registers_to_request(self._subscribed, seen, self._requested, now)
        if not registers:
            return
        _LOGGER.debug("NibePi: asking the gateway again for %s", registers)
        for reg in registers:
            await self._async_request_register(reg)
        await self._async_request_adjust()

    async def _async_rerequest_cb(self, _now: datetime) -> None:
        await self._async_rerequest()

    async def _async_subscribe(self) -> None:
        self._unsubs.append(await mqtt.async_subscribe(self.hass, self.status_topic, self._on_status))
        self._unsubs.append(
            await mqtt.async_subscribe(self.hass, self.prefix + "gateway/state", self._on_gateway_state)
        )
        self._unsubs.append(
            await mqtt.async_subscribe(self.hass, self.prefix + BUS_HEALTH, self._on_bus_health)
        )
        leases = [lease for sys in self.systems.values() for lease in (sys.lease, sys.rmu_lease)]
        leases.extend(self.hotwater.leases.leases.values())
        for lease in leases:
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass,
                    "%slease/%d/state" % (self.prefix, lease.register),
                    self._make_lease_cb(lease),
                )
            )
        for sys in self.systems.values():
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
                async_dispatcher_send(self.hass, signal_reading(self.entry.entry_id, reg))

        return _cb

    def _make_adjust_cb(self, system: str) -> Callable[[Any], None]:
        @callback
        def _cb(msg: Any) -> None:
            value = to_float(msg.payload)
            if value is not None:
                self.base_adjust[system] = value

        return _cb

    def _make_lease_cb(self, lease: LeaseManager) -> Callable[[Any], None]:
        @callback
        def _cb(msg: Any) -> None:
            data = _json(msg.payload)
            if data is None:
                return
            previous_error = lease.remote.get("error")
            lease.note_remote_state(data)
            error = data.get("error")
            if error and error != previous_error:
                _LOGGER.warning("Gateway rejected lease for %s: %s", lease.register, error)
            self._notify()

        return _cb

    @callback
    def _on_status(self, msg: Any) -> None:
        previous = self.status_payload
        self.status_payload = str(msg.payload).strip()
        if came_online(previous, self.status_payload):
            # A (re)started gateway may not poll what we asked for before.
            self.entry.async_create_background_task(
                self.hass, self._async_rerequest(sorted(self._subscribed)), "nibepi register requests"
            )
        self.status_at = dt_util.utcnow()
        self._notify()

    @callback
    def _on_gateway_state(self, msg: Any) -> None:
        data = _json(msg.payload)
        if data is not None:
            now = dt_util.utcnow()
            self.gateway_state = data
            self.gateway_state_at = now
            self._update_device_version()
            # The retained copy replayed on subscribe may be from a gateway
            # that has since died; only a live one shows it is running.
            if not getattr(msg, "retain", False):
                self.gateway_state_live_at = now
            self._notify()

    @callback
    def _on_bus_health(self, msg: Any) -> None:
        data = parse_bus_health(msg.payload)
        if data is not None:
            self.bus_health = data
            self.bus_health_at = dt_util.utcnow()
            async_dispatcher_send(self.hass, signal_reading(self.entry.entry_id, BUS_HEALTH))

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
            self.optimizer.request_plan()

    def set_enabled(self, system: str, component: str, enabled: bool) -> None:
        self.systems[system].enabled[component] = enabled

    def set_rmu_enabled(self, system: str, enabled: bool) -> None:
        sys = self.systems[system]
        if sys.rmu_enabled == enabled:
            return
        _LOGGER.info("NibePi %s RMU40 room temperature feed %s", system, "on" if enabled else "off")
        sys.rmu_enabled = enabled
        if self.started is not None:
            self.entry.async_create_background_task(
                self.hass, self._async_rmu_step(sys, dt_util.utcnow()), "nibepi rmu"
            )

    # -- away ---------------------------------------------------------------

    def away_settings(self) -> AwaySettings:
        return AwaySettings.from_values(
            setback=self.opt(OPT_AWAY_SETBACK),
            min_indoor=self.opt(OPT_AWAY_MIN_INDOOR),
            curve_offset=self.opt(OPT_AWAY_CURVE_OFFSET),
            hot_water=self.opt(OPT_AWAY_HOT_WATER),
            preheat=self.opt(OPT_AWAY_PREHEAT),
            return_lead_h=self.opt(OPT_AWAY_RETURN_LEAD),
        )

    def away_rules(self, now: datetime) -> bool:
        """Away for the rules strategy and hot water at ``now``."""
        return rules_away(self.away, self.away_settings(), now)

    def away_setpoint(self, sys: SystemRuntime) -> Optional[float]:
        """The lowered setpoint of ``sys`` (whether or not Away is on)."""
        settings = self.away_settings()
        return away_setpoint(self._setpoint(sys), settings.setback_for(self.away), settings.min_indoor)

    async def async_load_away(self) -> None:
        """Restore the Away state; one whose return has passed ends now."""
        try:
            data = await self.away_store.async_load()
        except Exception:  # noqa: BLE001 - a broken file must not stop setup
            _LOGGER.exception("Could not load the Away state; Away is off")
            data = None
        state = AwayState.from_dict(data)
        if state.expired(dt_util.utcnow()):
            _LOGGER.info("NibePi Away ended while Home Assistant was stopped (return %s)", state.until)
            state = AwayState()
            await self.away_store.async_save(state.to_dict())
        self.away = state
        self._schedule_away_end()

    async def async_set_away(
        self, enabled: bool, until: Optional[datetime] = None, setback: Optional[float] = None
    ) -> None:
        """Start (or change) or end Away. ``until`` not in the future: no end."""
        now = dt_util.utcnow()
        if enabled:
            state = away_start(now, until, setback)
            if self.away.enabled and self.away.since is not None:
                state = AwayState(True, state.until, state.setback, self.away.since)
        else:
            state = AwayState()
        if state == self.away:
            return
        _LOGGER.info(
            "NibePi Away %s%s",
            "on" if state.enabled else "off",
            " until %s" % state.until.isoformat() if state.until is not None else "",
        )
        if state.enabled and not self.away.enabled:
            idle = [s.system for s in self.systems.values() if s.mode != MODE_ACTIVE]
            if idle:
                _LOGGER.info(
                    "NibePi Away is on but %s %s not in active mode: the lowered target is "
                    "only computed, nothing is written to the pump",
                    ", ".join(idle), "is" if len(idle) == 1 else "are",
                )
        self.away = state
        await self.away_store.async_save(state.to_dict())
        self._schedule_away_end()
        self._away_changed()

    async def async_turn_away_on(self) -> None:
        """The switch: start Away without an end; leave a running Away as it is."""
        if not self.away.enabled:
            await self.async_set_away(True)

    def _away_changed(self) -> None:
        self._notify()
        if self.started is not None:
            self.entry.async_create_background_task(self.hass, self.async_tick(), "nibepi tick")
            self.optimizer.request_plan()

    def _cancel_away_end(self) -> None:
        if self._away_end is not None:
            self._away_end()
            self._away_end = None

    def _schedule_away_end(self) -> None:
        self._cancel_away_end()
        until = self.away.until if self.away.enabled else None
        if until is not None:
            self._away_end = async_track_point_in_time(self.hass, self._async_away_end_cb, until)

    async def _async_away_end_cb(self, _now: datetime) -> None:
        self._away_end = None
        if self.away.enabled:
            _LOGGER.info("NibePi Away ends: the return time %s has come", self.away.until)
            await self.async_set_away(False)

    # -- hot water --------------------------------------------------------

    def set_hw_mode(self, mode: str) -> None:
        hw = self.hotwater
        if hw.mode == mode:
            return
        _LOGGER.info("NibePi hot water mode %s -> %s", hw.mode, mode)
        hw.mode = mode
        self._hw_step_soon()
        self.optimizer.request_plan()

    def set_hw_feature(self, feature: str, enabled: bool) -> None:
        hw = self.hotwater
        if hw.features.get(feature) == enabled:
            return
        _LOGGER.info("NibePi hot water %s %s", feature, "on" if enabled else "off")
        hw.features[feature] = enabled
        self._hw_step_soon()

    def _hw_step_soon(self) -> None:
        if self.started is not None:
            self.entry.async_create_background_task(
                self.hass,
                self._async_hotwater_step(dt_util.utcnow(), sample=False),
                "nibepi hot water",
            )

    async def _async_load_vv_store(self) -> None:
        try:
            data = await self.vv_store.async_load()
        except Exception:  # noqa: BLE001 - a broken file must not stop setup
            _LOGGER.exception("Could not load the learned hot water profile; starting empty")
            data = None
        self.hotwater.controller.profile = VvProfile.from_dict(data)
        # The legionella guard's state lives in the same store (key
        # "legionella"; older files have none).
        self.hotwater.controller.legionella.state = LegionellaState.from_dict(
            data.get("legionella") if isinstance(data, dict) else None
        )
        self.hotwater.store_loaded = True

    def _vv_store_data(self) -> Dict[str, Any]:
        controller = self.hotwater.controller
        data = controller.profile.to_dict()
        data["legionella"] = controller.legionella.state.to_dict()
        return data

    def _save_vv_store(self) -> None:
        # Looked up at write time: an import replaces the profile object.
        self.vv_store.async_delay_save(self._vv_store_data, VV_STORE_SAVE_DELAY)

    async def async_import_vv_profile(self, path: str) -> Dict[str, Any]:
        """Replace the learned profile with Node-RED's ``vv_ai_profile.json``.

        ``path`` must lie inside the configuration directory. Raises
        ``ProfileImportError`` (a ValueError) with the reason.
        """
        config_dir = self.hass.config.config_dir
        real, data = await self.hass.async_add_executor_job(
            read_json_file, config_dir, path, VV_IMPORT_MAX_BYTES
        )
        source = os.path.relpath(real, os.path.realpath(config_dir))
        profile, summary = convert_nodered_store(data, source)
        hw = self.hotwater
        async with hw.lock:
            hw.controller.profile = profile
            hw.controller.detector.reset()
            hw.controller.vv.reset()
            await self.vv_store.async_save(self._vv_store_data())
        _LOGGER.info(
            "Imported the Node-RED hot water profile from %s: %d learned hours",
            source, summary["learned_slots"],
        )
        self._notify()
        return summary

    def _hw_settings(self) -> HotWaterSettings:
        return HotWaterSettings(
            learning_rate=self.opt(OPT_HW_LEARNING_RATE),
            min_drop=self.opt(OPT_HW_MIN_DROP),
            plan=PlanSettings.from_values(
                day_small=self.opt(OPT_HW_DAY_SMALL),
                day_medium=self.opt(OPT_HW_DAY_MEDIUM),
                day_large=self.opt(OPT_HW_DAY_LARGE),
                peak_threshold=self.opt(OPT_HW_PEAK),
                preheat_hours=self.opt(OPT_HW_PREHEAT),
                use_price=bool(self.opt(OPT_HW_USE_PRICE)),
                price_window_hours=self.opt(OPT_HW_PRICE_WINDOW),
            ),
            floor_enabled=bool(self.opt(OPT_HW_MIN_TEMP_ENABLE)),
            floor_temp=to_float(self.opt(OPT_HW_MIN_TEMP)),
            autolux_diff=self.opt(OPT_HW_AUTOLUX_DIFF),
            autolux_time=self.opt(OPT_HW_AUTOLUX_TIME),
            comfort={level: self.opt(key) for level, key in HW_COMFORT_OPTIONS.items()},
            sensor_timeout=self.opt(OPT_SENSOR_TIMEOUT),
            legionella=self._legionella_settings(),
        )

    def _legionella_settings(self) -> LegionellaSettings:
        return LegionellaSettings.from_values(
            enabled=self.opt(OPT_HW_LEGIONELLA),
            temperature=self.opt(OPT_HW_LEGIONELLA_TEMP),
            interval_days=self.opt(OPT_HW_LEGIONELLA_INTERVAL),
            hold_minutes=self.opt(OPT_HW_LEGIONELLA_HOLD),
            window_hours=self.opt(OPT_HW_LEGIONELLA_WINDOW),
            max_hours=self.opt(OPT_HW_LEGIONELLA_MAX),
            start_hour=self.opt(OPT_HW_LEGIONELLA_START),
        )

    # -- legionella guard -------------------------------------------------

    async def async_run_legionella(self) -> None:
        """The "Run legionella cycle now" button: start one at once."""
        hw = self.hotwater
        if hw.mode != MODE_ACTIVE:
            _LOGGER.warning(
                "NibePi: legionella cycle not started: hot water mode is %s (it runs only in active)", hw.mode
            )
            raise HomeAssistantError(
                "Hot water mode is %s: a legionella cycle runs only in active mode" % hw.mode
            )
        if not self._legionella_settings().enabled:
            raise HomeAssistantError("The legionella guard is off in the hot water options")
        _LOGGER.info("NibePi: legionella cycle requested")
        hw.controller.legionella.requested = True
        self._hw_step_soon()

    def _legionella_after_step(self, now: datetime) -> None:
        """Save, log and raise or clear the repair issue after a step."""
        guard = self.hotwater.controller.legionella
        ended = guard.ended
        guard.ended = None
        if ended == RESULT_DONE:
            _LOGGER.info("NibePi: legionella cycle done")
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_LEGIONELLA_FAILED)
        elif ended == RESULT_CANCELLED:
            _LOGGER.info("NibePi: legionella cycle cancelled (hot water mode not active or guard off)")
        elif ended is not None:
            _LOGGER.warning("NibePi: legionella cycle %s; retried later", ended)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_LEGIONELLA_FAILED,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_LEGIONELLA_FAILED,
                translation_placeholders={"result": ended},
            )
        elif guard.state.last_done is not None and guard.state.last_done == now:
            _LOGGER.info("NibePi: legionella temperature held (%s)", guard.state.last_done_by)
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_LEGIONELLA_FAILED)
        if guard.dirty:
            guard.dirty = False
            self._save_vv_store()
        if (
            self.hotwater.store_loaded
            and not guard.state.history_checked
            and self._legionella_history_task is None
            and self.hass.is_running
        ):
            self._legionella_history_task = self.entry.async_create_background_task(
                self.hass, self._async_legionella_history(), "nibepi legionella history"
            )

    async def _async_legionella_history(self) -> None:
        """First start: find the last legionella hold in the recorder's BT7."""
        hw = self.hotwater
        guard = hw.controller.legionella
        now = dt_util.utcnow()
        done_at: Optional[datetime] = None
        covered: Optional[datetime] = None
        try:
            # The own BT7 sensor and the gateway's discovered one (older
            # history, when its discovery is on): one series, merged.
            sources = self.optimizer.history_sources(REG_BT7)
            if "recorder" not in self.hass.config.components or not sources:
                self._legionella_history_tries += 1
                if self._legionella_history_tries < 10:
                    return  # discovery may come later: tried again on a later tick
                _LOGGER.info("NibePi: no BT7 history for the legionella guard; counting from now")
            else:
                from homeassistant.components.recorder import get_instance

                from .opt_runtime import _fetch_history

                settings = self._legionella_settings()
                status = guard.status
                temperature = status.info.get("temperature") if status is not None else None
                if temperature is None:
                    temperature = settings.temperature
                start = now - timedelta(days=LEGIONELLA_HISTORY_DAYS)
                rows: List[Any] = []
                for entity_id in sources:
                    rows.extend(
                        await get_instance(self.hass).async_add_executor_job(
                            _fetch_history, self.hass, entity_id, start, now
                        )
                    )
                points = parse_states(rows)
                done = last_done_from_history(
                    points, temperature - LEGIONELLA_TOLERANCE, settings.hold_minutes * 60.0, now.timestamp()
                )
                if done is not None:
                    done_at = dt_util.utc_from_timestamp(done)
                if points:
                    covered = dt_util.utc_from_timestamp(points[0][0])
                _LOGGER.info(
                    "NibePi: legionella history of %s (%d rows): last hold of %.1f °C %s",
                    ", ".join(sources), len(rows), temperature, done_at.isoformat() if done_at else "not found",
                )
            async with hw.lock:
                guard.apply_history(done_at, covered, now)
            self._save_vv_store()
            self._notify()
        except Exception:  # noqa: BLE001 - detection on live values goes on
            _LOGGER.exception("NibePi: legionella history lookup failed")
            async with hw.lock:
                guard.apply_history(None, None, now)
            self._save_vv_store()
        finally:
            self._legionella_history_task = None

    async def _async_hotwater_step(self, now: datetime, sample: bool = True) -> None:
        """Learn, decide and publish the hot water leases for this tick.

        ``sample`` False (a switch or mode change between ticks) decides
        again without feeding the per-minute learning and automatic luxury
        samples.
        """
        hw = self.hotwater
        in_grace = self.started is not None and now - self.started < STARTUP_GRACE
        async with hw.lock:
            try:
                if not hw.store_loaded:
                    raise RuntimeError("learned profile not loaded")
                if hw.mode != MODE_OFF and self.price_level_at != now:
                    try:
                        self.price_level = self._price_level(now)
                    except Exception:  # noqa: BLE001 - bad price attributes: no level
                        _LOGGER.exception("Could not compute the price level")
                        self.price_level = None
                    self.price_level_at = now
                level = self.price_level
                windows, problem = self.optimizer.hw_plan_windows(now)
                away_hw = self.away_settings().hot_water if self.away_rules(now) else None
                inputs = HotWaterInputs(
                    now=now,
                    tz=_local_tz(),
                    readings=dict(self.readings),
                    price_level=level.level if level is not None else None,
                    price_slots=list(self.price_slots),
                    held=hw.leases.held(),
                    optimiser_windows=windows,
                    optimiser_problem=problem,
                    away=away_hw,
                )
                settings = self._hw_settings()
                learning = hw.mode != MODE_OFF and (
                    hw.features.get("learning", False) or hw.features.get("vv_control", False)
                )
                if learning and sample and away_hw is not None:
                    # Away: no draws are expected; neither learn nor decay.
                    if hw.controller.learn_away(inputs):
                        self._save_vv_store()
                elif sample and hw.controller.learn(inputs, settings, learning):
                    self._save_vv_store()
                event = hw.controller.last_event
                if event is not None and hw.controller.last_event_at == now:
                    _LOGGER.debug(
                        "Hot water draw %s: %.1f °C over %d min (%.2f °C/h, %s)",
                        event.verdict, event.span, event.minutes, event.rate, event.reason,
                    )
                hw.decision = hw.controller.decide(hw.mode, hw.features, inputs, settings, sample)
                hw.healthy = True
                hw.error = None
            except Exception as err:  # noqa: BLE001 - release, the gateway restores
                _LOGGER.exception("NibePi hot water tick failed")
                hw.healthy = False
                hw.error = repr(err)
                hw.controller.reset_control()
            hw.last_tick = now
            self._legionella_after_step(now)
            for lease, action in hw.leases.decide(hw.mode, hw.decision, hw.healthy, in_grace):
                if await self._async_publish_lease(lease, action):
                    hw.last_actions[lease.register] = "%s %s" % (
                        action.kind, "" if action.value is None else action.value
                    )
        self._notify()

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
            await self._async_rmu_step(sys, now)
        try:
            self.optimizer.sample(now)
        except Exception:  # noqa: BLE001 - history is best effort
            _LOGGER.exception("NibePi optimiser: sampling failed")
        try:
            self.optimizer.on_tick(now)
        except Exception:  # noqa: BLE001 - the rules keep running
            _LOGGER.exception("NibePi optimiser: scheduling a plan failed")
        await self._async_hotwater_step(now)
        self._notify()

    async def _async_rmu_step(self, sys: SystemRuntime, now: datetime) -> None:
        """Feed (or stop feeding) the room temperature to the RMU40 register."""
        async with sys.rmu_lock:
            try:
                entity_id = self.opt(OPT_RMU_ENTITY, sys.system) or self.opt(OPT_INDOOR_ENTITY, sys.system)
                sys.rmu_entity = entity_id or None
                reading = self._entity_reading(entity_id) if entity_id else None
                action, room = rmu_decide(
                    sys.rmu_lease, sys.rmu_enabled, reading, now, self.opt(OPT_SENSOR_TIMEOUT)
                )
                if sys.rmu_enabled and not entity_id:
                    room = RoomTemperature(None, "no room temperature entity configured")
                sys.rmu_room = room
            except Exception as err:  # noqa: BLE001 - send nothing, the lease runs out
                _LOGGER.exception("NibePi %s RMU40 feed failed", sys.system)
                sys.rmu_room = RoomTemperature(None, "error: %r" % err)
                return
            if action is not None:
                await self._async_send(sys, action, sys.rmu_lease)
        self._notify()

    async def _async_send(
        self, sys: SystemRuntime, action: LeaseAction, lease: Optional[LeaseManager] = None
    ) -> None:
        lease = lease or sys.lease
        if not await self._async_publish_lease(lease, action):
            return
        text = "%s %s" % (action.kind, "" if action.value is None else action.value)
        if lease is sys.rmu_lease:
            sys.rmu_last_action = text
        else:
            sys.last_action = text

    async def _async_publish_lease(self, lease: LeaseManager, action: LeaseAction) -> bool:
        """Publish a lease request and record it; False if it failed."""
        reg = lease.register
        topic = "lease/%d/%s" % (reg, "set" if action.kind == "set" else "release")
        try:
            await self._publish(topic, json.dumps(action.payload(lease.ttl, OWNER)))
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Could not publish %s%s", self.prefix, topic, exc_info=True)
            return False
        lease.record(action)
        if not action.renew:
            _LOGGER.info("NibePi %s: lease %s %s", reg, action.kind, action.value)
        return True

    def _compute(self, sys: SystemRuntime, now: datetime) -> None:
        if sys.mode == MODE_OFF:
            sys.decisions = {}
            sys.plan = None
            sys.rules_plan = None
            return
        timeout = self.opt(OPT_SENSOR_TIMEOUT)
        indoor = self._indoor_reading(sys)
        away = self.away_rules(now)
        # Away: the indoor component and the price guard work on the
        # lowered setpoint.
        setpoint = self.away_setpoint(sys) if away else self._setpoint(sys)
        try:
            self.price_level = self._price_level(now)
            self.price_level_at = now
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
        if away:
            # A fixed away component only when indoor control cannot steer;
            # otherwise the lowered setpoint already does (no double setback).
            steering = indoor_active(sys.enabled.get("indoor", True), decisions.get("indoor"))
            decisions["away"] = away_component(steering, self.away_settings().curve_offset)

        if sys.mode == MODE_ACTIVE and sys.lease.held and sys.lease.value is not None:
            reference: Optional[float] = float(sys.lease.value)
        else:
            reference = self._value(sys.registers["offset"])
        sys.decisions = decisions
        rules = curve.plan(decisions, reference, self.base_adjust.get(sys.system, 0.0))
        sys.rules_plan = rules
        try:
            # Heating strategy "optimiser": its offset replaces the sum
            # while the gate allows it.
            sys.plan = self.optimizer.decide(sys.system, now, rules, reference)
        except Exception:  # noqa: BLE001 - fall back to the rules
            _LOGGER.exception("NibePi %s optimiser decision failed; using the rules", sys.system)
            sys.plan = rules

    # -- inputs -----------------------------------------------------------

    def _value(self, reg: int) -> Optional[float]:
        reading = self.readings.get(reg)
        return reading.value if reading is not None else None

    def _indoor_reading(self, sys: SystemRuntime) -> Optional[Reading]:
        entity_id = self.opt(OPT_INDOOR_ENTITY, sys.system)
        if not entity_id:
            return self.readings.get(sys.registers["indoor"])
        return self._entity_reading(entity_id)

    def _entity_reading(self, entity_id: str) -> Optional[Reading]:
        """A temperature entity in °C, timestamped by its last report."""
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
        self.price_slots = slots
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
