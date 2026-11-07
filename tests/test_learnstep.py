from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import context  # noqa: F401
from logic.learnstep import SLOT_HOURS, STEP, LearnSteps, learning_needed, slot_at
from logic.texts import code_of

LOWER, UPPER = 20.5, 22.0


def local(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute)


class LearningNeededTest(unittest.TestCase):
    def test_until_the_gate_level(self) -> None:
        self.assertTrue(learning_needed(False, "high", "high"))  # rejected fit: the prior
        self.assertTrue(learning_needed(True, "low", "high"))
        self.assertTrue(learning_needed(True, "medium", "high"))
        self.assertFalse(learning_needed(True, "high", "high"))
        self.assertFalse(learning_needed(True, "medium", "medium"))


class SlotTest(unittest.TestCase):
    def test_only_at_night(self) -> None:
        for hour in (5, 6, 12, 18, 22):
            self.assertIsNone(slot_at(local(7, hour, 30)), hour)
        for hour in (23, 0, 1, 2, 3, 4):
            self.assertIsNotNone(slot_at(local(8 if hour < 12 else 7, hour, 30)), hour)

    def test_pair_of_opposite_steps_spanning_midnight(self) -> None:
        first = slot_at(local(7, 23, 10))
        later = slot_at(local(8, 1, 59))  # same slot after midnight
        second = slot_at(local(8, 2, 0))
        self.assertEqual(first, later)
        self.assertEqual(first[0], local(7, 23))
        self.assertEqual(second[0], local(8, 2))
        self.assertEqual(abs(first[1]), STEP)
        self.assertEqual(second[1], -first[1])

    def test_order_alternates_night_by_night(self) -> None:
        tonight = slot_at(local(7, 23))[1]
        tomorrow = slot_at(local(8, 23))[1]
        self.assertEqual(tomorrow, -tonight)


class LearnStepsTest(unittest.TestCase):
    def _warm_first_night(self) -> int:
        return next(d for d in range(1, 10) if slot_at(local(d, 23))[1] > 0)

    def test_step_with_reason(self) -> None:
        day = self._warm_first_night()
        step = LearnSteps().at("s1", local(day, 23, 5), 21.2, LOWER, UPPER)
        self.assertEqual(step.offset, STEP)
        self.assertEqual(code_of(step.reason), "learn.step")
        self.assertIsNone(LearnSteps().at("s1", local(day, 12), 21.2, LOWER, UPPER))

    def test_skips_a_warming_step_near_the_upper_edge(self) -> None:
        day = self._warm_first_night()
        step = LearnSteps().at("s1", local(day, 23, 5), 21.8, LOWER, UPPER)
        self.assertEqual(step.offset, 0.0)
        self.assertEqual(code_of(step.reason), "learn.skip_warm")

    def test_skips_a_cooling_step_near_the_lower_edge_and_stays_skipped(self) -> None:
        day = self._warm_first_night()
        steps = LearnSteps()
        cool = local(day, 23) + timedelta(hours=SLOT_HOURS, minutes=5)
        self.assertEqual(steps.at("s1", cool, 20.7, LOWER, UPPER).offset, 0.0)
        # Warmer a moment later: the slot stays skipped (no flapping).
        again = steps.at("s1", cool + timedelta(minutes=30), 21.5, LOWER, UPPER)
        self.assertEqual(again.offset, 0.0)
        self.assertEqual(code_of(again.reason), "learn.skip_cold")
        # Another system is not affected.
        self.assertEqual(steps.at("s2", cool, 21.5, LOWER, UPPER).offset, -STEP)
        # The next night starts fresh.
        night = steps.at("s1", local(day + 1, 23, 5), 21.5, LOWER, UPPER)
        self.assertEqual(night.offset, -STEP)

    def test_no_indoor_no_step(self) -> None:
        step = LearnSteps().at("s1", local(7, 23, 5), None, LOWER, UPPER)
        self.assertEqual(step.offset, 0.0)
        self.assertEqual(code_of(step.reason), "learn.no_indoor")


if __name__ == "__main__":
    unittest.main()
