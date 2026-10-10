"""Sensors: planned offset, component offsets, pump offset, lease state, price level."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import NibePiConfigEntry
from .const import COMPONENTS, PRICE_SOURCE_TIBBER, price_source
from .entity import NibePiEntity
from .logic.lease import LeaseManager
from .logic.price import LEVELS
from .runtime import NibePiRuntime, SystemRuntime


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
        return {
            "mode": sys.mode,
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


class LeaseStateSensor(_SystemSensor):
    """The gateway's ``lease/<reg>/state``."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["active", "grace", "restoring", "inactive", "error"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: NibePiRuntime, system: str, key: str = "lease_state") -> None:
        super().__init__(runtime, key, system)

    @property
    def lease(self) -> LeaseManager:
        return self.sys.lease

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
