"""
automation/workflow_scheduler.py — Tier 18: time-triggered workflows.

Before this, a workflow (automation/... no - brain/handlers/automation.py's
create_workflow/run_workflow) could only ever be replayed on demand - by
voice/chat, or the Workflows page's "Run Now" button. Readme.MD's own
"Known limitations" section named this gap explicitly: "no 'run this
workflow every morning at 7' yet". This module is what closes it.

Mirrors CalendarManager's own split exactly (see that file's own
docstring): this class owns the actual LOGIC (parsing "when", walking a
recurring schedule's next_run_at forward past `now` on catch-up, same
algorithm db_manager.py's complete_or_reschedule_reminder already uses for
daily reminders), and database/db_manager.py's workflow_schedules methods
are plain storage underneath it - same shape as calendar_events is to
CalendarManager, or streaks is to StreakManager.

WorkflowScheduler is a stateless, classmethod-based backend (same shape as
CalendarManager/StreakManager) - schedule_workflow/list_schedules/
cancel_schedule return plain strings meant to be handed straight back to
the model as a tool result. get_due_schedules() is the one exception -
it's called by automation/proactive.py's _check_scheduled_workflows() each
tick, not by the model, so it returns workflow NAMES (plain data), not a
sentence.

Persisted (not RAM-only like most of automation/proactive.py's own state)
because the roadmap's Tier 18 "Done when" explicitly requires scheduled
workflows to survive a restart - a workflow set to run "every morning at
7" has to still be known about after the user closes and reopens Wave.
"""
import logging
from datetime import datetime, timedelta

from database.db_manager import db

logger = logging.getLogger("WorkflowScheduler")

try:
    import dateparser
    HAS_DATEPARSER = True
except ImportError:
    HAS_DATEPARSER = False
    logger.warning("dateparser not installed - workflow schedules will need unambiguous explicit times.")

# Mirrors automation/calendar_manager.py's _DATEPARSER_SETTINGS exactly -
# same library, same settings, so "tomorrow at 7am"/"every monday at 9"
# behave identically whether the user's scheduling a calendar event or a
# workflow.
_DATEPARSER_SETTINGS = {"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False}

_VALID_REPEATS = ("once", "daily", "weekly")
_REPEAT_DELTA = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}


def _parse_point(when_text: str):
    """A single future datetime for `when_text`, or None if it can't be
    parsed. See automation/calendar_manager.py's own _parse_point - this
    is that exact function, duplicated rather than imported, because the
    two modules are deliberately independent (calendar_manager has no
    reason to import workflow_scheduler or vice versa) and the function is
    four lines long - a shared helper module for four lines isn't worth
    the extra indirection."""
    if not when_text or not when_text.strip():
        return None
    if not HAS_DATEPARSER:
        return None
    return dateparser.parse(when_text, languages=["en", "hi"], settings=_DATEPARSER_SETTINGS)


def _normalize(name: str) -> str:
    return (name or "").strip().lower()


def _advance_past_now(next_run: datetime, repeat: str, now: datetime) -> datetime:
    """Walks `next_run` forward by `repeat`'s period until it's back in the
    future - the exact catch-up algorithm db_manager.py's
    complete_or_reschedule_reminder() already uses for daily reminders
    (see that method's own docstring), extended to also cover weekly.
    Wave being closed over one or more occurrences shouldn't leave a
    schedule permanently stuck in the past."""
    delta = _REPEAT_DELTA[repeat]
    next_run += delta
    while next_run <= now:
        next_run += delta
    return next_run


def _format_schedule_line(row) -> str:
    _id, name, time_text, repeat, next_run_at, enabled, _last_run_at = row
    try:
        when = datetime.fromisoformat(next_run_at).strftime("%a %d %b, %I:%M %p")
    except ValueError:
        when = next_run_at
    status = "" if enabled else " (disabled)"
    repeat_str = f", repeats {repeat}" if repeat != "once" else ""
    return f"'{name}' next runs {when}{repeat_str}{status}"


class WorkflowScheduler:
    @staticmethod
    def schedule_workflow(name: str, when_text: str, repeat: str = "once") -> str:
        wf_name = _normalize(name)
        if not wf_name:
            return "I need to know which workflow to schedule."
        if not db.get_workflow(wf_name):
            return f"I don't have a workflow called '{wf_name}' - create it first with create_workflow."

        repeat = (repeat or "once").strip().lower()
        if repeat not in _VALID_REPEATS:
            repeat = "once"

        start_dt = _parse_point(when_text)
        if not start_dt:
            if not HAS_DATEPARSER:
                return "Scheduling needs the 'dateparser' package installed to understand times."
            return f"I couldn't figure out when '{when_text}' means - could you give a clearer date/time?"

        schedule_id = db.save_workflow_schedule(wf_name, when_text, repeat, start_dt.isoformat())
        if not schedule_id:
            return f"Couldn't save a schedule for '{wf_name}' - something went wrong writing to the database."

        when_str = start_dt.strftime("%a %d %b, %I:%M %p")
        repeat_str = f", repeating {repeat}" if repeat != "once" else ""
        return f"Scheduled '{wf_name}' to run {when_str}{repeat_str}."

    @staticmethod
    def list_schedules() -> str:
        rows = db.list_workflow_schedules()
        if not rows:
            return "You don't have any scheduled workflows."
        lines = [_format_schedule_line(r) for r in rows]
        return f"{len(rows)} scheduled workflow(s): " + "; ".join(lines)

    @staticmethod
    def cancel_schedule(name: str) -> str:
        wf_name = _normalize(name)
        if not wf_name:
            return "I need to know which workflow's schedule to cancel."
        count = db.cancel_workflow_schedules_for(wf_name)
        if count == 0:
            return f"'{wf_name}' doesn't have any schedules to cancel."
        return f"Cancelled {count} schedule(s) for '{wf_name}'."

    @staticmethod
    def get_due_schedules() -> list:
        """Workflow names that are due RIGHT NOW, as plain strings - for
        automation/proactive.py's _check_scheduled_workflows() only, not a
        model-facing tool. Each due row is "claimed" here before it's
        returned (advanced to its next occurrence if repeating, disabled
        if 'once') so a slow or failed run on the caller's side can never
        cause the SAME occurrence to double-fire on the next tick - the
        same reasoning automation/proactive.py's other checks already
        document for their own dedup/cooldown state, just persisted here
        instead of RAM since these have to survive a restart."""
        now = datetime.now()
        try:
            due_rows = db.get_due_workflow_schedules(now.isoformat())
        except Exception:
            logger.exception("Couldn't fetch due workflow schedules")
            return []

        due_names = []
        for row in due_rows:
            schedule_id, wf_name, _time_text, repeat, next_run_at, _enabled, _last_run_at = row
            try:
                if repeat in _REPEAT_DELTA:
                    try:
                        current_next = datetime.fromisoformat(next_run_at)
                    except (TypeError, ValueError):
                        current_next = now
                    new_next = _advance_past_now(current_next, repeat, now)
                    db.advance_workflow_schedule(schedule_id, new_next.isoformat(), now.isoformat())
                else:
                    db.disable_workflow_schedule(schedule_id, now.isoformat())
            except Exception:
                logger.exception(f"Couldn't advance/disable workflow schedule {schedule_id}")
                continue
            due_names.append(wf_name)
        return due_names
