"""Import the learned profile of NibePi's Node-RED VV-AI.

Where Node-RED kept it (config_node.js, ``VV_AI_STORE_FILE``):
``$NIBEPI_CONFIG_DIR/vv_ai_profile.json``, default
``/etc/nibepi/vv_ai_profile.json`` inside the container, which is the
mounted config folder on the Pi (e.g. ``~/nibepi/etc/nibepi/``). Older bare
installs kept it next to the package
(``~/.node-red/node_modules/node-red-contrib-nibepi/vv_ai_profile.json``).

Format (``saveVvAiStore``, ``JSON.stringify(persist, null, 2)``)::

    {"profile":  [168 numbers, °C, Monday 00:00 first, local time],
     "tempPlan": [168 numbers], "modePlan": [168 numbers],
     "meta": {"lastPlanBuild": ms, "lastLearnTs": ms, "lastDropDeg": °C,
              "lastIndex": 0..167, "vvAiSeenTodayKey": "YYYY-MM-DD",
              "vvAiSeenToday": [168 bools], "lastDecayKey": "YYYY-MM-DD",
              "lastDecayTs": ms}}

Only ``profile`` carries what was learned; its slots mean the same here
(``vvlearn.week_slot``), so it is copied as is (non-finite or negative
values become 0). ``tempPlan``/``modePlan`` are rebuilt from it every tick.
The decay bookkeeping is not taken over: the days between Node-RED's
retirement and the import were not observed, so the first tick starts a
new day without decaying them. ``lastLearnTs``/``lastDropDeg``/
``lastIndex`` are kept for the sensor.

Not importable: the ``nibe-hotwater-ai`` node's own ``vv_profile`` in the
Node-RED context store ``vvStore`` (168 ``{count, avg}`` BT7 averages, a
display-only legacy profile, not draws).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

from .inputs import to_float
from .vvlearn import SLOTS, VvProfile, clean_profile, slot_name, top_slots

NODERED_FILE = "vv_ai_profile.json"


class ProfileImportError(ValueError):
    """The file cannot be imported; the message says why."""


def resolve_in_dir(base: str, path: str) -> str:
    """``path`` (relative to ``base`` or absolute) as a real path inside
    ``base``; symlinks and ``..`` that lead outside are refused."""
    if not isinstance(path, str) or not path.strip() or "\0" in path:
        raise ProfileImportError("no file path given")
    base_real = os.path.realpath(base)
    candidate = path.strip()
    if not os.path.isabs(candidate):
        candidate = os.path.join(base_real, candidate)
    real = os.path.realpath(candidate)
    try:
        inside = os.path.commonpath([real, base_real]) == base_real
    except ValueError:  # different drives
        inside = False
    if not inside or real == base_real:
        raise ProfileImportError("%s is not a file inside the configuration directory" % path)
    return real


def read_json_file(base: str, path: str, max_bytes: int) -> Tuple[str, Any]:
    """Blocking: resolve, check and parse the file. Returns (real path, data)."""
    real = resolve_in_dir(base, path)
    if not os.path.isfile(real):
        raise ProfileImportError("%s does not exist or is not a file" % path)
    size = os.path.getsize(real)
    if size > max_bytes:
        raise ProfileImportError("%s is too large (%d bytes)" % (path, size))
    try:
        with open(real, encoding="utf-8") as handle:
            return real, json.load(handle)
    except (OSError, UnicodeDecodeError, ValueError) as err:
        raise ProfileImportError("%s is not readable JSON: %s" % (path, err)) from err


def _ms_to_iso(value: Any) -> Any:
    number = to_float(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000.0, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def convert_nodered_store(data: Any, source: str) -> Tuple[VvProfile, Dict[str, Any]]:
    """Node-RED's ``vv_ai_profile.json`` content -> a new profile + summary."""
    if not isinstance(data, dict):
        raise ProfileImportError("not a VV-AI profile: expected a JSON object")
    raw = data.get("profile")
    if isinstance(raw, list) and raw and all(isinstance(v, dict) for v in raw):
        raise ProfileImportError(
            "this is the hotwater-ai node's BT7 average profile (context store vvStore), "
            "not VV-AI's learned draws; import vv_ai_profile.json instead"
        )
    if not isinstance(raw, list) or len(raw) != SLOTS:
        raise ProfileImportError("not a VV-AI profile: 'profile' must be a list of %d numbers" % SLOTS)
    profile = clean_profile(raw)
    assert profile is not None
    invalid = sum(
        1 for value in raw if to_float(value) is None or (to_float(value) or 0.0) < 0
    )
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    result = VvProfile(profile=profile)
    result.last_learn = _ms_to_iso(meta.get("lastLearnTs"))
    drop = to_float(meta.get("lastDropDeg"))
    result.last_drop = round(drop, 2) if drop is not None else None
    index = to_float(meta.get("lastIndex"))
    result.last_slot = int(index) if index is not None and 0 <= index < SLOTS else None
    result.imported_from = source
    summary: Dict[str, Any] = {
        "source": source,
        "learned_slots": sum(1 for v in profile if v > 0),
        "total": round(sum(profile), 2),
        "invalid_values": invalid,
        "top_slots": top_slots(profile),
        "last_learned": result.last_learn,
        "last_slot": slot_name(result.last_slot) if result.last_slot is not None else None,
    }
    return result, summary
