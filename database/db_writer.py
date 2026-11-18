"""
database/db_writer.py - Tier 19: a single background thread + FIFO queue
that a hot interactive path can hand a DB write off to, instead of
blocking the calling thread on sqlite3's connection-open + commit/fsync
cost. Shaped to match SARA's own orchestrator/db_writer.py (see
docs/SARA_GAP_ANALYSIS.md) - a deliberate parity item, not just an
internal refactor.

Why this exists: database/db_manager.py's get_connection() opens a BRAND
NEW sqlite3 connection for every call (no pooling, no persistent
connection), and every write commits under SQLite's default (safest, but
slowest) synchronous=FULL/journal_mode=DELETE pragmas - each commit is a
real fsync. automation/audit.py's _commit() runs a write through that path
on EVERY tool dispatch (every skill/handler call, across every turn and
every planner step - see planning/executor.py) - on a loaded system, or
just a slow disk, that's real, avoidable latency sitting directly in the
interactive path, for a write whose caller doesn't actually need to wait
on before continuing. See automation/audit.py's DbStore for the first
(and, as of Tier 19, only) adopter - "and similar" hot paths can reuse
this same class rather than re-deriving it.

Ordering guarantee: ONE worker thread processes jobs strictly FIFO, so
anything enqueued through the SAME BackgroundDbWriter instance for the
same logical row (an insert, then later an update, then maybe a prune)
runs in the order it was enqueued. Callers that need a later job to see
an earlier job's effect (e.g. an UPDATE that needs an INSERT's
auto-assigned row id) get that for free, AS LONG AS the later job reads
whatever it needs from a shared mutable object at RUN time rather than
baking a value in at enqueue time - see automation/audit.py's DbStore.
update_async() for the pattern this is meant to be used with.

Jobs are plain no-arg callables (closures) - this module has no idea what
a "row" or an "action" is, deliberately, so it stays reusable. A
fire-and-forget job (submit()) that raises is logged and does NOT kill
the worker thread or any later-queued job, matching this codebase's
"a crash in one unit of background work must never silently take down
the whole facility" rule elsewhere (MemoryConsolidator/ProactiveEngine's
own tick loops, automation/thread_supervisor.py).

Satisfies automation/thread_supervisor.py's start()/stop()/is_alive()
duck-type contract, so the one production instance can be watched and
restarted the same way as Wave's other background singletons.
"""
import logging
import queue
import threading
from typing import Callable, Optional

logger = logging.getLogger("DbWriter")

_SHUTDOWN = object()  # sentinel: tells the worker loop to stop


class BackgroundDbWriter:
    def __init__(self, name: str = "WaveDbWriter"):
        self._name = name
        self._queue: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ---- lifecycle ----
    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self, timeout: float = 2.0):
        """Already-queued jobs run to completion before the worker actually
        stops (the shutdown sentinel is just another FIFO item); anything
        submitted after stop() has been called may never run - fine here,
        since this is only ever called once, at app shutdown, same
        best-effort tradeoff as every other background daemon thread this
        app stops there."""
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            self._queue.put(_SHUTDOWN)
        thread.join(timeout=timeout)

    def _run(self):
        while True:
            job = self._queue.get()
            if job is _SHUTDOWN:
                return
            try:
                job()
            except Exception:
                logger.exception(f"{self._name}: a queued job raised")

    # ---- submitting work ----
    def submit(self, job: Callable[[], None]) -> None:
        """Enqueue `job` to run on the worker thread, in order relative to
        everything already queued. Starts the worker on first use -
        callers don't need a separate start() call just to begin writing.
        Fire-and-forget: an exception from `job` is logged (see _run())
        and never reaches the caller, which has already moved on."""
        self.start()
        self._queue.put(job)

    def submit_and_wait(self, job: Callable[[], None], timeout: float = 5.0) -> None:
        """Like submit(), but blocks until `job` has actually run, and
        re-raises whatever `job` itself raised in the CALLING thread (the
        one difference from submit()'s fire-and-forget contract) - for the
        rare caller that wants the exact same synchronous,
        exception-propagating behavior a direct call would have had, just
        run on the shared writer thread so it stays correctly ordered
        relative to other queued jobs (automation/audit.py's
        DbStore.clear() is the one user of this today - see its own
        docstring for why clear() specifically needs that ordering and
        that contract, where insert/update/prune don't).

        Raises TimeoutError if `job` hasn't run within `timeout` seconds
        (it stays queued and may still run later - this just stops
        waiting for it)."""
        done = threading.Event()
        outcome = {}

        def _wrapped():
            try:
                job()
            except BaseException as e:  # noqa: BLE001 - re-raised verbatim below, not swallowed
                outcome["exc"] = e
            finally:
                done.set()

        self.submit(_wrapped)
        if not done.wait(timeout):
            raise TimeoutError(f"{self._name}: timed out after {timeout:.0f}s waiting for a queued job")
        if "exc" in outcome:
            raise outcome["exc"]

    def flush(self, timeout: float = 5.0) -> bool:
        """Blocks until every job queued so far has run, or `timeout`
        elapses (returns False on timeout - nothing is cancelled,
        already-queued jobs keep running after this returns). A
        no-result, non-exception-propagating sibling of submit_and_wait()
        - a pure synchronization barrier ('everything queued so far is
        done'), used at app shutdown to give the last few writes a bounded
        chance to land - see automation/audit.py's Audit.shutdown()."""
        if self._thread is None or not self._thread.is_alive():
            return True  # nothing has ever been queued - trivially flushed
        done = threading.Event()
        self._queue.put(done.set)
        return done.wait(timeout)
