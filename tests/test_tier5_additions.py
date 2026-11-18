"""SARA-roadmap Tier 5 - the items still missing after the 50->62 tool
pass: standalone timers/stopwatch, and the composed skills joke/streak
(recent_actions and notes_qa turned out to already exist as
list_recent_actions/search_my_notes - see NEXT_STEPS.md).

    python -m unittest discover -s tests -p "test_tier5_additions.py"
"""
import datetime
import os
import shutil
import tempfile
import time
import unittest

import _phase4_support as S  # adds the project root to sys.path

# brain/handlers/__init__.py imports automation.app_control, which imports
# pyautogui - not installed in a headless/CI box. Same fake every other
# test file that touches brain/automation already installs.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from database.db_manager import db
from brain.handlers import reminders as reminders_handler
from brain.handlers import system as system_handler
from skills.joke import tell_joke, _JOKES
from skills import registry as standalone_skills


class JokeTests(unittest.TestCase):
    def test_tell_joke_returns_one_of_the_curated_jokes(self):
        self.assertIn(tell_joke(), _JOKES)

    def test_joke_skill_wrapper_delegates_to_the_standalone_skill(self):
        result = system_handler._skill_joke(None, {})
        self.assertIn(result, _JOKES)

    def test_registry_lists_joke(self):
        self.assertIn(tell_joke(), _JOKES)
        self.assertIsInstance(standalone_skills.run("joke"), str)


class DbBackedTestCase(unittest.TestCase):
    """Points the real db singleton at a fresh temp SQLite file per test -
    same pattern tests/test_memory_consolidation.py already established."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="wave_tier5_test_")
        self.addCleanup(shutil.rmtree, self._tmpdir, True)
        db.db_path = os.path.join(self._tmpdir, "t.db")
        db.init_db()
        db.history_enabled = True


class StreakTests(DbBackedTestCase):
    def test_no_streak_yet_reports_nothing_active(self):
        self.assertIn("don't have an active streak", system_handler._skill_streak(None, {}))

    def test_first_interaction_starts_a_1_day_streak(self):
        db.save_message("user", "hello")
        self.assertEqual(db.get_stat("streak_current"), "1")

    def test_same_day_repeat_messages_do_not_double_count(self):
        db.save_message("user", "hello")
        db.save_message("user", "hello again")
        db.save_message("assistant", "hi!")
        self.assertEqual(db.get_stat("streak_current"), "1")

    def test_consecutive_day_extends_the_streak(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        db.set_stat("streak_last_date", yesterday)
        db.set_stat("streak_current", "3")
        db.set_stat("streak_longest", "3")
        db.record_interaction_for_streak()
        self.assertEqual(db.get_stat("streak_current"), "4")
        self.assertEqual(db.get_stat("streak_longest"), "4")

    def test_a_gap_of_more_than_one_day_resets_to_1_not_0(self):
        three_days_ago = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        db.set_stat("streak_last_date", three_days_ago)
        db.set_stat("streak_current", "10")
        db.set_stat("streak_longest", "10")
        db.record_interaction_for_streak()
        self.assertEqual(db.get_stat("streak_current"), "1")
        self.assertEqual(db.get_stat("streak_longest"), "10")  # longest is a high-water mark, never lowered

    def test_history_disabled_means_no_streak_tracking(self):
        db.history_enabled = False
        db.save_message("user", "this should not start a streak")
        self.assertIsNone(db.get_stat("streak_current"))

    def test_streak_skill_reports_current_and_longest(self):
        db.set_stat("streak_current", "2")
        db.set_stat("streak_longest", "5")
        result = system_handler._skill_streak(None, {})
        self.assertIn("2-day streak", result)
        self.assertIn("5", result)

    def test_streak_skill_calls_it_the_longest_when_current_equals_longest(self):
        db.set_stat("streak_current", "5")
        db.set_stat("streak_longest", "5")
        result = system_handler._skill_streak(None, {})
        self.assertIn("longest", result.lower())


class TimerTests(DbBackedTestCase):
    def test_start_timer_requires_a_positive_duration(self):
        self.assertIn("positive", reminders_handler._skill_start_timer(None, {"duration_minutes": 0}))
        self.assertIn("positive", reminders_handler._skill_start_timer(None, {"duration_minutes": -5}))

    def test_start_timer_rejects_a_non_numeric_duration(self):
        self.assertIn("valid number", reminders_handler._skill_start_timer(None, {"duration_minutes": "soon"}))

    def test_start_then_list_timer_shows_it_with_its_label(self):
        reminders_handler._skill_start_timer(None, {"duration_minutes": 10, "label": "eggs"})
        result = reminders_handler._skill_list_timers(None, {})
        self.assertIn("eggs", result)

    def test_list_timers_empty_when_none_running(self):
        self.assertIn("No timers", reminders_handler._skill_list_timers(None, {}))

    def test_cancel_timer_by_label_removes_it(self):
        reminders_handler._skill_start_timer(None, {"duration_minutes": 10, "label": "eggs"})
        result = reminders_handler._skill_cancel_timer(None, {"label": "eggs"})
        self.assertIn("Cancelled 1", result)
        self.assertIn("No timers", reminders_handler._skill_list_timers(None, {}))

    def test_cancel_timer_all_removes_every_timer(self):
        reminders_handler._skill_start_timer(None, {"duration_minutes": 5, "label": "eggs"})
        reminders_handler._skill_start_timer(None, {"duration_minutes": 10, "label": "tea"})
        result = reminders_handler._skill_cancel_timer(None, {"label": "all"})
        self.assertIn("Cancelled 2", result)
        self.assertIn("No timers", reminders_handler._skill_list_timers(None, {}))

    def test_cancel_timer_never_touches_a_real_reminder(self):
        """The one regression this feature could plausibly cause: a timer
        labeled 'tea' and a REAL reminder that happens to mention 'tea'
        must not collide, since both live in the same reminders table."""
        db.save_reminder("remember to buy tea", (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat())
        reminders_handler._skill_start_timer(None, {"duration_minutes": 5, "label": "tea"})
        reminders_handler._skill_cancel_timer(None, {"label": "tea"})
        remaining = db.get_upcoming_reminders()
        self.assertEqual(len(remaining), 1)
        self.assertIn("buy tea", remaining[0][1])

    def test_cancel_timer_all_never_touches_a_real_reminder(self):
        db.save_reminder("remember to buy tea", (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat())
        reminders_handler._skill_start_timer(None, {"duration_minutes": 5, "label": "eggs"})
        reminders_handler._skill_cancel_timer(None, {"label": "all"})
        remaining = db.get_upcoming_reminders()
        self.assertEqual(len(remaining), 1)
        self.assertIn("buy tea", remaining[0][1])

    def test_cancel_timer_with_no_timers_running_says_so(self):
        self.assertIn("aren't any timers", reminders_handler._skill_cancel_timer(None, {"label": "eggs"}))

    def test_cancel_timer_no_match_says_so(self):
        reminders_handler._skill_start_timer(None, {"duration_minutes": 5, "label": "eggs"})
        self.assertIn("couldn't find", reminders_handler._skill_cancel_timer(None, {"label": "pasta"}))


class StopwatchTests(unittest.TestCase):
    def tearDown(self):
        with reminders_handler._stopwatches_lock:
            reminders_handler._stopwatches.clear()

    def test_start_then_stop_reports_elapsed_time(self):
        reminders_handler._skill_start_stopwatch(None, {"label": "workout"})
        time.sleep(0.05)
        result = reminders_handler._skill_stop_stopwatch(None, {"label": "workout"})
        self.assertIn("sec", result)

    def test_starting_the_same_label_twice_is_rejected(self):
        reminders_handler._skill_start_stopwatch(None, {"label": "workout"})
        result = reminders_handler._skill_start_stopwatch(None, {"label": "workout"})
        self.assertIn("already running", result)

    def test_stopping_an_unknown_label_says_so(self):
        result = reminders_handler._skill_stop_stopwatch(None, {"label": "nope"})
        self.assertIn("no stopwatch", result.lower())

    def test_default_label_used_when_none_given(self):
        reminders_handler._skill_start_stopwatch(None, {})
        result = reminders_handler._skill_stop_stopwatch(None, {})
        self.assertIn("sec", result)

    def test_two_different_labels_run_independently(self):
        reminders_handler._skill_start_stopwatch(None, {"label": "a"})
        reminders_handler._skill_start_stopwatch(None, {"label": "b"})
        self.assertIn("sec", reminders_handler._skill_stop_stopwatch(None, {"label": "a"}))
        self.assertIn("sec", reminders_handler._skill_stop_stopwatch(None, {"label": "b"}))


if __name__ == "__main__":
    unittest.main()
