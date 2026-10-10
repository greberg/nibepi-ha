"""Put custom_components/nibepi on sys.path so ``logic`` imports without HA.

Importing ``custom_components.nibepi`` would run its ``__init__`` and need
Home Assistant; the ``logic`` package on its own does not.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "nibepi")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

UTC = timezone.utc


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class FakeClock:
    """Injected clock for the lease tests."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now
