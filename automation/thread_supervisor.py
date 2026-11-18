"""
automation/thread_supervisor.py - Tier 19: a small watchdog for Wave's
background daemon-thread singletons.

Problem this closes: MemoryConsolidator (brain/memory_consolidation.py),
ProactiveEngine (automation/proactive.py), and UserSkillLoader
(brain/skill_loader.py) each wrap their own tick loop in `except
Exception`, so an ordinary exception from a check/tick can never kill the
thread - but a BaseException subtype (SystemExit, KeyboardInterrupt, or a
C-extension crash surfacing as one) escaping that guard silently ends the
thread, and whatever it was supposed to keep doing for the rest of the
session just... stops, with nothing anywhere noticing. Tier 5 hit exactly
this shape for real (pyautogui's own import doing a bare sys.exit() under
some Linux desktop environments) - that's the concrete precedent this is
closing, not a hypothetical.

ThreadSupervisor polls is_alive() on each watched (name, obj) pair from
its own daemon thread, and calls obj.start() again - with exponential
backoff and a cap on restart attempts (crash-loop protection: a thread
that's immediately dying again and again on every restart is a bug to
surface, not paper over by restarting it forever) - the moment a
previously-alive entry goes quiet. A restart count decays back to 0 once
an entry has stayed alive for config.THREAD_SUPERVISOR_HEALTHY_RESET_S,
so one rare crash doesn't get bundled with an unrelated one much later
into the same "crash loop" and eat into the restart cap.

Even the FIRST restart attempt after a crash waits out
THREAD_SUPERVISOR_BASE_BACKOFF_S, not just the second and later ones -
restarting instantly risks a tight crash-loop before any cooldown has
had a chance to apply. Since polling can only ever learn of a crash at
whatever tick happens to first observe it dead (there's no way to know
the true instant it died, somewhere since the last confirmed-alive
tick), the backoff clock starts from that first-noticed tick rather
than from the crash itself - so a crash is detected on one tick (which
never restarts anything, even if time has already technically elapsed)
and the restart itself happens on a later tick once the backoff has
elapsed since then. Each subsequent crash measures its own (doubled)
backoff from the previous actual restart directly, since that timestamp
is already on hand by then.

Contract with watched objects (duck-typed, not an ABC - matches this
codebase's existing style elsewhere, e.g. planning/executor.py's handler
dispatch): start() (idempotent - safe to call when already running),
stop() (idempotent), is_alive() -> bool. MemoryConsolidator, ProactiveEngine,
and UserSkillLoader all already satisfy this (UserSkillLoader via a small
is_alive() added alongside its pre-existing `watching` property).
database/db_writer.py's BackgroundDbWriter satisfies it too, so it can be
watched the same way.

IMPORTANT - the only thing that tells this supervisor "this death was
intentional, don't restart it" is its OWN stop()/unwatch(): nothing here
inspects *why* is_alive() went False. Call unwatch(name) before
intentionally stopping a watched object for any reason other than final
app shutdown - today, nothing in Wave does this; all three singletons
above (and the DB writer) are only ever stopped once, at shutdown. At
shutdown, stop this supervisor BEFORE stopping the watched objects
themselves, so it isn't racing a deliberate shutdown with its own
"helpful" restart.

NOT a QThread/Qt-aware class (same reasoning as ProactiveEngine/
MemoryConsolidator themselves) - ui/main_window.py owns translating
status() into whatever UI health indicator it wants, same cross-thread
pattern those two already use (a listener callback invoked from THIS
thread, which should only ever do a thread-safe signal .emit(), never
touch Qt widgets directly).

ui/workers.py's ReminderWorker is NOT watched here - it's a QThread owned
and signal-wired by MainWindow, a different shape from these module-level
singletons (recreating a dead QThread means building a fresh instance and
reconnecting its signals, which this generic, Qt-free supervisor has no
way to do). See ui/main_window.py's own _on_reminder_worker_finished for
that one's smaller, dedicated recreate-on-crash handling.
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from settings.config import config

logger = logging.getLogger("ThreadSupervisor")


@dataclass
class _Watched:
    name: str
    obj: object
    max_restarts: int
    was_alive: bool = False        # supervisor's own last-seen liveness for this entry
    recovering: bool = False       # a crash is being tracked (detected, maybe mid-backoff, or mid-cap)
    restart_count: int = 0
    last_restart_at: Optional[float] = None
    gave_up: bool = False


class ThreadSupervisor:
    """See module docstring. tick() is pure/synchronous (same check/tick
    split as ProactiveEngine/MemoryConsolidator) so it's testable without
    waiting on a real background thread; start() wraps it in a loop on
    its own daemon thread."""

    def __init__(self, interval: float = None):
        self.interval = interval if interval is not None else config.THREAD_SUPERVISOR_INTERVAL_S
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._entries: Dict[str, _Watched] = {}
        self._listeners: List[Callable[[str, str], None]] = []  # (name, event) - "restarted" | "gave_up"

    # ---- registration ----
    def watch(self, name: str, obj, max_restarts: Optional[int] = None) -> None:
        """Register `obj` (already started, or about to be) under `name`.
        Calling this again for the same name replaces the old entry -
        used when the app constructs a fresh object to replace a watched
        one (ReminderWorker's own restart does this, but through its own
        dedicated handling, not this class - see module docstring)."""
        effective_cap = max_restarts if max_restarts is not None else config.THREAD_SUPERVISOR_MAX_RESTARTS
        with self._lock:
            self._entries[name] = _Watched(name=name, obj=obj, max_restarts=effective_cap,
                                            was_alive=self._safe_is_alive(obj))

    def unwatch(self, name: str) -> None:
        """Stop supervising `name` - call this before intentionally
        stopping a watched object for any reason other than final
        shutdown (see module docstring)."""
        with self._lock:
            self._entries.pop(name, None)

    def add_listener(self, fn: Callable[[str, str], None]) -> None:
        """fn(name, event) with event in {"restarted", "gave_up"} - runs on
        this supervisor's own thread, same cross-thread rule as
        Audit/ProactiveEngine's own listeners: hop to the GUI thread
        before touching any widget."""
        if fn not in self._listeners:
            self._listeners.append(fn)

    def _emit(self, name: str, event: str) -> None:
        for fn in list(self._listeners):
            try:
                fn(name, event)
            except Exception:
                logger.exception(f"ThreadSupervisor listener raised for {name}/{event}")

    @staticmethod
    def _safe_is_alive(obj) -> bool:
        try:
            return bool(obj.is_alive())
        except Exception:
            logger.exception("is_alive() raised - treating the entry as dead")
            return False

    # ---- the actual supervision ----
    def tick(self) -> None:
        """One supervision pass over every watched entry. Safe to call
        directly (tests do) without a real background thread."""
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            self._tick_one(entry)

    def _tick_one(self, entry: _Watched) -> None:
        if entry.gave_up:
            return
        alive = self._safe_is_alive(entry.obj)
        now = time.monotonic()
        if alive:
            entry.was_alive = True
            entry.recovering = False  # back up - nothing left to be waiting out
            if (entry.restart_count > 0 and entry.last_restart_at is not None
                    and (now - entry.last_restart_at) >= config.THREAD_SUPERVISOR_HEALTHY_RESET_S):
                # Stayed up long enough since the last restart to no
                # longer count as part of the same crash loop.
                entry.restart_count = 0
                entry.last_restart_at = None
            return
        # Not alive right now. `was_alive` only ever detects the EDGE (it's
        # set False below the first time we notice); `recovering` is what
        # keeps us re-evaluating backoff/restart on every later tick while
        # the entry stays dead, since was_alive alone can't tell "still the
        # same ongoing crash" apart from "nothing's ever been alive here"
        # once it's already False either way.
        if not entry.was_alive and not entry.recovering:
            return  # never alive in the first place (e.g. deliberately disabled) - nothing to do
        entry.was_alive = False
        entry.recovering = True
        if entry.restart_count >= entry.max_restarts:
            if not entry.gave_up:
                entry.gave_up = True
                logger.error(f"{entry.name} crashed and hit its restart cap "
                             f"({entry.max_restarts}) - giving up on it for the "
                             f"rest of this session.")
                self._emit(entry.name, "gave_up")
            return
        backoff = min(config.THREAD_SUPERVISOR_MAX_BACKOFF_S,
                      config.THREAD_SUPERVISOR_BASE_BACKOFF_S * (2 ** entry.restart_count))
        if entry.last_restart_at is not None and (now - entry.last_restart_at) < backoff:
            return  # still cooling down from the last restart attempt
        if entry.last_restart_at is None:
            # First tick where this crash was noticed, and there's no
            # earlier restart to measure the cooldown from yet - start the
            # clock now instead of restarting with zero delay. The actual
            # restart happens on a later tick once `backoff` has elapsed,
            # same as it would for any later crash in this entry's loop.
            entry.last_restart_at = now
            return
        entry.restart_count += 1
        entry.last_restart_at = now
        logger.warning(f"{entry.name} is no longer running - restarting "
                        f"(attempt {entry.restart_count}/{entry.max_restarts}).")
        try:
            entry.obj.start()
            entry.was_alive = self._safe_is_alive(entry.obj)
            if entry.was_alive:
                entry.recovering = False
        except Exception:
            logger.exception(f"Restarting {entry.name} raised")
        self._emit(entry.name, "restarted")

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("ThreadSupervisor tick failed")
            self._stop_event.wait(self.interval)

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="WaveThreadSupervisor", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def status(self) -> dict:
        """A dict Task 19's later health-check work (and/or the UI) can
        render - {name: {"alive", "restart_count", "gave_up"}}."""
        with self._lock:
            entries = list(self._entries.values())
        return {
            e.name: {
                "alive": self._safe_is_alive(e.obj),
                "restart_count": e.restart_count,
                "gave_up": e.gave_up,
            }
            for e in entries
        }


thread_supervisor = ThreadSupervisor()
