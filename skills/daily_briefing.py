"""
skills/daily_briefing.py — "give me my daily briefing" / "aaj ka briefing do".

Combines three independent, already-existing lookups into one spoken
summary: current weather, today's calendar events, and today's pending
reminders. Each section is best-effort - one failing (no internet for
weather, say) doesn't blank the other two; a section with nothing to
report (e.g. no reminders today) is simply left out rather than saying
"you have no reminders" every single time.

This is exactly the kind of composition brain/skills_registry.py's raw
@skill tools are NOT meant to contain (see skills/__init__.py) - weather,
calendar events, and reminders already each have their own tool for when
the user wants just one of them; this skill's only job is combining them.
"""
import datetime
import logging

from database.db_manager import db
from web.web_ops import WebManager
from automation.calendar_manager import CalendarManager

logger = logging.getLogger("DailyBriefing")


def _weather_line() -> str:
    try:
        text = WebManager.get_weather("")
        if text and "unavailable" not in text.lower() and "offline" not in text.lower():
            return f"It's currently {text}."
    except Exception as e:
        logger.debug(f"Briefing weather lookup failed: {e}")
    return ""


def _calendar_line() -> str:
    try:
        summary = CalendarManager.list_events("today")
        # list_events() already returns a ready-to-speak sentence (or a
        # "nothing scheduled" one) - only pass it through when it actually
        # found something, so an empty day doesn't pad the briefing.
        if summary and "no events" not in summary.lower() and "nothing" not in summary.lower():
            return summary
    except Exception as e:
        logger.debug(f"Briefing calendar lookup failed: {e}")
    return ""


def _reminders_line() -> str:
    try:
        rows = db.get_upcoming_reminders()  # (id, message, due_at, recurrence)
        today = datetime.datetime.now().date()
        todays = [msg for _, msg, due, _ in rows if datetime.datetime.fromisoformat(due).date() == today]
        if not todays:
            return ""
        if len(todays) == 1:
            return f"You have one reminder today: {todays[0]}."
        return f"You have {len(todays)} reminders today: " + ", ".join(todays) + "."
    except Exception as e:
        logger.debug(f"Briefing reminders lookup failed: {e}")
        return ""


def run_daily_briefing() -> str:
    now = datetime.datetime.now()
    greeting_by_hours = {
        range(5, 12): "Good morning.", range(12, 17): "Good afternoon.", range(17, 22): "Good evening.",
    }
    hour_greeting = next((g for hours, g in greeting_by_hours.items() if now.hour in hours), "Hello.")

    sections = [s for s in (_weather_line(), _calendar_line(), _reminders_line()) if s]
    if not sections:
        return (f"{hour_greeting} I don't have anything to brief you on right now - "
                f"weather, calendar, and reminders all came back empty.")
    return hour_greeting + " " + " ".join(sections)
