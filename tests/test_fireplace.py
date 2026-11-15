from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import context  # noqa: F401
from logic.fireplace import (
    AFTERGLOW,
    AFTERGLOW_MAX,
    BURNING,
    DETECTED,
    OFF,
    SWITCH,
    Fireplace,
    detect_spans,
    without_fires,
)

T0 = datetime(2026, 11, 15, 17, tzinfo=timezone.utc)
MIN = timedelta(minutes=1)


def feed(fire: Fireplace, start: datetime, values, step=5 * MIN, detect=True):
    """Readings every ``step`` from ``start``; returns the time after the last."""
    t = start
    for v in values:
        fire.update(t, {"s1": v}, detect=detect)
        t += step
    return t


class SwitchTest(unittest.TestCase):
    def test_switch_holds_the_temperature_from_before(self) -> None:
        fire = Fireplace()
        t = feed(fire, T0, [21.0, 21.0, 21.1])
        fire.turn_on(t)
        self.assertEqual((fire.phase, fire.source), (BURNING, SWITCH))
        self.assertEqual(fire.baseline, {"s1": 21.0})
        t = feed(fire, t, [21.4, 22.0, 22.6, 23.0])
        self.assertEqual(fire.effective("s1", 23.0), 21.0)
        # A room colder than before is seen as it is.
        self.assertEqual(fire.effective("s1", 20.6), 20.6)
        fire.turn_off(t)
        self.assertEqual(fire.phase, AFTERGLOW)
        t = feed(fire, t, [22.5, 21.8, 21.3])
        self.assertEqual(fire.phase, AFTERGLOW)
        feed(fire, t, [21.2])
        self.assertEqual(fire.phase, OFF)
        self.assertEqual(fire.effective("s1", 21.2), 21.2)
        self.assertEqual(len(fire.spans), 1)

    def test_auto_off_after_the_burn_hours(self) -> None:
        fire = Fireplace()
        feed(fire, T0, [21.0])
        fire.turn_on(T0, hours=4)
        fire.update(T0 + timedelta(hours=4), {"s1": 22.5}, detect=False)
        self.assertEqual(fire.phase, AFTERGLOW)
        # The afterglow ends at the latest after AFTERGLOW_MAX.
        fire.update(T0 + timedelta(hours=4) + AFTERGLOW_MAX, {"s1": 22.0}, detect=False)
        self.assertEqual(fire.phase, OFF)


class DetectTest(unittest.TestCase):
    def test_a_fast_rise_is_a_fire(self) -> None:
        fire = Fireplace()
        t = feed(fire, T0, [21.0, 21.0, 21.0, 21.2, 21.4])
        self.assertEqual(fire.phase, OFF)
        t = feed(fire, t, [21.6])
        self.assertEqual((fire.phase, fire.source), (BURNING, DETECTED))
        self.assertEqual(fire.baseline, {"s1": 21.0})
        # Burning while it rises, afterglow an hour after the peak.
        t = feed(fire, t, [22.0, 22.4, 22.6])
        t = feed(fire, t, [22.5] * 12)
        self.assertEqual(fire.phase, AFTERGLOW)
        # Another log: burning again.
        t = feed(fire, t, [22.5, 22.8, 23.1])
        self.assertEqual(fire.phase, BURNING)

    def test_heat_pump_speed_is_not(self) -> None:
        fire = Fireplace()
        feed(fire, T0, [21.0 + 0.02 * i for i in range(60)])  # 0.24 °C/h
        self.assertEqual(fire.phase, OFF)
        fire = Fireplace()
        feed(fire, T0, [21.0, 21.0, 21.3, 21.6], detect=False)
        self.assertEqual(fire.phase, OFF)

    def test_survives_a_restart(self) -> None:
        fire = Fireplace()
        feed(fire, T0, [21.0])
        fire.turn_on(T0 + MIN)
        again = Fireplace()
        again.load(fire.to_dict())
        self.assertEqual((again.phase, again.source, again.baseline), (BURNING, SWITCH, {"s1": 21.0}))
        self.assertEqual(again.burn_until, fire.burn_until)


class HistoryTest(unittest.TestCase):
    def test_fires_in_the_history_are_left_out(self) -> None:
        start = int(T0.timestamp())
        values = [21.0] * 12 + [21.4, 22.0, 22.5, 22.8, 22.7, 22.5] + [22.3 - 0.1 * i for i in range(12)] + [21.0] * 12
        rows = {start + i * 600: {"s1_t_in": v, "t_out": 5.0} for i, v in enumerate(values)}
        spans = detect_spans([(ts, r["s1_t_in"]) for ts, r in rows.items()])
        self.assertEqual(len(spans), 1)
        cleaned, found = without_fires(rows, ["s1"], [])
        dropped = [ts for ts, r in cleaned.items() if "s1_t_in" not in r]
        self.assertTrue(dropped)
        self.assertLessEqual(min(dropped), start + 12 * 600)
        # Outdoor stays; the quiet hours before keep their indoor value.
        self.assertTrue(all("t_out" in r for r in cleaned.values()))
        self.assertIn("s1_t_in", cleaned[start])
        self.assertIn("s1_t_in", cleaned[start + (len(values) - 1) * 600])
        # A switched span is left out too.
        span = (T0, T0 + timedelta(minutes=30))
        cleaned, _ = without_fires({start: {"s1_t_in": 21.0}}, ["s1"], [span])
        self.assertNotIn("s1_t_in", cleaned[start])


if __name__ == "__main__":
    unittest.main()
