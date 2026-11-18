"""Phase 4 / idea 5 - action audit + undo (automation/audit.py, automation/undo.py,
automation/file_manager.py). Real files in a temp folder, real SQLite in a temp file.
"""
import os
import threading
import unittest

import _phase4_support as S  # noqa: F401

from automation import audit as audit_mod
from automation import undo as undo_mod
from automation.audit import Audit, DbStore, MemoryStore, infer_status, summarize
from automation.file_manager import FileManager
from automation.undo import TrashStore, UndoManager
from database.db_manager import DatabaseManager


def read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def write(p, text="x"):
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


class Base(unittest.TestCase):
    """Fresh audit + trash + undo manager swapped into the modules FileManager reads."""

    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.work = os.path.join(self.tmp, "desk")
        os.makedirs(self.work)
        self.audit = Audit(store=MemoryStore(), max_entries=50)
        self.trash = TrashStore(os.path.join(self.tmp, "trash"), retention_days=7, max_bytes=1024 * 1024)
        self.um = UndoManager(self.audit, self.trash)
        saved = (audit_mod.audit, undo_mod.trash, undo_mod.undo_manager, undo_mod.config.UNDO_TRASH_ENABLED)
        audit_mod.audit, undo_mod.trash, undo_mod.undo_manager = self.audit, self.trash, self.um
        undo_mod.config.UNDO_TRASH_ENABLED = True

        def restore():
            audit_mod.audit, undo_mod.trash, undo_mod.undo_manager = saved[:3]
            undo_mod.config.UNDO_TRASH_ENABLED = saved[3]
        self.addCleanup(restore)

    def p(self, name):
        return os.path.join(self.work, name)

    def last(self):
        return self.audit.recent(1)[0]


class FileUndoTests(Base):
    def test_create_file_then_undo_moves_it_to_trash_not_oblivion(self):
        msg = FileManager.create_file("notes.txt", "hello", self.work)
        self.assertEqual(msg, "File 'notes.txt' created successfully.")
        rec = self.last()
        self.assertEqual((rec.tool, rec.status, rec.undo_kind), ("create_file", "ok", "remove_created_file"))
        self.assertTrue(rec.reversible)

        res = self.um.undo(rec.id)
        self.assertTrue(res.ok, res.message)
        self.assertFalse(os.path.exists(self.p("notes.txt")))
        kept = [f for _, _, fs in os.walk(self.trash.root) for f in fs]
        self.assertEqual(kept, ["notes.txt"])                           # recoverable, not destroyed
        self.assertIsNotNone(self.audit.get(rec.id).undone_at)
        self.assertFalse(self.audit.get(rec.id).reversible)
        self.assertEqual(self.last().tool, "undo")                      # the undo itself is on the timeline

    def test_delete_then_undo_restores_exact_content(self):
        write(self.p("report.txt"), "quarterly numbers")
        msg = FileManager.delete_file("report.txt", self.work)
        self.assertIn("Deleted 'report.txt'", msg)
        self.assertIn("undo", msg)
        self.assertFalse(os.path.exists(self.p("report.txt")))
        res = self.um.undo_last()
        self.assertTrue(res.ok, res.message)
        self.assertEqual(read(self.p("report.txt")), "quarterly numbers")
        self.assertEqual([f for _, _, fs in os.walk(self.trash.root) for f in fs], [])   # item tidied away

    def test_overwrite_is_backed_up_and_undo_brings_back_the_old_version(self):
        write(self.p("a.txt"), "ORIGINAL")
        msg = FileManager.create_file("a.txt", "NEW", self.work)
        self.assertIn("already existed", msg)
        self.assertEqual(read(self.p("a.txt")), "NEW")
        self.assertEqual(self.last().undo_kind, "restore_overwritten")
        self.assertTrue(self.um.undo_last().ok)
        self.assertEqual(read(self.p("a.txt")), "ORIGINAL")

    def test_undo_is_one_shot_and_chain_walks_backwards(self):
        FileManager.create_file("one.txt", "1", self.work)
        FileManager.create_file("two.txt", "2", self.work)
        r1 = self.um.undo_last()
        self.assertIn("two.txt", r1.message)
        self.assertTrue(os.path.exists(self.p("one.txt")) and not os.path.exists(self.p("two.txt")))
        r2 = self.um.undo_last()
        self.assertIn("one.txt", r2.message)
        r3 = self.um.undo_last()
        self.assertFalse(r3.ok)
        self.assertIn("nothing I can undo", r3.message)

    def test_double_undo_of_the_same_action_is_refused(self):
        FileManager.create_file("x.txt", "", self.work)
        rid = self.last().id
        self.assertTrue(self.um.undo(rid).ok)
        again = self.um.undo(rid)
        self.assertFalse(again.ok)
        self.assertIn("already undone", again.message)

    def test_concurrent_double_click_runs_the_recipe_once(self):
        FileManager.create_file("x.txt", "", self.work)
        rid = self.last().id
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.um.undo(rid))) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(1 for r in results if r.ok), 1)

    def test_restore_never_overwrites_a_file_that_appeared_in_the_meantime(self):
        write(self.p("d.txt"), "old")
        FileManager.delete_file("d.txt", self.work)
        write(self.p("d.txt"), "someone made a new one")
        res = self.um.undo_last()
        self.assertTrue(res.ok)
        self.assertIn("didn't want to overwrite", res.message)
        self.assertEqual(read(self.p("d.txt")), "someone made a new one")
        self.assertEqual(read(self.p("d (restored).txt")), "old")

    def test_restore_when_the_original_folder_is_gone_fails_cleanly(self):
        write(self.p("d.txt"), "old")
        FileManager.delete_file("d.txt", self.work)
        os.rmdir(self.work)
        res = self.um.undo_last()
        self.assertFalse(res.ok)
        self.assertIn("no longer exists", res.message)
        self.assertTrue(self.audit.last_reversible() is not None)       # still undoable once the folder is back

    def test_folder_undo_only_removes_empty_folders(self):
        FileManager.create_folder("proj", self.work)
        self.assertEqual(self.last().undo_kind, "remove_created_folder")
        write(os.path.join(self.p("proj"), "keep.txt"), "precious")
        res = self.um.undo_last()
        self.assertFalse(res.ok)
        self.assertIn("isn't empty", res.message)
        self.assertEqual(read(os.path.join(self.p("proj"), "keep.txt")), "precious")
        os.remove(os.path.join(self.p("proj"), "keep.txt"))
        self.assertTrue(self.um.undo_last().ok)
        self.assertFalse(os.path.exists(self.p("proj")))

    def test_creating_a_folder_that_already_exists_is_not_undoable(self):
        os.makedirs(self.p("there"))
        msg = FileManager.create_folder("there", self.work)
        self.assertIn("already exists", msg)
        self.assertIsNone(self.last().undo_kind)

    def test_undoing_a_create_after_the_file_was_already_deleted_is_a_clean_noop(self):
        FileManager.create_file("gone.txt", "", self.work)
        os.remove(self.p("gone.txt"))
        res = self.um.undo_last()
        self.assertTrue(res.ok)
        self.assertIn("already gone", res.message)

    def test_big_files_are_deleted_for_real_and_the_reply_says_so(self):
        write(self.p("big.bin"), "x" * (1024 * 1024 + 10))              # > max_bytes of this test trash
        msg = FileManager.delete_file("big.bin", self.work)
        self.assertIn("can't be undone", msg)
        self.assertFalse(os.path.exists(self.p("big.bin")))
        self.assertIsNone(self.last().undo_kind)

    def test_undo_switched_off_restores_permanent_delete(self):
        undo_mod.config.UNDO_TRASH_ENABLED = False
        write(self.p("p.txt"), "x")
        msg = FileManager.delete_file("p.txt", self.work)
        self.assertEqual(msg, "Deleted 'p.txt' from " + self.work + ".")
        self.assertIsNone(self.last().undo_kind)
        self.assertFalse(os.path.exists(self.trash.root))

    def test_original_refusals_and_messages_are_unchanged(self):
        self.assertEqual(FileManager.delete_file("nope.txt", self.work),
                         f"I couldn't find a file named 'nope.txt' in {self.work}.")
        os.makedirs(self.p("adir"))
        self.assertEqual(FileManager.delete_file("adir", self.work),
                         "'adir' is a folder, not a file - I won't delete folders this way.")
        self.assertEqual([r.status for r in self.audit.recent(2)], ["blocked", "failed"])
        # path traversal in the name is still neutralised
        FileManager.create_file("../../evil.txt", "", self.work)
        self.assertTrue(os.path.exists(self.p("evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_failed_create_is_logged_as_failed(self):
        os.makedirs(self.p("dirname"))
        msg = FileManager.create_file("dirname", "x", self.work)        # name collides with a directory
        self.assertTrue(msg.startswith("Failed to create file"))
        rec = self.last()
        self.assertEqual((rec.status, rec.undo_kind), ("failed", None))

    def test_a_failed_backup_leaves_the_original_untouched(self):
        write(self.p("keep.txt"), "ORIGINAL")
        real = self.trash.stash

        def boom(*a, **k):
            raise OSError("disk full")
        self.trash.stash = boom
        try:
            msg = FileManager.create_file("keep.txt", "NEW", self.work)
        finally:
            self.trash.stash = real
        self.assertTrue(msg.startswith("Failed to create file"))
        self.assertEqual(read(self.p("keep.txt")), "ORIGINAL")


class TrashSafetyTests(Base):
    def test_restore_refuses_anything_outside_the_trash_folder(self):
        victim = os.path.join(self.tmp, "victim.txt")
        write(victim, "do not move me")
        with self.assertRaises(ValueError):
            self.trash.restore(victim, self.p("loot.txt"))
        self.assertEqual(read(victim), "do not move me")

    def test_a_tampered_recipe_cannot_move_arbitrary_files(self):
        victim = os.path.join(self.tmp, "victim.txt")
        write(victim, "do not move me")
        with self.audit.action("delete_file", {"file_name": "x"}) as act:
            act.set_undo("restore_from_trash", {"trash": victim, "original": self.p("loot.txt")}, "evil")
            act.ok("done")
        res = self.um.undo_last()
        self.assertFalse(res.ok)
        self.assertEqual(read(victim), "do not move me")
        self.assertFalse(os.path.exists(self.p("loot.txt")))

    def test_stash_cleans_up_after_a_failed_move(self):
        write(self.p("f.txt"), "x")
        import shutil
        real = shutil.move

        def boom(*a, **k):
            raise PermissionError("locked")
        shutil.move = boom
        try:
            with self.assertRaises(PermissionError):
                self.trash.stash(self.p("f.txt"))
        finally:
            shutil.move = real
        self.assertTrue(os.path.exists(self.p("f.txt")))
        self.assertEqual(os.listdir(self.trash.root), [])               # no empty husk left behind

    def test_symlinks_are_not_followed_into_the_trash(self):
        write(self.p("real.txt"), "x")
        link = self.p("link.txt")
        try:
            os.symlink(self.p("real.txt"), link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not available")
        with self.assertRaises(ValueError):
            self.trash.stash(link)
        msg = FileManager.delete_file("link.txt", self.work)            # only the link goes, never the target
        self.assertIn("Deleted 'link.txt'", msg)
        self.assertTrue(os.path.exists(self.p("real.txt")))

    def test_purge_expired_only_touches_old_items_inside_the_trash(self):
        write(self.p("old.txt"), "x")
        write(self.p("new.txt"), "x")
        old = self.trash.stash(self.p("old.txt"))
        self.trash.stash(self.p("new.txt"))
        outside = os.path.join(self.tmp, "outside_dir")
        os.makedirs(outside)
        write(os.path.join(outside, "keep.txt"), "x")
        # age the first item by renaming its folder to a stamp 10 days ago
        import time
        old_dir = os.path.dirname(old.trash_path)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() - 10 * 86400))
        os.rename(old_dir, os.path.join(self.trash.root, f"{stamp}-deadbeef"))
        self.assertEqual(self.trash.purge_expired(), 1)
        self.assertEqual(len(os.listdir(self.trash.root)), 1)
        self.assertTrue(os.path.exists(os.path.join(outside, "keep.txt")))

    def test_empty_and_size(self):
        write(self.p("a.txt"), "12345")
        self.trash.stash(self.p("a.txt"))
        self.assertEqual(self.trash.size_bytes(), 5)
        self.assertEqual(self.trash.empty(), 1)
        self.assertEqual(self.trash.size_bytes(), 0)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.a = Audit(store=MemoryStore(), max_entries=10)

    def test_adoption_gives_one_row_per_user_visible_action(self):
        with self.a.action("delete_file", {"file_name": "x"}, source="ai", adoptable=True) as outer:
            with self.a.action("delete_file", {"path": "/tmp"}) as inner:       # the backend
                inner.set_undo("restore_from_trash", {"trash": "t", "original": "o"}, "Restore x")
                inner.ok("Deleted 'x'.")
            outer.done("ignored: backend already decided")
        rows = self.a.recent()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r.tool, r.source, r.status, r.undo_kind, r.result), ("delete_file", "ai", "ok",
                         "restore_from_trash", "Deleted 'x'."))
        self.assertEqual(r.args, {"file_name": "x", "path": "/tmp"})            # merged

    def test_uninstrumented_tools_still_get_a_row_with_inferred_status(self):
        with self.a.action("play_youtube", {"query": "song"}, source="ai", adoptable=True) as act:
            act.done("Couldn't open YouTube.")
        self.assertEqual(self.a.recent(1)[0].status, "failed")

    def test_different_names_nest_as_separate_rows(self):
        with self.a.action("run_workflow", {"name": "morning"}, source="ai", adoptable=True) as wf:
            with self.a.action("open_app", {"app_name": "chrome"}) as step1:
                step1.ok("Opened chrome")
            with self.a.action("open_app", {"app_name": "spotify"}) as step2:
                step2.ok("Opened spotify")
            wf.ok("Ran 'morning'")
        self.assertEqual([r.tool for r in self.a.recent()], ["run_workflow", "open_app", "open_app"])
        self.assertTrue(all(r.source == "ai" for r in self.a.recent()))        # children inherit the source

    def test_collapse_if_children_drops_the_wrapper_row_only_when_steps_logged(self):
        with self.a.action("keyword_command", {"text": "create a file x"}, source="keyword", collapse_if_children=True) as k:
            with self.a.action("create_file", {"file_name": "x"}) as step:
                step.ok("made")
            k.done("made")
        self.assertEqual([r.tool for r in self.a.recent()], ["create_file"])
        with self.a.action("keyword_command", {"text": "volume up"}, source="keyword", collapse_if_children=True) as k:
            k.done("Turned the volume up.")
        self.assertEqual(self.a.recent(1)[0].tool, "keyword_command")

    def test_quiet_read_only_tools_are_not_logged_but_failures_are(self):
        with self.a.action("search_file", {"file_name": "x"}) as act:
            act.ok("Found it")
        self.assertEqual(self.a.recent(), [])
        with self.a.action("search_file", {"file_name": "x"}) as act:
            act.fail("exploded")
        self.assertEqual(len(self.a.recent()), 1)

    def test_exception_inside_scope_is_recorded_and_propagates(self):
        with self.assertRaises(ZeroDivisionError):
            with self.a.action("open_app", {"app_name": "x"}) as act:
                1 / 0
        r = self.a.recent(1)[0]
        self.assertEqual(r.status, "failed")
        self.assertIn("ZeroDivisionError", r.result)

    def test_threads_do_not_see_each_others_scopes(self):
        out = []

        def worker(tag):
            with self.a.action("open_app", {"app_name": tag}, source="ai", adoptable=True) as act:
                with self.a.action("open_app", {"app_name": tag}) as inner:
                    inner.ok(f"opened {tag}")
                act.done("x")
            out.append(tag)
        ts = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        rows = self.a.recent()
        self.assertEqual(len(rows), 8)
        self.assertEqual(sorted(r.args["app_name"] for r in rows), sorted(f"t{i}" for i in range(8)))

    def test_cap_and_copies(self):
        for i in range(25):
            self.a.record("open_app", {"app_name": str(i)}, "ok", status="ok")
        rows = self.a.recent(100)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0].args["app_name"], "24")
        rows[0].status = "mutated"                                              # callers get copies
        self.assertEqual(self.a.recent(1)[0].status, "ok")

    def test_listeners_hear_added_updated_cleared_and_cant_break_logging(self):
        # Tier 21: "started" is a new event (see ActionScope.__enter__ and
        # add_listener's own docstring) fired the moment the `with
        # audit.action(...)` scope opens, before "added" - this test now
        # expects it first, same listener-robustness assertion otherwise
        # unchanged (a listener that raises must not break logging for the
        # others, or for the commit itself).
        events = []
        self.a.add_listener(lambda ev, rec: events.append(ev))
        self.a.add_listener(lambda ev, rec: 1 / 0)
        with self.a.action("create_file", {"file_name": "x"}) as act:
            act.set_undo("remove_created_file", {"target": "x"}, "Remove x")
            act.ok("ok")
        self.a.mark_undone(self.a.recent(1)[0].id)
        self.a.clear()
        self.assertEqual(events, ["started", "added", "updated", "cleared"])
        self.assertEqual(self.a.recent(), [])

    def test_infer_status_and_summaries(self):
        cases = {"Opened chrome": "ok", "Error opening x.": "failed", "Couldn't find it": "failed",
                 "Failed to send": "failed", "For safety, I won't restart": "blocked", "": "ok",
                 "'x' is fine": "ok", "Sorry, no": "failed"}
        for text, want in cases.items():
            self.assertEqual(infer_status(text), want, text)
        self.assertEqual(summarize("delete_file", {"file_name": "report.txt"}), "Deleted file report.txt")
        self.assertEqual(summarize("lock_pc", {}), "Locked the PC")
        self.assertEqual(summarize("some_user_skill", {"query": "q"}), "Some user skill q")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.dbm = DatabaseManager()
        self.dbm.db_path = os.path.join(self.tmp, "t.db")
        self.dbm.init_db()
        self.on = True
        self.a = Audit(store=DbStore(self.dbm), max_entries=20, persist_enabled=lambda: self.on)

    def reopen(self):
        return Audit(store=DbStore(self.dbm), max_entries=20, persist_enabled=lambda: self.on)

    def _flush(self):
        """Tier 19: DbStore's insert/update/prune are async now (see its
        own docstring in automation/audit.py) - block until everything
        enqueued so far has actually landed on disk before asserting on
        it or reopen()-ing a fresh Audit against the same db file."""
        self.a._store.flush(timeout=5.0)

    def test_rows_and_undo_recipes_survive_a_restart(self):
        with self.a.action("delete_file", {"file_name": "é.txt"}, source="ai") as act:
            act.set_undo("restore_from_trash", {"trash": "T", "original": "O"}, "Restore é.txt")
            act.ok("Deleted 'é.txt'.")
        self._flush()
        again = self.reopen().recent(5)
        self.assertEqual(len(again), 1)
        r = again[0]
        self.assertGreater(r.id, 0)
        self.assertEqual((r.tool, r.args, r.undo_kind, r.undo_payload, r.reversible),
                         ("delete_file", {"file_name": "é.txt"}, "restore_from_trash", {"trash": "T", "original": "O"}, True))

    def test_undone_flag_is_persisted(self):
        self.a.record("open_app", {"app_name": "x"}, "ok", "ok")
        with self.a.action("create_file", {"file_name": "x"}) as act:
            act.set_undo("remove_created_file", {"target": "x"}, "")
            act.ok("ok")
        self._flush()  # so `rid` below is the real, stable id - not a transient placeholder
        rid = self.a.recent(1)[0].id
        self.a.mark_undone(rid)
        self._flush()  # wait for the UPDATE to land before reopening
        self.assertIsNotNone(self.reopen().get(rid).undone_at)

    def test_history_switch_off_keeps_rows_in_ram_only(self):
        self.on = False
        with self.a.action("open_app", {"app_name": "secret app"}) as act:
            act.ok("Opened")
        self.assertEqual(len(self.a.recent()), 1)                               # visible this session
        self.assertLess(self.a.recent(1)[0].id, 0)
        self.assertEqual(self.dbm.get_recent_actions(), [])                     # never reached disk
        self.assertEqual(self.reopen().recent(), [])

    def test_clear_all_local_data_also_wipes_the_audit_log(self):
        self.a.record("open_app", {"app_name": "x"}, "ok", "ok")
        self._flush()
        self.assertTrue(self.dbm.get_recent_actions())
        self.assertTrue(self.dbm.clear_all())
        self.assertEqual(self.dbm.get_recent_actions(), [])

    def test_db_is_pruned_to_the_cap(self):
        for i in range(30):
            self.a.record("open_app", {"app_name": str(i)}, "ok", "ok")
        self._flush()  # wait for every insert/prune enqueued above to actually land
        self.assertEqual(len(self.dbm.get_recent_actions(100)), 20)
        self.assertEqual(self.dbm.get_recent_actions(1)[0]["args"], '{"app_name": "29"}')


class StartedEventTests(unittest.TestCase):
    """Tier 21: the new "started" listener event (ActionScope.__enter__) -
    the data behind ui/main_window.py's live activity banner."""

    def setUp(self):
        self.a = Audit(store=MemoryStore(), max_entries=50)

    def test_started_fires_before_the_handler_runs_with_tool_and_args(self):
        seen = []
        self.a.add_listener(lambda ev, rec: seen.append((ev, rec.tool, dict(rec.args))) if ev == "started" else None)
        with self.a.action("open_app", {"app_name": "chrome"}) as act:
            # at this point the "started" listener must already have fired
            self.assertEqual(seen, [("started", "open_app", {"app_name": "chrome"})])
            act.ok("Opened chrome")
        self.assertEqual(seen, [("started", "open_app", {"app_name": "chrome"})])  # exactly once, not again on exit

    def test_started_record_is_never_committed_to_recent(self):
        """The placeholder ActionRecord "started" hands listeners is just a
        snapshot for display - it must never show up in audit.recent() on
        its own (only the real commit at __exit__ does that)."""
        seen_ids = []
        self.a.add_listener(lambda ev, rec: seen_ids.append(rec.id) if ev == "started" else None)
        with self.a.action("open_app", {"app_name": "x"}) as act:
            self.assertEqual(self.a.recent(), [])   # nothing committed yet, even though "started" already fired
            act.ok("ok")
        self.assertEqual(len(self.a.recent()), 1)
        self.assertEqual(seen_ids, [0])   # placeholder id, never a real row id

    def test_started_fires_even_for_a_quiet_tool_that_never_gets_an_added_event(self):
        """QUIET_TOOLS' successful calls never produce an "added" row (see
        _commit) - but a live "what's happening right now" banner still
        needs to know they started, so "started" must fire for them too."""
        from automation.audit import QUIET_TOOLS
        quiet_tool = next(iter(QUIET_TOOLS))
        events = []
        self.a.add_listener(lambda ev, rec: events.append(ev))
        with self.a.action(quiet_tool, {}) as act:
            act.ok("fine")
        self.assertIn("started", events)
        self.assertNotIn("added", events)   # confirms this tool really is quiet in this scenario

    def test_a_raising_started_listener_does_not_prevent_the_action_from_running(self):
        self.a.add_listener(lambda ev, rec: 1 / 0)
        with self.a.action("open_app", {"app_name": "x"}) as act:
            act.ok("ok")
        self.assertEqual(len(self.a.recent()), 1)   # the action itself still completed and was recorded


if __name__ == "__main__":
    unittest.main()
