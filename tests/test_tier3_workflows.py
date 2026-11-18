"""Tier 3 / item 9 - "workflows UI".

ui/workflows_panel.py itself needs real PyQt6 to instantiate (it's a
QWidget), so - same tradeoff test_tier3_reminders.py's own docstring
flags for ui/workers.py's ReminderWorker - it isn't driven directly here.
What IS fully covered, with no PyQt6 needed at all, is everything the panel
actually depends on: the three new database/db_manager.py methods it reads
and writes through (list_workflows_full, delete_workflow, rename_workflow),
plus a confirmation that save_workflow's existing replace-on-same-name
behavior (what the panel's "Save" button relies on for editing an existing
workflow's steps) still holds.

Run: python -m unittest discover -s tests -p "test_tier3_*.py"
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: F401  (only used here for make_tmpdir)


class TestWorkflowDbLayer(unittest.TestCase):

    def setUp(self):
        from database.db_manager import db
        self.db = db
        old_path = db.db_path
        tmp = S.make_tmpdir(self)
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", old_path)

    def test_list_workflows_full_returns_steps_with_names(self):
        self.db.save_workflow("Good Morning", ["open chrome", "play lofi beats"])
        self.db.save_workflow("Wind Down", ["lock pc"])
        rows = dict(self.db.list_workflows_full())
        self.assertEqual(rows["good morning"], ["open chrome", "play lofi beats"])
        self.assertEqual(rows["wind down"], ["lock pc"])

    def test_list_workflows_full_matches_list_workflows_names(self):
        self.db.save_workflow("A", ["x"])
        self.db.save_workflow("B", ["y"])
        self.assertEqual(sorted(name for name, _ in self.db.list_workflows_full()),
                          sorted(self.db.list_workflows()))

    def test_save_workflow_replaces_steps_on_same_name(self):
        """This is the behavior WorkflowsPanel._save_workflow relies on when
        editing an already-saved workflow's steps - no separate 'update'
        method was needed, save_workflow already replaces on conflict."""
        self.db.save_workflow("routine", ["step one"])
        self.db.save_workflow("routine", ["step one", "step two"])
        rows = dict(self.db.list_workflows_full())
        self.assertEqual(rows["routine"], ["step one", "step two"])
        self.assertEqual(len(self.db.list_workflows()), 1)  # not duplicated

    def test_delete_workflow(self):
        self.db.save_workflow("temp", ["a"])
        self.assertTrue(self.db.delete_workflow("temp"))
        self.assertEqual(self.db.list_workflows(), [])

    def test_delete_workflow_missing_name_returns_false(self):
        self.assertFalse(self.db.delete_workflow("does-not-exist"))

    def test_delete_workflow_is_case_insensitive(self):
        self.db.save_workflow("Routine", ["a"])
        self.assertTrue(self.db.delete_workflow("ROUTINE"))

    def test_rename_workflow(self):
        self.db.save_workflow("old-name", ["a", "b"])
        self.assertTrue(self.db.rename_workflow("old-name", "new-name"))
        self.assertEqual(self.db.list_workflows(), ["new-name"])
        self.assertEqual(self.db.get_workflow("new-name"), ["a", "b"])

    def test_rename_workflow_refuses_to_clobber_existing_name(self):
        self.db.save_workflow("one", ["a"])
        self.db.save_workflow("two", ["b"])
        self.assertFalse(self.db.rename_workflow("one", "two"))
        # Both should still exist, unchanged.
        self.assertEqual(sorted(self.db.list_workflows()), ["one", "two"])
        self.assertEqual(self.db.get_workflow("two"), ["b"])

    def test_rename_workflow_missing_name_returns_false(self):
        self.assertFalse(self.db.rename_workflow("ghost", "whatever"))

    def test_rename_workflow_same_name_is_a_noop_false(self):
        self.db.save_workflow("same", ["a"])
        self.assertFalse(self.db.rename_workflow("same", "same"))


if __name__ == "__main__":
    unittest.main()
