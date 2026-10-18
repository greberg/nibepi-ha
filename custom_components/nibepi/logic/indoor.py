"""Room-temperature P-controller.

Ports ``runIndoor`` from config_node.js:

    offset = toFixed((setpoint - indoor) * factor, 2)

- The indoor reading must be fresh (``sensorIsStale``, default 60 min),
  finite and at least 4 °C; config_node treated anything below 4 as a
  broken sensor. Otherwise the offset is 0.
- A missing setpoint or factor gives 0.
- The ±10 limit is applied by ``curve.component_value`` like
  ``curveComponent`` does.

Deliberate differences:

- config_node read the setpoint and factor from the pump (47398/47402 for
  S1, 47397/47401 for S2, Nibe menu 1.9.4) and took the sensor from its own
  MQTT list. Here they come from the integration options (or an
  ``input_number`` entity) and any HA sensor, or BT50 over MQTT.
- The degree-minute reset that runIndoor also did (``dm_reset_*``) writes
  another register and is not part of the curve offset; it is left out.
- The setpoint-accuracy statistic used only for the Node-RED dashboard is
  left out.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .inputs import Decision, Reading, to_fixed, to_float, usable
from .texts import msg

# Pump defaults for menu 1.9.4 (registers 47398 / 47402).
DEFAULT_SETPOINT = 20.0
DEFAULT_FACTOR = 2.0
MIN_VALID_INDOOR = 4.0


def compute_indoor(
    indoor: Optional[Reading],
    setpoint: Optional[float],
    factor: Optional[float],
    now: datetime,
    sensor_timeout_min: Optional[float] = None,
) -> Decision:
    problem = usable(indoor, now, sensor_timeout_min)
    if problem is not None:
        return Decision.zero(msg("indoor.sensor_%s" % problem))
    assert indoor is not None
    value = float(indoor.value)
    if value < MIN_VALID_INDOOR:
        return Decision.zero(msg("indoor.implausible", value=value), indoor=value)
    set_value = to_float(setpoint)
    if set_value is None:
        return Decision.zero(msg("indoor.setpoint_missing"), indoor=value)
    factor_value = to_float(factor)
    if factor_value is None:
        return Decision.zero(msg("indoor.factor_missing"), indoor=value, setpoint=set_value)
    offset = to_fixed((set_value - value) * factor_value, 2)
    return Decision(
        offset,
        msg("indoor.ok", setpoint=set_value, indoor=value, factor=factor_value),
        True,
        {"indoor": value, "setpoint": set_value, "factor": factor_value},
    )
