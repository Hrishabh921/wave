"""
tests/test_tier16_notes_todos.py — Tier 16: quick notes (take_note/
read_notes/clear_notes) and to-dos (add_todo/list_todos/complete_todo/
delete_todo), at both the database/db_manager.py accessor layer and the
automation/quick_notes.py + automation/todo_manager.py manager layer, plus
the brain/handlers/productivity.py skill wrappers - including add_todo's
one piece of brain-dependent logic, linking an optional due date to a real
reminder via brain._set_reminder().

Same DB-isolation pattern test_tier3_reminders.py's TestReminderDbLayer
already uses: redirect db.db_path to a fresh temp file for the duration of
each test, rather than touching the real wave_memory.db.

Run: python -m unittest discover -s tests -p "test_tier16_notes_todos.py"
(pytest will also collect it.)
"""
import os
import unittest

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from database.db_manager import db                                 # noqa: E402
from automation.quick_notes import QuickNotesManager                # noqa: E402
from automation.todo_manager import TodoManager                     # noqa: E402
from brain.skills_registry import skills                            # noqa: E402
import brain.handlers  # noqa: E402,F401  (side effect: registers every built-in skill)


class _DbIsolatedTestBase(unittest.TestCase):
    """Redirects db.db_path to a fresh temp sqlite file per test, same
    pattern test_tier3_reminders.py's TestReminderDbLayer uses - every
    quick_notes/todos accessor goes through the same module-level `db`
    singleton, so this is the one thing that has to be isolated for these
    tests to be independent of each other and of the real app data."""
    def setUp(self):
        self.old_path = db.db_path
        tmp = S.make_tmpdir(self)
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", self.old_path)


# ---------------------------------------------------------------------------
# database/db_manager.py accessors
# ---------------------------------------------------------------------------
class QuickNoteDbLayerTests(_DbIsolatedTestBase):
    def test_add_and_list_round_trip_in_order(self):
        self.assertTrue(db.add_quick_note("buy milk"))
        self.assertTrue(db.add_quick_note("wifi password is hunter2"))
        rows = db.list_quick_notes()
        self.assertEqual([r[1] for r in rows], ["buy milk", "wifi password is hunter2"])

    def test_clear_returns_the_count_removed_and_empties_the_table(self):
        db.add_quick_note("one")
        db.add_quick_note("two")
        self.assertEqual(db.clear_quick_notes(), 2)
        self.assertEqual(db.list_quick_notes(), [])

    def test_clear_on_an_empty_table_returns_zero(self):
        self.assertEqual(db.clear_quick_notes(), 0)


class TodoDbLayerTests(_DbIsolatedTestBase):
    def test_add_returns_a_row_id_and_list_shows_it_pending(self):
        todo_id = db.add_todo("buy milk")
        self.assertIsNotNone(todo_id)
        rows = db.list_todos()
        self.assertEqual(len(rows), 1)
        _id, text, due_text, completed, _created = rows[0]
        self.assertEqual((text, due_text, completed), ("buy milk", None, 0))

    def test_due_text_is_stored_and_returned_as_plain_text(self):
        db.add_todo("call dentist", due_text="tomorrow at 5pm")
        rows = db.list_todos()
        self.assertEqual(rows[0][2], "tomorrow at 5pm")

    def test_list_todos_excludes_completed_by_default(self):
        db.add_todo("buy milk")
        db.add_todo("call dentist")
        db.complete_todo("buy milk")
        pending = db.list_todos()
        self.assertEqual([r[1] for r in pending], ["call dentist"])
        everything = db.list_todos(include_completed=True)
        self.assertEqual({r[1] for r in everything}, {"buy milk", "call dentist"})

    def test_complete_todo_matches_by_partial_text_and_returns_count_affected(self):
        db.add_todo("buy milk")
        self.assertEqual(db.complete_todo("milk"), 1)
        self.assertEqual(db.complete_todo("milk"), 0)  # already completed - no pending match left

    def test_delete_todo_removes_it_entirely_not_just_marks_it_done(self):
        db.add_todo("water the plants")
        self.assertEqual(db.delete_todo("plants"), 1)
        self.assertEqual(db.list_todos(include_completed=True), [])

    def test_delete_todo_with_no_match_returns_zero(self):
        self.assertEqual(db.delete_todo("nonexistent"), 0)


# ---------------------------------------------------------------------------
# automation/quick_notes.py - QuickNotesManager
# ---------------------------------------------------------------------------
class QuickNotesManagerTests(_DbIsolatedTestBase):
    def test_take_rejects_empty_content(self):
        self.assertIn("What would you like me to note down", QuickNotesManager.take("   "))
        self.assertEqual(db.list_quick_notes(), [])

    def test_take_then_read_all_reports_count_and_numbered_lines(self):
        QuickNotesManager.take("buy milk")
        QuickNotesManager.take("call mom")
        result = QuickNotesManager.read_all()
        self.assertIn("You have 2 note(s):", result)
        self.assertIn("1. buy milk", result)
        self.assertIn("2. call mom", result)

    def test_read_all_on_no_notes_says_so(self):
        self.assertEqual(QuickNotesManager.read_all(), "You don't have any notes yet.")

    def test_clear_singular_vs_plural_wording(self):
        QuickNotesManager.take("only one")
        self.assertEqual(QuickNotesManager.clear(), "Cleared 1 note.")
        QuickNotesManager.take("a")
        QuickNotesManager.take("b")
        self.assertEqual(QuickNotesManager.clear(), "Cleared 2 notes.")

    def test_clear_with_nothing_to_clear(self):
        self.assertEqual(QuickNotesManager.clear(), "You don't have any notes to clear.")


# ---------------------------------------------------------------------------
# automation/todo_manager.py - TodoManager
# ---------------------------------------------------------------------------
class TodoManagerTests(_DbIsolatedTestBase):
    def test_add_rejects_empty_text(self):
        self.assertIn("What should I add", TodoManager.add("  "))

    def test_add_then_list_all_formats_due_text_and_done_tag(self):
        TodoManager.add("buy milk")
        TodoManager.add("call dentist", due_text="Friday")
        listing = TodoManager.list_all()
        self.assertIn("1. buy milk", listing)
        self.assertIn("2. call dentist - due Friday", listing)
        self.assertNotIn("(done)", listing)

    def test_completed_item_shows_done_tag_and_hides_its_due_text(self):
        TodoManager.add("call dentist", due_text="Friday")
        TodoManager.complete("dentist")
        listing = TodoManager.list_all(include_completed=True)
        self.assertIn("call dentist (done)", listing)
        self.assertNotIn("due Friday", listing)  # todo_manager.py's own formatting hides due text once done

    def test_list_all_empty_pending_vs_empty_completed_wording_differs(self):
        self.assertEqual(TodoManager.list_all(), "Your to-do list is empty.")
        self.assertEqual(TodoManager.list_all(include_completed=True), "Nothing completed yet either.")

    def test_complete_with_no_match_says_so(self):
        self.assertIn("couldn't find a pending to-do", TodoManager.complete("nonexistent"))

    def test_delete_with_no_match_says_so(self):
        self.assertIn("couldn't find a to-do", TodoManager.delete("nonexistent"))


# ---------------------------------------------------------------------------
# brain/handlers/productivity.py skills - including add_todo's
# brain._set_reminder() linkage, the one skill here that needs more than
# its own manager.
# ---------------------------------------------------------------------------
class _FakeReminderBrain:
    """Stands in for WaveBrain - add_todo only ever calls one method on
    `brain`. Deliberately NOT the real WaveBrain._set_reminder: dateparser
    isn't installed in every environment this suite runs in (it's an
    optional dependency - see requirements.txt), and this lets the test
    pin add_todo's OWN branching logic (did the reminder "take"? what does
    it say either way?) without that depending on dateparser's install
    status or its actual NLP parsing of 'tomorrow at 5pm'."""
    def __init__(self, reminder_result):
        self.reminder_result = reminder_result
        self.calls = []

    def _set_reminder(self, message, when_text, recurrence=""):
        self.calls.append((message, when_text, recurrence))
        return self.reminder_result


class AddTodoSkillTests(_DbIsolatedTestBase):
    def _call(self, brain, **kwargs):
        return skills.get_handler("add_todo")(brain, kwargs)

    def test_no_due_text_never_touches_set_reminder(self):
        brain = _FakeReminderBrain("irrelevant")
        result = self._call(brain, text="buy milk")
        self.assertEqual(result, "Added 'buy milk' to your to-do list.")
        self.assertEqual(brain.calls, [])

    def test_due_text_that_parses_also_sets_a_reminder(self):
        brain = _FakeReminderBrain("Got it - I'll remind you to call dentist at 05:00 PM on November 06.")
        result = self._call(brain, text="call dentist", due_text="tomorrow at 5pm")
        self.assertIn("Added 'call dentist' to your to-do list.", result)
        self.assertIn("I'll also remind you then.", result)
        self.assertEqual(brain.calls, [("call dentist", "tomorrow at 5pm", "")])

    def test_due_text_that_fails_to_parse_still_saves_the_todo(self):
        brain = _FakeReminderBrain("I couldn't understand the time 'someday never' - could you rephrase it?")
        result = self._call(brain, text="water plants", due_text="someday never")
        self.assertIn("Added 'water plants' to your to-do list.", result)
        self.assertIn("Couldn't also set a reminder", result)
        self.assertIn("someday never", result)
        # the to-do itself is unaffected by the reminder failing
        self.assertIn("water plants", TodoManager.list_all())

    def test_add_failure_short_circuits_before_touching_set_reminder(self):
        brain = _FakeReminderBrain("Got it - whatever")
        result = self._call(brain, text="", due_text="tomorrow")
        self.assertNotIn("Added", result)
        self.assertEqual(brain.calls, [])


class OtherProductivitySkillWrappersTests(_DbIsolatedTestBase):
    """The rest of the notes/todo skills are thin pass-throughs to their
    manager - one call each is enough to pin that the wiring (argument
    names, which manager method gets called) is correct; the managers'
    own behavior is already covered by the TestCases above."""
    def _call(self, name, **kwargs):
        return skills.get_handler(name)(None, kwargs)

    def test_take_note(self):
        self.assertIn("Noted:", self._call("take_note", content="buy milk"))

    def test_read_notes(self):
        self._call("take_note", content="buy milk")
        self.assertIn("buy milk", self._call("read_notes"))

    def test_clear_notes(self):
        self._call("take_note", content="buy milk")
        self.assertIn("Cleared", self._call("clear_notes"))

    def test_list_todos_include_completed_flag_reaches_the_manager(self):
        self._call("add_todo", text="buy milk")
        skills.get_handler("complete_todo")(None, {"query": "milk"})
        self.assertEqual(self._call("list_todos"), "Your to-do list is empty.")
        self.assertIn("buy milk", self._call("list_todos", include_completed=True))

    def test_complete_todo(self):
        self._call("add_todo", text="buy milk")
        self.assertIn("Marked", self._call("complete_todo", query="milk"))

    def test_delete_todo(self):
        self._call("add_todo", text="buy milk")
        self.assertIn("Deleted", self._call("delete_todo", query="milk"))


if __name__ == "__main__":
    unittest.main()
