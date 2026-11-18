"""
brain/handlers/timers.py — Tier 5: standalone timers and a stopwatch,
separate from reminders/calendar.

Why a 9th handler file
-----------------------
The roadmap's Tier 4 split named 8 domain files (automation, media, memory,
reminders, calendar, vision, web, system) - this is a 9th, added here in
Tier 5 rather than folded into reminders.py, because "timer" and "stopwatch"
are a genuinely different mental model from "reminder" even though a timer
happens to be IMPLEMENTED on top of the reminders table (see below): a
reminder names a point in time ("remind me at 6pm"), a timer names a
duration counting down ("set a timer for 10 minutes"), and a stopwatch
counts up with no target at all. SARA keeps exactly this as its own
orchestrator/handlers/timers.py rather than merging it into its reminder
handling, which is the precedent this follows.

Design choice: a timer IS a reminder
-------------------------------------
SARA's timers run on `threading.Timer` - a live, in-process countdown that is
gone the moment the process restarts. Wave already has a durable,
polled-from-SQLite scheduling mechanism for "fire this at time X and
announce it" - the exact same reminders table and ReminderWorker poll loop
that set_reminder uses - so a timer here is simply a reminder whose message
happens to start with a recognizable marker (_TIMER_PREFIX) and whose due
time was computed from "N seconds from now" instead of parsed from natural
language. That gets a timer real persistence across a restart (SARA's
doesn't), the same spoken/toast/beep announcement reminders already have,
and the same list/cancel plumbing, for the cost of zero new scheduling code.
The trade-off, made deliberately rather than by accident: a timer shows up
in list_reminders too (it reads the same table) - which this implementation
treats as a feature, not a leak, since "everything Wave is waiting to tell
you about" is a reasonable single list. list_timers/check_timer filter that
same table down to just the timer-marked rows so the two tools stay
useful on their own.

A stopwatch is a genuinely different shape - it counts up with no due time
and needs to expose live "elapsed so far" and a lap list, neither of which
the reminders table has a column for - so it gets its own tiny single-row
table instead (database/db_manager.py's stopwatch table + start_stopwatch/
stop_stopwatch/lap_stopwatch/stopwatch_status).

Tier 4: pulled out of brain/groq_core.py's WaveBrain class, which had grown
to 1942+ lines with 50 _skill_* methods - a "no behavior change, pure
refactor" per the roadmap. Each function here is registered via the exact
same @skill(...) decorator at module-import time (brain/skills_registry.py's
register() has always documented its handlers as plain
`fn(brain, args: dict) -> str` functions, not bound methods - both
_dispatch_tool() and _replay_confirmed() already call handler(self, args)
with the WaveBrain instance passed explicitly, so a plain top-level function
taking `brain` instead of `self` needs no change to either call site). See
brain/handlers/__init__.py for why this package exists at all.
"""
import datetime

from brain.skills_registry import skill
from database.db_manager import db

# Marks a reminders-table row as "really a timer" rather than a genuine
# user-authored reminder. Chosen to never collide with ordinary reminder
# text (set_reminder's messages come from free-form user speech, which
# essentially never starts with this exact phrase) and to double as the
# fallback label a bare "Timer done" announcement would read as.
_TIMER_PREFIX = "Timer done"


def _format_duration(seconds) -> str:
    """0 -> '0 seconds', 65 -> '1 minute 5 seconds', 3600 -> '1 hour' -
    the same kind of plain phrase a person would say out loud, used both
    when setting a timer and when reporting how much is left on one."""
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if secs and not hours:
        parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts) if parts else "0 seconds"


def _is_timer(message: str) -> bool:
    return (message or "").startswith(_TIMER_PREFIX)


def _timer_label(message: str) -> str:
    """'Timer done: pasta' -> 'pasta'; a bare 'Timer done' (no label given)
    -> 'Timer', so every timer always has SOME human-readable name to show
    in a list or ask "which one?" about."""
    if ":" in (message or ""):
        label = message.split(":", 1)[1].strip()
        if label:
            return label
    return "Timer"


def _active_timers():
    """Every not-yet-fired timer, soonest first - the timer-only slice of
    db.get_upcoming_reminders(), which returns (id, message, due_at,
    recurrence) for every untriggered reminder including ordinary ones."""
    return [r for r in db.get_upcoming_reminders() if _is_timer(r[1])]


@skill("set_timer", "Starts a countdown timer that alerts the user (a beep, spoken announcement, and toast "
       "notification) when it's done - 'set a timer for 10 minutes', 'start a 5 minute timer for the pasta'. "
       "For something that should fire at a specific clock time/date instead of after a duration, use "
       "set_reminder instead.",
       {"duration_seconds": {"type": "integer", "description": "How long the timer should run, in seconds "
                             "(e.g. 300 for 5 minutes, 5400 for 1.5 hours)."},
        "label": {"type": "string", "description": "Optional short label, e.g. 'pasta' or 'laundry'."}},
       ["duration_seconds"])
def _skill_set_timer(brain, args: dict) -> str:
    try:
        seconds = int(args.get("duration_seconds"))
    except (TypeError, ValueError):
        return "I need a valid number of seconds for the timer."
    if seconds <= 0:
        return "Give me a duration longer than zero seconds."
    if seconds > 24 * 3600:
        return "That's more than a day out - set a reminder for a specific time instead, a timer isn't meant for that."
    label = (args.get("label") or "").strip()
    message = f"{_TIMER_PREFIX}: {label}" if label else _TIMER_PREFIX
    due_at = (datetime.datetime.now() + datetime.timedelta(seconds=seconds)).isoformat()
    db.save_reminder(message, due_at)
    suffix = f" ({label})" if label else ""
    return f"Timer set for {_format_duration(seconds)}{suffix}."


@skill("list_timers", "Lists every active (not yet fired) countdown timer and how much time is left on each.")
def _skill_list_timers(brain, args: dict) -> str:
    rows = _active_timers()
    if not rows:
        return "You don't have any timers running."
    now = datetime.datetime.now()
    parts = []
    for _id, message, due, _recurrence in rows:
        try:
            remaining = (datetime.datetime.fromisoformat(due) - now).total_seconds()
        except (TypeError, ValueError):
            continue
        label = _timer_label(message)
        parts.append(f"{label}: about to go off" if remaining <= 0 else f"{label}: {_format_duration(remaining)} left")
    return "Your timers: " + "; ".join(parts) + "." if parts else "You don't have any timers running."


@skill("check_timer", "Checks how much time is left on a timer - the only one, if just one is running.",
       {"label": {"type": "string", "description": "Which timer, if more than one is running - matches by "
                  "label."}}, [])
def _skill_check_timer(brain, args: dict) -> str:
    rows = _active_timers()
    if not rows:
        return "You don't have any timers running."
    query = (args.get("label") or "").strip().lower()
    if query:
        rows = [r for r in rows if query in _timer_label(r[1]).lower()]
        if not rows:
            return f"I don't see a timer matching '{args.get('label')}'."
    elif len(rows) > 1:
        return _skill_list_timers(brain, {})
    _id, message, due, _recurrence = rows[0]
    try:
        remaining = (datetime.datetime.fromisoformat(due) - datetime.datetime.now()).total_seconds()
    except (TypeError, ValueError):
        return "I couldn't read that timer's remaining time."
    label = _timer_label(message)
    return f"{label} is about to go off." if remaining <= 0 else f"{label}: {_format_duration(remaining)} left."


@skill("cancel_timer", "Cancels a running timer, or all of them.",
       {"label": {"type": "string", "description": "Which timer to cancel, by label. Omit (or say 'all') to "
                  "cancel every timer."}}, [])
def _skill_cancel_timer(brain, args: dict) -> str:
    rows = _active_timers()
    if not rows:
        return "You don't have any timers running."
    query = (args.get("label") or "").strip().lower()
    targets = rows if (not query or query == "all") else [r for r in rows if query in _timer_label(r[1]).lower()]
    if not targets:
        return f"I don't see a timer matching '{args.get('label')}'."
    cancelled = sum(1 for r in targets if db.cancel_reminder_by_id(r[0]))
    return f"Cancelled {cancelled} timer{'s' if cancelled != 1 else ''}."


@skill("start_stopwatch", "Starts a stopwatch counting up from zero.")
def _skill_start_stopwatch(brain, args: dict) -> str:
    if db.start_stopwatch():
        return "Stopwatch started."
    return "Your stopwatch is already running - stop it first, or use lap_stopwatch to mark a lap."


@skill("stop_stopwatch", "Stops the stopwatch and reports the total elapsed time.")
def _skill_stop_stopwatch(brain, args: dict) -> str:
    elapsed = db.stop_stopwatch()
    if elapsed is None:
        return "Your stopwatch isn't running."
    return f"Stopwatch stopped at {_format_duration(elapsed)}."


@skill("lap_stopwatch", "Records a lap on the running stopwatch and reports the elapsed time at this lap.")
def _skill_lap_stopwatch(brain, args: dict) -> str:
    elapsed = db.lap_stopwatch()
    if elapsed is None:
        return "Your stopwatch isn't running - start it first."
    n = len(db.stopwatch_status()["laps"])
    return f"Lap {n}: {_format_duration(elapsed)}."


@skill("stopwatch_status", "Reports whether the stopwatch is running and the current/final elapsed time, "
       "without stopping it.")
def _skill_stopwatch_status(brain, args: dict) -> str:
    status = db.stopwatch_status()
    if status["elapsed_s"] is None:
        return "Your stopwatch hasn't been started."
    state = "running" if status["running"] else "stopped"
    laps = f", {len(status['laps'])} lap{'s' if len(status['laps']) != 1 else ''} recorded" if status["laps"] else ""
    return f"Stopwatch is {state} at {_format_duration(status['elapsed_s'])}{laps}."
