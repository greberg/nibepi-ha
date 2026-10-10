from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime

import context  # noqa: F401
from logic.vvimport import ProfileImportError, convert_nodered_store, read_json_file, resolve_in_dir
from logic.vvlearn import SLOTS, VvProfile
from logic.vvplan import PlanSettings, StopTemps, build_plan

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

STHLM = ZoneInfo("Europe/Stockholm")
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "vv_ai_profile.json")


class ConvertTest(unittest.TestCase):
    def setUp(self) -> None:
        with open(FIXTURE, encoding="utf-8") as handle:
            self.data = json.load(handle)

    def test_fixture_converts(self) -> None:
        profile, summary = convert_nodered_store(self.data, "nibepi/vv_ai_profile.json")
        self.assertEqual(profile.profile, [float(v) for v in self.data["profile"]])
        self.assertEqual(profile.profile[4 * 24 + 19], 3.126)
        self.assertEqual(profile.last_slot, 115)
        self.assertEqual(profile.last_drop, 9.7)
        self.assertEqual(profile.last_learn, "2026-10-09T17:12:00+00:00")
        self.assertEqual(profile.imported_from, "nibepi/vv_ai_profile.json")
        # Decay starts afresh: no catch-up for days nobody observed.
        self.assertIsNone(profile.seen_day)
        self.assertEqual(summary["learned_slots"], 20)
        self.assertEqual(summary["last_slot"], "Fri 19:00")
        self.assertEqual(summary["top_slots"][0], "Mon 19:00 4.3")
        # And it round-trips through the store format.
        self.assertEqual(VvProfile.from_dict(profile.to_dict()), profile)

    def test_imported_profile_plans(self) -> None:
        profile, _ = convert_nodered_store(self.data, "x")
        now = datetime(2026, 10, 12, 0, 30, tzinfo=STHLM)  # Monday
        windows = build_plan(profile.profile, now, STHLM, PlanSettings(), StopTemps(48.0, 52.0, 56.0))
        monday = [w for w in windows if w.day == now.date()]
        self.assertEqual([w.peak.astimezone(STHLM).hour for w in monday], [7, 19])
        self.assertEqual({w.day_mode for w in monday}, {2})

    def test_bad_values_become_zero(self) -> None:
        self.data["profile"][0] = None
        self.data["profile"][1] = -3
        self.data["profile"][2] = "2.5"
        profile, summary = convert_nodered_store(self.data, "x")
        self.assertEqual(profile.profile[:3], [0.0, 0.0, 2.5])
        self.assertEqual(summary["invalid_values"], 2)

    def test_rejected_formats(self) -> None:
        with self.assertRaises(ProfileImportError):
            convert_nodered_store([1, 2, 3], "x")
        with self.assertRaises(ProfileImportError):
            convert_nodered_store({"profile": [0] * 24}, "x")
        legacy = {"profile": [{"count": 3, "avg": 51.2}] * SLOTS}
        with self.assertRaisesRegex(ProfileImportError, "vvStore"):
            convert_nodered_store(legacy, "x")

    def test_minimal_file_without_meta(self) -> None:
        profile, summary = convert_nodered_store({"profile": [1] * SLOTS}, "x")
        self.assertIsNone(profile.last_learn)
        self.assertEqual(summary["total"], 168.0)


class SandboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp()
        self.config = os.path.join(self.root, "config")
        os.makedirs(os.path.join(self.config, "nibepi"))
        shutil.copy(FIXTURE, os.path.join(self.config, "nibepi", "vv_ai_profile.json"))
        with open(os.path.join(self.root, "secret.json"), "w") as handle:
            handle.write("{}")

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def test_relative_and_absolute_paths_inside(self) -> None:
        real, data = read_json_file(self.config, "nibepi/vv_ai_profile.json", 1_000_000)
        self.assertEqual(len(data["profile"]), SLOTS)
        absolute = os.path.join(self.config, "nibepi", "vv_ai_profile.json")
        self.assertEqual(read_json_file(self.config, absolute, 1_000_000)[0], real)

    def test_outside_is_refused(self) -> None:
        for path in ("../secret.json", os.path.join(self.root, "secret.json"), "/etc/passwd", "", "."):
            with self.assertRaises(ProfileImportError, msg=path):
                resolve_in_dir(self.config, path)

    def test_symlink_out_is_refused(self) -> None:
        link = os.path.join(self.config, "link.json")
        os.symlink(os.path.join(self.root, "secret.json"), link)
        with self.assertRaises(ProfileImportError):
            read_json_file(self.config, "link.json", 1_000_000)

    def test_missing_large_and_broken_files(self) -> None:
        with self.assertRaisesRegex(ProfileImportError, "does not exist"):
            read_json_file(self.config, "nibepi/none.json", 1_000_000)
        with self.assertRaisesRegex(ProfileImportError, "too large"):
            read_json_file(self.config, "nibepi/vv_ai_profile.json", 100)
        with open(os.path.join(self.config, "broken.json"), "w") as handle:
            handle.write("{nope")
        with self.assertRaisesRegex(ProfileImportError, "not readable JSON"):
            read_json_file(self.config, "broken.json", 1_000_000)


if __name__ == "__main__":
    unittest.main()
