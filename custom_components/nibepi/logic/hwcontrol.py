"""The hot water controller: one owner for 47041, 47134, 48132 and 47047.

Node-RED had three writers for these registers (``vvAiTick``,
``hotwaterPlugin``, ``priceAdjustCurve``) that kept each other apart with
config flags. Here one controller asks every feature what it wants and
makes one decision per tick, with a reason per feature:

- 48132 temporary lux: 4 while any feature wants a charge (VV-AI control,
  automatic luxury, hot water priority), else released (the gateway writes
  0). It is never held at 0.
- 47134 hot water period: 0 while VV-AI control is in charge (the pump
  then makes hot water only when told to), else released (the gateway
  restores its captured baseline).
- 47041 comfort mode: the mode for the current price level while price
  comfort mode is on, else released (baseline).

Optimised hot water charges (feature ``optimiser``, G6): the cost
optimiser's charge blocks replace the VV-AI plan's heating windows. The
VV path is otherwise unchanged: 47134 is held at 0, a charge is started
with 48132 = 4 and ends when BT7 reaches the block's target, the min-temp
floor still applies, and automatic luxury or priority still make it stand
aside. Without a current optimiser plan the VV-AI plan is used when VV-AI
control is on, otherwise hot water is left to the pump.

Rules carried over from Node-RED:

- Automatic luxury or hot water priority on means VV-AI control stands
  aside (``aiOk = aiControlEnabled && !hwRegEnabled``); the min-temp floor
  belongs to VV-AI control and stands aside with it.
- Learning observes in every mode except ``off`` while learning or VV-AI
  control is on (Node-RED ran the detection whenever ``vvAiTick`` ran).

Fail-safe (new; see docs/hotwater.md):

- A missing or stale input releases what depends on it at once: BT7 or the
  stop temperatures for VV-AI control, BT6 for automatic luxury, BT7 and
  the start/stop temperatures for priority, the price for comfort mode.
- VV-AI control does not take 47134 when the pump's period reads 0 and the
  lease is not ours: the gateway would capture 0 as the baseline and
  "restore" the pump to no hot water. Nor while the period is unknown, nor
  with nothing learned and the min-temp floor off.
- ``HotWaterLeases.decide`` releases every lease when the tick was
  unhealthy or the mode is not ``active``, and sends nothing in active mode
  during the start-up grace period (register values are not retained).

Away mode (``HotWaterInputs.away``, the ``away_hot_water`` option):

- ``normal``: nothing changes.
- ``economy`` (default): 47041 is held at 0 (economy) whatever the price
  comfort mode says; no VV-AI or optimiser charges and no automatic luxury
  or priority boosts. VV-AI control releases 47134, so the pump makes hot
  water itself in economy mode (its own periodic increase included); the
  min-temp floor still starts a one-time increase (48132 = 4) if BT7 falls
  under it.
- ``minimum``: as economy, but where VV-AI control (or optimised charges)
  is on and may hold 47134 at 0 it keeps doing so and only the min-temp
  floor heats. All of VV-AI control's refusals still apply (pump period 0,
  stop temperatures or BT7 unknown), and it needs the floor on; otherwise
  it falls back to economy. It never takes 47134 when VV-AI control is
  off, so the pump's own periodic increase (legionella) is only held off
  where it already was.
- Learning neither learns nor decays while away (``learn_away``).

Legionella guard (``logic/legionella.py``, settings ``legionella``):

- Every tick, in every mode but ``off``, BT7 is watched for a legionella
  hold (the pump's own periodic increase counts), and a window is planned
  when a cycle is due.
- While a cycle runs (hot water mode ``active`` only) it overrides every
  other decision, Away included: 47047 (luxury stop temperature) = the
  legionella temperature, 48132 = 4 (a one-time increase heats to 47047),
  47134 released so the pump may charge. 47041 is left to the other
  features: a one-time increase heats to the luxury stop temperature
  whatever the comfort mode.
- While 47047 is (or was just) leased, the luxury stop temperature the
  plans use is the value from before the cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .hotwater import (
    COMFORT_DEFAULTS,
    COMFORT_NAMES,
    DEFAULT_AUTOLUX_DIFF,
    DEFAULT_AUTOLUX_TIME,
    START_REGISTERS,
    STOP_REGISTERS,
    TEMP_LUX_START,
    AutoLuxury,
    HwPriority,
    comfort_mode,
)
from .inputs import PriceSlot, Reading, to_float, usable
from .legionella import INPUT_REGISTERS as LEGIONELLA_REGISTERS
from .legionella import (
    REG_LUX_STOP,
    REG_PERIODIC,
    REG_PERIODIC_INTERVAL,
    REG_PERIODIC_STOP,
    LegionellaGuard,
    LegionellaSettings,
    LegionellaStatus,
    parameters,
)
from .away import HW_ECONOMY as AWAY_ECONOMY
from .away import HW_MINIMUM as AWAY_MINIMUM
from .lease import MODE_ACTIVE, MODE_OFF, LeaseAction, LeaseManager
from .texts import Msg, msg, term
from .vvlearn import DrawDetector, DrawEvent, VvProfile, is_charging, learning_alpha, min_drop, week_slot
from .vvplan import (
    HeatWindow,
    PlanSettings,
    StopTemps,
    VvControl,
    build_plan,
    day_mode,
    day_sums,
    next_heat_start,
)

REG_BT7 = 40013
REG_BT6 = 40014
REG_PRIO = 43086
REG_COMFORT = 47041
REG_PERIOD = 47134
REG_TEMP_LUX = 48132

INPUT_REGISTERS = (
    REG_BT7,
    REG_BT6,
    REG_PRIO,
    REG_COMFORT,
    REG_PERIOD,
    REG_TEMP_LUX,
) + tuple(sorted(set(START_REGISTERS.values()) | set(STOP_REGISTERS.values()) | set(LEGIONELLA_REGISTERS)))

LEASE_LIMITS = {REG_COMFORT: (0, 2), REG_PERIOD: (0, 180), REG_TEMP_LUX: (0, 4), REG_LUX_STOP: (45, 70)}
LEASE_DECIMALS = {REG_LUX_STOP: 1}

FEATURES = ("learning", "vv_control", "autoluxury", "priority", "price_comfort", "optimiser")
# Node-RED's defaults, except learning (see docs/hotwater.md).
FEATURE_DEFAULTS = {
    "learning": True,
    "vv_control": False,
    "autoluxury": False,
    "priority": False,
    "price_comfort": False,
    "optimiser": False,
}

STATES = ("off", "fault", "idle", "heat_now", "block", "economy", "normal", "luxury", "legionella")

# A feature that is switched off.
OFF = msg("common.off")

DEFAULT_FLOOR_TEMP = 35.0


@dataclass(frozen=True)
class HotWaterSettings:
    learning_rate: Any = 10
    min_drop: Any = 3.0
    plan: PlanSettings = field(default_factory=PlanSettings)
    floor_enabled: bool = True
    floor_temp: Optional[float] = DEFAULT_FLOOR_TEMP
    autolux_diff: Any = DEFAULT_AUTOLUX_DIFF
    autolux_time: Any = DEFAULT_AUTOLUX_TIME
    comfort: Mapping[str, Any] = field(default_factory=lambda: dict(COMFORT_DEFAULTS))
    sensor_timeout: Optional[float] = None
    legionella: LegionellaSettings = field(default_factory=LegionellaSettings)


@dataclass(frozen=True)
class HotWaterInputs:
    now: datetime
    tz: tzinfo
    readings: Mapping[int, Reading]
    price_level: Optional[str] = None
    price_slots: Sequence[PriceSlot] = ()
    held: Mapping[int, bool] = field(default_factory=dict)
    # The cost optimiser's charge blocks as windows (None: no current plan).
    optimiser_windows: Optional[Sequence[HeatWindow]] = None
    optimiser_problem: Optional[str] = None
    # Away mode's hot water policy (away.HW_*), None when not away.
    away: Optional[str] = None


@dataclass
class HotWaterDecision:
    state: str
    targets: Dict[int, Optional[float]]
    reasons: Dict[str, str]
    inputs_ok: bool = True
    windows: List[HeatWindow] = field(default_factory=list)
    next_heat: Optional[datetime] = None
    details: Dict[str, Any] = field(default_factory=dict)
    legionella: Optional[LegionellaStatus] = None

    @classmethod
    def idle(cls, state: str, reason: str) -> "HotWaterDecision":
        return cls(state, {reg: None for reg in LEASE_LIMITS}, {"mode": reason})


class HotWaterController:
    """Holds the learning and control state; ``decide`` once per tick."""

    def __init__(self, profile: Optional[VvProfile] = None) -> None:
        self.profile = profile or VvProfile()
        self.detector = DrawDetector()
        self.vv = VvControl()
        self.autolux = AutoLuxury()
        self.priority = HwPriority()
        self.last_event: Optional[DrawEvent] = None
        self.last_event_at: Optional[datetime] = None
        self.legionella = LegionellaGuard()

    def reset_control(self) -> None:
        self.vv.reset()
        self.autolux.reset()
        self.priority.reset()

    @staticmethod
    def _value(inputs: HotWaterInputs, reg: int, timeout: Optional[float]) -> Optional[float]:
        reading = inputs.readings.get(reg)
        if usable(reading, inputs.now, timeout) is not None:
            return None
        assert reading is not None
        return float(reading.value)

    # -- learning -----------------------------------------------------------

    def learn(self, inputs: HotWaterInputs, settings: HotWaterSettings, enabled: bool) -> bool:
        """One detection step; True when the profile changed (save it)."""
        if not enabled:
            self.detector.reset()
            return False
        alpha = learning_alpha(settings.learning_rate)
        changed = self.profile.apply_daily_decay(inputs.now.astimezone(inputs.tz).date(), alpha)
        bt6 = self._value(inputs, REG_BT6, settings.sensor_timeout)
        prio = self._value(inputs, REG_PRIO, settings.sensor_timeout)
        event = self.detector.step(
            bt6, is_charging(prio), inputs.now, inputs.tz, min_drop(settings.min_drop)
        )
        if event is not None:
            self.last_event = event
            self.last_event_at = inputs.now
            if event.learned:
                changed = (
                    self.profile.register_draw(event.drop, event.booked, inputs.now, inputs.tz, alpha)
                    or changed
                )
        return changed

    def learn_away(self, inputs: HotWaterInputs) -> bool:
        """Away: no detection, no learning, no decay (``VvProfile.hold``)."""
        self.detector.reset()
        local = inputs.now.astimezone(inputs.tz)
        return self.profile.hold(local.date(), week_slot(inputs.now, inputs.tz))

    # -- control --------------------------------------------------------------

    def decide(
        self,
        mode: str,
        features: Mapping[str, bool],
        inputs: HotWaterInputs,
        settings: HotWaterSettings,
        sample: bool = True,
    ) -> HotWaterDecision:
        """``sample`` False: an extra decision between ticks, which does not
        add an automatic luxury sample (they are counted per minute)."""
        now = inputs.now
        timeout = settings.sensor_timeout
        legionella = self._legionella(mode, inputs, settings)
        if mode == MODE_OFF:
            self.reset_control()
            decision = HotWaterDecision.idle("off", msg("hw.mode_off"))
            decision.legionella = legionella
            return decision
        lux_held = bool(inputs.held.get(REG_LUX_STOP))

        def value(reg: int) -> Optional[float]:
            raw = self._value(inputs, reg, timeout)
            if reg == REG_LUX_STOP:
                # During (and just after) a cycle 47047 reads the
                # legionella temperature; plan with the pump's own value.
                return self.legionella.lux_stop(now, raw, lux_held)
            return raw

        def on(name: str) -> bool:
            return bool(features.get(name, FEATURE_DEFAULTS[name]))

        bt6 = value(REG_BT6)
        bt7 = value(REG_BT7)
        comfort_raw = value(REG_COMFORT)
        comfort_now = int(comfort_raw) if comfort_raw is not None else None
        stop_now = value(STOP_REGISTERS[comfort_now]) if comfort_now in STOP_REGISTERS else None
        start_now = value(START_REGISTERS[comfort_now]) if comfort_now in START_REGISTERS else None
        stops = StopTemps(value(STOP_REGISTERS[0]), value(STOP_REGISTERS[1]), value(STOP_REGISTERS[2]))
        pump_lux = value(REG_TEMP_LUX)
        period = value(REG_PERIOD)
        held_lux = bool(inputs.held.get(REG_TEMP_LUX))
        held_period = bool(inputs.held.get(REG_PERIOD))

        reasons: Dict[str, str] = {}
        sources: List[str] = []
        faults: List[str] = []
        targets: Dict[int, Optional[int]] = {reg: None for reg in LEASE_LIMITS}
        # Away with economy or minimum hot water: economy mode, no charges
        # or boosts; the min-temp floor stays.
        reduced = inputs.away in (AWAY_ECONOMY, AWAY_MINIMUM)
        if reduced:
            reasons["away"] = msg("hw.away_reduced", away=term("away_hw", inputs.away))
        elif inputs.away is not None:
            reasons["away"] = msg("hw.away_unchanged", away=term("away_hw", inputs.away))

        # Price comfort mode -> 47041.
        if reduced:
            targets[REG_COMFORT] = 0
            reasons["price_comfort"] = msg("hw.away_economy")
        elif on("price_comfort"):
            targets[REG_COMFORT], reasons["price_comfort"] = comfort_mode(inputs.price_level, settings.comfort)
            if inputs.price_level is None:
                faults.append("price_comfort")
        else:
            reasons["price_comfort"] = OFF

        # Automatic luxury and hot water priority -> 48132.
        if reduced:
            self.autolux.reset()
            self.priority.reset()
            reasons["autoluxury"] = msg("hw.away_off") if on("autoluxury") else OFF
            reasons["priority"] = msg("hw.away_off") if on("priority") else OFF
        elif on("autoluxury"):
            result = self.autolux.step(
                bt6, stop_now, pump_lux, held_lux, now, settings.autolux_diff, settings.autolux_time, sample
            )
            reasons["autoluxury"] = result.reason
            if result.heat:
                sources.append("autoluxury")
            if bt6 is None or stop_now is None:
                faults.append("autoluxury")
        else:
            self.autolux.reset()
            reasons["autoluxury"] = OFF
        if reduced:
            pass
        elif on("priority"):
            result = self.priority.step(
                bt7, start_now, stop_now, pump_lux, held_lux, self.autolux.active, now
            )
            reasons["priority"] = result.reason
            if result.heat:
                sources.append("priority")
            if bt7 is None or start_now is None or stop_now is None:
                faults.append("priority")
        else:
            self.priority.reset()
            reasons["priority"] = OFF

        # VV-AI control -> 47134 and 48132, with the VV-AI plan's windows or
        # the cost optimiser's charge blocks.
        plan_settings = settings.plan
        optimised = on("optimiser") and inputs.optimiser_windows is not None and not reduced
        use_price = plan_settings.use_price and not optimised and not reduced
        if reduced:
            windows = []
            learned_plan = False
            reasons["optimiser"] = msg("away.no_charges") if on("optimiser") else OFF
        elif optimised:
            windows = list(inputs.optimiser_windows or ())
            learned_plan = True
            reasons["optimiser"] = msg("hw.opt_drives")
        else:
            windows = build_plan(self.profile.profile, now, inputs.tz, plan_settings, stops, inputs.price_slots)
            learned_plan = any(day_mode(total, plan_settings) > 0 for total in day_sums(self.profile.profile))
            if not on("optimiser"):
                reasons["optimiser"] = OFF
            else:
                reasons["optimiser"] = msg(
                    "hw.opt_no_plan_vv" if on("vv_control") else "hw.opt_no_plan",
                    why=inputs.optimiser_problem or msg("common.unknown"),
                )
        floor_temp = to_float(settings.floor_temp)
        floor = bool(settings.floor_enabled) and floor_temp is not None
        # (reason, is a missing input) when VV-AI control must not drive.
        blocked: Optional[Tuple[Msg, bool]] = None
        # Away economy (or minimum that cannot hold 47134): the pump heats
        # hot water itself in economy mode, the floor via a one-time increase.
        pump_heats = False
        if inputs.away == AWAY_ECONOMY:
            pump_heats = True
            blocked = (msg("hw.away_economy_pump"), False)
        elif inputs.away == AWAY_MINIMUM and not on("vv_control") and not on("optimiser"):
            pump_heats = True
            blocked = (msg("hw.away_minimum_vv_off"), False)
        if blocked is not None:
            pass
        elif not on("vv_control") and not on("optimiser"):
            blocked = (OFF, False)
        elif not on("vv_control") and not optimised and not reduced:
            blocked = (msg("hw.no_opt_plan"), False)
        elif not reduced and (on("autoluxury") or on("priority")):
            blocked = (msg("hw.precedence"), False)
        elif bt7 is None:
            blocked = (msg("hw.no_bt7"), True)
        elif not stops.complete():
            blocked = (msg("hw.no_stops"), True)
        elif period is None and not held_period:
            blocked = (msg("hw.no_period"), True)
        elif period == 0 and not held_period:
            blocked = (msg("hw.period_zero"), False)
        elif reduced and not floor:
            blocked = (msg("hw.minimum_needs_floor"), False)
        elif not learned_plan and not floor:
            blocked = (msg("hw.nothing_learned"), False)
        if blocked is not None and reduced and not pump_heats:
            # Away minimum that may not hold 47134: as economy.
            pump_heats = True
            blocked = (msg("hw.minimum_fallback", reason=blocked[0]), blocked[1])
        if blocked is not None and pump_heats:
            reasons["vv_control"] = blocked[0]
            if floor and bt7 is not None:
                result = self.vv.decide(bt7, now, inputs.tz, [], stops, True, floor_temp, False)
                reasons["vv_control"] = msg(
                    "common.join",
                    first=blocked[0],
                    second=result.reason if result.heat else msg("hw.floor_status", bt7=bt7, floor=floor_temp),
                )
                if result.heat:
                    sources.append("away_floor")
            else:
                self.vv.reset()
        elif blocked is not None:
            self.vv.reset()
            reasons["vv_control"] = blocked[0]
            if optimised:
                # Its blocks are not written either: say so instead of
                # "drives the charges".
                reasons["optimiser"] = msg("hw.opt_blocked", reason=blocked[0])
            if blocked[1]:
                faults.append("vv_control")
        else:
            targets[REG_PERIOD] = 0
            result = self.vv.decide(
                bt7, now, inputs.tz, windows, stops, bool(settings.floor_enabled), floor_temp, use_price
            )
            reasons["vv_control"] = (
                msg("hw.minimum_floor_only", reason=result.reason) if reduced else result.reason
            )
            if result.heat:
                sources.append("vv_control")

        if sources:
            targets[REG_TEMP_LUX] = TEMP_LUX_START

        # Legionella cycle: first priority, Away included.
        reasons["legionella"] = legionella.reason
        if legionella.run and legionella.temperature is not None:
            targets[REG_LUX_STOP] = legionella.temperature
            targets[REG_TEMP_LUX] = TEMP_LUX_START
            targets[REG_PERIOD] = None
            sources.append("legionella")

        if targets[REG_LUX_STOP] is not None:
            state = "legionella"
        elif targets[REG_TEMP_LUX] is not None:
            state = "heat_now"
        elif targets[REG_PERIOD] is not None:
            state = "block"
        elif faults:
            state = "fault"
        elif targets[REG_COMFORT] is not None:
            state = COMFORT_NAMES[int(targets[REG_COMFORT])]
        else:
            state = "idle"

        details: Dict[str, Any] = {
            "bt6": bt6,
            "bt7": bt7,
            "comfort_mode": comfort_now,
            "hot_water_period": period,
            "temporary_lux": pump_lux,
            "stop_temperatures": {"economy": stops.economy, "normal": stops.normal, "luxury": stops.luxury},
            "heat_sources": sources,
            "faults": faults,
            "closed_until": self.vv.closed_until,
            "floor_active": self.vv.floor_active,
            "plan_source": "optimiser" if optimised else ("away" if reduced else "vv_ai"),
            # The optimiser's blocks are actually written (not blocked, e.g. by 47134 = 0).
            "plan_applied": bool(optimised and blocked is None),
            "away": inputs.away,
            "legionella": legionella.state,
        }
        return HotWaterDecision(
            state=state,
            targets=targets,
            reasons=reasons,
            inputs_ok=not faults,
            windows=windows,
            next_heat=next_heat_start(windows, now, self.vv.closed_until, use_price),
            details=details,
            legionella=legionella,
        )

    def _legionella(self, mode: str, inputs: HotWaterInputs, settings: HotWaterSettings) -> LegionellaStatus:
        """Watch BT7 for a legionella hold and plan or run a cycle."""
        timeout = settings.sensor_timeout

        def value(reg: int) -> Optional[float]:
            return self._value(inputs, reg, timeout)

        guard = self.legionella
        params = parameters(
            settings.legionella, value(REG_PERIODIC), value(REG_PERIODIC_INTERVAL), value(REG_PERIODIC_STOP)
        )
        bt7 = value(REG_BT7)
        temperature = guard.state.run_temperature or params.temperature
        guard.observe(inputs.now, bt7, temperature, settings.legionella.hold_minutes)
        lux = value(REG_LUX_STOP)
        if inputs.held.get(REG_LUX_STOP):
            lux = None  # ours: not the pump's own value
        return guard.step(
            inputs.now, inputs.tz, mode, settings.legionella, bt7, params, inputs.price_slots, lux
        )


class HotWaterLeases:
    """The four leases and when to set, renew or release them."""

    def __init__(self, owner: str = "ha", clock: Optional[Callable[[], datetime]] = None) -> None:
        self.leases: Dict[int, LeaseManager] = {
            reg: LeaseManager(
                reg, owner=owner, clock=clock, low=low, high=high, decimals=LEASE_DECIMALS.get(reg, 0)
            )
            for reg, (low, high) in LEASE_LIMITS.items()
        }

    def held(self) -> Dict[int, bool]:
        return {reg: lease.held for reg, lease in self.leases.items()}

    def decide(
        self,
        mode: str,
        decision: Optional[HotWaterDecision],
        healthy: bool,
        in_grace: bool = False,
    ) -> List[Tuple[LeaseManager, LeaseAction]]:
        if in_grace and mode == MODE_ACTIVE:
            return []
        actions: List[Tuple[LeaseManager, LeaseAction]] = []
        for reg, lease in self.leases.items():
            target = decision.targets.get(reg) if (decision is not None and healthy) else None
            if mode == MODE_ACTIVE and target is not None:
                action = lease.decide(MODE_ACTIVE, target, True)
            else:
                action = lease.decide(MODE_OFF, None, True)
            if action is not None:
                actions.append((lease, action))
        return actions

    def release_all(self) -> List[Tuple[LeaseManager, LeaseAction]]:
        actions = []
        for lease in self.leases.values():
            action = lease.release_action()
            if action is not None:
                actions.append((lease, action))
        return actions
