"""
automation/timer_manager.py — Beat-Sara Tier 5: standalone countdown timers
and a stopwatch, distinct from brain/handlers/reminders.py's reminders.

A reminder is for a MESSAGE at a CLOCK TIME ("remind me to check the oven at
7pm") and goes through dateparser + the reminders table + ui/workers.py's
ReminderWorker (a QThread polling loop) + reminders/composer.py's LLM-reworded
wording, because that whole pipeline earns its keep for a feature meant to
survive a restart and be worth a composed, context-aware wake-up line. A
timer is for a plain DURATION ("set a 10 minute timer") with nothing to say
beyond "time's up" - no restart durability needed (if Wave wasn't running,
neither was the timer, exactly the same limitation Sara's own
sara/tools/system/timers.py accepts), so a plain in-process
threading.Timer is the right amount of machinery, not an under-use of the
reminder pipeline's.

TimerManager/StopwatchManager are classmethod-based like CalendarManager,
and for the same reason do NOT open their own automation.audit.action scope
- brain/groq_core.py's _dispatch_tool() already wraps every tool call in
one. Unlike CalendarManager, there IS small in-process state here (the live
threading.Timer handles), so it lives at module level - same shape as
automation.audit's `audit` / automation.undo's `undo_manager` singletons.

Notification on fire is deliberately modest: a best-effort OS beep
(winsound.Beep(1000, 300) - the exact call ui/main_window.py's
on_reminder_due already uses for a fired reminder) plus a log line. Routing
a fired timer through the SAME rich path reminders get (a toast + a chat
bubble + TTS, via ui/main_window.py's on_reminder_due) would need a Qt
signal crossing from this background thread onto the GUI thread, mirroring
ui/workers.py's ReminderWorker(QThread) + its reminder_due pyqtSignal - real
UI-framework wiring that has to be written and exercised inside a running
Qt app to get the thread-safety right, which this sandbox can't do blindly.
That wiring is a clearly-scoped follow-up (reuse on_reminder_due's toast/
beep/chat-bubble lines, triggered by a new signal this module would need to
emit through), not attempted here - a beep is the whole notification for
now, same as before this module existed (there was no timer at all).
"""
import itertools
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("TimerManager")

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

_timers: dict = {}          # id -> {"label", "seconds", "fires_at", "handle"}
_timers_lock = threading.Lock()
_next_id = itertools.count(1)


def _beep() -> None:
    if not HAS_WINSOUND:
        return
    try:
        winsound.Beep(1000, 300)
    except Exception as e:
        logger.debug(f"Timer beep failed (non-fatal): {e}")


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        mins, secs = divmod(seconds, 60)
        out = f"{mins} minute{'s' if mins != 1 else ''}"
        return out + (f" and {secs} second{'s' if secs != 1 else ''}" if secs else "")
    hours, rem = divmod(seconds, 3600)
    mins = rem // 60
    out = f"{hours} hour{'s' if hours != 1 else ''}"
    return out + (f" and {mins} minute{'s' if mins != 1 else ''}" if mins else "")


class TimerManager:
    @classmethod
    def set_timer(cls, seconds, label: str = "") -> str:
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return "I need a number of seconds (or a duration I've already converted to seconds) to set a timer."
        if seconds <= 0:
            return "A timer needs a duration greater than zero."
        label = (label or "").strip() or _format_duration(seconds)
        timer_id = next(_next_id)
        fires_at = datetime.now() + timedelta(seconds=seconds)

        def _fire():
            with _timers_lock:
                _timers.pop(timer_id, None)
            _beep()
            logger.info(f"Timer '{label}' (#{timer_id}) finished.")

        handle = threading.Timer(seconds, _fire)
        handle.daemon = True
        with _timers_lock:
            _timers[timer_id] = {"label": label, "seconds": seconds, "fires_at": fires_at, "handle": handle}
        handle.start()
        return f"Timer set for {_format_duration(seconds)} ({label}). I'll beep when it's done."

    @classmethod
    def cancel_timer(cls, query: str = "all") -> str:
        query = (query or "all").strip().lower()
        with _timers_lock:
            if query in ("all", "every timer", "everything"):
                matches = list(_timers.items())
            else:
                matches = [(tid, t) for tid, t in _timers.items() if query in t["label"].lower()]
            for tid, t in matches:
                t["handle"].cancel()
                _timers.pop(tid, None)
        if not matches:
            return (f"No active timers to cancel." if query in ("all", "every timer", "everything")
                    else f"I couldn't find an active timer matching '{query}'.")
        return f"Cancelled {len(matches)} timer(s)." if len(matches) > 1 else f"Cancelled the '{matches[0][1]['label']}' timer."

    @classmethod
    def list_timers(cls) -> str:
        with _timers_lock:
            items = sorted(_timers.values(), key=lambda t: t["fires_at"])
        if not items:
            return "You have no active timers."
        now = datetime.now()
        parts = []
        for t in items:
            remaining = max(0, int((t["fires_at"] - now).total_seconds()))
            parts.append(f"{t['label']}: {_format_duration(remaining)} left")
        return "Active timers: " + "; ".join(parts) + "."


class StopwatchManager:
    """One global stopwatch (same scope Sara's own start_stopwatch/
    stop_stopwatch cover) - module-level state for the same reason
    _timers above is: real, small, process-lifetime-only state."""
    _started_at: Optional[datetime] = None

    @classmethod
    def start(cls) -> str:
        if cls._started_at is not None:
            elapsed = int((datetime.now() - cls._started_at).total_seconds())
            return f"The stopwatch is already running - {_format_duration(elapsed)} so far."
        cls._started_at = datetime.now()
        return "Stopwatch started."

    @classmethod
    def stop(cls) -> str:
        if cls._started_at is None:
            return "The stopwatch isn't running."
        elapsed = int((datetime.now() - cls._started_at).total_seconds())
        cls._started_at = None
        return f"Stopwatch stopped at {_format_duration(elapsed)}."

    @classmethod
    def check(cls) -> str:
        if cls._started_at is None:
            return "The stopwatch isn't running."
        elapsed = int((datetime.now() - cls._started_at).total_seconds())
        return f"The stopwatch is at {_format_duration(elapsed)}."
