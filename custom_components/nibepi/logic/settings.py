"""Pump settings changed for good through the gateway's settings channel.

The integration otherwise only writes through leases, which the gateway
undoes when Home Assistant stops renewing them: right for control, wrong
for a setting such as the luxury stop temperature. The gateway (from
build e19a814) takes ``<prefix>setting/<reg>/set`` for a fixed list of
registers, writes it for good, and while a lease holds the register makes
the value the lease's baseline instead (the restore at the end writes it).
``gateway/state`` lists those registers under ``settings``.

Here: which registers are settings, their limits (the gateway's), what to
show while a lease holds the register, and the hot water stop
temperatures' order (economy <= normal <= luxury, which the pump and the
tank planner assume).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .inputs import to_float
from .texts import Msg, msg


@dataclass(frozen=True)
class Setting:
    register: int
    key: str  # entity translation key
    minimum: float
    maximum: float
    step: float = 0.5


# The gateway's limits (lib/lease.js DEFAULT_SETTINGS).
HW_STOP_ECONOMY = Setting(47049, "hw_stop_economy", 35.0, 55.0)
HW_STOP_NORMAL = Setting(47048, "hw_stop_normal", 40.0, 60.0)
HW_STOP_LUXURY = Setting(47047, "hw_stop_luxury", 45.0, 65.0)
SETTINGS = (HW_STOP_ECONOMY, HW_STOP_NORMAL, HW_STOP_LUXURY)
# Lowest to highest: each must not be above the next.
HW_STOP_ORDER = (HW_STOP_ECONOMY, HW_STOP_NORMAL, HW_STOP_LUXURY)


def gateway_supports(state: Optional[Mapping[str, Any]], register: int) -> bool:
    """Whether the gateway's ``gateway/state`` lists ``register`` as a setting."""
    settings = (state or {}).get("settings")
    if not isinstance(settings, (list, tuple)):
        return False
    return str(register) in {str(r) for r in settings}


def setting_value(reading: Optional[float], lease: Optional[Mapping[str, Any]]) -> Optional[float]:
    """The setting as the pump keeps it: while a lease holds the register
    (47047 during a legionella cycle) it reads the lease's value, and the
    setting is the baseline the lease restores."""
    lease = lease or {}
    if lease.get("active") is True or lease.get("restoring") is True:
        baseline = to_float(lease.get("baseline"))
        if baseline is not None:
            return baseline
    return reading


def order_problem(setting: Setting, value: float, current: Mapping[int, Optional[float]]) -> Optional[Msg]:
    """Why ``value`` would break economy <= normal <= luxury, else None.
    ``current``: the other stop temperatures by register (None: unknown)."""
    if setting not in HW_STOP_ORDER:
        return None
    i = HW_STOP_ORDER.index(setting)
    for lower in HW_STOP_ORDER[:i]:
        other = current.get(lower.register)
        if other is not None and value < other:
            return msg("setting.order", value=value, other=other)
    for higher in HW_STOP_ORDER[i + 1:]:
        other = current.get(higher.register)
        if other is not None and value > other:
            return msg("setting.order", value=value, other=other)
    return None


def setting_payload(setting: Setting, value: float) -> str:
    """The ``setting/<reg>/set`` payload: the value rounded to the step."""
    steps = round(float(value) / setting.step)
    return ("%g" % (steps * setting.step))
