"""History for the cost optimiser: the user's meter and the sample log.

Pure helpers between Home Assistant and ``logic.optimizer``:

- ``meter_kind``: which kind of meter an entity is, from its unit,
  device class and state class.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

POWER_UNITS = {"w": "W", "kw": "kW", "mw": "MW"}
ENERGY_UNITS = {"wh": "Wh", "kwh": "kWh", "mwh": "MWh"}


def meter_kind(
    unit: Any, device_class: Any = None, state_class: Any = None
) -> Optional[Tuple[str, str]]:
    """``("power", unit)`` or ``("energy", unit)``, or None when unusable.

    W/kW/MW is a power meter, Wh/kWh/MWh an energy meter. An energy meter
    must count up (state class ``total_increasing`` or ``total``, or none
    given); a ``measurement`` in kWh is a per-period value, not a meter.
    A device class that contradicts the unit is refused.
    """
    if not isinstance(unit, str):
        return None
    key = unit.strip().lower()
    dev = str(device_class).strip().lower() if device_class else None
    state = str(state_class).strip().lower() if state_class else None
    if key in POWER_UNITS:
        if dev not in (None, "power"):
            return None
        return "power", POWER_UNITS[key]
    if key in ENERGY_UNITS:
        if dev not in (None, "energy"):
            return None
        if state not in (None, "total", "total_increasing"):
            return None
        return "energy", ENERGY_UNITS[key]
    return None
