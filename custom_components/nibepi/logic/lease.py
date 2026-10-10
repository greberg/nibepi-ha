"""When to acquire, renew and release a register lease on the gateway.

New in the HA component, there is no config_node counterpart: config_node
wrote 47011/47010 directly. The MQTT contract is in docs/ha-migration.md of
the workspace:

- ``<prefix>lease/<reg>/set`` ``{"value": int, "ttl": 900, "owner": "ha"}``
  acquires or renews; the gateway only writes when the value changed.
- ``<prefix>lease/<reg>/release`` hands the register back at once.
- If renewals stop, the gateway restores the register when the TTL runs
  out. That is the safety net, so this module never "holds on": an
  unhealthy tick sends nothing.

Rules implemented by ``LeaseManager.decide``:

- Mode not ``active`` and a lease is held (by us, or by owner "ha" as seen
  in the gateway's lease state after a restart): release.
- Active but the tick loop is unhealthy: nothing, so the lease expires.
- Active and healthy: set when not held or the target differs from the
  leased value; otherwise renew every ``renew_interval`` (5 min, TTL 15 min,
  so two renewals can be lost before the gateway falls back).
- A gateway lease state saying "not active" while we think we hold it
  (gateway restart, expiry) makes the next tick re-send at once.
- After a gateway restart a previously active lease becomes a grace lease
  with owner ``"grace"``; it is treated as ours: renewed in active mode
  (the gateway keeps the original baseline), released otherwise.
- A rejected request comes back as the full state with ``error`` set (an
  active lease stays as it was). The rejected value is not retried at once:
  "Heatpump is not ready yet." is retried after one tick interval
  (``not_ready_retry``), other rejections after ``renew_interval``.

The clock is injected so tests can drive time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

DEFAULT_TTL = 900
DEFAULT_RENEW = 300
DEFAULT_NOT_READY_RETRY = 60
GRACE_OWNER = "grace"
NOT_READY = "Heatpump is not ready yet."
LEASE_MIN = -10
LEASE_MAX = 10

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ACTIVE = "active"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ACTIVE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LeaseAction:
    kind: str  # "set" or "release"
    value: Optional[int] = None
    renew: bool = False

    def payload(self, ttl: int, owner: str) -> Mapping[str, Any]:
        if self.kind == "set":
            return {"value": self.value, "ttl": ttl, "owner": owner}
        return {"owner": owner}


class LeaseManager:
    def __init__(
        self,
        register: int,
        owner: str = "ha",
        ttl: int = DEFAULT_TTL,
        renew_interval: int = DEFAULT_RENEW,
        clock: Optional[Callable[[], datetime]] = None,
        low: int = LEASE_MIN,
        high: int = LEASE_MAX,
        not_ready_retry: int = DEFAULT_NOT_READY_RETRY,
    ) -> None:
        self.register = register
        self.owner = owner
        self.ttl = ttl
        self.renew_interval = timedelta(seconds=renew_interval)
        self.clock = clock or _utcnow
        self.low = low
        self.high = high
        self.held = False
        self.value: Optional[int] = None
        self.last_sent: Optional[datetime] = None
        self.not_ready_retry = timedelta(seconds=not_ready_retry)
        self.remote: Mapping[str, Any] = {}
        self.remote_at: Optional[datetime] = None
        self.retry_after: Optional[datetime] = None

    # -- inputs ---------------------------------------------------------

    def note_remote_state(self, state: Mapping[str, Any]) -> None:
        """Feed ``<prefix>lease/<reg>/state`` JSON from the gateway."""
        self.remote = dict(state)
        self.remote_at = self.clock()
        active = state.get("active") is True
        owner = state.get("owner")
        error = state.get("error")
        value = state.get("value")
        remote_value = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if error:
            # Rejected request. Whatever the gateway holds is the truth.
            now = self.clock()
            wait = self.not_ready_retry if str(error).strip() == NOT_READY else self.renew_interval
            self.retry_after = now + wait
            if active and owner in (self.owner, GRACE_OWNER):
                self.held = True
                self.value = remote_value
            elif not active:
                self.held = False
                self.value = None
                self.last_sent = None
            return
        if active and owner in (self.owner, GRACE_OWNER):
            if not self.held or owner == GRACE_OWNER:
                # Ours from before a restart, or the gateway's grace lease.
                self.held = True
                self.value = remote_value
                self.last_sent = None
        elif not active and self.held:
            # Gateway dropped it (restart or expiry): re-send next tick.
            self.last_sent = None

    def is_grace(self) -> bool:
        return self.remote.get("active") is True and self.remote.get("owner") == GRACE_OWNER

    # -- decisions ------------------------------------------------------

    def clamp(self, value: int) -> int:
        return max(self.low, min(self.high, int(value)))

    def decide(self, mode: str, target: Optional[int], healthy: bool) -> Optional[LeaseAction]:
        if mode != MODE_ACTIVE:
            return LeaseAction("release") if self.held else None
        if not healthy or target is None:
            return None
        value = self.clamp(target)
        if self.retry_after is not None:
            if self.clock() < self.retry_after:
                return None
            self.retry_after = None
        if not self.held or self.value != value or self.last_sent is None:
            return LeaseAction("set", value, renew=self.held and self.value == value)
        if self.clock() - self.last_sent >= self.renew_interval:
            return LeaseAction("set", value, renew=True)
        return None

    def release_action(self) -> Optional[LeaseAction]:
        """On unload: release if anything may be held."""
        return LeaseAction("release") if self.held else None

    def record(self, action: LeaseAction) -> None:
        """Call after the action was published."""
        if action.kind == "set":
            self.held = True
            self.value = action.value
            self.last_sent = self.clock()
        else:
            self.held = False
            self.value = None
            self.last_sent = None

    # -- state for entities ---------------------------------------------

    def expires(self) -> Optional[datetime]:
        raw = self.remote.get("expires")
        if isinstance(raw, (int, float)) and raw > 0:
            return datetime.fromtimestamp(raw / 1000.0, timezone.utc)
        return None
