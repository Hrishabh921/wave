"""Tier 8 - brain/handlers/fun.py, automation/streak_manager.py, and the
streaks table in database/db_manager.py.

StreakDbLayerTests    - the new CRUD methods against an isolated temp
                         SQLite file, same pattern test_phase6_calendar.py's
                         CalendarTestBase uses for calendar_events.
StreakManagerTests     - the actual day-boundary logic (same day/
                         consecutive day/missed day/best-streak
                         preservation) - this is where most of the
                         coverage belongs, same reasoning
                         test_phase8_intent_router.py gives for testing
                         brain/intent_router.py's route() directly rather
                         than only through a brain instance.
JokeTests              - get_joke's handler: always one of the curated
                         jokes, never empty, no network/DB dependency.
HandlerWiringTests     - the @skill-decorated functions in fun.py actually
                         delegate to StreakManager/the joke list, with the
                         args dict unpacked correctly (test_handlers_structure.py
                         already checks domain/naming/signature for every
                         built-in tool including these four - this class
                         is the behavioral check that file deliberately
                         doesn't do, same split as its own
                         SettingsPagesShortcutTests).

Run: python -m unittest discover -s tests -p "test_tier8_*.py"
(pytest will also collect it.)
"""
import os
import unittest
from datetime import datetime, timedelta

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

# brain.handlers.fun is reached (in JokeTests/HandlerWiringTests below) via
# brain.handlers' own __init__, which imports every OTHER handler file too -
# automation.py imports automation.app_control, which imports the real
# pyautogui unconditionally. Same fake-module bootstrap
# test_phase8_intent_router.py/test_handlers_structure.py/test_tier4_bench.py
# all use, installed before anything pulls that import chain in.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from database.db_manager import db                       # noqa: E402
from automation.streak_manager import StreakManager        # noqa: E402


class StreakTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db.db_path = self.old_path


class StreakDbLayerTests(StreakTestBase):
    def test_get_streak_is_none_before_any_checkin(self):
        self.assertIsNone(db.get_streak("workout"))

    def test_save_streak_inserts_then_updates_the_same_row(self):
        self.assertTrue(db.save_streak("workout", 1, 1, "2026-01-01"))
        row = db.get_streak("workout")
        self.assertEqual(row[1:], ("workout", 1, 1, "2026-01-01"))

        # Second save for the SAME habit must UPDATE, not insert a second row.
        self.assertTrue(db.save_streak("workout", 2, 2, "2026-01-02"))
        row2 = db.get_streak("workout")
        self.assertEqual(row2[1:], ("workout", 2, 2, "2026-01-02"))
        self.assertEqual(row[0], row2[0])  # same id, not a new row
        self.assertEqual(len(db.list_streaks()), 1)

    def test_list_streaks_orders_by_current_streak_descending(self):
        db.save_streak("reading", 2, 5, "2026-01-01")
        db.save_streak("workout", 9, 9, "2026-01-01")
        db.save_streak("meditation", 1, 1, "2026-01-01")
        habits_in_order = [row[1] for row in db.list_streaks()]
        self.assertEqual(habits_in_order, ["workout", "reading", "meditation"])

    def test_delete_streak_removes_it_and_reports_whether_one_existed(self):
        db.save_streak("workout", 3, 3, "2026-01-01")
        self.assertTrue(db.delete_streak("workout"))
        self.assertIsNone(db.get_streak("workout"))
        self.assertFalse(db.delete_streak("workout"))  # already gone


class StreakManagerTests(StreakTestBase):
    def test_first_checkin_ever_starts_at_day_one(self):
        result = StreakManager.check_in("workout")
        self.assertIn("day 1", result)
        row = db.get_streak("workout")
        self.assertEqual(row[2:4], (1, 1))  # current, best

    def test_checking_in_twice_the_same_day_does_not_double_count(self):
        StreakManager.check_in("workout")
        result = StreakManager.check_in("workout")
        self.assertIn("already checked in", result)
        row = db.get_streak("workout")
        self.assertEqual(row[2], 1)  # still 1, not 2

    def test_consecutive_day_checkin_extends_the_streak(self):
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        db.save_streak("workout", 4, 4, yesterday)
        result = StreakManager.check_in("workout")
        self.assertIn("extended to 5", result)
        row = db.get_streak("workout")
        self.assertEqual(row[2:4], (5, 5))  # current and best both climb

    def test_new_best_is_called_out_but_not_a_tied_non_record(self):
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        db.save_streak("workout", 4, 10, yesterday)  # current climbs to 5, best (10) unaffected
        result = StreakManager.check_in("workout")
        self.assertIn("extended to 5", result)
        self.assertNotIn("new best", result.lower())
        self.assertEqual(db.get_streak("workout")[3], 10)  # best untouched

    def test_missed_day_resets_current_but_keeps_the_best(self):
        three_days_ago = (datetime.now().date() - timedelta(days=3)).isoformat()
        db.save_streak("workout", 20, 20, three_days_ago)
        result = StreakManager.check_in("workout")
        self.assertIn("missed a day", result.lower())
        self.assertIn("restarts at 1", result)
        self.assertIn("20", result)  # still mentions the preserved best
        row = db.get_streak("workout")
        self.assertEqual(row[2:4], (1, 20))  # current reset, best preserved

    def test_blank_habit_is_rejected_without_touching_the_db(self):
        result = StreakManager.check_in("   ")
        self.assertIn("need to know", result.lower())
        self.assertEqual(db.list_streaks(), [])

    def test_habit_names_are_case_and_whitespace_normalized(self):
        StreakManager.check_in("  Workout ")
        self.assertIsNotNone(db.get_streak("workout"))
        # Checking in again under different casing hits the SAME row.
        StreakManager.check_in("WORKOUT")
        self.assertEqual(len(db.list_streaks()), 1)

    def test_get_status_for_a_habit_with_no_streak_yet(self):
        result = StreakManager.get_status("workout")
        self.assertIn("haven't started", result.lower())

    def test_get_status_for_a_specific_habit(self):
        StreakManager.check_in("workout")
        result = StreakManager.get_status("workout")
        self.assertIn("workout", result)
        self.assertIn("1", result)

    def test_get_status_with_no_habit_lists_everything(self):
        StreakManager.check_in("workout")
        StreakManager.check_in("reading")
        result = StreakManager.get_status("")
        self.assertIn("workout", result)
        self.assertIn("reading", result)
        self.assertIn("2 streak", result)

    def test_get_status_with_no_streaks_at_all(self):
        self.assertIn("haven't started any", StreakManager.get_status("").lower())

    def test_reset_deletes_an_existing_streak(self):
        StreakManager.check_in("workout")
        result = StreakManager.reset("workout")
        self.assertIn("reset", result.lower())
        self.assertIsNone(db.get_streak("workout"))

    def test_reset_a_habit_with_no_streak_says_so(self):
        result = StreakManager.reset("workout")
        self.assertIn("don't have", result.lower())

    def test_reset_blank_habit_is_rejected(self):
        self.assertIn("need to know", StreakManager.reset("").lower())

    def test_a_malformed_last_checkin_date_restarts_rather_than_crashing(self):
        """Defensive: get_streak()/save_streak() never write anything but
        a valid ISO date, but a hand-edited or corrupted DB row shouldn't
        be able to crash check_in() - it should just treat the habit as
        having no usable prior date and restart the run."""
        db.save_streak("workout", 7, 7, "not-a-real-date")
        result = StreakManager.check_in("workout")
        self.assertIn("restarts at 1", result)
        self.assertEqual(db.get_streak("workout")[2], 1)


class JokeTests(unittest.TestCase):
    def test_jokes_list_is_non_empty_and_every_entry_is_real_text(self):
        from brain.handlers.fun import _JOKES
        self.assertGreater(len(_JOKES), 10)
        for joke in _JOKES:
            self.assertIsInstance(joke, str)
            self.assertTrue(joke.strip())

    def test_get_joke_handler_returns_one_of_the_curated_jokes(self):
        from brain.handlers.fun import _skill_get_joke, _JOKES
        for _ in range(20):  # a few draws, since it's random
            result = _skill_get_joke(None, {})
            self.assertIn(result, _JOKES)


class HandlerWiringTests(StreakTestBase):
    """Confirms fun.py's @skill wrappers actually delegate to
    StreakManager with the args dict unpacked correctly - complements
    test_handlers_structure.py, which checks domain/naming/signature for
    these same four tools but never calls them."""

    def test_check_in_streak_handler_delegates_with_the_right_arg(self):
        from brain.handlers.fun import _skill_check_in_streak
        result = _skill_check_in_streak(None, {"habit": "workout"})
        self.assertIn("day 1", result)
        self.assertIsNotNone(db.get_streak("workout"))

    def test_get_streak_status_handler_delegates_and_defaults_to_everything(self):
        from brain.handlers.fun import _skill_get_streak_status
        db.save_streak("workout", 3, 3, datetime.now().date().isoformat())
        self.assertIn("workout", _skill_get_streak_status(None, {"habit": "workout"}))
        self.assertIn("workout", _skill_get_streak_status(None, {}))

    def test_reset_streak_handler_delegates(self):
        from brain.handlers.fun import _skill_reset_streak
        db.save_streak("workout", 3, 3, datetime.now().date().isoformat())
        result = _skill_reset_streak(None, {"habit": "workout"})
        self.assertIn("reset", result.lower())
        self.assertIsNone(db.get_streak("workout"))


if __name__ == "__main__":
    unittest.main()
