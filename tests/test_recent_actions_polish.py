"""
tests/test_recent_actions_polish.py — Tier 5: pure unit tests for the three
helper functions behind list_recent_actions' polish (brain/handlers/
automation.py's _relative_time/_parses_to_today/_group_consecutive).

These are deliberately kept separate from tests/test_phase4_brain.py's
through-the-brain coverage of list_recent_actions itself: those tests
exercise the real dispatch path end to end (gating, audit rows, the tool's
own text), while this file hand-builds ActionRecord objects to pin each
helper's exact behavior - including edge cases (a bad timestamp, an empty
list, a genuine tie) that would be awkward or slow to provoke by actually
running real tool calls through a real WaveBrain.

    python -m unittest discover -s tests -p "test_recent_actions_polish.py"
"""
import datetime
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S                                     # noqa: E402

# brain.handlers.automation imports automation.app_control, which (via
# AppController's platform backends) ultimately reaches pyautogui - not
# available on a headless test box, same reason every other Phase 4+ test
# file stubs it before the first import of anything under brain/ or
# automation/.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation.audit import ActionRecord                        # noqa: E402
from brain.handlers.automation import (                          # noqa: E402
    _group_consecutive, _parses_to_today, _relative_time,
)


def _record(tool="volume_up", status="ok", ts=None, args=None):
    return ActionRecord(id=1, ts=ts or datetime.datetime.now().isoformat(), tool=tool, args=args or {},
                        status=status)


def _frozen_now(fixed_now):
    """A mock.patch target for brain.handlers.automation's `datetime.datetime`
    that freezes .now() at `fixed_now` while leaving .fromisoformat and
    everything else a real datetime subclass can do untouched. Tests that
    build `ts` relative to the REAL wall clock (datetime.datetime.now() at
    whatever moment the test happens to run) are flaky right around
    midnight - e.g. "2 hours ago" computed at 00:55 lands on the PREVIOUS
    calendar day, which _relative_time (deliberately - see its own
    docstring) treats as "yesterday", not "N hours ago". Freezing `now` at
    a safe, fixed, mid-afternoon moment removes that real-clock dependency
    entirely, the same way test_older_than_yesterday_gets_month_and_day
    already avoids it by using a fully fixed datetime instead of anything
    relative to datetime.datetime.now()."""
    class _Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now
    return mock.patch("brain.handlers.automation.datetime.datetime", _Frozen)


class RelativeTimeTests(unittest.TestCase):
    def test_just_now_for_anything_under_45_seconds(self):
        ts = (datetime.datetime.now() - datetime.timedelta(seconds=10)).isoformat()
        self.assertEqual(_relative_time(ts), "just now")

    def test_minutes_ago_singular_and_plural(self):
        one = (datetime.datetime.now() - datetime.timedelta(minutes=1, seconds=5)).isoformat()
        five = (datetime.datetime.now() - datetime.timedelta(minutes=5)).isoformat()
        self.assertEqual(_relative_time(one), "1 minute ago")
        self.assertEqual(_relative_time(five), "5 minutes ago")

    def test_hours_ago_stays_within_the_same_day(self):
        fixed_now = datetime.datetime(2026, 6, 15, 12, 0, 0)
        ts = (fixed_now - datetime.timedelta(hours=2)).isoformat()
        with _frozen_now(fixed_now):
            self.assertEqual(_relative_time(ts), "2 hours ago")

    def test_yesterday_gets_a_clock_time_not_just_a_day_count(self):
        fixed_now = datetime.datetime(2026, 6, 15, 12, 0, 0)
        ts = (fixed_now - datetime.timedelta(days=1)).replace(
            hour=15, minute=15, second=0, microsecond=0).isoformat()
        with _frozen_now(fixed_now):
            self.assertEqual(_relative_time(ts), "yesterday at 3:15 PM")

    def test_older_than_yesterday_gets_month_and_day(self):
        old = datetime.datetime(2026, 3, 4, 9, 2)
        self.assertEqual(_relative_time(old.isoformat()), "on Mar 4 at 9:02 AM")

    def test_bad_or_missing_timestamp_degrades_gracefully(self):
        self.assertEqual(_relative_time("not a timestamp"), "not a timestamp")
        self.assertEqual(_relative_time(""), "sometime")
        self.assertEqual(_relative_time(None), "sometime")

    def test_a_timestamp_a_few_seconds_in_the_future_is_still_just_now(self):
        """Clock skew between when a row was written and when it's read
        back a moment later should never produce a negative/garbled
        duration - the seconds-elapsed clamp at zero is what this pins."""
        ts = (datetime.datetime.now() + datetime.timedelta(seconds=2)).isoformat()
        self.assertEqual(_relative_time(ts), "just now")


class ParsesToTodayTests(unittest.TestCase):
    def test_true_for_a_timestamp_from_today(self):
        today = datetime.date(2026, 6, 15)
        ts = datetime.datetime(2026, 6, 15, 8, 0).isoformat()
        self.assertTrue(_parses_to_today(ts, today))

    def test_false_for_yesterday_and_tomorrow(self):
        today = datetime.date(2026, 6, 15)
        self.assertFalse(_parses_to_today(datetime.datetime(2026, 6, 14, 23, 59).isoformat(), today))
        self.assertFalse(_parses_to_today(datetime.datetime(2026, 6, 16, 0, 0).isoformat(), today))

    def test_bad_or_missing_timestamp_is_false_not_a_crash(self):
        today = datetime.date(2026, 6, 15)
        self.assertFalse(_parses_to_today("garbage", today))
        self.assertFalse(_parses_to_today(None, today))
        self.assertFalse(_parses_to_today("", today))


class GroupConsecutiveTests(unittest.TestCase):
    def test_empty_list(self):
        self.assertEqual(_group_consecutive([]), [])

    def test_single_record_is_its_own_group_of_one(self):
        r = _record()
        self.assertEqual(_group_consecutive([r]), [(r, 1)])

    def test_three_identical_in_a_row_become_one_group_of_three(self):
        rows = [_record(tool="volume_up") for _ in range(3)]
        grouped = _group_consecutive(rows)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0][1], 3)
        self.assertIs(grouped[0][0], rows[0])            # keeps the FIRST record of the run (its timestamp is used)

    def test_a_different_tool_in_the_middle_breaks_the_run(self):
        rows = [_record(tool="volume_up"), _record(tool="volume_down"), _record(tool="volume_up")]
        grouped = _group_consecutive(rows)
        self.assertEqual([n for _r, n in grouped], [1, 1, 1])

    def test_a_different_status_also_breaks_the_run_even_with_the_same_tool(self):
        rows = [_record(tool="delete_file", status="ok"), _record(tool="delete_file", status="failed")]
        grouped = _group_consecutive(rows)
        self.assertEqual([n for _r, n in grouped], [1, 1])

    def test_a_different_summary_breaks_the_run_even_with_the_same_tool_and_status(self):
        """Two open_app calls for DIFFERENT apps are both 'ok' and both
        open_app, but summarize() includes the app name - they must not be
        collapsed into a fake 'Opened chrome (x2)'."""
        rows = [_record(tool="open_app", args={"app_name": "chrome"}),
                _record(tool="open_app", args={"app_name": "spotify"})]
        grouped = _group_consecutive(rows)
        self.assertEqual([n for _r, n in grouped], [1, 1])

    def test_identical_runs_separated_by_something_else_are_two_separate_groups(self):
        rows = [_record(tool="volume_up"), _record(tool="volume_up"),
                _record(tool="volume_down"),
                _record(tool="volume_up"), _record(tool="volume_up")]
        grouped = _group_consecutive(rows)
        self.assertEqual([n for _r, n in grouped], [2, 1, 2])


if __name__ == "__main__":
    unittest.main()
