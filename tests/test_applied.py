from __future__ import annotations

import unittest
from datetime import timedelta

import context  # noqa: F401
from context import utc
from logic.applied import HOT_WATER, AppliedLog

T0 = utc(2026, 10, 24, 10)
MIN = timedelta(minutes=1)


def ticks(log: AppliedLog, key: str, start, states) -> None:
    for i, applied in enumerate(states):
        log.mark(key, start + i * MIN, applied)


class AppliedLogTest(unittest.TestCase):
    def test_span_between_ticks_follows_the_earlier_tick(self) -> None:
        log = AppliedLog()
        # Applied for ticks 0..4, then the gate closes at tick 5.
        ticks(log, "s1", T0, [True] * 5 + [False] * 5)
        self.assertEqual(log.spans("s1", T0, T0 + 20 * MIN), [(T0, T0 + 5 * MIN)])
        self.assertEqual(log.seconds("s1", T0, T0 + 20 * MIN), 300.0)
        self.assertFalse(log.applied_now("s1"))

    def test_never_applied_has_no_spans(self) -> None:
        log = AppliedLog()
        ticks(log, "s1", T0, [False] * 30)
        self.assertEqual(log.spans("s1", T0, T0 + 60 * MIN), [])
        self.assertEqual(log.share(["s1"], T0, T0 + 30 * MIN), 0.0)
        self.assertIsNone(log.share([], T0, T0 + 30 * MIN))

    def test_a_long_gap_does_not_count(self) -> None:
        log = AppliedLog()
        log.mark("s1", T0, True)
        log.mark("s1", T0 + 30 * MIN, True)  # HA was down in between
        log.mark("s1", T0 + 31 * MIN, True)
        self.assertEqual(log.spans("s1", T0, T0 + 60 * MIN), [(T0 + 30 * MIN, T0 + 31 * MIN)])

    def test_clipping_share_and_prune(self) -> None:
        log = AppliedLog()
        ticks(log, "s1", T0, [True] * 11)
        ticks(log, HOT_WATER, T0, [True] * 5 + [False] * 6)
        self.assertEqual(log.spans("s1", T0 + 2 * MIN, T0 + 4 * MIN), [(T0 + 2 * MIN, T0 + 4 * MIN)])
        # The smallest share counts: hot water steered half of the 10 minutes.
        self.assertAlmostEqual(log.share(["s1", HOT_WATER], T0, T0 + 10 * MIN), 0.5)
        log.prune(T0 + 8 * MIN)
        self.assertEqual(log.spans("s1", T0, T0 + 20 * MIN), [(T0 + 8 * MIN, T0 + 10 * MIN)])
        self.assertEqual(log.spans(HOT_WATER, T0, T0 + 20 * MIN), [])


if __name__ == "__main__":
    unittest.main()
