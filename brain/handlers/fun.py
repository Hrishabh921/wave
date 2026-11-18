"""
brain/handlers/fun.py - Tier 8: a new, 10th handler file (see
brain/handlers/__init__.py's own docstring for the precedent - Tier 5
added `timers.py` the same way, once Wave had a standalone feature with
nowhere else to live). get_joke and the streak tools don't fit any of
the existing 9 domains (automation/media/memory/reminders/calendar/
vision/web/system/timers), so rather than wedge two unrelated,
lightweight "fun" features into an unrelated file, they get their own.

🔧 Judgment call worth flagging: the roadmap item behind these was
originally grouped (by me, scoping Tier 8 from the open README checklist
- the original docs this whole multi-tier project is benchmarked against
aren't recoverable, see Readme.MD's Tier 8 section) as "joke/streak/
notes_qa composed skills" - but skills/__init__.py's own docstring is
explicit that the skills/ "standalone skill" layer exists specifically
for ORCHESTRATION across several existing raw tools (daily_briefing =
weather + calendar + reminders; self_diagnostics = LLM + DB + disk +
voice). get_joke is a single curated-list lookup with nothing to
compose. The streak tools are a single cohesive feature with its own
dedicated storage (database/db_manager.py's streaks table +
automation/streak_manager.py) - the same SHAPE as reminders/calendar
(a manager/backend + thin @skill wrappers), not daily_briefing's shape
(no storage of its own, purely reads three OTHER already-existing
features). Neither one actually composes existing raw tools, so routing
either through skills/registry.py would have been forcing them to match
a loose roadmap label rather than the architecture this codebase actually
documents. They're ordinary raw @skill tools here instead - the "right
shape for most of what Wave does" per that same docstring. (notes_qa is
a separate story entirely - see search_my_notes in system.py and this
tier's Readme.MD section: that one turned out to already exist.)
"""
import random

from automation.streak_manager import StreakManager
from brain.skills_registry import skill

# Clean, family-friendly, no setup assumptions about the user's life -
# same "no network dependency" posture as skills/self_diagnostics.py's
# checks: this always works, offline, instantly. Deliberately a plain
# random.choice() each call rather than tracking "don't repeat the last
# one" - that needs state for a feature where an occasional repeat is a
# completely normal, unremarkable thing (every voice assistant's joke
# feature works this way).
_JOKES = (
    "Why don't scientists trust atoms? Because they make up everything.",
    "I told my computer I needed a break, and it said no problem - it froze immediately.",
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I would tell you a UDP joke, but you might not get it.",
    "Why did the scarecrow win an award? He was outstanding in his field.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Why don't skeletons fight each other? They don't have the guts.",
    "What do you call a fish with no eyes? A fsh.",
    "I used to be a banker, but I lost interest.",
    "Why did the invisible man turn down the job offer? He couldn't see himself doing it.",
    "What do you call a bear with no teeth? A gummy bear.",
    "I'm on a seafood diet. I see food, and I eat it.",
    "Why did the bicycle fall over? It was two-tired.",
    "What do you call a factory that makes okay products? A satisfactory.",
    "Why did the math book look sad? It had too many problems.",
    "How do you organize a space party? You planet.",
    "Why can't you give Elsa a balloon? Because she'll let it go.",
    "I tried to catch some fog earlier. I mist.",
    "What did one wall say to the other wall? I'll meet you at the corner.",
    "Why don't eggs tell jokes? They'd crack each other up.",
    "What's the best thing about Switzerland? I don't know, but the flag is a big plus.",
    "Why did the coffee file a police report? It got mugged.",
    "I used to hate facial hair, but then it grew on me.",
    "What do you call cheese that isn't yours? Nacho cheese.",
    "Why did the golfer bring two pairs of pants? In case he got a hole in one.",
)


@skill("get_joke", "Tells the user a short, clean joke. Use this for 'tell me a joke', "
       "'make me laugh', 'say something funny', or similar requests.")
def _skill_get_joke(brain, args: dict) -> str:
    return random.choice(_JOKES)


@skill("check_in_streak", "Records today's check-in for a habit the user is tracking "
       "(e.g. 'I meditated today', 'mark my workout streak', 'I read today') and reports "
       "the resulting streak. Checking in again later the same day doesn't double-count; "
       "missing a day restarts the streak at 1 without erasing the best streak reached so far.",
       {"habit": {"type": "string", "description": "The habit being checked in for, in a few words "
                 "(e.g. 'workout', 'meditation', 'reading')."}},
       ["habit"])
def _skill_check_in_streak(brain, args: dict) -> str:
    return StreakManager.check_in(args.get("habit", ""))


@skill("get_streak_status", "Reports the user's current and best streak for a habit, or "
       "every habit they're tracking if none is named. Use this for 'what's my streak', "
       "'how am I doing on my workout streak', or 'show me all my streaks'.",
       {"habit": {"type": "string", "description": "The habit to check, e.g. 'workout'. "
                 "Leave this out to list every habit the user is tracking."}})
def _skill_get_streak_status(brain, args: dict) -> str:
    return StreakManager.get_status(args.get("habit", ""))


@skill("reset_streak", "Resets (deletes) the user's streak for a habit, starting it over "
       "from nothing. Use this when the user explicitly asks to reset/clear/forget a streak "
       "- not for an ordinary missed day, which already restarts the streak on its own.",
       {"habit": {"type": "string", "description": "The habit whose streak should be reset, e.g. 'workout'."}},
       ["habit"])
def _skill_reset_streak(brain, args: dict) -> str:
    return StreakManager.reset(args.get("habit", ""))
