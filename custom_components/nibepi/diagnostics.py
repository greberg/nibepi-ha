"""Config entry diagnostics: settings, inputs, leases, models and plans.

Download from the integration's page (menu -> Download diagnostics) to
see or send back what the integration based its decisions on. The Tibber
home and the location are redacted.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import NibePiConfigEntry
from .logic import optcost
from .logic.lease import LeaseManager
from .logic.optrun import models_summary
from .runtime import NibePiRuntime

TO_REDACT = {"tibber_home", "latitude", "longitude", "elevation_m"}
MAX_STEPS = 48


def _plain(value: Any) -> Any:
    """JSON-safe copy: datetimes as ISO text, dataclasses as dicts, the
    logic's texts (``Msg``) as plain English text."""
    if isinstance(value, str):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _plain(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None  # NaN / infinity are not JSON
    return value


def _lease(lease: LeaseManager) -> Dict[str, Any]:
    return {
        "register": lease.register,
        "held": lease.held,
        "value": lease.value,
        "last_sent": lease.last_sent,
        "retry_after": lease.retry_after,
        "remote": dict(lease.remote),
        "remote_at": lease.remote_at,
    }


def _systems(runtime: NibePiRuntime) -> Dict[str, Any]:
    optimizer = runtime.optimizer
    out: Dict[str, Any] = {}
    for system, sys in runtime.systems.items():
        out[system] = {
            "mode": sys.mode,
            "strategy": optimizer.strategy.get(system),
            "enabled": dict(sys.enabled),
            "plan": sys.plan,
            "rules_plan": sys.rules_plan,
            "choice": optimizer.choices.get(system),
            "gate": optimizer.gates.get(system),
            "decisions": {k: {"offset": d.offset, "reason": d.reason, "inputs_ok": d.inputs_ok}
                          for k, d in sys.decisions.items()},
            "base_adjust": runtime.base_adjust.get(system),
            "healthy": sys.healthy,
            "error": sys.error,
            "last_action": sys.last_action,
            "lease": _lease(sys.lease),
            "rmu": {"enabled": sys.rmu_enabled, "entity": sys.rmu_entity, "room": sys.rmu_room,
                    "lease": _lease(sys.rmu_lease)},
        }
    return out


def _hotwater(runtime: NibePiRuntime) -> Dict[str, Any]:
    hw = runtime.hotwater
    decision = hw.decision
    profile = hw.controller.profile
    return {
        "mode": hw.mode,
        "features": dict(hw.features),
        "healthy": hw.healthy,
        "error": hw.error,
        "decision": (
            {
                "state": decision.state,
                "targets": decision.targets,
                "reasons": decision.reasons,
                "inputs_ok": decision.inputs_ok,
                "next_heat": decision.next_heat,
                "details": decision.details,
            }
            if decision is not None
            else None
        ),
        "leases": {str(reg): _lease(lease) for reg, lease in hw.leases.leases.items()},
        "legionella": {
            "state": hw.controller.legionella.state.to_dict(),
            "status": (
                {"state": status.state, "run": status.run, "reason": status.reason, "info": status.info}
                if (status := hw.controller.legionella.status) is not None
                else None
            ),
        },
        "profile": {"learned": profile.learned, "imported_from": profile.imported_from,
                    "last_learn": profile.last_learn, "values": [round(v, 2) for v in profile.profile]},
    }


def _optimizer(runtime: NibePiRuntime) -> Dict[str, Any]:
    optimizer = runtime.optimizer
    log = optimizer.log
    span = log.span()
    heating: Dict[str, Any] = {}
    for system, result in optimizer.heating.items():
        entry: Dict[str, Any] = {
            "planned_at": result.planned_at,
            "base_offset": result.base_offset,
            "problem": result.problem,
            "summary": result.summary,
            "explanation": [b.text for b in result.blocks],
            "slab": {"state": result.slab, "temperature": result.slab_temp, "source": result.slab_source},
        }
        if result.plan is not None:
            plan = result.plan
            entry.update(
                {
                    "savings": plan.savings,
                    "cost": plan.plan.energy_adjusted,
                    "baseline_cost": plan.baseline.energy_adjusted,
                    "elapsed_s": plan.elapsed_s,
                    "steps": [
                        [s.start, s.offset, round(s.price, 3), round(s.t_out, 1), round(s.t_in_end, 2),
                         round(s.electric_kwh, 3)]
                        for s in plan.plan.steps[:MAX_STEPS]
                    ],
                }
            )
        heating[system] = entry
    tank = optimizer.tank
    return {
        "profiles": {system: optimizer.profile(system) for system in runtime.systems},
        "comfort": {system: optimizer.comfort(system) for system in runtime.systems},
        "fits": optimizer.fits,
        "fit_error": optimizer.fit_error,
        "models": {system: models_summary(m) for system, m in optimizer.models.items()},
        "heating": heating,
        "plan_error": optimizer.plan_error,
        "last_plan_at": optimizer.last_plan_at,
        "hot_water": (
            {
                "planned_at": tank.planned_at,
                "problem": tank.problem,
                "savings": tank.plan.savings if tank.plan else None,
                "min_temp": tank.plan.min_temp if tank.plan else None,
                "blocks": tank.plan.blocks if tank.plan else [],
            }
            if tank is not None
            else None
        ),
        "savings": optimizer.savings,
        "savings_today": optimizer.savings_today,
        "savings_if_applied": optimizer.savings_if_applied,
        "applied_now": {key: optimizer.applied.applied_now(key) for key in optimizer.savings_today},
        # Measured vs predicted heat pump electricity today (and yesterday).
        "hp_cost": (
            dict(optcost.summary(optimizer.hp_cost), yesterday=optimizer.hp_cost.get("yesterday"))
            if optimizer.hp_cost.get("day")
            else None
        ),
        "history": {
            "bins": len(log),
            "first": datetime.fromtimestamp(span[0], timezone.utc).isoformat() if span else None,
            "last": datetime.fromtimestamp(span[1], timezone.utc).isoformat() if span else None,
            "columns": {name: log.count(name) for name in log.columns()},
            "imported": optimizer.imported,
            "imported_at": optimizer.bootstrap_at,
            "import_report": optimizer.bootstrap_report,
            "gateway_discovery_id": optimizer.discovery_id,
        },
        "meter": {
            "entity": optimizer.meter_entity,
            "config": optimizer.meter_config,
            "problem": optimizer.meter_problem,
            "scope": optimizer.meter_scope,
        },
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NibePiConfigEntry
) -> Dict[str, Any]:
    runtime: NibePiRuntime = entry.runtime_data
    data = {
        "entry": {"title": entry.title, "data": dict(entry.data), "options": dict(entry.options)},
        "gateway": {
            "online": runtime.gateway_online,
            "status": runtime.status_payload,
            "state": runtime.gateway_state,
            "last_message_at": runtime.last_message_at,
        },
        "prices": {
            "status": runtime.price_status,
            "level": runtime.price_level,
            "slots": len(runtime.price_slots),
            "first": runtime.price_slots[0].start if runtime.price_slots else None,
            "last": runtime.price_slots[-1].end if runtime.price_slots else None,
        },
        "forecast": {"fetched": runtime.forecast_fetched, "points": len(runtime.forecast or [])},
        "readings": {str(reg): [r.value, r.ts] for reg, r in sorted(runtime.readings.items())},
        "away": {
            "state": runtime.away.to_dict(),
            "settings": runtime.away_settings(),
            "rules_away": runtime.away_rules(datetime.now(timezone.utc)),
        },
        "systems": _systems(runtime),
        "hot_water": _hotwater(runtime),
        "optimizer": _optimizer(runtime),
    }
    return async_redact_data(_plain(data), TO_REDACT)
