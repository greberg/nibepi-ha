"""Hot water: automatic luxury, hot water priority, comfort mode by price.

Ports from config_node.js:

- ``hotwaterPlugin``, automatic luxury (``enable_autoluxury``): every
  minute BT6 is stored; the trigger is the BT6 value ``time`` samples ago
  (strictly: ``time - 1``, as the Node-RED list is popped when full) minus
  ``diff``. BT6 at or under the trigger starts a one-time increase
  (48132 = 4) towards ``trigger + diff - 5`` (5 °C under where BT6 was);
  it ends when BT6 reaches that, or the stop temperature of the current
  comfort mode minus 1. Defaults ``diff`` 20 °C, ``time`` 10 (default.json).
- ``hotwaterPlugin``, hot water priority (``enable_hw_priority``): BT7 at
  or under the start temperature of the current comfort mode (47045/47044/
  47043) starts a one-time increase; it ends at the stop temperature minus
  1, unless automatic luxury is still running.
- The hot water part of ``priceAdjustCurve`` / ``priceSetHwMode``: with
  ``hotwater_enable`` the comfort mode (47041) is set to
  ``hotwater_<level>`` for the current price level. Defaults (default.json):
  very cheap 2 (luxury), cheap 1 (normal), normal/expensive/very expensive
  0 (economy).

Deliberate differences:

- Both features keep their own "running" state instead of reading 48132
  back from the pump: the lease value is what the gateway holds. A charge
  started by someone else (48132 = 4 not under our lease) still blocks a
  new trigger, as Node-RED's ``hwON.raw_data !== 4`` did.
- Both stop after 3 hours at most: in shadow mode, or if the target is
  never reached, a held 48132 = 4 would otherwise never end.
- Missing inputs (BT6/BT7, the comfort mode or its stop/start
  temperature) end a running charge at once; Node-RED skipped the run and
  left 48132 as it was.
- A level whose setting is "unchanged" leaves 47041 to the pump (Node-RED:
  an unset ``hotwater_<level>``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .inputs import to_fixed, to_float

TEMP_LUX_START = 4  # 48132: one-time increase
FEATURE_LIMIT = timedelta(hours=3)

DEFAULT_AUTOLUX_DIFF = 20.0
DEFAULT_AUTOLUX_TIME = 10

COMFORT_UNCHANGED = "unchanged"
COMFORT_CHOICES = (COMFORT_UNCHANGED, "0", "1", "2")
COMFORT_NAMES = {0: "economy", 1: "normal", 2: "luxury"}
COMFORT_DEFAULTS = {
    "VERY_CHEAP": "2",
    "CHEAP": "1",
    "NORMAL": "0",
    "EXPENSIVE": "0",
    "VERY_EXPENSIVE": "0",
}

# Comfort mode (47041) -> its start / stop temperature registers.
START_REGISTERS = {0: 47045, 1: 47044, 2: 47043}
STOP_REGISTERS = {0: 47049, 1: 47048, 2: 47047}


@dataclass(frozen=True)
class FeatureResult:
    heat: bool  # wants 48132 = 4
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)


def comfort_mode(level: Optional[str], settings: Mapping[str, Any]) -> Tuple[Optional[int], str]:
    """47041 for the price level, or None to leave it to the pump."""
    if level is None:
        return None, "no price for the current hour"
    choice = settings.get(level, COMFORT_DEFAULTS.get(level, COMFORT_UNCHANGED))
    choice = str(choice).strip() if choice is not None else COMFORT_UNCHANGED
    if choice not in COMFORT_CHOICES or choice == COMFORT_UNCHANGED:
        return None, "%s: unchanged" % level
    value = int(choice)
    return value, "%s: %s" % (level, COMFORT_NAMES[value])


def _running_elsewhere(pump_lux: Optional[float], held: bool) -> bool:
    return pump_lux is not None and int(pump_lux) == TEMP_LUX_START and not held


class AutoLuxury:
    """Automatic luxury: a one-time increase on a fast BT6 drop."""

    def __init__(self) -> None:
        self.history: List[float] = []
        self.reset()

    def reset(self) -> None:
        self.history = []
        self.active = False
        self.target: Optional[float] = None
        self.trigger: Optional[float] = None
        self.since: Optional[datetime] = None

    def _stop(self) -> None:
        self.active = False
        self.target = None
        self.since = None

    def step(
        self,
        bt6: Optional[float],
        stop_temp: Optional[float],
        pump_lux: Optional[float],
        held: bool,
        now: datetime,
        diff: Any = DEFAULT_AUTOLUX_DIFF,
        samples: Any = DEFAULT_AUTOLUX_TIME,
    ) -> FeatureResult:
        if bt6 is None or stop_temp is None:
            self.reset()
            return FeatureResult(False, "BT6 missing" if bt6 is None else "stop temperature unknown")
        delta = to_float(diff)
        if delta is None or delta <= 0:
            delta = DEFAULT_AUTOLUX_DIFF
        count = to_float(samples)
        count_int = int(count) if count is not None and count >= 1 else DEFAULT_AUTOLUX_TIME
        self.history.insert(0, bt6)
        if len(self.history) >= count_int:
            del self.history[count_int:]
            reference = self.history.pop()
        else:
            reference = self.history[-1]
        self.trigger = to_fixed(reference - delta, 2)
        limit = stop_temp - 1
        details = {"trigger": self.trigger, "target": self.target, "stop": limit}
        if not self.active:
            if _running_elsewhere(pump_lux, held):
                return FeatureResult(False, "temporary lux already running", details)
            if bt6 <= self.trigger:
                self.active = True
                self.target = to_fixed(self.trigger + delta - 5, 2)
                self.since = now
                details["target"] = self.target
                return FeatureResult(
                    True, "BT6 %.1f fell to the trigger %.1f: heat to %.1f" % (bt6, self.trigger, self.target), details
                )
            return FeatureResult(False, "BT6 %.1f, trigger %.1f" % (bt6, self.trigger), details)
        assert self.target is not None
        if bt6 >= self.target or bt6 >= limit:
            self._stop()
            return FeatureResult(False, "BT6 %.1f reached the target" % bt6, details)
        if self.since is not None and now - self.since >= FEATURE_LIMIT:
            self._stop()
            return FeatureResult(False, "time limit reached", details)
        return FeatureResult(True, "BT6 %.1f, heat to %.1f" % (bt6, self.target), details)


class HwPriority:
    """Hot water priority: a one-time increase at the start temperature."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.active = False
        self.since: Optional[datetime] = None

    def step(
        self,
        bt7: Optional[float],
        start_temp: Optional[float],
        stop_temp: Optional[float],
        pump_lux: Optional[float],
        held: bool,
        autolux_active: bool,
        now: datetime,
    ) -> FeatureResult:
        if bt7 is None or start_temp is None or stop_temp is None:
            self.reset()
            return FeatureResult(False, "BT7 missing" if bt7 is None else "start/stop temperature unknown")
        limit = stop_temp - 1
        details = {"start": start_temp, "stop": limit}
        if not self.active:
            if _running_elsewhere(pump_lux, held):
                return FeatureResult(False, "temporary lux already running", details)
            if bt7 <= start_temp:
                self.active = True
                self.since = now
                return FeatureResult(True, "BT7 %.1f at the start temperature %.1f" % (bt7, start_temp), details)
            return FeatureResult(False, "BT7 %.1f over the start temperature %.1f" % (bt7, start_temp), details)
        if bt7 >= limit and not autolux_active:
            self.reset()
            return FeatureResult(False, "BT7 %.1f reached %.1f" % (bt7, limit), details)
        if self.since is not None and now - self.since >= FEATURE_LIMIT:
            self.reset()
            return FeatureResult(False, "time limit reached", details)
        return FeatureResult(True, "BT7 %.1f, heat to %.1f" % (bt7, limit), details)
