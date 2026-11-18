"""
automation/streak_manager.py — Tier 8: daily check-in streaks.

"I meditated today", "did my workout", "read for 20 minutes" - a simple
habit-tracking mechanic: the user checks in once per calendar day for a
named habit, and Wave tracks how many CONSECUTIVE days they've kept it up,
plus their all-time best run. Checking in again the same day doesn't
double-count; missing a day resets the current streak back to 1 (not 0 -
today's check-in still counts) while remembering the best streak reached
so far.

Mirrors CalendarManager's own split exactly: this class owns the actual
day-boundary LOGIC (same day / consecutive day / missed day), and
database/db_manager.py's streaks methods are plain storage underneath it,
same as calendar_events is to CalendarManager. Dates are stored and
compared as plain ISO *date* strings (no time-of-day component) - a
streak is inherently calendar-day-grained, unlike reminders/calendar
events which care about a specific time.

StreakManager is a stateless, classmethod-based backend (same shape as
CalendarManager/AppController/FileManager) - every public method returns a
plain string meant to be handed straight back to the model as a tool
result.
"""
import logging
from datetime import datetime, timedelta

from database.db_manager import db

logger = logging.getLogger("StreakManager")


def _normalize(habit: str) -> str:
    """Lowercased/trimmed lookup key - so 'Workout' and 'workout' (or the
    same habit said differently across two different conversations) share
    one streak instead of silently splitting into two."""
    return (habit or "").strip().lower()


def _today() -> "datetime.date":
    return datetime.now().date()


class StreakManager:
    @staticmethod
    def check_in(habit: str) -> str:
        key = _normalize(habit)
        if not key:
            return "I need to know which habit you're checking in for."
        today = _today()
        today_iso = today.isoformat()

        row = db.get_streak(key)
        if row is None:
            # First-ever check-in for this habit.
            if not db.save_streak(key, 1, 1, today_iso):
                return f"Couldn't save your check-in for '{key}' - something went wrong writing to the database."
            return f"Nice, you've started a streak for '{key}' - day 1! Check in again tomorrow to keep it going."

        _id, _habit, current_streak, best_streak, last_checkin_date = row
        last_date = None
        if last_checkin_date:
            try:
                last_date = datetime.fromisoformat(last_checkin_date).date()
            except (TypeError, ValueError):
                last_date = None

        if last_date == today:
            return (f"You've already checked in for '{key}' today - current streak: "
                    f"{current_streak} day{'s' if current_streak != 1 else ''}.")

        # Consecutive only if last_date is EXACTLY yesterday - anything
        # else (a gap of 2+ days, or no valid last_date at all, e.g.
        # corrupted/missing data) restarts the run. Computed once and
        # reused for both the save and the message below, rather than
        # re-deriving the same comparison twice and risking the two
        # falling out of sync.
        is_consecutive = last_date == today - timedelta(days=1)
        if is_consecutive:
            new_current = current_streak + 1
            new_best = max(best_streak, new_current)
        else:
            # today's check-in still counts, but the run restarts at 1.
            # best_streak is left untouched - it's an all-time record, not
            # something a reset should erase.
            new_current = 1
            new_best = best_streak

        if not db.save_streak(key, new_current, new_best, today_iso):
            return f"Couldn't save your check-in for '{key}' - something went wrong writing to the database."

        if not is_consecutive:
            msg = f"Looks like you missed a day - '{key}' streak restarts at 1."
            if best_streak > 0:
                msg += f" Your best is still {best_streak} day{'s' if best_streak != 1 else ''}."
            return msg

        msg = f"Nice - '{key}' streak extended to {new_current} day{'s' if new_current != 1 else ''}!"
        if new_current == new_best and new_current > 1:
            msg += " That's a new best!"
        return msg

    @staticmethod
    def get_status(habit: str = "") -> str:
        key = _normalize(habit)
        if key:
            row = db.get_streak(key)
            if row is None:
                return f"You haven't started a streak for '{key}' yet - check in to start one."
            _id, _habit, current_streak, best_streak, _last = row
            return (f"'{key}': current streak {current_streak} day{'s' if current_streak != 1 else ''}, "
                    f"best {best_streak} day{'s' if best_streak != 1 else ''}.")

        rows = db.list_streaks()
        if not rows:
            return "You haven't started any streaks yet - check in on a habit to start one."
        lines = [
            f"'{h}': {cur} day{'s' if cur != 1 else ''} (best {best})"
            for _id, h, cur, best, _last in rows
        ]
        return f"{len(rows)} streak(s): " + "; ".join(lines)

    @staticmethod
    def reset(habit: str) -> str:
        key = _normalize(habit)
        if not key:
            return "I need to know which habit's streak to reset."
        if db.delete_streak(key):
            return f"Reset the streak for '{key}'."
        return f"You don't have a streak for '{key}' to reset."
