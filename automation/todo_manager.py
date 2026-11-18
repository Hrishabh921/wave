"""
automation/todo_manager.py — Tier 16: "add to my to-do list", "what's on
my to-do list", "mark ... done", "delete ... from my to-do list".

Gap analysis: "Wave has no to-do list at all; write_notepad just types
into Notepad." This is plain, dedicated storage for one (database/
db_manager.py's todos table), with the same stateless, classmethod-based
"backend" shape as StreakManager/CalendarManager/AppController.

due_text is deliberately just free text stored for display (see the
todos table's own comment in db_manager.py) - TodoManager itself never
parses it into an actual datetime or creates a reminder. That linkage
("optional due dates that become reminders", per the roadmap) happens one
layer up, in brain/handlers/productivity.py's add_todo skill, which is
the one place that already has a `brain` instance to call
brain._set_reminder() on (the same dateparser-based parsing set_reminder
itself uses) - TodoManager stays decoupled from brain/groq_core.py
entirely, same as every other manager in this package.

🔧 Honest scope limit, worth knowing: the to-do and the reminder it
creates are independent after that - completing or deleting the to-do
does NOT cancel the linked reminder, and cancelling the reminder doesn't
touch the to-do. They're created together, not kept in sync. Linking them
for real would need a foreign key + extra bookkeeping in complete_todo/
delete_todo that the roadmap's one-line item for this tier doesn't ask
for; cancel_reminder covers removing the reminder half by hand.

No audit/undo wiring, matching StreakManager/reminders/calendar's own
precedent - see quick_notes.py's module docstring for why.
"""
import logging

from database.db_manager import db

logger = logging.getLogger("TodoManager")


class TodoManager:
    @staticmethod
    def add(text: str, due_text: str = None) -> str:
        text = (text or "").strip()
        if not text:
            return "What should I add to your to-do list?"
        if db.add_todo(text, due_text) is None:
            return "Couldn't save that to-do - something went wrong writing to the database."
        return f"Added '{text}' to your to-do list."

    @staticmethod
    def list_all(include_completed: bool = False) -> str:
        rows = db.list_todos(include_completed=include_completed)
        if not rows:
            return "Nothing completed yet either." if include_completed else "Your to-do list is empty."
        lines = []
        for i, (_id, text, due_text, completed, _created) in enumerate(rows, start=1):
            tag = " (done)" if completed else ""
            due = f" - due {due_text}" if due_text and not completed else ""
            lines.append(f"{i}. {text}{due}{tag}")
        return f"{len(rows)} to-do(s):\n" + "\n".join(lines)

    @staticmethod
    def complete(query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "Which to-do should I mark as done?"
        count = db.complete_todo(query)
        if count == 0:
            return f"I couldn't find a pending to-do matching '{query}'."
        return f"Marked {count} to-do(s) matching '{query}' as done."

    @staticmethod
    def delete(query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "Which to-do should I delete?"
        count = db.delete_todo(query)
        if count == 0:
            return f"I couldn't find a to-do matching '{query}'."
        return f"Deleted {count} to-do(s) matching '{query}'."
