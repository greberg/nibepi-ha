"""Sensors: planned offset, component offsets, pump offset, lease states,
price level, the hot water plan, draw profile and leases, and the cost
optimiser's plans, house model, expected savings and the heat pump's
measured cost."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import NibePiConfigEntry
from .const import COMPONENTS, OPT_HW_PEAK, PRICE_SOURCE_TIBBER, price_source
from .entity import NibePiEntity
from .logic.hwcontrol import REG_COMFORT, REG_PERIOD, REG_TEMP_LUX, STATES
from .logic.lease import MODE_ACTIVE, LeaseManager
from .logic.optcost import summary as cost_summary
from .logic.optrun import models_summary
from .logic.optstrategy import SOURCE_RULES, offset_at
from .logic.price import LEVELS
from .logic.vvlearn import DAYS, next_draw, slot_name, top_slots
from .logic.vvplan import PlanSettings, day_sums
from .runtime import NibePiRuntime, SystemRuntime, _local_tz

HW_LEASES = {
    REG_COMFORT: "hw_comfort_lease_state",
    REG_PERIOD: "hw_period_lease_state",
    REG_TEMP_LUX: "hw_temp_lux_lease_state",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: NibePiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime = entry.runtime_data
    entities: List[SensorEntity] = [PriceLevelSensor(runtime)]
    for system in runtime.systems:
        entities.append(PlannedOffsetSensor(runtime, system))
        entities.extend(ComponentOffsetSensor(runtime, system, c) for c in COMPONENTS)
        entities.append(PumpOffsetSensor(runtime, system))
        entities.append(LeaseStateSensor(runtime, system))
        entities.append(RmuLeaseStateSensor(runtime, system))
        entities.append(OptimiserPlanSensor(runtime, system))
        entities.append(HouseModelSensor(runtime, system))
    entities.append(SavingsTodaySensor(runtime))
    entities.append(HeatPumpCostTodaySensor(runtime))
    entities.append(HotWaterActionSensor(runtime))
    entities.append(DrawProfileSensor(runtime))
    entities.append(HotWaterOptimiserSensor(runtime))
    entities.extend(HotWaterLeaseSensor(runtime, reg, key) for reg, key in HW_LEASES.items())
    async_add_entities(entities)


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if value is not None else None


class _SystemSensor(NibePiEntity, SensorEntity):
    def __init__(self, runtime: NibePiRuntime, key: str, system: str) -> None:
        super().__init__(runtime, key, system)

    @property
    def sys(self) -> SystemRuntime:
        return self.runtime.systems[self.system]


class PlannedOffsetSensor(_SystemSensor):
    """What the curve offset should be (shadow) or is leased to (active)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(self, runtime: NibePiRuntime, system: str) -> None:
        super().__init__(runtime, "planned_offset", system)

    @property
    def native_value(self) -> Optional[int]:
        plan = self.sys.plan
        return plan.target if plan is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        sys = self.sys
        plan = sys.plan
        rules = sys.rules_plan
        optimizer = self.runtime.optimizer
        choice = optimizer.choices.get(sys.system)
        return {
            "mode": sys.mode,
            "strategy": optimizer.strategy.get(sys.system),
            "source": choice.source if choice is not None else None,
            "strategy_reason": choice.reason if choice is not None else None,
            "rules_total": round(rules.total, 2) if rules else None,
            "total": round(plan.total, 2) if plan else None,
            "components": plan.components if plan else {},
            "reasons": plan.reasons if plan else {},
            "inputs_ok": plan.inputs_ok if plan else {},
            "base_adjust": self.runtime.base_adjust.get(sys.system, 0.0),
            "write_needed": plan.write if plan else None,
            "healthy": sys.healthy,
            "error": sys.error,
            "last_tick": _iso(sys.last_tick),
            "last_action": sys.last_action,
        }


class ComponentOffsetSensor(_SystemSensor):
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, runtime: NibePiRuntime, system: str, component: str) -> None:
        super().__init__(runtime, "%s_offset" % component, system)
        self.component = component

    @property
    def native_value(self) -> Optional[float]:
        decision = self.sys.decisions.get(self.component)
        return decision.offset if decision is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        decision = self.sys.decisions.get(self.component)
        if decision is None:
            return {}
        attrs: Dict[str, Any] = {"reason": decision.reason, "inputs_ok": decision.inputs_ok}
        attrs.update(decision.details)
        return attrs


class PumpOffsetSensor(_SystemSensor):
    """Register 47011 / 47010 as the gateway last read it."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:tune-vertical"

    def __init__(self, runtime: NibePiRuntime, system: str) -> None:
        super().__init__(runtime, "pump_offset", system)

    @property
    def native_value(self) -> Optional[float]:
        reading = self.runtime.readings.get(self.sys.registers["offset"])
        return reading.value if reading is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        reading = self.runtime.readings.get(self.sys.registers["offset"])
        return {
            "register": self.sys.registers["offset"],
            "received": _iso(reading.ts) if reading is not None else None,
        }


class _LeaseStateMixin:
    """The gateway's ``lease/<reg>/state`` for the entity's ``lease``."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["active", "grace", "restoring", "inactive", "error"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def lease(self) -> LeaseManager:
        raise NotImplementedError

    @property
    def native_value(self) -> Optional[str]:
        remote = self.lease.remote
        if not remote:
            # The gateway publishes a lease state only once a lease was
            # taken; shadow mode never takes one.
            return "inactive"
        if remote.get("restoring"):
            return "restoring"
        if remote.get("active"):
            return "grace" if self.lease.is_grace() else "active"
        if remote.get("error"):
            return "error"
        return "inactive"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        lease = self.lease
        remote = lease.remote
        return {
            "register": lease.register,
            "value": remote.get("value"),
            "written": remote.get("written"),
            "expires": _iso(lease.expires()),
            "baseline": remote.get("baseline"),
            "neutral": remote.get("neutral"),
            "owner": remote.get("owner"),
            "error": remote.get("error"),
            "restoring": remote.get("restoring"),
            "held_by_ha": lease.held,
            "ha_value": lease.value,
            "last_sent": _iso(lease.last_sent),
            "retry_after": _iso(lease.retry_after),
            "last_state_received": _iso(lease.remote_at),
        }


class LeaseStateSensor(_LeaseStateMixin, _SystemSensor):
    """The curve offset's lease (47011 / 47010)."""

    def __init__(self, runtime: NibePiRuntime, system: str, key: str = "lease_state") -> None:
        super().__init__(runtime, key, system)

    @property
    def lease(self) -> LeaseManager:
        return self.sys.lease


class RmuLeaseStateSensor(LeaseStateSensor):
    """The gateway's ``lease/10060/state`` (S1) or ``lease/10160/state`` (S2)."""

    _attr_icon = "mdi:home-thermometer-outline"

    def __init__(self, runtime: NibePiRuntime, system: str) -> None:
        super().__init__(runtime, system, "rmu_lease_state")

    @property
    def lease(self) -> LeaseManager:
        return self.sys.rmu_lease

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        sys = self.sys
        room = sys.rmu_room
        attrs = super().extra_state_attributes
        attrs.update(
            {
                "feed_enabled": sys.rmu_enabled,
                "source_entity": sys.rmu_entity,
                "room_temperature": room.value if room is not None else None,
                "reason": room.reason if room is not None else None,
                "last_action": sys.rmu_last_action,
            }
        )
        return attrs


class PriceLevelSensor(NibePiEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [level.lower() for level in LEVELS]
    _attr_icon = "mdi:cash-clock"
    _unrecorded_attributes = frozenset({"levels"})

    def __init__(self, runtime: NibePiRuntime) -> None:
        super().__init__(runtime, "price_level")

    @property
    def native_value(self) -> Optional[str]:
        level = self.runtime.price_level
        return level.level.lower() if level is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        runtime = self.runtime
        attrs: Dict[str, Any] = {
            "price_source": price_source(dict(runtime.entry.options)),
            "price_status": runtime.price_status,
        }
        if attrs["price_source"] == PRICE_SOURCE_TIBBER:
            attrs["tibber_home"] = runtime.tibber_home
            attrs["prices_fetched"] = _iso(runtime.tibber_fetched)
        level = runtime.price_level
        if level is not None:
            attrs.update(
                {
                    "price_ore": round(level.price, 2),
                    "hour_start": _iso(level.hour_start),
                    "source": level.reason,
                    "levels": level.levels,
                }
            )
        return attrs


# --------------------------------------------------------------------------
# Hot water
# --------------------------------------------------------------------------


class HotWaterLeaseSensor(_LeaseStateMixin, NibePiEntity, SensorEntity):
    """Lease state of 47041 comfort mode, 47134 period or 48132 temporary lux."""

    _attr_icon = "mdi:water-boiler"

    def __init__(self, runtime: NibePiRuntime, register: int, key: str) -> None:
        super().__init__(runtime, key)
        self.register = register

    @property
    def lease(self) -> LeaseManager:
        return self.runtime.hotwater.leases.leases[self.register]

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["last_action"] = self.runtime.hotwater.last_actions.get(self.register)
        return attrs


class HotWaterActionSensor(NibePiEntity, SensorEntity):
    """What hot water control does now, and why."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATES)
    _attr_icon = "mdi:water-boiler"
    _unrecorded_attributes = frozenset({"plan_24h", "reasons", "inputs"})

    def __init__(self, runtime: NibePiRuntime) -> None:
        super().__init__(runtime, "hw_planned_action")

    @property
    def native_value(self) -> Optional[str]:
        hw = self.runtime.hotwater
        if not hw.healthy:
            return "fault"
        return hw.decision.state if hw.decision is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        hw = self.runtime.hotwater
        decision = hw.decision
        attrs: Dict[str, Any] = {
            "mode": hw.mode,
            "features": dict(hw.features),
            "healthy": hw.healthy,
            "error": hw.error,
            "last_tick": _iso(hw.last_tick),
        }
        if decision is None:
            return attrs
        tz = _local_tz()
        now = hw.last_tick or dt_util.utcnow()
        horizon = now + timedelta(hours=24)
        details = dict(decision.details)
        closed = details.pop("closed_until", None)
        attrs.update(
            {
                "reasons": decision.reasons,
                "targets": {str(reg): value for reg, value in decision.targets.items()},
                "inputs_ok": decision.inputs_ok,
                "next_heat_start": _iso(decision.next_heat),
                "closed_until": _iso(closed),
                "plan_24h": [
                    w.compact(tz) for w in decision.windows if w.end > now and w.start < horizon
                ],
                "inputs": details,
                "last_actions": {str(reg): text for reg, text in hw.last_actions.items()},
            }
        )
        return attrs


class DrawProfileSensor(NibePiEntity, SensorEntity):
    """The learned hot water draw profile: when the next draw is expected."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:chart-timeline-variant"
    _unrecorded_attributes = frozenset({"profile", "day_sums", "top_slots"})

    def __init__(self, runtime: NibePiRuntime) -> None:
        super().__init__(runtime, "hw_draw_profile")

    def _expected(self):
        profile = self.runtime.hotwater.controller.profile.profile
        threshold = PlanSettings.from_values(peak_threshold=self.runtime.opt(OPT_HW_PEAK)).peak_threshold
        return next_draw(profile, dt_util.utcnow(), _local_tz(), threshold)

    @property
    def native_value(self):
        expected = self._expected()
        return expected.when if expected is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        controller = self.runtime.hotwater.controller
        profile = controller.profile
        expected = self._expected()
        event = controller.last_event
        attrs: Dict[str, Any] = {
            "confidence": expected.confidence if expected is not None else None,
            "expected_drop": expected.drop if expected is not None else None,
            "slot": slot_name(expected.slot) if expected is not None else None,
            "learned_draws": profile.learned,
            "last_learned": profile.last_learn,
            "last_drop": profile.last_drop,
            "last_slot": slot_name(profile.last_slot) if profile.last_slot is not None else None,
            "last_event": (
                {
                    "verdict": event.verdict,
                    "span": round(event.span, 1),
                    "minutes": event.minutes,
                    "rate": round(event.rate, 2) if event.rate != float("inf") else None,
                    "reason": event.reason,
                    "at": _iso(controller.last_event_at),
                }
                if event is not None
                else None
            ),
            "imported_from": profile.imported_from,
            "day_sums": {DAYS[i]: round(v, 1) for i, v in enumerate(day_sums(profile.profile))},
            "top_slots": top_slots(profile.profile),
            # Monday 00:00 first, one value per hour, °C.
            "profile": [round(v, 1) for v in profile.profile],
        }
        return attrs


# --------------------------------------------------------------------------
# Cost optimiser
# --------------------------------------------------------------------------

PLAN_HOURS = 24
MAX_BLOCKS = 12


def _hm(when, tz) -> str:
    return when.astimezone(tz).strftime("%H:%M")


def _day_start(day: Any) -> Optional[datetime]:
    """Local midnight of an ISO ``day``: the last reset of a daily total."""
    try:
        return dt_util.start_of_local_day(date.fromisoformat(str(day)))
    except ValueError:
        return None


def _money(value: Optional[float]) -> Optional[float]:
    return round(value, 2) if value is not None else None


def _currency(runtime: NibePiRuntime) -> str:
    return str(getattr(runtime.hass.config, "currency", None) or "SEK")


class OptimiserPlanSensor(_SystemSensor):
    """The optimiser's planned curve offset now, with the plan.

    Computed whatever the strategy, so it can be compared with the rules
    in shadow mode; ``applied`` says whether it is what goes to the lease.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-timeline-variant-shimmer"
    _unrecorded_attributes = frozenset({"plan_24h", "explanation", "summary"})

    def __init__(self, runtime: NibePiRuntime, system: str) -> None:
        super().__init__(runtime, "optimiser_plan", system)

    @property
    def native_value(self) -> Optional[int]:
        result = self.runtime.optimizer.heating.get(self.system)
        if result is None or result.plan is None:
            return None
        return offset_at(result.plan.plan.steps, dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        optimizer = self.runtime.optimizer
        system = self.system
        result = optimizer.heating.get(system)
        decision = optimizer.gates.get(system)
        choice = optimizer.choices.get(system)
        models = optimizer.models.get(system)
        applied = (
            choice is not None and choice.source != SOURCE_RULES and self.sys.mode == MODE_ACTIVE
        )
        attrs: Dict[str, Any] = {
            "strategy": optimizer.strategy.get(system),
            "applied": applied,
            "gate": (
                {"use": decision.use_optimizer, "offset": decision.offset, "reason": decision.reason}
                if decision is not None
                else None
            ),
            "model_confidence": round(models.fit.confidence, 2) if models is not None else None,
            "model_level": (models.fit.level if models.fit.ok else "prior") if models is not None else None,
            "planned_at": _iso(result.planned_at) if result is not None else None,
            "problem": result.problem if result is not None else optimizer.plan_error,
        }
        if result is None or result.plan is None:
            return attrs
        plan = result.plan
        tz = _local_tz()
        now = dt_util.utcnow()
        horizon = now + timedelta(hours=PLAN_HOURS)
        rows: List[List[Any]] = []
        last_offset = None
        for step in plan.plan.steps:
            if step.end <= now or step.start >= horizon:
                continue
            on_hour = step.start.astimezone(tz).minute == 0
            if step.offset != last_offset or on_hour:
                rows.append([_hm(step.start, tz), step.offset, round(step.t_in_end, 1)])
            last_offset = step.offset
        base = plan.baseline.energy_adjusted
        attrs.update(
            {
                "base_offset": result.base_offset,
                "horizon_h": round(sum((s.end - s.start).total_seconds() for s in plan.plan.steps) / 3600.0, 1),
                "expected_cost": _money(plan.plan.energy_adjusted),
                "baseline_cost": _money(base),
                "savings": _money(plan.savings),
                "savings_pct": round(100.0 * plan.savings / base, 1) if base > 0 else None,
                "currency": _currency(self.runtime),
                "predicted_min": round(plan.plan.min_t_in, 2),
                "predicted_max": round(plan.plan.max_t_in, 2),
                "below_band_kh": round(plan.plan.below_band_kh, 2),
                "slab_temp": result.slab_temp,
                "slab_state": result.slab,
                "slab_source": result.slab_source,
                "changes": plan.plan.changes,
                "elapsed_s": round(plan.elapsed_s, 3),
                "summary": result.summary,
                "explanation": [b.text for b in result.blocks if b.start < horizon][:MAX_BLOCKS],
                "plan_24h": rows,
            }
        )
        return attrs


class SavingsTodaySensor(NibePiEntity, SensorEntity):
    """What the optimiser's plans expect to save today against the base offset.

    The plans' savings, spread over the day by the baseline's cost; what
    earlier plans expected for the hours already passed is kept. Computed
    in shadow mode too: it is an expectation, not a measurement.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    # A daily total (it can fall: negative prices), so long-term statistics
    # give the savings per day.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:piggy-bank-outline"

    def __init__(self, runtime: NibePiRuntime) -> None:
        super().__init__(runtime, "optimiser_savings_today")

    @property
    def native_unit_of_measurement(self) -> str:
        return _currency(self.runtime)

    @property
    def last_reset(self) -> Optional[datetime]:
        return _day_start(self.runtime.optimizer.savings.get("day"))

    @property
    def native_value(self) -> Optional[float]:
        today = self.runtime.optimizer.savings_today
        if not today:
            return None
        return round(sum(today.values()), 2)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        optimizer = self.runtime.optimizer
        attrs: Dict[str, Any] = {k: _money(v) for k, v in optimizer.savings_today.items()}
        attrs["day"] = optimizer.savings.get("day")
        return attrs


class HeatPumpCostTodaySensor(NibePiEntity, SensorEntity):
    """The heat pump's electricity cost today, from a heat pump meter.

    Each 10-minute bin of the meter priced at its slot. The attributes
    hold what the plans predicted for the same hours (heating, hot water,
    and the heating baseline) and the gap; yesterday's figures too. Only
    with a meter whose scope is the heat pump.
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    # A daily total (negative prices can lower it): the cost per day in
    # the long-term statistics.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:heat-pump"
    _unrecorded_attributes = frozenset({"yesterday"})

    def __init__(self, runtime: NibePiRuntime) -> None:
        super().__init__(runtime, "heat_pump_cost_today")

    @property
    def native_unit_of_measurement(self) -> str:
        return _currency(self.runtime)

    @property
    def last_reset(self) -> Optional[datetime]:
        return _day_start(self.runtime.optimizer.hp_cost.get("day"))

    @property
    def native_value(self) -> Optional[float]:
        optimizer = self.runtime.optimizer
        tally = optimizer.hp_cost
        if not optimizer.measures_heat_pump or tally.get("day") != dt_util.now().date().isoformat():
            return None
        return round(float(tally.get("cost") or 0.0), 2)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        optimizer = self.runtime.optimizer
        tally = optimizer.hp_cost
        attrs: Dict[str, Any] = {"meter_scope": optimizer.meter_scope, "meter": optimizer.meter_entity}
        if tally.get("day"):
            attrs.update(cost_summary(tally))
            attrs["yesterday"] = tally.get("yesterday")
        return attrs


class HouseModelSensor(_SystemSensor):
    """The house model's confidence; the fitted figures as attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["prior", "low", "medium", "high"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:home-analytics"
    _unrecorded_attributes = frozenset(
        {"curve", "prior", "emitter_fit", "eta_fit", "meter_share", "history", "bt3_fit", "loop_fit"}
    )

    def __init__(self, runtime: NibePiRuntime, system: str) -> None:
        super().__init__(runtime, "house_model", system)

    @property
    def native_value(self) -> Optional[str]:
        models = self.runtime.optimizer.models.get(self.system)
        if models is None:
            return None
        return models.fit.level if models.fit.ok else "prior"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        optimizer = self.runtime.optimizer
        models = optimizer.models.get(self.system)
        if models is None:
            return {}
        attrs = models_summary(models)
        fits = optimizer.fits or {}
        system_fits = (fits.get("systems") or {}).get(self.system) or {}
        log = optimizer.log
        span = log.span()
        result = optimizer.heating.get(self.system)
        house_fit = system_fits.get("house") or {}
        attrs.update(
            {
                # The slab now, as the last plan started from it (floor heating).
                "slab_temperature": result.slab_temp if result is not None else None,
                "slab_source": result.slab_source if result is not None else None,
                "slab_fit_source": house_fit.get("slab_source"),
                "bt3_fit": house_fit.get("bt3"),
                "loop_fit": system_fits.get("loop"),
                "last_fit": fits.get("fitted_at"),
                "fit_error": optimizer.fit_error,
                "prior": system_fits.get("prior"),
                "emitter_fit": system_fits.get("emitter"),
                "eta_fit": fits.get("eta"),
                "meter_share": fits.get("hp_share"),
                "history": {
                    "bins": len(log),
                    "days": round((span[1] - span[0]) / 86400.0, 1) if span else 0.0,
                    "imported_at": optimizer.bootstrap_at,
                    "meter": optimizer.meter_entity,
                    "meter_problem": optimizer.meter_problem,
                },
            }
        )
        return attrs


class HotWaterOptimiserSensor(NibePiEntity, SensorEntity):
    """The optimiser's hot water plan: when the next charge starts."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:water-boiler-auto"
    _unrecorded_attributes = frozenset({"blocks"})

    def __init__(self, runtime: NibePiRuntime) -> None:
        super().__init__(runtime, "hw_optimiser_plan")

    def _next(self):
        tank = self.runtime.optimizer.tank
        if tank is None or tank.plan is None:
            return None
        now = dt_util.utcnow()
        return next((b for b in tank.plan.blocks if b.action == "charge" and b.end > now), None)

    @property
    def native_value(self):
        block = self._next()
        return block.start if block is not None else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        runtime = self.runtime
        tank = runtime.optimizer.tank
        decision = runtime.hotwater.decision
        attrs: Dict[str, Any] = {
            "enabled": bool(runtime.hotwater.features.get("optimiser")),
            "in_use": bool(decision is not None and decision.details.get("plan_source") == "optimiser"),
            "planned_at": _iso(tank.planned_at) if tank is not None else None,
            "problem": tank.problem if tank is not None else "not planned yet",
        }
        if tank is None or tank.plan is None:
            return attrs
        plan = tank.plan
        tz = _local_tz()
        now = dt_util.utcnow()
        horizon = now + timedelta(hours=PLAN_HOURS)
        nxt = self._next()
        attrs.update(
            {
                "next_target": nxt.target if nxt is not None else None,
                "next_boost": nxt.boost if nxt is not None else None,
                "expected_cost": _money(plan.cost),
                "baseline_cost": _money(plan.baseline_cost),
                "savings": _money(plan.savings),
                "currency": _currency(runtime),
                "min_temp": round(plan.min_temp, 1),
                "blocks": [
                    {
                        "start": _hm(b.start, tz),
                        "end": _hm(b.end, tz),
                        "action": b.action,
                        "target": b.target,
                        "boost": b.boost,
                        "reason": b.reason,
                    }
                    for b in plan.blocks
                    if b.end > now and b.start < horizon
                ],
            }
        )
        return attrs
