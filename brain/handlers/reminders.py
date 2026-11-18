"""
brain/handlers/reminders.py - set/list/cancel a reminder or alarm.
set_reminder is a thin wrapper over WaveBrain._set_reminder() (the
dateparser-based parsing + db.save_reminder() logic), which stays on
WaveBrain itself rather than moving here: nothing else needs it to be a
free function, and it's shared with other call sites inside groq_core.py.

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
import datetime

from brain.skills_registry import skill
from database.db_manager import db


@skill("set_reminder", "Sets a reminder or alarm that fires at a given time - a beep, a spoken announcement, and a toast notification. Use this for both 'remind me to...' requests and 'set an alarm for...' requests (an alarm is just a reminder with no message beyond the time). Also handles recurring requests like 'remind me every day to...' or 'daily at 9am, remind me to...'.",
       {"message": {"type": "string", "description": "What to remind the user about, e.g. 'check the oven', 'call mom'. For a plain alarm with nothing else said, use something like 'Alarm'."},
        "when": {"type": "string", "description": "Natural language time for when it's due, exactly as the user said it, e.g. 'in 10 minutes', 'tomorrow at 5pm', '7am', 'tonight at 9'. For a daily reminder, the time of day it should repeat at, e.g. '9am'."},
        "recurrence": {"type": "string", "enum": ["none", "daily"],
                       "description": "'daily' if the user wants this to repeat every day at that time (e.g. 'remind me every day to...', 'daily reminder to...'). Otherwise 'none' (the default) for a one-off reminder."}},
       ["message", "when"])
def _skill_set_reminder(brain, args: dict) -> str:
    return brain._set_reminder(args.get("message", ""), args.get("when", ""), args.get("recurrence", ""))


@skill("list_reminders", "Lists every reminder/alarm that hasn't fired yet, soonest first.")
def _skill_list_reminders(brain, args: dict) -> str:
    rows = db.get_upcoming_reminders()
    if not rows:
        return "You have no upcoming reminders."
    lines = [
        f"{msg} at {datetime.datetime.fromisoformat(due).strftime('%I:%M %p on %b %d')}"
        + (" (repeats daily)" if recurrence == "daily" else "")
        for _, msg, due, recurrence in rows
    ]
    return "Upcoming reminders: " + "; ".join(lines)


@skill("cancel_reminder", "Cancels one or more pending reminders/alarms that haven't fired yet.",
       {"query": {"type": "string", "description": "A keyword or phrase from the reminder's message to match (e.g. 'oven', 'call mom'), or 'all' to cancel every pending reminder."}},
       ["query"])
def _skill_cancel_reminder(brain, args: dict) -> str:
    query = args.get("query", "").strip()
    if not query:
        return "I need to know which reminder to cancel (or say 'all')."
    if query.lower() in ("all", "everything", "every reminder"):
        count = db.cancel_all_reminders()
    else:
        count = db.cancel_reminder(query)
    if count == 0:
        return f"I couldn't find a pending reminder matching '{query}'."
    return f"Cancelled {count} reminder(s)."
