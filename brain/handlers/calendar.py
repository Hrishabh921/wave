"""
brain/handlers/calendar.py - create/list/cancel calendar events, all thin
wrappers over automation/calendar_manager.py; plus (Tier 18) the calendar
SYNC tools - thin wrappers over automation/calendar_sync.py, which pulls
Google Calendar/ICS-feed events into this same local calendar.

Tier 4: pulled out of brain/groq_core.py's WaveBrain class, which had grown to
1942+ lines with 50 _skill_* methods - a "no behavior change, pure refactor" per
the roadmap. Each function here is registered via the exact same @skill(...)
decorator at module-import time (brain/skills_registry.py's register() has
always documented its handlers as plain `fn(brain, args: dict) -> str` functions,
not bound methods - both _dispatch_tool() and _replay_confirmed() already call
handler(self, args) with the WaveBrain instance passed explicitly, so moving a
method out of the class into a plain top-level function taking `brain` instead of
`self` needed no change to either call site). See brain/handlers/__init__.py for
why these 8 files, and groq_core.py's own top for why it still imports this
package even though nothing in it is referenced by name.
"""
from automation.calendar_manager import CalendarManager
from automation.calendar_sync import CalendarSync
from brain.skills_registry import skill


@skill("create_calendar_event", "Creates a calendar event/appointment/meeting at a specific time - for actual "
       "scheduled events with a start time (and optionally a duration/location), not a one-off alarm/nudge "
       "(use set_reminder for those).",
       {"title": {"type": "string", "description": "Short name of the event, e.g. 'Team standup', 'Dentist appointment'."},
        "when": {"type": "string", "description": "When it starts, in the user's own words (e.g. 'tomorrow at 3pm', "
                 "'next Monday 10am') - passed through to dateparser exactly as said, same as set_reminder's `when`."},
        "duration_minutes": {"type": "integer", "description": "How long it lasts, in minutes. Defaults to 60 if not given."},
        "location": {"type": "string", "description": "Where it is, if mentioned (e.g. 'Zoom', 'Office')."},
        "recurrence": {"type": "string", "enum": ["none", "daily", "weekly"],
                       "description": "'daily' or 'weekly' if the user wants this event to repeat (e.g. 'every day', "
                       "'every Monday', 'weekly standup'). Otherwise 'none' (the default) for a one-off event."}},
       ["title", "when"])
def _skill_create_calendar_event(brain, args: dict) -> str:
    return CalendarManager.create_event(
        args.get("title", ""), args.get("when", ""),
        args.get("duration_minutes"), args.get("location", ""),
        args.get("recurrence", ""),
    )


@skill("list_calendar_events", "Lists calendar events for a day or range - 'today', 'tomorrow', 'this week', "
       "or a specific date. Defaults to today if not given. Today's events are usually already listed in the "
       "system context, so this is mainly for a different day.",
       {"when": {"type": "string", "description": "Which day/range to list, e.g. 'today', 'tomorrow', "
                 "'this week', 'next Monday'. Defaults to today if omitted."}})
def _skill_list_calendar_events(brain, args: dict) -> str:
    return CalendarManager.list_events(args.get("when", ""))


@skill("cancel_calendar_event", "Cancels one or more calendar events (not reminders/alarms - use cancel_reminder for those).",
       {"query": {"type": "string", "description": "A keyword or phrase from the event's title to match (e.g. 'dentist', 'standup')."}},
       ["query"])
def _skill_cancel_calendar_event(brain, args: dict) -> str:
    return CalendarManager.cancel_event(args.get("query", ""))


# ---- Tier 18: calendar sync (Google Calendar / ICS feeds) ----
@skill("add_ics_calendar_feed", "Registers an external calendar's .ics (iCalendar) subscribe/webcal URL so "
       "sync_calendars can pull its events into Wave's calendar - for a work calendar, a shared team calendar, "
       "or any calendar app that offers a 'subscribe via URL'/webcal link (not for Google Calendar - use "
       "setup_google_calendar for that instead).",
       {"name": {"type": "string", "description": "A short name for this feed, e.g. 'work', 'team-holidays'."},
        "url": {"type": "string", "description": "The feed's .ics/webcal URL."}},
       ["name", "url"])
def _skill_add_ics_calendar_feed(brain, args: dict) -> str:
    return CalendarSync.add_ics_feed(args.get("name", ""), args.get("url", ""))


@skill("remove_calendar_feed", "Removes a registered calendar feed (ICS or the Google Calendar connection) so "
       "it's no longer synced - already-synced events already in the calendar are left alone.",
       {"name": {"type": "string", "description": "The feed's name (as given to add_ics_calendar_feed, or "
                "'google' for the Google Calendar connection)."}},
       ["name"])
def _skill_remove_calendar_feed(brain, args: dict) -> str:
    return CalendarSync.remove_feed(args.get("name", ""))


@skill("list_calendar_feeds", "Lists every registered calendar feed (ICS and/or Google Calendar) and when each "
       "was last synced.")
def _skill_list_calendar_feeds(brain, args: dict) -> str:
    return CalendarSync.list_feeds()


@skill("sync_calendars", "Syncs every registered calendar feed (ICS and/or Google Calendar) now, pulling their "
       "events into Wave's own calendar so list_calendar_events/the daily briefing/upcoming-event nudges see "
       "them too - run this after add_ics_calendar_feed/setup_google_calendar, or whenever the user wants fresh "
       "events pulled in (e.g. 'sync my calendar').")
def _skill_sync_calendars(brain, args: dict) -> str:
    return CalendarSync.sync_all()


@skill("setup_google_calendar", "Connects Wave to the user's Google Calendar via a one-time browser sign-in/"
       "consent flow, so sync_calendars can pull its events in afterward. Needs a Google OAuth credentials.json "
       "already saved locally (see settings/config.py's GOOGLE_CALENDAR_CREDENTIALS_PATH) - if it's missing, "
       "this tells the user how to get one instead of failing silently.")
def _skill_setup_google_calendar(brain, args: dict) -> str:
    return CalendarSync.setup_google()
