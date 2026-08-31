"""Both books read one session clock, and it follows DST.

Two hardcoded UTC tables used to disagree about when "NY" is, while
ict/model.py anchored its daily/weekly structure to New York local time.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from fabio.model import _session as fabio_session
from ict.model import session_score
from ict.sessions import FABIO_SESSIONS, ICT_KILLZONES, NY, current


def _utc(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


class WinterMatchesTheOldTables(unittest.TestCase):
    """Standard time is unchanged on purpose; only the summer half was wrong."""

    def test_ict_ny_killzone(self) -> None:
        self.assertEqual(session_score(_utc(1, 14, 13))[1], "ny")
        self.assertEqual(session_score(_utc(1, 14, 15, 59))[1], "ny")
        self.assertEqual(session_score(_utc(1, 14, 16))[1], "dead")

    def test_ict_dead_zone_is_a_hard_veto_score(self) -> None:
        score, name = session_score(_utc(1, 14, 22))
        self.assertEqual(name, "dead")
        self.assertLess(score, 3)  # config session_min_score

    def test_asia_window(self) -> None:
        # London opens at 07:00 and is checked first, exactly as the old table did.
        self.assertEqual(session_score(_utc(1, 14, 0))[1], "asia")
        self.assertEqual(session_score(_utc(1, 14, 6, 59))[1], "asia")
        self.assertEqual(session_score(_utc(1, 14, 7))[1], "london")

    def test_fabio_runs_the_full_cash_session(self) -> None:
        self.assertEqual(fabio_session(_utc(1, 14, 20, 30)).name, "ny")
        self.assertEqual(fabio_session(_utc(1, 14, 21)).name, "dead")


class SummerFollowsNewYork(unittest.TestCase):
    def test_ict_killzone_shifts_with_dst(self) -> None:
        # 08:00-11:00 New York, which is 12:00-15:00 UTC while EDT is in force.
        self.assertEqual(session_score(_utc(7, 15, 12))[1], "ny")
        self.assertEqual(session_score(_utc(7, 15, 14, 59))[1], "ny")
        self.assertEqual(session_score(_utc(7, 15, 15))[1], "dead")

    def test_window_bounds_are_new_york_local(self) -> None:
        session = current(_utc(7, 15, 13), ICT_KILLZONES)
        self.assertEqual(session.start.astimezone(NY).hour, 8)
        self.assertEqual(session.end.astimezone(NY).hour, 11)


class OneClockTwoWindows(unittest.TestCase):
    def test_both_books_use_the_shared_module(self) -> None:
        moment = _utc(7, 15, 18)
        self.assertEqual(fabio_session(moment).name, current(moment, FABIO_SESSIONS).name)
        self.assertEqual(session_score(moment)[1], current(moment, ICT_KILLZONES).name)

    def test_the_windows_differ_on_purpose(self) -> None:
        # ICT trades the AM killzone; Fabio needs the whole session profile.
        # Same instant, different windows — declared in one file, not two.
        afternoon = _utc(1, 14, 19)
        self.assertEqual(session_score(afternoon)[1], "dead")
        self.assertEqual(fabio_session(afternoon).name, "ny")


if __name__ == "__main__":
    unittest.main()
