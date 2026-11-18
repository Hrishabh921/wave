"""
tests/test_timers_and_stopwatch.py — Tier 5: standalone timers and a
stopwatch (brain/handlers/timers.py + their database/db_manager.py backing).

Four layers, narrowest first:
    DurationFormattingTests / TimerMessageHelperTests
        Pure functions, no DB, no brain: _format_duration/_is_timer/
        _timer_label against brain/handlers/timers.py directly.
    StopwatchDbLayerTests
        The Tier 5 additions to database/db_manager.py (the stopwatch
        table + its 4 methods, plus cancel_reminder_by_id) against an
        isolated temp SQLite file - same pattern
        tests/test_tier3_reminders.py's TestReminderDbLayer uses for its
        own temp DB.
    TimerSkillTests / StopwatchSkillTests
        The @skill(...)-decorated functions themselves, called directly
        (handler(None, args) - none of these take anything meaningful
        from `brain`) against that same isolated DB: the actual
        label-matching/disambiguation/validation logic lives here, one
        level up from the raw db.* methods.

None of this needs BrainBase's full fake-LLM-server harness (unlike
tests/test_phase4_brain.py) - nothing here is reached through
_dispatch_tool()'s gating/audit wrapper, since every timer/stopwatch tool
is T1 or T0 (ungated) per brain/tiers.py; that wrapper itself is already
covered generically by test_security_tiers.py and test_phase4_brain.py's
own audit-logging tests.

    python -m unittest discover -s tests -p "test_timers_and_stopwatch.py"
"""
import datetime
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S                                     # noqa: E402

# brain.handlers.timers is a submodule of brain.handlers, so importing it
# runs brain/handlers/__init__.py first, which imports every OTHER handler
# file too (automation, media, ...) - media.py reaches pyautogui/pycaw at
# import time, not available on a headless test box, same reason every
# other Phase 4+ test file stubs it before the first import under brain/.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from database.db_manager import db                               # noqa: E402
from brain.handlers.timers import (                              # noqa: E402
    _format_duration, _is_timer, _timer_label,
    _skill_set_timer, _skill_list_timers, _skill_check_timer, _skill_cancel_timer,
    _skill_start_stopwatch, _skill_stop_stopwatch, _skill_lap_stopwatch, _skill_stopwatch_status,
)


class DurationFormattingTests(unittest.TestCase):
    def test_zero_seconds(self):
        self.assertEqual(_format_duration(0), "0 seconds")

    def test_seconds_only(self):
        self.assertEqual(_format_duration(5), "5 seconds")
        self.assertEqual(_format_duration(1), "1 second")

    def test_minutes_and_seconds(self):
        self.assertEqual(_format_duration(65), "1 minute 5 seconds")
        self.assertEqual(_format_duration(125), "2 minutes 5 seconds")

    def test_exact_minutes_drops_the_zero_seconds(self):
        self.assertEqual(_format_duration(300), "5 minutes")

    def test_hours_drop_seconds_entirely_but_keep_minutes(self):
        """An hour-plus timer reporting '1 hour 0 minutes 45 seconds' would
        be needlessly precise for something that long - seconds are only
        shown when there are NO hours at all."""
        self.assertEqual(_format_duration(3600), "1 hour")
        self.assertEqual(_format_duration(3660), "1 hour 1 minute")
        self.assertEqual(_format_duration(3600 + 45), "1 hour")

    def test_rounds_to_the_nearest_second(self):
        self.assertEqual(_format_duration(59.6), "1 minute")
        self.assertEqual(_format_duration(0.4), "0 seconds")

    def test_negative_is_clamped_to_zero(self):
        self.assertEqual(_format_duration(-5), "0 seconds")


class TimerMessageHelperTests(unittest.TestCase):
    def test_is_timer_recognizes_the_prefix(self):
        self.assertTrue(_is_timer("Timer done"))
        self.assertTrue(_is_timer("Timer done: pasta"))
        self.assertFalse(_is_timer("call mom at 6pm"))
        self.assertFalse(_is_timer(""))
        self.assertFalse(_is_timer(None))

    def test_label_extraction(self):
        self.assertEqual(_timer_label("Timer done: pasta"), "pasta")
        self.assertEqual(_timer_label("Timer done:    laundry  "), "laundry")

    def test_bare_timer_with_no_label_falls_back_to_timer(self):
        self.assertEqual(_timer_label("Timer done"), "Timer")
        self.assertEqual(_timer_label("Timer done:"), "Timer")       # colon but nothing after it
        self.assertEqual(_timer_label("Timer done:   "), "Timer")    # colon but only whitespace after it


class StopwatchDbLayerTests(unittest.TestCase):
    def setUp(self):
        self.db = db
        old_path = db.db_path
        tmp = S.make_tmpdir(self)
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", old_path)

    def test_migration_is_idempotent(self):
        self.db.init_db()
        self.db.init_db()

    def test_never_started_status(self):
        self.assertEqual(self.db.stopwatch_status(), {"running": False, "elapsed_s": None, "laps": []})

    def test_start_then_stop_reports_a_sane_elapsed_time(self):
        self.assertTrue(self.db.start_stopwatch())
        status = self.db.stopwatch_status()
        self.assertTrue(status["running"])
        self.assertIsNotNone(status["elapsed_s"])
        self.assertGreaterEqual(status["elapsed_s"], 0)
        elapsed = self.db.stop_stopwatch()
        self.assertIsNotNone(elapsed)
        self.assertGreaterEqual(elapsed, 0)
        final = self.db.stopwatch_status()
        self.assertFalse(final["running"])
        self.assertAlmostEqual(final["elapsed_s"], elapsed, delta=0.5)

    def test_starting_twice_without_stopping_fails(self):
        self.assertTrue(self.db.start_stopwatch())
        self.assertFalse(self.db.start_stopwatch())

    def test_restarting_after_a_stop_clears_the_old_laps(self):
        self.db.start_stopwatch()
        self.db.lap_stopwatch()
        self.db.stop_stopwatch()
        self.assertTrue(self.db.start_stopwatch())          # stopped, so starting again is fine
        self.assertEqual(self.db.stopwatch_status()["laps"], [])

    def test_stopping_without_starting_returns_none(self):
        self.assertIsNone(self.db.stop_stopwatch())

    def test_lapping_without_starting_returns_none(self):
        self.assertIsNone(self.db.lap_stopwatch())

    def test_laps_accumulate_in_order(self):
        self.db.start_stopwatch()
        first = self.db.lap_stopwatch()
        second = self.db.lap_stopwatch()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertGreaterEqual(second, first)
        laps = self.db.stopwatch_status()["laps"]
        self.assertEqual(len(laps), 2)
        self.assertEqual(laps, [first, second])

    def test_status_does_not_itself_count_as_a_lap_or_stop(self):
        self.db.start_stopwatch()
        self.db.stopwatch_status()
        self.db.stopwatch_status()
        status = self.db.stopwatch_status()
        self.assertTrue(status["running"])
        self.assertEqual(status["laps"], [])

    def test_cancel_reminder_by_id_removes_only_that_one(self):
        now = datetime.datetime.now().isoformat()
        keep = self.db.save_reminder("keep me", now)
        gone = self.db.save_reminder("cancel me", now)
        self.assertTrue(self.db.cancel_reminder_by_id(gone))
        remaining = [r[0] for r in self.db.get_upcoming_reminders()]
        self.assertEqual(remaining, [keep])

    def test_cancel_reminder_by_id_on_an_unknown_id_returns_false(self):
        self.assertFalse(self.db.cancel_reminder_by_id(999999))


class TimerSkillTests(unittest.TestCase):
    def setUp(self):
        self.db = db
        old_path = db.db_path
        tmp = S.make_tmpdir(self)
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", old_path)

    def test_set_timer_reports_the_duration_and_label(self):
        out = _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        self.assertIn("5 minutes", out)
        self.assertIn("pasta", out)

    def test_set_timer_with_no_label_still_works(self):
        out = _skill_set_timer(None, {"duration_seconds": 60})
        self.assertIn("1 minute", out)

    def test_set_timer_rejects_zero_and_negative_durations(self):
        self.assertIn("longer than zero", _skill_set_timer(None, {"duration_seconds": 0}))
        self.assertIn("longer than zero", _skill_set_timer(None, {"duration_seconds": -5}))

    def test_set_timer_rejects_more_than_a_day(self):
        out = _skill_set_timer(None, {"duration_seconds": 25 * 3600})
        self.assertIn("set a reminder", out.lower())

    def test_set_timer_rejects_a_non_numeric_duration(self):
        out = _skill_set_timer(None, {"duration_seconds": "soon"})
        self.assertIn("valid number", out)

    def test_list_timers_when_none_are_running(self):
        self.assertIn("don't have any timers", _skill_list_timers(None, {}))

    def test_list_timers_shows_every_active_one(self):
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        _skill_set_timer(None, {"duration_seconds": 600, "label": "laundry"})
        out = _skill_list_timers(None, {})
        self.assertIn("pasta", out)
        self.assertIn("laundry", out)

    def test_list_timers_ignores_ordinary_reminders(self):
        self.db.save_reminder("call mom", (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat())
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        out = _skill_list_timers(None, {})
        self.assertIn("pasta", out)
        self.assertNotIn("call mom", out)

    def test_check_timer_with_only_one_running_needs_no_label(self):
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        out = _skill_check_timer(None, {})
        self.assertIn("pasta", out)
        self.assertIn("left", out)

    def test_check_timer_with_several_running_and_no_label_lists_them_all(self):
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        _skill_set_timer(None, {"duration_seconds": 600, "label": "laundry"})
        out = _skill_check_timer(None, {})
        self.assertIn("pasta", out)
        self.assertIn("laundry", out)

    def test_check_timer_matches_by_label(self):
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        _skill_set_timer(None, {"duration_seconds": 600, "label": "laundry"})
        out = _skill_check_timer(None, {"label": "pasta"})
        self.assertIn("pasta", out)
        self.assertNotIn("laundry", out)

    def test_check_timer_with_no_match_says_so(self):
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        out = _skill_check_timer(None, {"label": "bread"})
        self.assertIn("don't see a timer", out)

    def test_check_timer_when_none_are_running(self):
        self.assertIn("don't have any timers", _skill_check_timer(None, {}))

    def test_cancel_timer_by_label(self):
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        _skill_set_timer(None, {"duration_seconds": 600, "label": "laundry"})
        out = _skill_cancel_timer(None, {"label": "pasta"})
        self.assertIn("Cancelled 1 timer", out)
        remaining = _skill_list_timers(None, {})
        self.assertNotIn("pasta", remaining)
        self.assertIn("laundry", remaining)

    def test_cancel_timer_all(self):
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        _skill_set_timer(None, {"duration_seconds": 600, "label": "laundry"})
        out = _skill_cancel_timer(None, {})
        self.assertIn("Cancelled 2 timers", out)
        self.assertIn("don't have any timers", _skill_list_timers(None, {}))

    def test_cancel_timer_with_no_match_says_so(self):
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        out = _skill_cancel_timer(None, {"label": "bread"})
        self.assertIn("don't see a timer", out)

    def test_cancel_timer_when_none_are_running(self):
        self.assertIn("don't have any timers", _skill_cancel_timer(None, {}))

    def test_cancelling_a_timer_never_touches_an_ordinary_reminder_with_a_matching_word(self):
        """cancel_timer only ever iterates _active_timers() (already
        filtered to _is_timer() rows), so an ordinary reminder that happens
        to share a word with a timer's label must survive."""
        self.db.save_reminder("pasta recipe reminder", (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat())
        _skill_set_timer(None, {"duration_seconds": 300, "label": "pasta"})
        _skill_cancel_timer(None, {"label": "all"})
        remaining = [r[1] for r in self.db.get_upcoming_reminders()]
        self.assertEqual(remaining, ["pasta recipe reminder"])


class StopwatchSkillTests(unittest.TestCase):
    def setUp(self):
        self.db = db
        old_path = db.db_path
        tmp = S.make_tmpdir(self)
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", old_path)

    def test_full_start_lap_lap_stop_flow(self):
        self.assertIn("started", _skill_start_stopwatch(None, {}).lower())
        self.assertIn("already running", _skill_start_stopwatch(None, {}))
        self.assertIn("Lap 1", _skill_lap_stopwatch(None, {}))
        self.assertIn("Lap 2", _skill_lap_stopwatch(None, {}))
        self.assertIn("stopped", _skill_stop_stopwatch(None, {}).lower())

    def test_stop_without_starting(self):
        self.assertIn("isn't running", _skill_stop_stopwatch(None, {}))

    def test_lap_without_starting(self):
        self.assertIn("isn't running", _skill_lap_stopwatch(None, {}))

    def test_status_before_ever_starting(self):
        self.assertIn("hasn't been started", _skill_stopwatch_status(None, {}))

    def test_status_while_running_vs_after_stopping(self):
        _skill_start_stopwatch(None, {})
        running = _skill_stopwatch_status(None, {})
        self.assertIn("running", running)
        _skill_stop_stopwatch(None, {})
        stopped = _skill_stopwatch_status(None, {})
        self.assertIn("stopped", stopped)

    def test_status_mentions_lap_count_only_when_there_are_laps(self):
        _skill_start_stopwatch(None, {})
        no_laps = _skill_stopwatch_status(None, {})
        self.assertNotIn("lap", no_laps.lower())
        _skill_lap_stopwatch(None, {})
        with_laps = _skill_stopwatch_status(None, {})
        self.assertIn("1 lap recorded", with_laps)


if __name__ == "__main__":
    unittest.main()
