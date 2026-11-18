"""
automation/quick_notes.py — Tier 16: "take a note", "read my notes",
"clear my notes".

A lightweight, DB-backed scratchpad - deliberately NOT automation/
notes_rag.py, which indexes real .txt/.md/.pdf files already sitting on
disk for semantic search. This is the other half of the gap the roadmap
calls out: "Wave has no to-do list at all; write_notepad just types into
Notepad" applies just as much to quick notes - before this, the only way
to "take a note" by voice was dictating straight into the Notepad app,
with nothing Wave itself could read back later. These notes live only in
database/db_manager.py's quick_notes table (see its own comment for why
that's a separate table from note_chunks) and are read/cleared from there,
nothing touches the filesystem.

Same stateless, classmethod-based "backend" shape as StreakManager/
CalendarManager/AppController - every public method returns a plain
string meant to be handed straight back to the model as a tool result.
No audit/undo wiring here, matching StreakManager/reminders/calendar's own
precedent: automation/undo.py's docstring scopes the audit/undo timeline
to "what Wave does to your files [and system]", and a quick note is
exactly as reversible (or not) as a reminder or a streak check-in - none
of which go through it either.
"""
import logging

from database.db_manager import db

logger = logging.getLogger("QuickNotes")


class QuickNotesManager:
    @staticmethod
    def take(content: str) -> str:
        content = (content or "").strip()
        if not content:
            return "What would you like me to note down?"
        if not db.add_quick_note(content):
            return "Couldn't save that note - something went wrong writing to the database."
        return f"Noted: \"{content}\"."

    @staticmethod
    def read_all() -> str:
        rows = db.list_quick_notes()
        if not rows:
            return "You don't have any notes yet."
        lines = [f"{i}. {content}" for i, (_id, content, _created) in enumerate(rows, start=1)]
        return f"You have {len(rows)} note(s):\n" + "\n".join(lines)

    @staticmethod
    def clear() -> str:
        count = db.clear_quick_notes()
        if count == 0:
            return "You don't have any notes to clear."
        return f"Cleared {count} note{'s' if count != 1 else ''}."
