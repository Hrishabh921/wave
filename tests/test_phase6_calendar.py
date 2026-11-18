"""Phase 6 - automation/calendar_manager.py (Tier 3 gap #8).

No PyQt6/groq needed - this module is a plain backend, same shape as
file_manager.py. Run with:
    python -m unittest discover -s tests -p "test_phase6_*.py"
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

from database.db_manager import db                      # noqa: E402
import automation.calendar_manager as cal                # noqa: E402


class CalendarTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db.db_path = self.old_path


class ParsingFreeTests(CalendarTestBase):
    """Exercises create_event with dateparser's parse step mocked out, so
    these pass identically whether or not the real `dateparser` package is
    installed in the environment running them - only CalendarManager's OWN
    logic (duration math, storage, formatting) is under test here."""

    def test_create_event_computes_end_time_from_duration(self):
        start = datetime(2026, 10, 10, 15, 0, 0)
        with mock.patch.object(cal, "_parse_point", return_value=start):
            result = cal.CalendarManager.create_event("Team sync", "tomorrow 3pm", 45, "Zoom")
        self.assertIn("Team sync", result)
        self.assertIn("Zoom", result)

        rows = db.get_calendar_events_between("2026-10-10T00:00:00", "2026-10-11T00:00:00")
        self.assertEqual(len(rows), 1)
        _id, title, start_at, end_at, location, notes, recurrence = rows[0]
        self.assertEqual(title, "Team sync")
        self.assertEqual(datetime.fromisoformat(end_at) - datetime.fromisoformat(start_at), timedelta(minutes=45))
        self.assertEqual(location, "Zoom")

    def test_create_event_defaults_duration_to_60_minutes(self):
        start = datetime(2026, 11, 1, 9, 0, 0)
        with mock.patch.object(cal, "_parse_point", return_value=start):
            cal.CalendarManager.create_event("Dentist", "nov 1 9am", None, "")
        rows = db.get_calendar_events_between("2026-11-01T00:00:00", "2026-11-02T00:00:00")
        _id, title, start_at, end_at, location, notes, recurrence = rows[0]
        self.assertEqual(datetime.fromisoformat(end_at) - datetime.fromisoformat(start_at), timedelta(minutes=60))

    def test_create_event_rejects_blank_title(self):
        result = cal.CalendarManager.create_event("", "tomorrow 3pm", 30, "")
        self.assertIn("title", result.lower())
        self.assertEqual(db.get_calendar_events_between("2020-01-01", "2099-01-01"), [])

    def test_create_event_reports_unparseable_time_clearly(self):
        with mock.patch.object(cal, "_parse_point", return_value=None), \
             mock.patch.object(cal, "HAS_DATEPARSER", True):
            result = cal.CalendarManager.create_event("Standup", "sometime whenever", 30, "")
        self.assertNotIn("Added", result)
        self.assertIn("couldn't figure out", result.lower())

    def test_cancel_event_by_keyword(self):
        start = datetime(2026, 12, 1, 10, 0, 0)
        with mock.patch.object(cal, "_parse_point", return_value=start):
            cal.CalendarManager.create_event("Birthday party", "dec 1 10am", 60, "")
        result = cal.CalendarManager.cancel_event("birthday")
        self.assertIn("Cancelled 1", result)
        self.assertEqual(db.get_calendar_events_between("2026-12-01T00:00:00", "2026-12-02T00:00:00"), [])

    def test_cancel_event_no_match(self):
        result = cal.CalendarManager.cancel_event("nonexistent event xyz")
        self.assertIn("couldn't find", result.lower())


class RangeResolutionTests(CalendarTestBase):
    def test_list_events_defaults_to_today(self):
        now = datetime.now()
        db.save_calendar_event("Now-ish meeting", now.isoformat(), None, None, None)
        result = cal.CalendarManager.list_events("")
        self.assertIn("Now-ish meeting", result)

    def test_list_events_today_excludes_tomorrow(self):
        tomorrow = datetime.now() + timedelta(days=1, hours=1)
        db.save_calendar_event("Tomorrow's meeting", tomorrow.isoformat(), None, None, None)
        result = cal.CalendarManager.list_events("today")
        self.assertNotIn("Tomorrow's meeting", result)
        self.assertIn("No calendar events", result)

    def test_list_events_this_week_includes_a_few_days_out(self):
        in_three_days = datetime.now() + timedelta(days=3)
        db.save_calendar_event("Later this week", in_three_days.isoformat(), None, None, None)
        result = cal.CalendarManager.list_events("this week")
        self.assertIn("Later this week", result)

    def test_unparseable_range_falls_back_to_today_not_an_error(self):
        start, end = cal._resolve_range("completely nonsense phrase")
        today_start, today_end = cal._day_bounds(datetime.now())
        self.assertEqual((start, end), (today_start, today_end))


class RecurrenceTests(CalendarTestBase):
    """Tier 8 calendar audit: recurring events. get_calendar_events_between()
    expands them into virtual occurrences at QUERY TIME rather than ever
    rewriting the stored row (contrast with reminders.due_at, which
    complete_or_reschedule_reminder mutates forward each time a poll fires
    it) - see that method's docstring in database/db_manager.py."""

    def test_create_event_with_daily_recurrence_is_stored_and_confirmed(self):
        start = datetime(2026, 10, 10, 9, 0, 0)
        with mock.patch.object(cal, "_parse_point", return_value=start):
            result = cal.CalendarManager.create_event("Standup", "oct 10 9am", 30, "", "daily")
        self.assertIn("repeating daily", result)
        rows = db.get_calendar_events_between("2026-10-10T00:00:00", "2026-10-11T00:00:00")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][6], "daily")

    def test_create_event_with_weekly_recurrence_is_stored_and_confirmed(self):
        start = datetime(2026, 10, 10, 9, 0, 0)
        with mock.patch.object(cal, "_parse_point", return_value=start):
            result = cal.CalendarManager.create_event("1:1", "oct 10 9am", 30, "", "weekly")
        self.assertIn("repeating weekly", result)
        rows = db.get_calendar_events_between("2026-10-10T00:00:00", "2026-10-11T00:00:00")
        self.assertEqual(rows[0][6], "weekly")

    def test_invalid_recurrence_value_is_treated_as_one_off(self):
        start = datetime(2026, 10, 10, 9, 0, 0)
        with mock.patch.object(cal, "_parse_point", return_value=start):
            result = cal.CalendarManager.create_event("Standup", "oct 10 9am", 30, "", "monthly")
        self.assertNotIn("repeating", result)
        rows = db.get_calendar_events_between("2026-10-10T00:00:00", "2026-10-11T00:00:00")
        self.assertIsNone(rows[0][6])

    def test_daily_recurring_event_appears_today_with_no_poll_ever_advancing_it(self):
        """The stored row's start_at stays on its ORIGINAL date forever -
        nothing ever rewrites it, unlike a reminder - yet a window over
        TODAY still finds today's occurrence, purely from query-time
        expansion. Querying back at the original date still finds the
        unchanged original row, proving this really is expansion and not
        a disguised mutation."""
        origin = (datetime.now() - timedelta(days=10)).replace(hour=9, minute=0, second=0, microsecond=0)
        db.save_calendar_event("Standup", origin.isoformat(), None, None, None, "daily")

        origin_day_start, origin_day_end = cal._day_bounds(origin)
        rows_at_origin = db.get_calendar_events_between(origin_day_start.isoformat(), origin_day_end.isoformat())
        self.assertEqual(len(rows_at_origin), 1)
        self.assertEqual(datetime.fromisoformat(rows_at_origin[0][2]), origin)

        today_start, today_end = cal._day_bounds(datetime.now())
        rows_today = db.get_calendar_events_between(today_start.isoformat(), today_end.isoformat())
        self.assertEqual(len(rows_today), 1)
        _id, title, start_at, end_at, location, notes, recurrence = rows_today[0]
        self.assertEqual(title, "Standup")
        self.assertEqual(recurrence, "daily")
        occ = datetime.fromisoformat(start_at)
        self.assertEqual((occ.hour, occ.minute), (9, 0))
        self.assertEqual(occ.date(), datetime.now().date())

    def test_daily_recurring_event_appears_once_on_every_day_of_a_week_long_window(self):
        origin = (datetime.now() - timedelta(days=3)).replace(hour=9, minute=0, second=0, microsecond=0)
        db.save_calendar_event("Standup", origin.isoformat(), None, None, None, "daily")

        today_start, _ = cal._day_bounds(datetime.now())
        window_end = today_start + timedelta(days=7)
        rows = db.get_calendar_events_between(today_start.isoformat(), window_end.isoformat())

        self.assertEqual(len(rows), 7)
        days_seen = {datetime.fromisoformat(r[2]).date() for r in rows}
        self.assertEqual(len(days_seen), 7)  # one distinct occurrence per day, no duplicates

    def test_weekly_recurring_event_appears_once_in_a_week_long_window_on_the_right_day(self):
        origin = (datetime.now() - timedelta(weeks=3)).replace(hour=14, minute=30, second=0, microsecond=0)
        db.save_calendar_event("1:1", origin.isoformat(), None, None, None, "weekly")

        today_start, _ = cal._day_bounds(datetime.now())
        window_end = today_start + timedelta(days=7)
        rows = db.get_calendar_events_between(today_start.isoformat(), window_end.isoformat())

        self.assertEqual(len(rows), 1)
        occ = datetime.fromisoformat(rows[0][2])
        self.assertEqual((occ.hour, occ.minute), (14, 30))
        self.assertEqual(occ.weekday(), origin.weekday())

    def test_recurring_event_occurrences_preserve_the_original_duration(self):
        origin = (datetime.now() - timedelta(days=5)).replace(hour=9, minute=0, second=0, microsecond=0)
        end = origin + timedelta(minutes=45)
        db.save_calendar_event("Standup", origin.isoformat(), end.isoformat(), None, None, "daily")

        today_start, today_end = cal._day_bounds(datetime.now())
        rows = db.get_calendar_events_between(today_start.isoformat(), today_end.isoformat())

        self.assertEqual(len(rows), 1)
        occ_start = datetime.fromisoformat(rows[0][2])
        occ_end = datetime.fromisoformat(rows[0][3])
        self.assertEqual(occ_end - occ_start, timedelta(minutes=45))

    def test_non_recurring_past_event_is_never_resurrected_into_a_later_window(self):
        ten_days_ago = datetime.now() - timedelta(days=10)
        db.save_calendar_event("One-off thing", ten_days_ago.isoformat(), None, None, None, None)

        today_start, today_end = cal._day_bounds(datetime.now())
        rows = db.get_calendar_events_between(today_start.isoformat(), today_end.isoformat())
        self.assertEqual(rows, [])

    def test_cancelling_a_recurring_event_ends_the_whole_series(self):
        origin = (datetime.now() - timedelta(days=5)).replace(hour=9, minute=0, second=0, microsecond=0)
        db.save_calendar_event("Standup", origin.isoformat(), None, None, None, "daily")

        result = cal.CalendarManager.cancel_event("standup")
        self.assertIn("Cancelled 1", result)

        today_start, _ = cal._day_bounds(datetime.now())
        window_end = today_start + timedelta(days=7)
        rows = db.get_calendar_events_between(today_start.isoformat(), window_end.isoformat())
        self.assertEqual(rows, [])

    def test_list_events_spoken_summary_marks_a_recurring_event(self):
        now = datetime.now()
        db.save_calendar_event("Standup", now.isoformat(), None, None, None, "daily")
        result = cal.CalendarManager.list_events("today")
        self.assertIn("(repeats daily)", result)


class PromptBlockTests(CalendarTestBase):
    def test_get_events_for_prompt_empty_when_nothing_today(self):
        self.assertEqual(cal.CalendarManager.get_events_for_prompt(), "")

    def test_get_events_for_prompt_lists_todays_events(self):
        now = datetime.now()
        db.save_calendar_event("Standup", now.isoformat(), None, "Zoom", None)
        block = cal.CalendarManager.get_events_for_prompt()
        self.assertIn("Standup", block)
        self.assertIn("Zoom", block)


if __name__ == "__main__":
    unittest.main()
