"""Away (vacation) mode: lower the target while nobody is home.

One Away state per config entry, for every climate system and hot water.
It is switched on without an end (an alarm automation turning the Away
switch on when the house is armed), or with an expected return
(``nibepi.set_away`` with ``until``), when it ends by itself.

What it changes (whether anything is written still depends on the
off/shadow/active modes):

- **Target.** The indoor setpoint is lowered by the setback (option
  ``away_setback``, 3 °C, or the action's ``setback``), never below the
  absolute floor ``away_min_indoor`` (16 °C) and never above the normal
  setpoint. ``away_setpoint``.
- **Rules strategy.** The indoor component and the price component's
  temperature guard work on the lowered setpoint. When the indoor
  component cannot steer (switched off, or no usable sensor) a fixed
  ``away`` component (``away_curve_offset``, -3) lowers the curve instead,
  so the pump still goes down; when the indoor component is active the
  ``away`` component is 0, so the setback is not applied twice.
  ``away_component``. With ``away_return_lead_h`` the rules return to
  normal that many hours before ``until``.
- **Cost optimiser.** The comfort target, the band's lower edge and the
  hard floor move down with the target; the hard floor stays at or above
  ``away_min_indoor``. Warmer than the lowered target is only paid for in
  energy (no tracking above it, the upper edge stays). ``away_comfort``. With ``away_preheat`` and a known
  return the band is the lowered one until ``until - away_return_lead_h``,
  ramps to the normal band and is normal from ``until``, so the planner
  heats the house back in time (``planner.comfort_schedule``).
- **Hot water.** ``away_hot_water``: ``economy`` (default), ``normal``
  (unchanged) or ``minimum``; see ``hwcontrol``. VV learning neither learns
  nor decays while away (``VvProfile.hold``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

from .inputs import Decision, to_float
from .optimizer.planner import ComfortSettings

HW_ECONOMY = "economy"
HW_NORMAL = "normal"
HW_MINIMUM = "minimum"
HW_CHOICES = (HW_ECONOMY, HW_NORMAL, HW_MINIMUM)

DEFAULT_SETBACK = 3.0
DEFAULT_MIN_INDOOR = 16.0
DEFAULT_CURVE_OFFSET = -3.0
DEFAULT_HOT_WATER = HW_ECONOMY
DEFAULT_PREHEAT = True
DEFAULT_RETURN_LEAD_H = 0.0

MAX_SETBACK = 10.0
MAX_RETURN_LEAD_H = 48.0


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


@dataclass(frozen=True)
class AwayState:
    """What is persisted: on/off, the expected return and a setback override."""

    enabled: bool = False
    until: Optional[datetime] = None  # aware; None: no end
    setback: Optional[float] = None  # None: the option
    since: Optional[datetime] = None

    def active(self, now: datetime) -> bool:
        return self.enabled and (self.until is None or now < self.until)

    def expired(self, now: datetime) -> bool:
        """On, with an end that has passed: to be switched off."""
        return self.enabled and self.until is not None and now >= self.until

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "until": self.until.isoformat() if self.until is not None else None,
            "setback": self.setback,
            "since": self.since.isoformat() if self.since is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "AwayState":
        """Tolerant load: anything malformed is off / no end / no override."""
        if not isinstance(data, Mapping):
            return cls()
        enabled = data.get("enabled") is True
        if not enabled:
            return cls()
        setback = to_float(data.get("setback"))
        if setback is not None and not 0 <= setback <= MAX_SETBACK:
            setback = None
        return cls(True, _parse_time(data.get("until")), setback, _parse_time(data.get("since")))


def start(now: datetime, until: Optional[datetime] = None, setback: Optional[float] = None) -> AwayState:
    """Away from ``now``. An ``until`` that is not in the future (or naive)
    means no end; a setback outside 0..10 °C means the option's."""
    end = until if until is not None and until.tzinfo is not None and until > now else None
    value = to_float(setback)
    if value is not None and not 0 <= value <= MAX_SETBACK:
        value = None
    return AwayState(True, end.astimezone(timezone.utc) if end is not None else None, value, now)


@dataclass(frozen=True)
class AwaySettings:
    setback: float = DEFAULT_SETBACK
    min_indoor: float = DEFAULT_MIN_INDOOR
    curve_offset: float = DEFAULT_CURVE_OFFSET
    hot_water: str = DEFAULT_HOT_WATER
    preheat: bool = DEFAULT_PREHEAT
    return_lead_h: float = DEFAULT_RETURN_LEAD_H

    @classmethod
    def from_values(
        cls,
        setback: Any = None,
        min_indoor: Any = None,
        curve_offset: Any = None,
        hot_water: Any = None,
        preheat: Any = None,
        return_lead_h: Any = None,
    ) -> "AwaySettings":
        sb = to_float(setback)
        mi = to_float(min_indoor)
        co = to_float(curve_offset)
        lead = to_float(return_lead_h)
        return cls(
            setback=min(MAX_SETBACK, max(0.0, sb)) if sb is not None else DEFAULT_SETBACK,
            min_indoor=mi if mi is not None else DEFAULT_MIN_INDOOR,
            curve_offset=max(-10.0, min(10.0, co)) if co is not None else DEFAULT_CURVE_OFFSET,
            hot_water=str(hot_water) if hot_water in HW_CHOICES else DEFAULT_HOT_WATER,
            preheat=DEFAULT_PREHEAT if preheat is None else bool(preheat),
            return_lead_h=min(MAX_RETURN_LEAD_H, max(0.0, lead)) if lead is not None else DEFAULT_RETURN_LEAD_H,
        )

    def setback_for(self, state: AwayState) -> float:
        return state.setback if state.setback is not None else self.setback


def returning_at(state: AwayState, settings: AwaySettings) -> Optional[datetime]:
    """When the rules (and hot water) go back to normal: ``until - lead``."""
    if state.until is None:
        return None
    return state.until - timedelta(hours=settings.return_lead_h)


def rules_away(state: AwayState, settings: AwaySettings, now: datetime) -> bool:
    """Away for the rules strategy and hot water: on, and the return lead
    before ``until`` has not started."""
    if not state.active(now):
        return False
    back = returning_at(state, settings)
    return back is None or now < back


def away_setpoint(setpoint: Optional[float], setback: float, min_indoor: Optional[float]) -> Optional[float]:
    """The lowered setpoint: ``setpoint - setback``, not below ``min_indoor``
    and never above the normal setpoint."""
    value = to_float(setpoint)
    if value is None:
        return None
    lowered = value - max(0.0, setback)
    floor = to_float(min_indoor)
    if floor is not None:
        lowered = max(lowered, floor)
    return min(value, lowered)


def away_component(indoor_active: bool, curve_offset: float) -> Decision:
    """The ``away`` curve component while away (rules strategy).

    With an active indoor component the lowered setpoint already pulls the
    curve down, so this is 0; otherwise it is the fixed ``curve_offset``.
    """
    if indoor_active:
        return Decision(
            0.0, "away: indoor control works on the lowered setpoint", True, {"indoor_active": True}
        )
    return Decision(
        float(curve_offset),
        "away: fixed curve offset %+g (indoor control not active)" % curve_offset,
        True,
        {"indoor_active": False, "curve_offset": float(curve_offset)},
    )


def indoor_active(enabled: bool, decision: Optional[Decision]) -> bool:
    """The indoor component steers: switched on with a usable sensor and setpoint."""
    return bool(enabled) and decision is not None and decision.inputs_ok


def away_comfort(comfort: ComfortSettings, setback: float, min_indoor: Optional[float]) -> ComfortSettings:
    """The optimiser's comfort band moved down with the lowered target.

    The target, the lower edge and the hard floor move down by the
    setback; the hard floor (target - hard_floor_below + sensor margin) is
    kept at or above ``min_indoor`` and the lower edge never under it.
    Above the target nothing is penalised but the energy: the upper edge
    stays the normal one and there is no tracking above the target. Being
    warmer than the lowered target costs energy, which the plan already
    counts, and a band that pushed the house down would fight the heating
    back before the return (``comfort_schedule``).
    """
    target = away_setpoint(comfort.target, setback, min_indoor)
    if target is None or target >= comfort.target:
        return comfort
    floor_below = comfort.hard_floor_below
    floor = to_float(min_indoor)
    if floor is not None:
        floor_below = max(0.0, min(floor_below, target + comfort.sensor_margin - floor))
    return replace(
        comfort,
        target=target,
        hard_floor_below=floor_below,
        below=min(comfort.below, floor_below),
        above=comfort.upper - target,
        tracking_above=0.0,
    )
