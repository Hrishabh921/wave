"""
skills/streak.py — "what's my streak" / "kitne din se baat kar raha hoon".

Reads the counters database/db_manager.py's record_interaction_for_streak()
keeps updated on every saved message (once per calendar day, local time -
see that method's own docstring for the exact day-gap rules). This skill
is read-only: it never writes anything itself, it only reports what's
already there.
"""
import logging

from database.db_manager import db

logger = logging.getLogger("Streak")


def run_streak() -> str:
    try:
        current = int(db.get_stat("streak_current", "0") or "0")
        longest = int(db.get_stat("streak_longest", "0") or "0")
    except Exception as e:
        logger.error(f"Failed to read streak stats: {e}")
        return "I couldn't check your streak right now."

    if current <= 0:
        return "You don't have an active streak yet - talk to me again tomorrow to start one!"
    if current >= longest:
        return f"You're on a {current}-day streak - that's your longest one yet!"
    return f"You're on a {current}-day streak. Your longest ever is {longest} days."
