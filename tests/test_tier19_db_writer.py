"""
tests/test_tier19_db_writer.py — Tier 19: database/db_writer.py's
BackgroundDbWriter in isolation. No sqlite3, no Audit/DbStore - those are
covered where they already live (tests/test_phase4_undo.py's
PersistenceTests, which now flush this writer before asserting on disk
state - see its own _flush() helper). This file is purely about the
generic queue/thread mechanics: FIFO ordering, a raising job never killing
the worker or blocking what's queued after it, submit_and_wait()'s
blocking + exception-propagating contract, and flush()'s plain
synchronization-barrier one.

Run: python -m unittest discover -s tests -p "test_tier19_db_writer.py"
(pytest will also collect it.)
"""
import threading
import time
import unittest

import _phase4_support as S  # noqa: F401  (adds the repo root to sys.path)

from database.db_writer import BackgroundDbWriter


class LifecycleTests(unittest.TestCase):
    def test_not_alive_before_anything_is_submitted(self):
        w = BackgroundDbWriter()
        self.assertFalse(w.is_alive())

    def test_submit_starts_the_worker_lazily(self):
        w = BackgroundDbWriter()
        w.submit(lambda: None)
        self.assertTrue(w.is_alive())
        w.stop()

    def test_start_is_idempotent(self):
        w = BackgroundDbWriter()
        w.start()
        first_thread = w._thread
        w.start()  # should not replace the already-running thread
        self.assertIs(w._thread, first_thread)
        w.stop()

    def test_stop_lets_already_queued_jobs_finish_first(self):
        w = BackgroundDbWriter()
        ran = []
        gate = threading.Event()

        def _slow():
            gate.wait(2)
            ran.append("slow")

        w.submit(_slow)
        w.submit(lambda: ran.append("fast"))
        gate.set()  # let _slow proceed almost immediately - just proving ordering, not real slowness
        w.stop(timeout=2)
        self.assertEqual(ran, ["slow", "fast"])
        self.assertFalse(w.is_alive())

    def test_a_writer_stopped_can_be_used_again_via_submit(self):
        # Same idempotent-restart property automation/thread_supervisor.py
        # relies on for the other background singletons (a fresh
        # threading.Thread each time start() is actually needed).
        w = BackgroundDbWriter()
        w.submit(lambda: None)
        w.stop()
        self.assertFalse(w.is_alive())
        ran = threading.Event()
        w.submit(ran.set)
        self.assertTrue(ran.wait(2))
        self.assertTrue(w.is_alive())
        w.stop()


class SubmitOrderingTests(unittest.TestCase):
    def test_jobs_run_in_the_order_they_were_submitted(self):
        w = BackgroundDbWriter()
        order = []
        lock = threading.Lock()
        n = 50
        for i in range(n):
            def _job(i=i):
                with lock:
                    order.append(i)
            w.submit(_job)
        self.assertTrue(w.flush(timeout=5))
        self.assertEqual(order, list(range(n)))
        w.stop()

    def test_a_raising_job_is_logged_and_does_not_stop_later_jobs(self):
        w = BackgroundDbWriter()
        ran = []
        w.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")))  # raises when called
        w.submit(lambda: ran.append("after"))
        self.assertTrue(w.flush(timeout=5))
        self.assertEqual(ran, ["after"])
        self.assertTrue(w.is_alive())  # the worker thread itself survived the raise
        w.stop()

    def test_a_later_job_can_see_a_mutation_an_earlier_job_made(self):
        # The exact property automation/audit.py's DbStore relies on for
        # its id-reconciliation trick: a later job reading shared mutable
        # state at RUN time sees whatever an earlier job (same queue, same
        # worker thread, strict FIFO) already did to it.
        w = BackgroundDbWriter()
        state = {"value": None}
        w.submit(lambda: state.__setitem__("value", "set-by-first-job"))
        seen = []
        w.submit(lambda: seen.append(state["value"]))
        self.assertTrue(w.flush(timeout=5))
        self.assertEqual(seen, ["set-by-first-job"])
        w.stop()


class SubmitAndWaitTests(unittest.TestCase):
    def test_blocks_until_the_job_has_actually_run(self):
        w = BackgroundDbWriter()
        ran = []
        w.submit_and_wait(lambda: ran.append("done"), timeout=5)
        self.assertEqual(ran, ["done"])  # already true the instant submit_and_wait returns
        w.stop()

    def test_reraises_the_jobs_own_exception_in_the_caller(self):
        w = BackgroundDbWriter()

        def _boom():
            raise ValueError("specific message")
        with self.assertRaises(ValueError) as ctx:
            w.submit_and_wait(_boom, timeout=5)
        self.assertEqual(str(ctx.exception), "specific message")
        self.assertTrue(w.is_alive())  # the worker thread survives the re-raised exception too
        w.stop()

    def test_times_out_if_the_job_does_not_finish_in_time(self):
        w = BackgroundDbWriter()
        release = threading.Event()
        w.submit(lambda: release.wait(5))  # occupies the worker so the next job can't start yet
        with self.assertRaises(TimeoutError):
            w.submit_and_wait(lambda: None, timeout=0.2)
        release.set()  # let the blocking job finish so stop() below doesn't hang
        w.stop(timeout=5)

    def test_ordering_is_preserved_relative_to_other_submits(self):
        w = BackgroundDbWriter()
        order = []
        w.submit(lambda: order.append("a"))
        w.submit_and_wait(lambda: order.append("b"), timeout=5)
        w.submit(lambda: order.append("c"))
        self.assertTrue(w.flush(timeout=5))
        self.assertEqual(order, ["a", "b", "c"])
        w.stop()


class FlushTests(unittest.TestCase):
    def test_flush_with_nothing_ever_queued_is_trivially_true(self):
        w = BackgroundDbWriter()
        self.assertTrue(w.flush(timeout=1))
        self.assertFalse(w.is_alive())  # never had to start a thread at all

    def test_flush_waits_for_everything_queued_so_far(self):
        w = BackgroundDbWriter()
        ran = []
        for i in range(10):
            w.submit(lambda i=i: ran.append(i))
        self.assertTrue(w.flush(timeout=5))
        self.assertEqual(ran, list(range(10)))
        w.stop()

    def test_flush_times_out_without_cancelling_the_stuck_job(self):
        w = BackgroundDbWriter()
        release = threading.Event()
        ran = threading.Event()
        w.submit(lambda: (release.wait(5), ran.set()))
        self.assertFalse(w.flush(timeout=0.2))  # the job above hasn't released yet
        release.set()
        self.assertTrue(ran.wait(2))  # ...but it's still running, not cancelled - it finishes right after
        w.stop(timeout=5)


if __name__ == "__main__":
    unittest.main()
