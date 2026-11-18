"""
tests/test_tier19_thread_supervisor.py — Tier 19:
automation/thread_supervisor.py's ThreadSupervisor. tick()'s pure, no-
sleep logic is exercised directly (same check/tick split as
ProactiveEngine/MemoryConsolidator themselves - see that module's own
docstring), with a fake clock (time.monotonic patched) for the
backoff/healthy-reset timing, plus a couple of real-background-thread
tests for start()/stop() themselves.

Run: python -m unittest discover -s tests -p "test_tier19_thread_supervisor.py"
(pytest will also collect it.)
"""
import threading
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the repo root to sys.path)

import automation.thread_supervisor as ts_mod
from automation.thread_supervisor import ThreadSupervisor
from settings.config import config


class _FakeWatched:
    """A minimal start()/stop()/is_alive() object the supervisor can watch
    - alive by default, flipped by the test to simulate a crash, with
    start() recording how many times it's been (re)started."""

    def __init__(self, alive=True):
        self.alive = alive
        self.start_count = 0
        self.stopped = False

    def start(self):
        self.start_count += 1
        self.alive = True

    def stop(self):
        self.stopped = True
        self.alive = False

    def is_alive(self):
        return self.alive


class WatchRegistrationTests(unittest.TestCase):
    def test_watch_then_status_reports_it_alive(self):
        sup = ThreadSupervisor()
        obj = _FakeWatched(alive=True)
        sup.watch("thing", obj)
        self.assertTrue(sup.status()["thing"]["alive"])
        self.assertEqual(sup.status()["thing"]["restart_count"], 0)

    def test_unwatch_removes_it_from_status(self):
        sup = ThreadSupervisor()
        sup.watch("thing", _FakeWatched())
        sup.unwatch("thing")
        self.assertNotIn("thing", sup.status())

    def test_watching_the_same_name_again_replaces_the_old_entry(self):
        sup = ThreadSupervisor()
        old = _FakeWatched(alive=False)
        sup.watch("thing", old)
        new = _FakeWatched(alive=True)
        sup.watch("thing", new)
        self.assertTrue(sup.status()["thing"]["alive"])

    def test_an_is_alive_that_raises_is_treated_as_dead_not_a_crash_of_the_supervisor(self):
        sup = ThreadSupervisor()

        class _Broken:
            def is_alive(self):
                raise RuntimeError("boom")
            def start(self):
                pass
        sup.watch("thing", _Broken())
        self.assertFalse(sup.status()["thing"]["alive"])  # doesn't raise out of status()
        sup.tick()  # doesn't raise either


class TickRestartTests(unittest.TestCase):
    def setUp(self):
        # Deterministic policy for these tests regardless of config.py's
        # own defaults - small numbers, fast to reason about.
        patcher = mock.patch.multiple(
            config,
            THREAD_SUPERVISOR_BASE_BACKOFF_S=1.0,
            THREAD_SUPERVISOR_MAX_BACKOFF_S=4.0,
            THREAD_SUPERVISOR_MAX_RESTARTS=3,
            THREAD_SUPERVISOR_HEALTHY_RESET_S=100.0,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sup = ThreadSupervisor()

    def _at(self, t):
        return mock.patch.object(ts_mod.time, "monotonic", return_value=t)

    def test_a_healthy_entry_is_never_restarted(self):
        obj = _FakeWatched(alive=True)
        self.sup.watch("thing", obj)
        for t in range(0, 20, 5):
            with self._at(float(t)):
                self.sup.tick()
        self.assertEqual(obj.start_count, 0)

    def test_a_crash_triggers_a_restart_after_the_base_backoff(self):
        # The supervisor only ever learns about a crash at the moment one
        # of its own ticks happens to observe it dead - there's no way for
        # it to know the TRUE instant of the crash, which could have been
        # anywhere since the last confirmed-alive tick. So "the base
        # backoff" is measured from first-noticed, not from the crash
        # itself: the first tick that sees it dead starts the clock (and
        # never restarts on that same tick, even if the clock happens to
        # already satisfy the backoff), and a later tick restarts once
        # that much time has actually elapsed.
        obj = _FakeWatched(alive=True)
        with self._at(0.0):
            self.sup.watch("thing", obj)
            self.sup.tick()  # sees it alive, records was_alive=True
        obj.alive = False  # simulate the crash
        with self._at(0.5):
            self.sup.tick()  # first tick to notice it's dead - starts the backoff clock, no restart yet
        self.assertEqual(obj.start_count, 0)
        with self._at(0.5 + 0.6):
            self.sup.tick()  # only 0.6s since noticing - base backoff (1.0s) hasn't elapsed
        self.assertEqual(obj.start_count, 0)
        with self._at(0.5 + 1.1):
            self.sup.tick()  # 1.1s since noticing - backoff elapsed, restart attempt 1
        self.assertEqual(obj.start_count, 1)
        self.assertTrue(obj.alive)  # _FakeWatched.start() marks itself alive again

    def test_restart_fires_the_restarted_listener_event(self):
        obj = _FakeWatched(alive=True)
        events = []
        self.sup.add_listener(lambda name, event: events.append((name, event)))
        with self._at(0.0):
            self.sup.watch("thing", obj)
            self.sup.tick()
        obj.alive = False
        with self._at(0.0):
            self.sup.tick()  # notices the crash, starts the backoff clock
        with self._at(2.0):
            self.sup.tick()  # backoff long elapsed - restarts
        self.assertIn(("thing", "restarted"), events)

    def test_backoff_doubles_on_each_further_crash_up_to_the_cap(self):
        obj = _FakeWatched(alive=True)
        with self._at(0.0):
            self.sup.watch("thing", obj)
            self.sup.tick()
        # crash #1: noticed at t=0 (no prior restart to measure from, so
        # this tick only starts the clock) -> restart at base backoff (1.0s)
        obj.alive = False
        with self._at(0.0):
            self.sup.tick()
        with self._at(1.1):
            self.sup.tick()
        self.assertEqual(obj.start_count, 1)
        # crash #2 right after restarting -> this time last_restart_at is
        # already set (from the restart itself), so the very next tick
        # measures the cooldown directly against it - no separate
        # "noticed" tick needed before the doubled backoff starts counting.
        obj.alive = False
        with self._at(1.1 + 1.5):
            self.sup.tick()          # only 1.5s since the last restart attempt - too soon for a 2.0s backoff
        self.assertEqual(obj.start_count, 1)
        with self._at(1.1 + 2.1):
            self.sup.tick()          # 2.1s since the last attempt - now past the doubled backoff
        self.assertEqual(obj.start_count, 2)

    def test_gives_up_after_hitting_the_restart_cap_and_stops_trying(self):
        obj = _FakeWatched(alive=True)
        events = []
        self.sup.add_listener(lambda name, event: events.append((name, event)))
        t = 0.0
        with self._at(t):
            self.sup.watch("thing", obj)
            self.sup.tick()
        # Drive it through MAX_RESTARTS (3) crash/restart cycles. The very
        # first crash needs its own "notice" tick (no prior restart to
        # measure the cooldown from yet); every crash after that has
        # last_restart_at already set from the previous restart, so one
        # sufficiently-later tick is enough on its own.
        obj.alive = False
        with self._at(t):
            self.sup.tick()  # notices crash #1, starts the clock
        for _ in range(config.THREAD_SUPERVISOR_MAX_RESTARTS):
            obj.alive = False
            t += 10.0  # comfortably past even the doubled/capped backoff each time
            with self._at(t):
                self.sup.tick()
        self.assertEqual(obj.start_count, config.THREAD_SUPERVISOR_MAX_RESTARTS)
        obj.alive = False  # one crash beyond the cap
        t += 10.0
        with self._at(t):
            self.sup.tick()
        self.assertEqual(obj.start_count, config.THREAD_SUPERVISOR_MAX_RESTARTS)  # no further attempt
        self.assertTrue(self.sup.status()["thing"]["gave_up"])
        self.assertIn(("thing", "gave_up"), events)
        # And it stays given up - ticking forever more never restarts it again.
        obj.alive = False
        t += 1000.0
        with self._at(t):
            self.sup.tick()
        self.assertEqual(obj.start_count, config.THREAD_SUPERVISOR_MAX_RESTARTS)

    def test_restart_count_decays_after_staying_healthy_long_enough(self):
        obj = _FakeWatched(alive=True)
        with self._at(0.0):
            self.sup.watch("thing", obj)
            self.sup.tick()
        obj.alive = False
        with self._at(0.0):
            self.sup.tick()  # notices the crash, starts the clock
        with self._at(1.1):
            self.sup.tick()  # restart #1
        self.assertEqual(self.sup.status()["thing"]["restart_count"], 1)
        # Stays alive (obj.start() already marked it so) well past HEALTHY_RESET_S (100s).
        with self._at(1.1 + 150.0):
            self.sup.tick()
        self.assertEqual(self.sup.status()["thing"]["restart_count"], 0)
        # Now a fresh crash gets the BASE backoff again, not a doubled one -
        # proving the count actually reset, not just the status() display.
        # (last_restart_at was also cleared by the reset, so this crash
        # needs its own "notice" tick again, same as the very first one did.)
        obj.alive = False
        with self._at(1.1 + 150.0):
            self.sup.tick()
        with self._at(1.1 + 150.0 + 1.1):
            self.sup.tick()
        self.assertEqual(obj.start_count, 2)


class RealThreadTests(unittest.TestCase):
    """A lighter integration check that start()/stop() actually drive a
    real background thread end-to-end, not just tick()'s own pure logic."""

    def test_start_runs_tick_on_its_own_thread_and_stop_joins_it(self):
        # Real wall-clock time, so keep the backoff tiny - this is only
        # testing that the background polling loop itself calls tick() and
        # that stop() joins it, not backoff timing precision (TickRestartTests
        # already covers that with a fake clock).
        with mock.patch.multiple(config, THREAD_SUPERVISOR_BASE_BACKOFF_S=0.05,
                                  THREAD_SUPERVISOR_MAX_BACKOFF_S=0.2):
            sup = ThreadSupervisor(interval=0.05)
            obj = _FakeWatched(alive=True)
            sup.watch("thing", obj)
            obj.alive = False
            sup.start()
            try:
                deadline = time.monotonic() + 3
                while obj.start_count == 0 and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                sup.stop()
            self.assertGreaterEqual(obj.start_count, 1)


if __name__ == "__main__":
    unittest.main()
