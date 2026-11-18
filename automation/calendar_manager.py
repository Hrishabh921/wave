"""
automation/calendar_manager.py — Phase 6: local calendar.

Before this, "calendar" had zero real matches anywhere in the codebase -
Wave could set a one-off reminder/alarm (brain/groq_core.py's
set_reminder) but had no concept of a scheduled EVENT: something with a
start time, usually a duration, sometimes a location, that the user wants
to see listed back ("what's on my calendar today").

This mirrors _set_reminder's own dateparser usage in brain/groq_core.py as
closely as possible - same library, same settings, same "tell the user
plainly if parsing failed" behavior - just storing to calendar_events
instead of reminders, and supporting range queries (today/tomorrow/this
week) on top of single points in time.

CalendarManager is a stateless, classmethod-based backend (same shape as
FileManager/AppController) - every method returns a plain string meant to
be handed straight back to the model as a tool result. It does NOT open
its own automation.audit.action scope: brain/groq_core.py's _dispatch_tool
already wraps every tool call in one, exactly as it does for the
inline _set_reminder helper these methods are modeled on.
"""
import logging
from datetime import datetime, timedelta

from database.db_manager import db

logger = logging.getLogger("CalendarManager")

try:
    import dateparser
    HAS_DATEPARSER = True
except ImportError:
    HAS_DATEPARSER = False
    logger.warning("dateparser not installed - calendar events will need unambiguous explicit dates/times.")

_DATEPARSER_SETTINGS = {"PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False}


def _parse_point(when_text: str):
    """A single datetime for `when_text` ('tomorrow at 3pm', 'next Monday
    10am'), or None if it can't be parsed. Mirrors _set_reminder's own
    dateparser call in brain/groq_core.py exactly, so the two tools behave
    identically for the same kind of phrase."""
    if not when_text or not when_text.strip():
        return None
    if not HAS_DATEPARSER:
        return None
    return dateparser.parse(when_text, languages=["en", "hi"], settings=_DATEPARSER_SETTINGS)


def _day_bounds(day: datetime):
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _resolve_range(when_text: str):
    """(start, end) datetimes for a list_calendar_events query. Handles the
    canned phrases a user actually says ('today', 'tomorrow', 'this week',
    'upcoming') without needing dateparser for those, then falls back to
    parsing a specific date/phrase (-> that whole day), and finally to
    today if nothing could be understood at all - list_calendar_events
    should never hard-fail just because the day was unclear."""
    now = datetime.now()
    text = (when_text or "").strip().lower()

    if not text or text in ("today", "aaj"):
        return _day_bounds(now)
    if text in ("tomorrow", "kal"):
        return _day_bounds(now + timedelta(days=1))
    if text in ("this week", "the week", "week", "next 7 days"):
        start, _ = _day_bounds(now)
        return start, start + timedelta(days=7)
    if text in ("upcoming", "all upcoming", "everything", "all"):
        start, _ = _day_bounds(now)
        return start, start + timedelta(days=365)

    parsed = _parse_point(when_text)
    if parsed:
        return _day_bounds(parsed)

    return _day_bounds(now)  # unparseable -> default to today rather than erroring


def _format_event_line(row, include_date: bool = False) -> str:
    _id, title, start_at, end_at, location, notes, recurrence = row
    try:
        start_dt = datetime.fromisoformat(start_at)
        when = start_dt.strftime("%a %d %b, %I:%M %p") if include_date else start_dt.strftime("%I:%M %p")
    except ValueError:
        when = start_at
    line = f"{when}: {title}"
    if location:
        line += f" ({location})"
    if recurrence in ("daily", "weekly"):
        line += f" (repeats {recurrence})"
    return line


class CalendarManager:
    @staticmethod
    def create_event(title: str, when_text: str, duration_minutes=None, location: str = "", recurrence: str = "") -> str:
        title = (title or "").strip()
        if not title:
            return "I need a title for the event."
        start_dt = _parse_point(when_text)
        if not start_dt:
            if not HAS_DATEPARSER:
                return "Calendar needs the 'dateparser' package installed to understand event times."
            return f"I couldn't figure out when '{when_text}' means - could you give a clearer date/time?"

        try:
            duration = int(duration_minutes) if duration_minutes not in (None, "") else 60
        except (TypeError, ValueError):
            duration = 60
        end_dt = start_dt + timedelta(minutes=max(0, duration))

        # Tier 8: mirrors _set_reminder's own recurrence normalization in
        # brain/groq_core.py exactly, just with 'weekly' also accepted -
        # see db_manager.py's calendar_events.recurrence migration comment
        # for why calendar goes one step further than reminders here.
        recurrence = (recurrence or "").strip().lower()
        recurrence = recurrence if recurrence in ("daily", "weekly") else None

        event_id = db.save_calendar_event(title, start_dt.isoformat(), end_dt.isoformat(),
                                           location or None, None, recurrence)
        if not event_id:
            return f"Couldn't save the '{title}' event - something went wrong writing to the database."

        when_str = start_dt.strftime("%a %d %b, %I:%M %p")
        loc_str = f" at {location}" if location else ""
        repeat_str = f", repeating {recurrence}" if recurrence else ""
        return f"Added '{title}' to your calendar for {when_str}{loc_str} ({duration} min){repeat_str}."

    @staticmethod
    def list_events(when_text: str = "") -> str:
        start, end = _resolve_range(when_text)
        rows = db.get_calendar_events_between(start.isoformat(), end.isoformat())
        if not rows:
            label = (when_text or "today").strip() or "today"
            return f"No calendar events found for {label}."

        span_days = (end - start).days
        lines = [_format_event_line(r, include_date=span_days > 1) for r in rows]
        return f"{len(rows)} event(s): " + "; ".join(lines)

    @staticmethod
    def cancel_event(query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "I need to know which event to cancel."
        count = db.cancel_calendar_event(query)
        if count == 0:
            return f"I couldn't find a calendar event matching '{query}'."
        return f"Cancelled {count} event(s) matching '{query}'."

    @staticmethod
    def get_events_for_prompt() -> str:
        """Compact 'HH:MM: Title (location)' lines for TODAY, for the
        ambient 'TODAY'S CALENDAR' block in get_dynamic_system_prompt() -
        same idea as that function's existing memory_section, just for
        events instead of remembered facts. '' (not a placeholder string)
        when there's nothing today, so the caller can cleanly omit the
        whole section."""
        try:
            start, end = _day_bounds(datetime.now())
            rows = db.get_calendar_events_between(start.isoformat(), end.isoformat())
        except Exception as e:
            logger.error(f"Failed to fetch today's events for the system prompt: {e}")
            return ""
        if not rows:
            return ""
        return "\n".join(f"- {_format_event_line(r)}" for r in rows)
