"""
brain/handlers/productivity.py — Tier 16: a new, 11th handler file (see
brain/handlers/__init__.py's own docstring for the precedent - Tier 5 added
timers.py and Tier 8 added fun.py the same way, once Wave had a standalone
feature cluster with nowhere else to live). Quick notes, to-dos, the
calculator, and modes are each their own small, dedicated feature with its
own storage/backend (quick_notes.py, todo_manager.py, calculator.py,
app_control.py's mode bundles) - the same "manager/backend + thin @skill
wrappers" shape reminders.py/calendar.py already use, not something that
composes other existing tools (which is what skills/ is for - see fun.py's
own docstring on that distinction). Deterministic time/date rides along
here too since it's this tier's own roadmap bullet and has nowhere else
to live either.

get_current_time/get_current_date are worth a second look: WaveBrain.
get_dynamic_system_prompt() (brain/groq_core.py) already puts the current
time and date into every turn's system prompt, so the LLM could always
answer "what time is it" from context alone - but doing that still costs
a full cloud round-trip, and the whole point of this tier's "deterministic
time and date answers" bullet is answering those two specific questions
WITHOUT one (see brain/intent_router.py's fast-path rules for these two
tool names, added alongside this file). These tools exist so there's
still a real, callable answer on the rare miss that reaches the LLM tool-
calling path instead of the fast path - same safety net relationship
set_reminder's deterministic dateparser parsing has to the keyword
fallback that can't attempt it (see groq_core.py's own _set_reminder
docstring).

add_todo is the one skill here that reaches outside its own manager: an
optional due date becomes a real reminder too ("optional due dates that
become reminders," per the roadmap), which needs `brain` to call
brain._set_reminder() - the same dateparser-based parsing set_reminder
itself uses. TodoManager stays decoupled from brain/groq_core.py entirely
(see its own module docstring) - that linkage happens HERE, in the skill,
which already has a `brain` instance to call it on.
"""
from automation.app_control import AppController
from automation.calculator import calculate, convert_units
from automation.quick_notes import QuickNotesManager
from automation.todo_manager import TodoManager
from brain.skills_registry import skill

import datetime

# ---------------------------------------------------------------------------
# Quick notes
# ---------------------------------------------------------------------------
@skill("take_note", "Takes a quick note for the user to read back later - 'take a note: pick up dry "
       "cleaning', 'note down that the wifi password is hunter2'. Not for to-dos (use add_todo) and not "
       "for anything that needs to end up in an actual file on disk (use create_file/write_notepad).",
       {"content": {"type": "string", "description": "The note's content, word for word."}},
       ["content"])
def _skill_take_note(brain, args: dict) -> str:
    return QuickNotesManager.take(args.get("content", ""))


@skill("read_notes", "Reads back every quick note the user has saved, in the order they were taken - "
       "'what are my notes', 'read my notes back to me', 'do I have any notes'.")
def _skill_read_notes(brain, args: dict) -> str:
    return QuickNotesManager.read_all()


@skill("clear_notes", "Deletes every quick note the user has saved, all at once. Use this only when "
       "they explicitly ask to clear/erase/delete ALL their notes - there's no undo for this, and it's "
       "not for removing one specific note (there's no tool for that yet - read_notes, then take_note "
       "again with corrected content, is the current workaround).")
def _skill_clear_notes(brain, args: dict) -> str:
    return QuickNotesManager.clear()


# ---------------------------------------------------------------------------
# To-dos
# ---------------------------------------------------------------------------
@skill("add_todo", "Adds an item to the user's to-do list - 'add buy milk to my to-do list', 'to-do: "
       "call the dentist by Friday'. If a due date/time is mentioned, also sets a reminder for it - the "
       "to-do itself is still saved even if that due text doesn't parse into an actual time.",
       {"text": {"type": "string", "description": "The to-do item itself, e.g. 'buy milk'."},
        "due_text": {"type": "string", "description": "When it's due, in the user's own words (e.g. "
                     "'Friday', 'tomorrow at 5pm') - leave this out entirely if no due date was mentioned."}},
       ["text"])
def _skill_add_todo(brain, args: dict) -> str:
    text = (args.get("text") or "").strip()
    due_text = (args.get("due_text") or "").strip() or None
    result = TodoManager.add(text, due_text)
    if not due_text or not result.startswith("Added"):
        return result
    reminder_result = brain._set_reminder(text, due_text)
    if reminder_result.startswith("Got it"):
        return f"{result} I'll also remind you then."
    # 🔧 Honest scope limit (see todo_manager.py's own module docstring):
    # the to-do and the reminder are independent - saved together, not kept
    # in sync after that. The to-do is already saved either way; due_text
    # just didn't parse into a real time Wave could schedule a reminder for.
    return f"{result} (Couldn't also set a reminder for '{due_text}', so that's saved as plain text only.)"


@skill("list_todos", "Lists the user's to-do items - 'what's on my to-do list', 'show my to-dos'. "
       "Pending items only unless they ask to also see ones already marked done.",
       {"include_completed": {"type": "boolean", "description": "True to also show items already "
                     "marked done - default false (pending items only)."}})
def _skill_list_todos(brain, args: dict) -> str:
    return TodoManager.list_all(bool(args.get("include_completed", False)))


@skill("complete_todo", "Marks a to-do item as done - 'mark buy milk as done', 'I finished calling the "
       "dentist'. Matches by the item's text, not a number.",
       {"query": {"type": "string", "description": "Text identifying which to-do to mark done, e.g. "
                  "'buy milk' - a partial match is fine, it doesn't need to be exact."}},
       ["query"])
def _skill_complete_todo(brain, args: dict) -> str:
    return TodoManager.complete(args.get("query", ""))


@skill("delete_todo", "Removes an item from the user's to-do list entirely - 'delete buy milk from my "
       "to-do list', 'remove the dentist to-do'. Use complete_todo instead if they actually finished it "
       "and just want it marked done rather than erased.",
       {"query": {"type": "string", "description": "Text identifying which to-do to delete, e.g. 'buy "
                  "milk' - a partial match is fine, it doesn't need to be exact."}},
       ["query"])
def _skill_delete_todo(brain, args: dict) -> str:
    return TodoManager.delete(args.get("query", ""))


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------
@skill("calculate", "Evaluates an arithmetic expression exactly, via a real calculator rather than the "
       "LLM's own mental math - '340 minus 15% of itself', '2**10', '(4+5)*3'. Always call this for "
       "actual arithmetic instead of computing it yourself, since an LLM doing mental math can look "
       "right and occasionally isn't, with no way for the user to tell which one they got.",
       {"expression": {"type": "string", "description": "The expression to evaluate, in plain notation "
                     "(+ - * / // % **) - e.g. '340 - (15/100)*340' or '15% of 340'."}},
       ["expression"])
def _skill_calculate(brain, args: dict) -> str:
    return calculate(args.get("expression", ""))


@skill("convert_units", "Converts a value between units of length, mass, volume, or temperature - "
       "'12 miles in km', '350F to C', '2 cups in ml'.",
       {"value": {"type": "number", "description": "The numeric value to convert."},
        "from_unit": {"type": "string", "description": "The unit `value` is currently in, e.g. 'miles', 'kg', 'F'."},
        "to_unit": {"type": "string", "description": "The unit to convert to, e.g. 'km', 'lb', 'C'."}},
       ["value", "from_unit", "to_unit"])
def _skill_convert_units(brain, args: dict) -> str:
    return convert_units(args.get("value"), args.get("from_unit", ""), args.get("to_unit", ""))


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
@skill("enable_mode", "Turns on a named settings bundle - dnd, focus, gaming, sleep, study - adjusting "
       "volume/mute/brightness together as one action, e.g. 'turn on study mode', 'gaming mode on', "
       "'enable dnd'. disable_mode (or undo_last_action right after) reverts it.",
       {"name": {"type": "string", "description": "Which mode to turn on: dnd, focus, gaming, sleep, or study."}},
       ["name"])
def _skill_enable_mode(brain, args: dict) -> str:
    return AppController.enable_mode(args.get("name", ""))


@skill("disable_mode", "Turns off whichever mode is currently on, restoring volume/mute/brightness to "
       "what they were right before it was enabled - 'turn off study mode', 'mode off', 'stop focus mode'.")
def _skill_disable_mode(brain, args: dict) -> str:
    return AppController.disable_mode()


@skill("get_active_mode", "Reports which mode (if any) is currently on - 'what mode am I in', 'is focus "
       "mode on', 'am I in dnd'.")
def _skill_get_active_mode(brain, args: dict) -> str:
    return AppController.get_active_mode()


# ---------------------------------------------------------------------------
# Deterministic time/date - see this module's own docstring for why these
# exist even though get_dynamic_system_prompt() already puts both into
# every turn's system prompt.
# ---------------------------------------------------------------------------
@skill("get_current_time", "Tells the user the current time. Use this for 'what time is it', 'what's "
       "the time right now', or similar - it's deterministic (a real clock read, not a guess), so call "
       "it rather than answering from whatever time happens to be in your own system prompt.")
def _skill_get_current_time(brain, args: dict) -> str:
    return f"It's {datetime.datetime.now().strftime('%I:%M %p').lstrip('0')}."


@skill("get_current_date", "Tells the user today's date. Use this for 'what's the date', 'what day is "
       "it today', or similar - it's deterministic (a real clock read, not a guess), so call it rather "
       "than answering from whatever date happens to be in your own system prompt.")
def _skill_get_current_date(brain, args: dict) -> str:
    return f"It's {datetime.datetime.now().strftime('%A, %d %B %Y')}."
