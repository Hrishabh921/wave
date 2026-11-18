"""Tier 21 item: id-precise CRUD helpers added to database/db_manager.py
for the new DB-backed management pages (Reminders & Timers already had
cancel_reminder_by_id from Tier 5; this adds the equivalents for to-dos,
quick notes, and calendar events).

Each new method's whole reason to exist is explained in its own docstring
in db_manager.py: the existing text-substring methods (delete_todo,
cancel_calendar_event, ...) are the right tool for a voice command that
only has words to go on, but the wrong tool once a UI page already knows
the exact row id - a substring match could touch more rows than the one
the user actually clicked.
"""
import os
import unittest

import _phase4_support as S

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                       hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                       typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                       size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

from database.db_manager import db


class DbIsolationMixin:
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()

    def tearDown(self):
        db.db_path = self.old_path


class TodoByIdTests(DbIsolationMixin, unittest.TestCase):
    def test_delete_todo_by_id_removes_only_that_row(self):
        a = db.add_todo("buy milk")
        b = db.add_todo("buy milk and bread")  # shares a substring with `a`
        self.assertTrue(db.delete_todo_by_id(a))
        remaining = db.list_todos()
        self.assertEqual([r[0] for r in remaining], [b])

    def test_delete_todo_by_id_unknown_id_returns_false(self):
        self.assertFalse(db.delete_todo_by_id(99999))

    def test_complete_todo_by_id_marks_only_that_row_done(self):
        a = db.add_todo("call mom")
        b = db.add_todo("call mom again")
        self.assertTrue(db.complete_todo_by_id(a))
        pending = db.list_todos()
        self.assertEqual([r[0] for r in pending], [b])

    def test_complete_todo_by_id_already_completed_returns_false(self):
        a = db.add_todo("water the plants")
        db.complete_todo_by_id(a)
        self.assertFalse(db.complete_todo_by_id(a))  # already done - 0 rows affected


class QuickNoteByIdTests(DbIsolationMixin, unittest.TestCase):
    def test_delete_quick_note_by_id_removes_only_that_row(self):
        db.add_quick_note("first note")
        db.add_quick_note("second note")
        notes = db.list_quick_notes()
        first_id = notes[0][0]
        self.assertTrue(db.delete_quick_note_by_id(first_id))
        remaining = db.list_quick_notes()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0][1], "second note")

    def test_delete_quick_note_by_id_unknown_id_returns_false(self):
        self.assertFalse(db.delete_quick_note_by_id(99999))


class CalendarEventByIdTests(DbIsolationMixin, unittest.TestCase):
    def test_cancel_calendar_event_by_id_removes_only_that_row(self):
        a = db.save_calendar_event("Team standup", "2026-12-01T09:00:00")
        b = db.save_calendar_event("Team standup retro", "2026-12-01T10:00:00")
        self.assertTrue(db.cancel_calendar_event_by_id(a))
        remaining = db.get_calendar_events_between("2026-01-01T00:00:00", "2027-01-01T00:00:00")
        self.assertEqual([r[0] for r in remaining], [b])

    def test_cancel_calendar_event_by_id_unknown_id_returns_false(self):
        self.assertFalse(db.cancel_calendar_event_by_id(99999))

    def test_cancel_calendar_event_by_id_deletes_a_recurring_series_entirely(self):
        event_id = db.save_calendar_event("Daily standup", "2026-12-01T09:00:00", recurrence="daily")
        self.assertTrue(db.cancel_calendar_event_by_id(event_id))
        remaining = db.get_calendar_events_between("2026-12-01T00:00:00", "2026-12-10T00:00:00")
        self.assertEqual(remaining, [])


if __name__ == "__main__":
    unittest.main()
