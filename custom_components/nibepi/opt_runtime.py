"""Cost optimiser glue: history, recorder bootstrap, fitting and planning.

The optimiser itself is pure Python (``logic/optimizer``, ``logic/opt*``);
this module only collects its inputs from Home Assistant, runs the heavy
parts in executor jobs and keeps the results for the runtime and the
entities. See docs/optimizer.md in the workspace for the design.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from homeassistant.components import mqtt

from .const import (
    OPT_INDOOR_ENTITY,
    OPT_METER_ENTITY,
    OPT_SENSOR_TIMEOUT,
    OPT_STORE_SAVE_DELAY,
    OPT_STORE_VERSION,
    REG_ADDITION_POWER,
    REG_BRINE_IN,
    REG_COMPRESSOR_FREQ,
    REG_COMPRESSOR_POWER,
    REG_HEAT_METER,
    REG_HW_METER,
    REG_OUTDOOR,
    opt_store_key,
)
from .logic.hwcontrol import REG_BT6, REG_PRIO
from .logic.inputs import Reading, state_timestamp, state_value, usable
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
from .logic.optimizer.meter import MeterConfig, MeterReading

if TYPE_CHECKING:
    from .runtime import NibePiRuntime

_LOGGER = logging.getLogger(__name__)

DISCOVERY_TOPIC = "homeassistant/device/+/config"
BOOTSTRAP_DELAY_S = 60
# Live samples: a register value older than this is left out of the bin.
SAMPLE_MAX_AGE_MIN = 30.0
METER_BUFFER = timedelta(hours=2)

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

    # -- lifecycle ----------------------------------------------------------

    async def async_start(self) -> None:
        await self._async_load()
        self._unsubs.append(await mqtt.async_subscribe(self.hass, DISCOVERY_TOPIC, self._on_discovery))
        self._unsubs.append(async_at_started(self.hass, self._started))

    @callback
    def _started(self, _hass: Any) -> None:
        self._unsubs.append(async_call_later(self.hass, BOOTSTRAP_DELAY_S, self._bootstrap_cb))

    async def async_stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        if self.loaded:
            await self.store.async_save(self._store_data())

    async def async_options_updated(self) -> None:
        # A new indoor sensor or meter may have history to import.
        self.meter_buffer = []
        if self.loaded and not self.bootstrap_running:
            self.rt.entry.async_create_background_task(
                self.hass, self.async_bootstrap(), "nibepi optimiser history import"
            )

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
        """Hook for the fitted models and savings (later sections)."""

    def _store_data(self) -> Dict[str, Any]:
        return {
            "log": self.log.to_dict(),
            "imported": dict(self.imported),
            "bootstrap_at": self.bootstrap_at,
        }

    def save_soon(self) -> None:
        if self.loaded:
            self.store.async_delay_save(self._store_data, OPT_STORE_SAVE_DELAY)

    # -- gateway discovery --------------------------------------------------

    @callback
    def _on_discovery(self, msg: Any) -> None:
        device_id = discovery_device_id(msg.payload, self.rt.prefix)
        if device_id and device_id != self.discovery_id:
            _LOGGER.debug("NibePi gateway discovery id %s", device_id)
            self.discovery_id = device_id

    def gateway_entity(self, register: int) -> Optional[str]:
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
        self.log.put(start, result)
        self.log.trim(retention_start(now, RETENTION_DAYS))
        self.save_soon()

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
            entity_id = self.gateway_entity(reg)
            problem = None if entity_id else "no discovered gateway sensor for register %d" % reg
            return ImportItem(name, entity_id, kind, wanted, problem)

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
            if self.imported.get(item.column) == item.entity_id:
                continue
            state = self.hass.states.get(item.entity_id)
            convert = None
            if item.kind == "mean" and state is not None and state.attributes.get("device_class") == "temperature":
                convert = _celsius(state.attributes.get("unit_of_measurement"))
            rows = await get_instance(self.hass).async_add_executor_job(
                _fetch_history, self.hass, item.entity_id, start, end
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
            )
            added = self.log.fill(item.column, series)
            self.imported[item.column] = item.entity_id
            self.bootstrap_report[item.column] = "%s: %d of %d bins imported (%d rows)" % (
                item.entity_id, added, len(series), len(rows),
            )
            changed = True
        if missing:
            self._log_once(
                "missing",
                "NibePi optimiser: no recorder history for %s (gateway sensors are found by "
                "their MQTT discovery unique id; is discovery on in the gateway?). "
                "The history builds up from now on.",
                ", ".join(missing),
            )
        if changed:
            self.log.trim(retention_start(end, RETENTION_DAYS))
            self.bootstrap_at = end.isoformat()
            _LOGGER.info(
                "NibePi optimiser: history imported, %d bins of %d min", len(self.log), STEP_S // 60
            )
            await self.store.async_save(self._store_data())
