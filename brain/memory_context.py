"""
brain/memory_context.py — what the model is told it remembers, for one message.

Moved out of WaveBrain.get_dynamic_system_prompt() in Tier 46 so the memory
bench (bench/memory_bench.py) reads exactly the lines the model gets:
  - the remembered facts relevant to this message (db.get_relevant_memory():
    all of them while there are few; else the most similar ones, the people
    the message names - brain/people_graph.py - and facts one step away),
    each changed fact with what it was before (Tier 35);
  - the "never guess personal details" rule (Tier 35);
  - for a question about the past, the user's own dated messages from then
    or about it (brain/episodic_recall.py, Tier 46).
"""
from brain import episodic_recall
from database.db_manager import db


def fact_lines(facts) -> list:
    lines = []
    for k, v in facts:
        line = f"- {k}: {v}"
        history = db.get_memory_history(k, limit=2)
        if history:
            when = (history[0].get("changed_at") or "")[:10]
            line += f" (before: {history[0]['old_value']}" + (f", changed {when})" if when else ")")
        lines.append(line)
    return lines


def section(user_text: str, user_name: str) -> str:
    remembered = (db.get_relevant_memory(user_text) if user_text
                  else db.get_all_memory(include_archived=False))
    no_guess = (f"If {user_name} asks about something personal that isn't listed here, say you don't know "
                f"it yet (and offer to remember it) - never guess personal details.\n")
    if remembered:
        text = (f"\nTHINGS YOU'VE BEEN ASKED TO REMEMBER ABOUT {user_name.upper()}:\n"
                + "\n".join(fact_lines(remembered)) + "\n" + no_guess)
    else:
        text = "\n" + no_guess
    return text + (episodic_recall.section(user_text, user_name) if user_text else "")
