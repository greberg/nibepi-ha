"""Cost optimiser glue: history, recorder bootstrap, fitting and planning.

The optimiser itself is pure Python (``logic/optimizer``, ``logic/opt*``);
this module only collects its inputs from Home Assistant, runs the heavy
parts in executor jobs and keeps the results for the runtime and the
entities. See docs/optimizer.md in the workspace for the design.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from homeassistant.components import mqtt

from .const import (
    DOMAIN,
    HOUSE_EMITTERS,
    METER_SCOPE_HEAT_PUMP,
    METER_SCOPES,
    OPT_COMFORT_ABOVE,
    OPT_COMFORT_BELOW,
    OPT_COMFORT_FLOOR,
    OPT_HOUSE_AREA,
    OPT_HOUSE_CONSTRUCTION,
    OPT_HOUSE_EMITTER,
    OPT_HOUSE_ERA,
    OPT_HOUSE_FLOORS,
    OPT_HOUSE_SENSORS,
    OPT_HW_OPT_FLOOR,
    OPT_INDOOR_ENTITY,
    OPT_METER_ENTITY,
    OPT_METER_SCOPE,
    OPT_OPT_MIN_LEVEL,
    OPT_PRICE_UNIT_FACTOR,
    OPT_SENSOR_TIMEOUT,
    OPT_STORE_SAVE_DELAY,
    OPT_STORE_SAVE_EVERY,
    OPT_STORE_VERSION,
    REG_ADDITION_POWER,
    REG_BRINE_IN,
    REG_COMPRESSOR_FREQ,
    REG_COMPRESSOR_POWER,
    REG_HEAT_METER,
    REG_HW_METER,
    REG_OUTDOOR,
    REG_PUMP_SPEED,
    REG_RETURN,
    UNKNOWN,
    opt_store_key,
    signal_update,
)
from .logic.curve import CurvePlan
from .logic.hotwater import START_REGISTERS, STOP_REGISTERS
from .logic.hwcontrol import REG_BT6, REG_PRIO
from .logic.legionella import REG_LUX_STOP, legionella_pin
from .logic.lease import MODE_OFF
from .logic.inputs import Reading, state_timestamp, state_value, to_float, usable
from .logic.opthistory import (
    BOOTSTRAP_DAYS,
    PRIO_HEAT,
    PRIO_HW,
    RETENTION_DAYS,
    STEP_S,
    BinAccumulator,
    SampleLog,
    bin_meter_energy,
    column,
    discovery_device_id,
    gateway_ids,
    import_series,
    meter_kind,
    register_unique_id,
    retention_start,
)
from .logic import optcost
from .logic.pump import PRIORITY_VALUES, history_sources, own_unique_id, register_owner
from .logic.optimizer.gate import GateDecision, gate
from .logic.optimizer.meter import MeterConfig, MeterReading
from .logic.away import HW_NORMAL as AWAY_HW_NORMAL
from .logic.away import away_comfort
from .logic.optimizer.planner import ComfortSettings, comfort_at
from .logic.optimizer.profile import HouseProfile
from .logic.optrun import (
    PLAN_MAX_UP,
    HeatingInputs,
    HeatingResult,
    Models,
    SystemSetup,
    TankInputs,
    TankResult,
    base_offset,
    comfort_settings,
    fit_all,
    models_for,
    plan_system,
    plan_tank,
    profile_confidence,
    scale_prices,
)
from .logic.optstrategy import (
    STRATEGY_RULES,
    STRATEGIES,
    StrategyChoice,
    apply_choice,
    choose_total,
    hours_left,
    hw_windows,
    offset_at,
    savings_between,
    time_share,
)

if TYPE_CHECKING:
    from .runtime import NibePiRuntime

_LOGGER = logging.getLogger(__name__)

DISCOVERY_TOPIC = "homeassistant/device/+/config"
BOOTSTRAP_DELAY_S = 60
FIT_DELAY_S = 600  # first fit after start (after the history import)
FIT_TIME = (3, 15)  # nightly, local time
DEFAULT_TARGET = 21.0
PLAN_MINUTES = (0, 15, 30, 45)
PLAN_MAX_AGE = timedelta(minutes=40)  # an older plan does not steer
FIRST_PLAN_AFTER = timedelta(seconds=150)  # register values are in by then
FORECAST_MAX_AGE = timedelta(hours=6)
# Live samples: a register value older than this is left out of the bin.
SAMPLE_MAX_AGE_MIN = 30.0
METER_BUFFER = timedelta(hours=2)
SLAB_HISTORY_S = 24 * 3600  # history for the slab temperature of a lagged house

# Shared columns from gateway registers: (column, register, import kind, wanted).
SHARED_SOURCES = (
    ("t_out", REG_OUTDOOR, "mean", None),
    ("bt10", REG_BRINE_IN, "mean", None),
    ("bt6", REG_BT6, "mean", None),
    ("prio_hw", REG_PRIO, "indicator", PRIO_HW),
    ("prio_heat", REG_PRIO, "indicator", PRIO_HEAT),
    ("power_w", REG_COMPRESSOR_POWER, "mean", None),
    ("add_kw", REG_ADDITION_POWER, "mean", None),
    ("freq_hz", REG_COMPRESSOR_FREQ, "mean", None),
    ("bt3", REG_RETURN, "mean", None),
    ("pump_on", REG_PUMP_SPEED, "running", None),
    ("heat_kwh", REG_HEAT_METER, "last", None),
    ("hw_kwh", REG_HW_METER, "last", None),
)


@dataclass(frozen=True)
class ImportItem:
    column: str
    entity_id: Optional[str]
    kind: str
    wanted: Optional[float] = None
    problem: Optional[str] = None
    # Read after ``entity_id``, for the bins it left empty (the gateway's
    # discovered sensor behind the integration's own one).
    fallbacks: Tuple[str, ...] = ()
    # Text state -> number (the own priority sensor's enum states).
    states: Optional[Dict[str, float]] = None

    @property
    def sources(self) -> Tuple[str, ...]:
        return ((self.entity_id,) if self.entity_id else ()) + self.fallbacks


def _tz() -> Any:
    getter = getattr(dt_util, "get_default_time_zone", None)
    return getter() if getter is not None else dt_util.DEFAULT_TIME_ZONE


def _celsius(unit: Optional[str]) -> Optional[Callable[[float], Optional[float]]]:
    if not unit or unit == UnitOfTemperature.CELSIUS:
        return None

    def convert(value: float) -> Optional[float]:
        try:
            return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        except Exception:  # noqa: BLE001 - unknown unit: drop the value
            return None

    return convert


def _fetch_history(hass: Any, entity_id: str, start: datetime, end: datetime) -> List[Any]:
    """Runs in the recorder's executor: one entity's state changes."""
    from homeassistant.components.recorder import history

    result = history.get_significant_states(
        hass,
        start,
        end,
        [entity_id],
        significant_changes_only=False,
        minimal_response=True,
        no_attributes=True,
        compressed_state_format=True,
    )
    return list(result.get(entity_id, []))


class OptimizerRuntime:
    """Owned by ``NibePiRuntime``; one per config entry."""

    def __init__(self, runtime: "NibePiRuntime") -> None:
        self.rt = runtime
        self.hass = runtime.hass
        self.store: Store = Store(self.hass, OPT_STORE_VERSION, opt_store_key(runtime.entry.entry_id))
        self.log = SampleLog()
        self.acc = BinAccumulator()
        self.loaded = False
        self.meter_buffer: List[MeterReading] = []
        self.meter_config: Optional[MeterConfig] = None
        self.meter_entity: Optional[str] = None
        self.meter_problem: Optional[str] = None
        self.discovery_id: Optional[str] = None
        # Bootstrap: column -> entity id imported; report: column -> text.
        self.imported: Dict[str, str] = {}
        self.bootstrap_report: Dict[str, str] = {}
        self.bootstrap_at: Optional[str] = None
        self.bootstrap_running = False
        self._logged: set = set()
        self._unsubs: List[Callable[[], None]] = []
        self._save_requested: Optional[datetime] = None
        # Fits: the persisted dict (``optrun.fit_all``) and the models built
        # from it per system.
        self.fits: Dict[str, Any] = {}
        self.models: Dict[str, Models] = {}
        self.fit_running = False
        self.fit_error: Optional[str] = None
        # Planning (in memory; replanned every 15 minutes).
        self.strategy: Dict[str, str] = {system: STRATEGY_RULES for system in runtime.systems}
        self.heating: Dict[str, HeatingResult] = {}
        self.tank: Optional[TankResult] = None
        self.gates: Dict[str, Optional[GateDecision]] = {}
        self.choices: Dict[str, StrategyChoice] = {}
        self.plan_running = False
        self.plan_error: Optional[str] = None
        self.last_plan_at: Optional[datetime] = None
        self._plan_again = False
        self._price_key: Any = None
        # Expected savings today: {"day", "heating": {system: banked}, ...}.
        self.savings: Dict[str, Any] = {}
        self.savings_today: Dict[str, float] = {}
        # Heat pump electricity today, measured (heat pump meter) vs planned.
        self.hp_cost: Dict[str, Any] = {}

    # -- lifecycle ----------------------------------------------------------

    async def async_start(self) -> None:
        await self._async_load()
        self._unsubs.append(await mqtt.async_subscribe(self.hass, DISCOVERY_TOPIC, self._on_discovery))
        self._unsubs.append(async_at_started(self.hass, self._started))
        self._unsubs.append(
            async_track_time_change(self.hass, self._fit_cb, hour=FIT_TIME[0], minute=FIT_TIME[1], second=0)
        )
        self._unsubs.append(
            async_track_time_change(self.hass, self._plan_cb, minute=list(PLAN_MINUTES), second=30)
        )
        self.refresh_models()

    @callback
    def _started(self, _hass: Any) -> None:
        self._unsubs.append(async_call_later(self.hass, BOOTSTRAP_DELAY_S, self._bootstrap_cb))
        self._unsubs.append(async_call_later(self.hass, FIT_DELAY_S, self._fit_cb))

    async def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        if self.loaded:
            await self.store.async_save(self._store_data())

    async def async_options_updated(self) -> None:
        # A new indoor sensor or meter may have history to import.
        self.meter_buffer = []
        self.refresh_models()
        self.request_plan()
        if self.loaded and not self.bootstrap_running:
            self.rt.entry.async_create_background_task(
                self.hass, self._async_import_and_fit(), "nibepi optimiser history import"
            )

    async def _async_import_and_fit(self) -> None:
        await self.async_bootstrap()
        await self.async_fit()

    async def _async_load(self) -> None:
        try:
            data = await self.store.async_load()
        except Exception:  # noqa: BLE001 - a broken file must not stop setup
            _LOGGER.exception("Could not load the optimiser's history; starting empty")
            data = None
        data = data if isinstance(data, dict) else {}
        self.log = SampleLog.from_dict(data.get("log"))
        imported = data.get("imported")
        self.imported = {str(k): str(v) for k, v in imported.items()} if isinstance(imported, dict) else {}
        self.bootstrap_at = data.get("bootstrap_at") if isinstance(data.get("bootstrap_at"), str) else None
        self._load_extra(data)
        self.loaded = True

    def _load_extra(self, data: Dict[str, Any]) -> None:
        fits = data.get("fits")
        self.fits = fits if isinstance(fits, dict) else {}
        savings = data.get("savings")
        self.savings = savings if isinstance(savings, dict) else {}
        hp_cost = data.get("hp_cost")
        self.hp_cost = hp_cost if isinstance(hp_cost, dict) else {}

    def _store_data(self) -> Dict[str, Any]:
        return {
            "log": self.log.to_dict(),
            "imported": dict(self.imported),
            "bootstrap_at": self.bootstrap_at,
            "fits": self.fits,
            "savings": self.savings,
            "hp_cost": self.hp_cost,
        }

    def save_soon(self) -> None:
        """Save within the hour (and always at shutdown)."""
        if not self.loaded:
            return
        now = dt_util.utcnow()
        last = self._save_requested
        if last is not None and (now - last).total_seconds() < OPT_STORE_SAVE_EVERY:
            return
        self._save_requested = now
        self.store.async_delay_save(self._store_data, OPT_STORE_SAVE_DELAY)

    # -- gateway discovery --------------------------------------------------

    @callback
    def _on_discovery(self, msg: Any) -> None:
        device_id = discovery_device_id(msg.payload, self.rt.prefix)
        if device_id and device_id != self.discovery_id:
            _LOGGER.debug("NibePi gateway discovery id %s", device_id)
            self.discovery_id = device_id

    def gateway_entity(self, register: Any) -> Optional[str]:
        """The entity id of the gateway's discovered sensor for ``register``.

        From the gateway's own discovery config when it was seen on MQTT,
        else from the entity registry, when exactly one NibePi gateway's
        sensors are registered.
        """
        registry = er.async_get(self.hass)
        if self.discovery_id:
            ids = [self.discovery_id]
        else:
            ids = sorted(
                gateway_ids(
                    e.unique_id
                    for e in registry.entities.values()
                    if e.platform == "mqtt" and e.domain == "sensor"
                )
            )
        if len(ids) != 1:
            return None
        return registry.async_get_entity_id("sensor", "mqtt", register_unique_id(ids[0], register))

    def own_entity(self, register: Any) -> Optional[str]:
        """The entity id of the integration's own sensor for ``register``
        (logic/pump.py, or the pump offset), unless it is disabled."""
        rt = self.rt
        offsets = {system: sys.registers["offset"] for system, sys in rt.systems.items()}
        owner = register_owner(register, rt.systems, offsets)
        if owner is None:
            return None
        key, system = owner
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, own_unique_id(rt.entry.entry_id, key, system))
        entry = registry.async_get(entity_id) if entity_id else None
        if entry is None or entry.disabled_by is not None:
            return None
        return entity_id

    def history_sources(self, register: Any) -> Tuple[str, ...]:
        """Where ``register``'s recorder history is: the own sensor, then the
        gateway's discovered one (logic/pump.py ``history_sources``)."""
        return history_sources(self.own_entity(register), self.gateway_entity(register))

    @property
    def meter_scope(self) -> str:
        """What the meter measures: ``heat_pump`` (default) or ``whole_house``."""
        scope = self.rt.opt(OPT_METER_SCOPE)
        return str(scope) if scope in METER_SCOPES else METER_SCOPE_HEAT_PUMP

    # -- live sampling ----------------------------------------------------------

    def _fresh(self, reading: Optional[Reading], now: datetime, timeout: Optional[float] = None) -> Optional[float]:
        if usable(reading, now, timeout if timeout is not None else SAMPLE_MAX_AGE_MIN) is not None:
            return None
        assert reading is not None
        return float(reading.value)

    def _meter_setup(self) -> None:
        """The meter entity and its kind, from the options and its state."""
        entity_id = self.rt.opt(OPT_METER_ENTITY) or None
        if entity_id != self.meter_entity:
            self.meter_buffer = []
            self.meter_config = None
        self.meter_entity = entity_id
        if not entity_id:
            self.meter_config = None
            self.meter_problem = None
            return
        state = self.hass.states.get(entity_id)
        if state is None:
            self.meter_problem = "%s not found" % entity_id
            return
        kind = meter_kind(
            state.attributes.get("unit_of_measurement"),
            state.attributes.get("device_class"),
            state.attributes.get("state_class"),
        )
        if kind is None:
            self.meter_problem = "%s: unit %r is not a power or energy meter" % (
                entity_id, state.attributes.get("unit_of_measurement"),
            )
            self.meter_config = None
            return
        if self.meter_config is None or (self.meter_config.kind, self.meter_config.unit) != kind:
            self.meter_buffer = []
            self.meter_config = MeterConfig(kind[0], kind[1])
        self.meter_problem = None

    def _sample_meter(self, now: datetime) -> None:
        self._meter_setup()
        if self.meter_config is None or not self.meter_entity:
            return
        state = self.hass.states.get(self.meter_entity)
        value = state_value(state.state) if state is not None else None
        if value is not None and state is not None:
            ts = state_timestamp(getattr(state, "last_reported", None), state.last_updated)
            if ts is None or now - ts > self.meter_config.max_hold:
                if self.meter_config.kind == "power":
                    value = None  # a power value this old is not trusted to hold
        self.meter_buffer.append(MeterReading(now, value))
        limit = now - METER_BUFFER
        while self.meter_buffer and self.meter_buffer[0].ts < limit:
            self.meter_buffer.pop(0)

    def sample(self, now: datetime) -> None:
        """Called every tick: add this minute's values to the 10-minute bin."""
        if not self.loaded:
            return
        rt = self.rt
        readings = rt.readings
        values: Dict[str, Optional[float]] = {}
        for name, reg, kind, wanted in SHARED_SOURCES:
            value = self._fresh(readings.get(reg), now)
            if kind == "indicator":
                values[name] = None if value is None else (1.0 if int(value) == wanted else 0.0)
            elif kind == "running":
                values[name] = None if value is None else (1.0 if value > 0 else 0.0)
            else:
                values[name] = value
        timeout = rt.opt(OPT_SENSOR_TIMEOUT)
        for system, sys in rt.systems.items():
            values[column("t_in", system)] = self._fresh(rt._indoor_reading(sys), now, timeout)
            values[column("supply", system)] = self._fresh(readings.get(sys.registers["supply"]), now)
            values[column("offset", system)] = self._fresh(readings.get(sys.registers["offset"]), now)
        self._sample_meter(now)
        done = self.acc.add(now.timestamp(), values)
        if done is None:
            return
        start, result = done
        if self.meter_config is not None and self.meter_buffer:
            energy = bin_meter_energy(self.meter_buffer, [start], self.meter_config)
            if start in energy:
                result["meter_kwh"] = energy[start]
                self._count_cost(start, energy[start])
        self.log.put(start, result)
        self.log.trim(retention_start(now, RETENTION_DAYS))
        self.save_soon()

    @property
    def measures_heat_pump(self) -> bool:
        return bool(self.meter_entity) and self.meter_scope == METER_SCOPE_HEAT_PUMP

    def _local_day(self, when: datetime) -> str:
        return dt_util.as_local(when).date().isoformat()

    def _count_cost(self, start: int, kwh: float) -> None:
        """A finished bin of a heat pump meter: price it into today's cost."""
        if not self.measures_heat_pump:
            return
        when = datetime.fromtimestamp(start, timezone.utc)
        prices = scale_prices(self.rt.price_slots, self.rt.opt(OPT_PRICE_UNIT_FACTOR))
        price = optcost.price_at(prices, when + timedelta(seconds=STEP_S / 2))
        self.hp_cost = optcost.roll(self.hp_cost, self._local_day(when))
        optcost.add_measured(self.hp_cost, kwh, price)

    # -- bootstrap from the recorder ----------------------------------------

    @callback
    def _bootstrap_cb(self, _now: Any) -> None:
        self.rt.entry.async_create_background_task(
            self.hass, self.async_bootstrap(), "nibepi optimiser history import"
        )

    def _log_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._logged:
            self._logged.add(key)
            _LOGGER.info(message, *args)

    def import_plan(self) -> List[ImportItem]:
        """What to import from the recorder and from which entity."""
        items: List[ImportItem] = []

        def register_item(name: str, reg: int, kind: str, wanted: Optional[float] = None) -> ImportItem:
            sources = self.history_sources(reg)
            if not sources:
                return ImportItem(name, None, kind, wanted, "no own or discovered gateway sensor for register %d" % reg)
            states = PRIORITY_VALUES if reg == REG_PRIO else None
            return ImportItem(name, sources[0], kind, wanted, None, sources[1:], states)

        for name, reg, kind, wanted in SHARED_SOURCES:
            items.append(register_item(name, reg, kind, wanted))
        for system, sys in self.rt.systems.items():
            indoor = self.rt.opt(OPT_INDOOR_ENTITY, system)
            if indoor:
                items.append(ImportItem(column("t_in", system), indoor, "mean"))
            else:
                items.append(register_item(column("t_in", system), sys.registers["indoor"], "mean"))
            items.append(register_item(column("supply", system), sys.registers["supply"], "mean"))
            items.append(register_item(column("offset", system), sys.registers["offset"], "mean"))
        self._meter_setup()
        if self.meter_entity:
            problem = self.meter_problem if self.meter_config is None else None
            items.append(ImportItem("meter_kwh", self.meter_entity, "meter", None, problem))
        return items

    async def async_bootstrap(self) -> None:
        """Fill the log from the recorder's last 28 days, once per source.

        Runs at the first start and when an option points a column at a
        new entity. Only bins without a value are filled; values collected
        live always win.
        """
        if self.bootstrap_running or not self.loaded:
            return
        if "recorder" not in self.hass.config.components:
            self._log_once("recorder", "NibePi optimiser: the recorder is not loaded; no history import")
            return
        self.bootstrap_running = True
        try:
            await self._async_bootstrap()
        except Exception:  # noqa: BLE001 - the live history still builds up
            _LOGGER.exception("NibePi optimiser: history import failed")
        finally:
            self.bootstrap_running = False

    async def _async_bootstrap(self) -> None:
        from homeassistant.components.recorder import get_instance

        end = dt_util.utcnow()
        start = end - timedelta(days=BOOTSTRAP_DAYS)
        missing = []
        changed = False
        for item in self.import_plan():
            if item.entity_id is None or item.problem:
                self.bootstrap_report[item.column] = "skipped: %s" % (item.problem or "no entity")
                missing.append(item.column)
                continue
            key = ",".join(item.sources)
            if self.imported.get(item.column) == key:
                continue
            # The own sensor first; a fallback only fills the bins left empty.
            report = []
            for entity_id in item.sources:
                state = self.hass.states.get(entity_id)
                convert = None
                if item.kind == "mean" and state is not None and state.attributes.get("device_class") == "temperature":
                    convert = _celsius(state.attributes.get("unit_of_measurement"))
                rows = await get_instance(self.hass).async_add_executor_job(
                    _fetch_history, self.hass, entity_id, start, end
                )
                series = await self.hass.async_add_executor_job(
                    import_series,
                    rows,
                    item.kind,
                    start.timestamp(),
                    end.timestamp(),
                    convert,
                    self.meter_config if item.kind == "meter" else None,
                    item.wanted,
                    item.states,
                )
                added = self.log.fill(item.column, series)
                report.append("%s: %d of %d bins imported (%d rows)" % (entity_id, added, len(series), len(rows)))
            self.imported[item.column] = key
            self.bootstrap_report[item.column] = "; ".join(report)
            changed = True
        if missing:
            self._log_once(
                "missing",
                "NibePi optimiser: no recorder history for %s (no own sensor for it, and "
                "no gateway sensor from MQTT discovery). The history builds up from now on.",
                ", ".join(missing),
            )
        if changed:
            self.log.trim(retention_start(end, RETENTION_DAYS))
            self.bootstrap_at = end.isoformat()
            _LOGGER.info(
                "NibePi optimiser: history imported, %d bins of %d min", len(self.log), STEP_S // 60
            )
            await self.store.async_save(self._store_data())

    # -- house profile and fitting --------------------------------------------

    def profile(self, system: str) -> HouseProfile:
        rt = self.rt
        config = self.hass.config
        floors = to_float(str(rt.opt(OPT_HOUSE_FLOORS) or "").replace("_", ".")) or 1.5
        era = rt.opt(OPT_HOUSE_ERA)
        construction = rt.opt(OPT_HOUSE_CONSTRUCTION)
        emitter = rt.opt(OPT_HOUSE_EMITTER)
        sensors = to_float(rt.opt(OPT_HOUSE_SENSORS)) or 1
        sys = rt.systems.get(system)
        target = rt._setpoint(sys) if sys is not None else None
        return HouseProfile(
            floor_area_m2=to_float(rt.opt(OPT_HOUSE_AREA)) or 150.0,
            floors=floors,
            target_temp=target if target is not None else DEFAULT_TARGET,
            era=None if era in (None, UNKNOWN) else str(era),
            construction=None if construction in (None, UNKNOWN) else str(construction),
            indoor_sensors=int(sensors),
            emitter=str(emitter) if emitter in HOUSE_EMITTERS else "floor",
            latitude=config.latitude,
            longitude=config.longitude,
            elevation_m=float(config.elevation or 0),
            time_zone=str(config.time_zone),
        )

    def setup(self, system: str) -> SystemSetup:
        sys = self.rt.systems[system]
        return SystemSetup(system, self.profile(system), self.rt._value(sys.registers["curve"]) or 0.0)

    def refresh_models(self) -> None:
        """Rebuild the models from the stored fits and the current options."""
        models: Dict[str, Models] = {}
        for system in self.rt.systems:
            try:
                models[system] = models_for(self.setup(system), self.fits)
            except Exception:  # noqa: BLE001 - keep the previous models
                _LOGGER.exception("NibePi optimiser: could not build the %s models", system)
                if system in self.models:
                    models[system] = self.models[system]
        self.models = models

    @callback
    def _fit_cb(self, _now: Any) -> None:
        self.rt.entry.async_create_background_task(self.hass, self.async_fit(), "nibepi optimiser fit")

    async def async_fit(self) -> None:
        """Fit all models in an executor job and keep the result."""
        if self.fit_running or not self.loaded:
            return
        self.fit_running = True
        try:
            for _ in range(40):  # the history import may still be running
                if not self.bootstrap_running:
                    break
                await asyncio.sleep(30)
            now = dt_util.utcnow()
            rows = self.log.snapshot(since=now.timestamp() - BOOTSTRAP_DAYS * 86400)
            setups = [self.setup(system) for system in self.rt.systems]
            fits = await self.hass.async_add_executor_job(fit_all, rows, setups, now, self.meter_scope)
            self.fits = fits
            self.fit_error = None
            self.refresh_models()
            for system, models in self.models.items():
                fit = models.fit
                _LOGGER.info(
                    "NibePi optimiser %s: house model %s (%s, confidence %.2f, sim RMSE %.2f °C, %d h)",
                    system, "fitted" if fit.ok else "prior", fit.reason, fit.confidence, fit.sim_rmse, fit.samples,
                )
            await self.store.async_save(self._store_data())
        except Exception as err:  # noqa: BLE001 - keep the previous fits
            _LOGGER.exception("NibePi optimiser: fitting failed")
            self.fit_error = repr(err)
        finally:
            self.fit_running = False
        async_dispatcher_send(self.hass, signal_update(self.rt.entry.entry_id))

    # -- strategy and the live gate -------------------------------------------

    def set_strategy(self, system: str, strategy: str) -> None:
        if strategy not in STRATEGIES or self.strategy.get(system) == strategy:
            return
        _LOGGER.info("NibePi %s heating strategy %s -> %s", system, self.strategy.get(system), strategy)
        self.strategy[system] = strategy
        self.request_plan()

    def normal_comfort(self, system: str) -> ComfortSettings:
        """The comfort band around the setpoint, as when nobody is away."""
        rt = self.rt
        sys = rt.systems[system]
        target = rt._setpoint(sys)
        models = self.models.get(system)
        return comfort_settings(
            target if target is not None else DEFAULT_TARGET,
            to_float(rt.opt(OPT_COMFORT_BELOW, system)) or 0.0,
            to_float(rt.opt(OPT_COMFORT_ABOVE, system)) or 0.0,
            to_float(rt.opt(OPT_COMFORT_FLOOR, system)) or 1.5,
            models.prior.sensor_margin if models is not None else 0.0,
        )

    def comfort_plan(self, system: str, now: datetime):
        """(band now, band after the return, return time, ramp hours).

        Not away: the normal band, nothing after. Away: the lowered band;
        with preheat and a known return, the normal band from the return,
        ramping over the return lead before it.
        """
        normal = self.normal_comfort(system)
        away = self.rt.away
        if not away.active(now):
            return normal, None, None, 0.0
        settings = self.rt.away_settings()
        lowered = away_comfort(normal, settings.setback_for(away), settings.min_indoor)
        if settings.preheat and away.until is not None:
            return lowered, normal, away.until, settings.return_lead_h
        return lowered, None, None, 0.0

    def comfort(self, system: str, when: Optional[datetime] = None) -> ComfortSettings:
        """The comfort band in force at ``when`` (default now): the gate's
        floor and the band shown on the sensor."""
        when = when or dt_util.utcnow()
        now_band, after, change, ramp = self.comfort_plan(system, when)
        return comfort_at(now_band, after, change, when, ramp)

    def _indoor(self, system: str, now: datetime):
        reading = self.rt._indoor_reading(self.rt.systems[system])
        problem = usable(reading, now, self.rt.opt(OPT_SENSOR_TIMEOUT))
        return (float(reading.value) if problem is None and reading is not None else None), problem

    def live_gate(self, system: str, now: datetime) -> Optional[GateDecision]:
        """The gate with this minute's indoor temperature and the last plan."""
        models = self.models.get(system)
        if models is None:
            return None
        comfort = self.comfort(system, now)
        t_in, problem = self._indoor(system, now)
        result = self.heating.get(system)
        planned = None
        horizon = 0.0
        if result is not None:
            base = result.base_offset
            if result.plan is not None and now - result.planned_at <= PLAN_MAX_AGE:
                steps = result.plan.plan.steps
                planned = offset_at(steps, now)
                horizon = hours_left(steps, now)
        else:
            t_out = self._fresh(self.rt.readings.get(REG_OUTDOOR), now)
            base = base_offset(models, t_out if t_out is not None else 0.0, comfort.target)
        decision = gate(
            models.fit,
            planned,
            horizon,
            t_in,
            comfort.floor,
            base,
            PLAN_MAX_UP,
            indoor_problem=problem,
            min_level=str(self.rt.opt(OPT_OPT_MIN_LEVEL) or "high"),
        )
        if decision.reason == "no plan" and result is not None:
            why = result.problem or ("plan older than %d min" % (PLAN_MAX_AGE.total_seconds() // 60))
            decision = GateDecision(False, None, "no plan (%s)" % why)
        return decision

    def decide(self, system: str, now: datetime, rules: CurvePlan, current: Optional[float]) -> CurvePlan:
        """The curve plan for this tick: the rules, or the optimiser's offset."""
        decision = self.live_gate(system, now)
        choice = choose_total(self.strategy.get(system, STRATEGY_RULES), rules.total, decision)
        self.gates[system] = decision
        self.choices[system] = choice
        return apply_choice(rules, choice, current)

    def on_tick(self, now: datetime) -> None:
        """Replan when the prices changed, and the first time after start."""
        started = self.rt.started
        if started is None or now - started < FIRST_PLAN_AFTER:
            return
        slots = self.rt.price_slots
        key = (len(slots), slots[0].start, slots[-1].end) if slots else None
        if self.last_plan_at is None or key != self._price_key:
            self._price_key = key
            self.request_plan()

    # -- planning ----------------------------------------------------------------

    @callback
    def _plan_cb(self, _now: Any) -> None:
        self.request_plan()

    def request_plan(self) -> None:
        if not self.loaded or self.rt.started is None:
            return
        if self.plan_running:
            self._plan_again = True
            return
        self.rt.entry.async_create_background_task(self.hass, self.async_plan(), "nibepi optimiser plan")

    async def async_plan(self) -> None:
        if self.plan_running:
            self._plan_again = True
            return
        self.plan_running = True
        try:
            while True:
                self._plan_again = False
                try:
                    await self._async_plan_once()
                    self.plan_error = None
                except Exception as err:  # noqa: BLE001 - the rules keep running
                    _LOGGER.exception("NibePi optimiser: planning failed")
                    self.plan_error = repr(err)
                self.last_plan_at = dt_util.utcnow()
                if not self._plan_again:
                    break
        finally:
            self.plan_running = False
        async_dispatcher_send(self.hass, signal_update(self.rt.entry.entry_id))

    def _heating_inputs(self, now: datetime) -> List[HeatingInputs]:
        rt = self.rt
        tz = _tz()
        prices = scale_prices(rt.price_slots, rt.opt(OPT_PRICE_UNIT_FACTOR))
        fetched = rt.forecast_fetched
        forecast = list(rt.forecast or []) if fetched is not None and now - fetched <= FORECAST_MAX_AGE else []
        t_out = self._fresh(rt.readings.get(REG_OUTDOOR), now, rt.opt(OPT_SENSOR_TIMEOUT))
        t_brine = self._fresh(rt.readings.get(REG_BRINE_IN), now)
        currency = str(getattr(self.hass.config, "currency", None) or "SEK")
        history = self.log.snapshot(since=now.timestamp() - SLAB_HISTORY_S)
        out = []
        for system, sys in rt.systems.items():
            if sys.mode == MODE_OFF or system not in self.models:
                continue
            t_in, _problem = self._indoor(system, now)
            current = rt._value(sys.registers["offset"])
            band, after, change, ramp = self.comfort_plan(system, now)
            out.append(
                HeatingInputs(
                    system=system,
                    models=self.models[system],
                    comfort=band,
                    prices=prices,
                    now=now,
                    tz=tz,
                    t_in=t_in,
                    t_out=t_out,
                    forecast=forecast,
                    t_brine=t_brine,
                    current_offset=int(round(current)) if current is not None else None,
                    latitude=self.hass.config.latitude,
                    longitude=self.hass.config.longitude,
                    currency=currency,
                    history=history,
                    comfort_after=after,
                    comfort_change=change,
                    comfort_ramp_h=ramp,
                )
            )
        return out

    def away_blocks_charges(self, now: datetime) -> bool:
        """Away with economy or minimum hot water: no planned charges."""
        return self.rt.away_rules(now) and self.rt.away_settings().hot_water != AWAY_HW_NORMAL

    def _tank_inputs(self, now: datetime) -> Optional[TankInputs]:
        rt = self.rt
        if rt.hotwater.mode == MODE_OFF or self.away_blocks_charges(now):
            return None
        readings = rt.readings
        timeout = rt.opt(OPT_SENSOR_TIMEOUT)

        def value(reg: int) -> Optional[float]:
            return self._fresh(readings.get(reg), now, timeout)

        profile = rt.hotwater.controller.profile
        guard = rt.hotwater.controller.legionella
        lux_held = rt.hotwater.leases.held().get(REG_LUX_STOP, False)
        return TankInputs(
            prices=scale_prices(rt.price_slots, rt.opt(OPT_PRICE_UNIT_FACTOR)),
            now=now,
            tz=_tz(),
            bt6=value(REG_BT6),
            economy_stop=value(STOP_REGISTERS[0]),
            normal_start=value(START_REGISTERS[1]),
            normal_stop=value(STOP_REGISTERS[1]),
            lux_stop=guard.lux_stop(now, value(STOP_REGISTERS[2]), lux_held),
            floor=to_float(rt.opt(OPT_HW_OPT_FLOOR)) or 45.0,
            profile=list(profile.profile),
            confidence=profile_confidence(profile.learned, bool(profile.imported_from)),
            t_brine=self._fresh(readings.get(REG_BRINE_IN), now),
            legionella=legionella_pin(guard.status),
        )

    def hw_plan_windows(self, now: datetime):
        """(windows, None) from a current hot water plan, or (None, why not)."""
        tank = self.tank
        if tank is None:
            return None, "not planned yet"
        if tank.plan is None:
            return None, tank.problem or "no plan"
        if now - tank.planned_at > PLAN_MAX_AGE:
            return None, "plan older than %d min" % (PLAN_MAX_AGE.total_seconds() // 60)
        return hw_windows(tank.plan.blocks, now, _tz()), None

    async def _async_plan_once(self) -> None:
        now = dt_util.utcnow()
        self.refresh_models()
        inputs = self._heating_inputs(now)
        tank_inputs = self._tank_inputs(now)
        results, tank = await self.hass.async_add_executor_job(_run_plans, inputs, tank_inputs)
        if tank is None and self.away_blocks_charges(now):
            tank = TankResult(now, problem="away: no planned charges")
        self._book_savings(now, results, tank)
        self.tank = tank
        active = {r.system for r in results}
        for system in list(self.heating):
            if system not in active:
                del self.heating[system]
        for result in results:
            self.heating[result.system] = result
        self.save_soon()

    # -- expected savings today ------------------------------------------------

    def _book_savings(self, now: datetime, heating: List[HeatingResult], tank: Optional[TankResult]) -> None:
        """Bank what the previous plans expected to save until now; add the
        new plans' share of the rest of today."""
        day_start = dt_util.start_of_local_day(dt_util.as_local(now))
        day_end = dt_util.start_of_local_day(dt_util.as_local(now) + timedelta(days=1))
        day = day_start.date().isoformat()
        if self.savings.get("day") != day:
            self.savings = {"day": day, "heating": {}, "hot_water": 0.0}
        banked: Dict[str, float] = self.savings.setdefault("heating", {})
        today: Dict[str, float] = {}
        if self.measures_heat_pump:
            self.hp_cost = optcost.roll(self.hp_cost, day)
        for result in heating:
            prev = self.heating.get(result.system)
            if prev is not None and prev.plan is not None:
                since = max(prev.planned_at, day_start)
                banked[result.system] = banked.get(result.system, 0.0) + savings_between(prev.plan, since, now)
                if self.measures_heat_pump:
                    optcost.add_planned(
                        self.hp_cost, "heating", *optcost.energy_between(prev.plan.plan.steps, since, now)
                    )
                    optcost.add_planned(
                        self.hp_cost, "baseline", *optcost.energy_between(prev.plan.baseline.steps, since, now)
                    )
            ahead = savings_between(result.plan, now, day_end) if result.plan is not None else 0.0
            today[result.system] = banked.get(result.system, 0.0) + ahead
        # Hot water: spread over the plan's span by time.
        prev_tank = self.tank
        hw_banked = float(self.savings.get("hot_water") or 0.0)
        if prev_tank is not None and prev_tank.plan is not None and prev_tank.plan.steps:
            span = (prev_tank.plan.steps[0].start, prev_tank.plan.steps[-1].end)
            hw_banked += prev_tank.plan.savings * time_share(max(prev_tank.planned_at, day_start), now, *span)
            if self.measures_heat_pump:
                optcost.add_planned(
                    self.hp_cost, "hot_water",
                    *optcost.energy_between(prev_tank.plan.steps, max(prev_tank.planned_at, day_start), now),
                )
        self.savings["hot_water"] = hw_banked
        if tank is not None and tank.plan is not None and tank.plan.steps:
            span = (tank.plan.steps[0].start, tank.plan.steps[-1].end)
            today["hot_water"] = hw_banked + tank.plan.savings * time_share(now, day_end, *span)
        elif hw_banked:
            today["hot_water"] = hw_banked
        self.savings_today = today


def _run_plans(inputs: List[HeatingInputs], tank: Optional[TankInputs]):
    """Executor job: one plan per system and the hot water plan; a failure
    is that plan's problem."""
    results = []
    for item in inputs:
        try:
            results.append(plan_system(item))
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("NibePi optimiser: planning %s failed", item.system)
            results.append(HeatingResult(item.system, item.now, 0, problem="error: %r" % err))
    tank_result = None
    if tank is not None:
        try:
            tank_result = plan_tank(tank)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("NibePi optimiser: hot water planning failed")
            tank_result = TankResult(tank.now, problem="error: %r" % err)
    return results, tank_result
