"""
brain/handlers/memory.py - the explicit remember_this/recall_memory/
forget_memory key-value memory tools (see brain/memory_consolidation.py,
Tier 2 item #6, for the automatic/background counterpart to these).

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
from brain.skills_registry import skill
from database.db_manager import db
from telemetry import trace


@skill("remember_this", "Saves a fact or preference about the user for future sessions (e.g. their name, a favorite, a recurring habit).",
       {"key": {"type": "string", "description": "Short label for what's being remembered, e.g. 'name', 'favorite_color'."},
        "value": {"type": "string", "description": "The actual fact/preference to remember."}}, ["key", "value"])
def _skill_remember_this(brain, args: dict) -> str:
    key, value = args.get("key", "").strip(), args.get("value", "").strip()
    if not key or not value:
        return "I need both a label and a value to remember something."
    # Tier 20: provenance - the user (via the AI or the keyword path)
    # directly asked Wave to remember this, so source='explicit', and the
    # turn id ties it back to this exact turn's log lines (Tier 19's
    # correlation ids) for "why do you remember that?" to read later.
    ok = db.save_memory(key, value, source="explicit", turn_id=trace.current_turn_id())
    return f"Got it, I'll remember that {key} is {value}." if ok else "Couldn't save that to memory."


@skill("recall_memory", "Looks up something specific that was previously remembered, if it isn't already listed in the system context.",
       {"key": {"type": "string", "description": "The label used when it was remembered, e.g. 'name'."}}, ["key"])
def _skill_recall_memory(brain, args: dict) -> str:
    value = db.get_memory(args.get("key", ""))
    if value:
        db.touch_memory([args.get("key", "")])     # Tier 46: recalled = still in use (and revived if set aside)
    return f"{args.get('key')}: {value}" if value else f"I don't have anything remembered for '{args.get('key')}'."


@skill("why_remembered", "Explains where a remembered fact came from - whether you directly asked Wave "
       "to remember it or it was picked up automatically from conversation - and roughly when. Use this "
       "when asked things like 'why do you remember that' or 'how do you know that about me'.",
       {"key": {"type": "string", "description": "The label of the fact to explain, e.g. 'name'."}}, ["key"])
def _skill_why_remembered(brain, args: dict) -> str:
    key = args.get("key", "")
    info = db.get_memory_provenance(key)
    if not info:
        return f"I don't have anything remembered for '{key}'."
    source = {
        "explicit": "you directly asked me to remember it",
        "auto_consolidated": "I picked it up on my own from something you said in conversation",
    }.get(info["source"], "I don't know how this one was saved - it predates this feature")
    when = f", on {info['updated_at']}" if info.get("updated_at") else ""
    return f"'{info['key']}' is '{info['value']}' - {source}{when}."


@skill("forget_memory", "Deletes a previously remembered fact/preference.",
       {"key": {"type": "string", "description": "The label of the fact to forget."}}, ["key"],
       confirm=True)
def _skill_forget_memory(brain, args: dict) -> str:
    key = args.get("key", "")
    return f"Forgot what I knew about '{key}'." if db.delete_memory(key) else f"Couldn't forget '{key}'."
