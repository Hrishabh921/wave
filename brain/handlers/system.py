"""
brain/handlers/system.py - status/introspection (hardware info, the
clipboard, which custom skills loaded), the composed daily_briefing/
run_self_diagnostics skills (skills/registry.py does the actual multi-step
work - see skills/__init__.py for why that split exists), and local notes
search/reindexing.

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
from monitoring.sys_monitor import SystemMonitor
from automation.app_control import AppController
from automation.backends import get_backend
from automation.backends.base import NotSupported
from automation.notes_rag import notes_index
from automation import security_log
from settings.config import config

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False


@skill("daily_briefing", "Gives the user a combined briefing: current weather, today's calendar events, and today's pending reminders, all in one spoken summary. Use this for 'give me my briefing', 'what's my day look like', 'good morning' style requests - not for a single piece of that (use get_weather/list_calendar_events/list_reminders directly for those).")
def _skill_daily_briefing(brain, args: dict) -> str:
    from skills import registry as standalone_skills
    return standalone_skills.run("daily_briefing")


@skill("run_self_diagnostics", "Checks whether Wave's core systems (the LLM provider, the database, disk space, and the voice/speech stack) are actually working right now, and reports what it found. Use this when the user asks something like 'is everything working', 'run a self check', 'why isn't voice working', or 'diagnose yourself'.")
def _skill_run_self_diagnostics(brain, args: dict) -> str:
    from skills import registry as standalone_skills
    return standalone_skills.run("self_diagnostics")


@skill("list_my_skills", "Lists the custom skills the user dropped into Wave's user_skills folder: which are loaded and which failed to load (with the reason).")
def _skill_list_my_skills(brain, args: dict) -> str:
    from brain.skill_loader import get_loader
    st = get_loader().status()
    loaded = [f"{f} ({', '.join(names)})" for f, names in st["loaded"].items()]
    problems = [f"{f}: {why}" for f, why in st["errors"].items()]
    if not loaded and not problems:
        return f"No custom skills yet. Drop a .py file into {st['directory']} and I'll pick it up without a restart."
    out = ("Loaded: " + "; ".join(loaded) + ". ") if loaded else ""
    return (out + ("Not loaded: " + "; ".join(problems) + ".") if problems else out).strip()


@skill("get_pc_info", "Gets hardware/system information about this PC (CPU, RAM, OS, etc).")
def _skill_get_pc_info(brain, args: dict) -> str:
    return SystemMonitor.get_hardware_info()


@skill("get_clipboard", "Reads the current contents of the clipboard.")
def _skill_get_clipboard(brain, args: dict) -> str:
    if not HAS_PYPERCLIP:
        return "Clipboard reading needs the 'pyperclip' package installed."
    clip_content = pyperclip.paste()
    return f"Clipboard contains: {clip_content}" if clip_content else "The clipboard is empty."


@skill("search_my_notes", "Searches the user's own text/markdown/PDF notes (Desktop, Documents, Downloads, "
       "and any configured notes folder) and answers from what they actually say. Use this whenever the user "
       "asks you to check, search, or answer from 'my notes'/'my files' - never say you can't access their files.",
       {"question": {"type": "string", "description": "What to look for or ask, in the user's own words, e.g. "
                     "'what did I write about the Johnson project' or 'find my wifi password note'."}},
       ["question"])
def _skill_search_my_notes(brain, args: dict) -> str:
    if not config.NOTES_RAG_ENABLED:
        return "Notes search is turned off (NOTES_RAG_ENABLED=false)."
    return notes_index.search(args.get("question", ""))


@skill("reindex_notes", "Re-scans the user's notes folders for new/changed/deleted files and refreshes the "
       "search_my_notes index. Only call this if the user explicitly asks to refresh/reindex/rescan their notes.")
def _skill_reindex_notes(brain, args: dict) -> str:
    if not config.NOTES_RAG_ENABLED:
        return "Notes search is turned off (NOTES_RAG_ENABLED=false)."
    return notes_index.reindex_all()


# Tier 16: "notes study tools" - summarize_my_notes and quiz_me_on are both
# thin wrappers around the exact same notes_index.search() search_my_notes
# already uses, just with a higher top_k (a summary/quiz needs more source
# material than answering one specific question does) and a description
# that tells the model what to DO with what comes back, same division of
# labor notes_index.search()'s own docstring already documents: it returns
# "a tool result the model can quote/summarize from" - the model is what
# turns those excerpts into an actual summary or quiz questions, not this
# function.
@skill("summarize_my_notes", "Summarizes what the user's own indexed notes say about a topic - "
       "'summarize my notes on X'. Retrieves the relevant excerpts for you to condense into an actual "
       "summary; don't add anything beyond what the excerpts actually say.",
       {"topic": {"type": "string", "description": "What to summarize, e.g. 'the Johnson project' or "
                     "'linear algebra'."}},
       ["topic"])
def _skill_summarize_my_notes(brain, args: dict) -> str:
    if not config.NOTES_RAG_ENABLED:
        return "Notes search is turned off (NOTES_RAG_ENABLED=false)."
    return notes_index.search(args.get("topic", ""), top_k=8)


@skill("quiz_me_on", "Quizzes the user on a topic using their own indexed notes - 'quiz me on X'. "
       "Retrieves the relevant excerpts; write 2-4 quiz questions testing what the excerpts actually "
       "say (not general knowledge), ask one at a time, and tell the user whether each answer is right "
       "before moving on.",
       {"topic": {"type": "string", "description": "What to be quizzed on, e.g. 'the Johnson project' "
                     "or 'linear algebra'."}},
       ["topic"])
def _skill_quiz_me_on(brain, args: dict) -> str:
    if not config.NOTES_RAG_ENABLED:
        return "Notes search is turned off (NOTES_RAG_ENABLED=false)."
    return notes_index.search(args.get("topic", ""), top_k=8)


# Tier 14: system info. All plain reads (T0, brain/tiers.py) - same
# un-audited style get_pc_info/get_clipboard above already use, since a
# lookup like this has nothing for an "undo" to mean and nothing worth a
# row in the "what did Wave just do" history either.
@skill("get_uptime", "Tells the user how long this PC has been running since its last boot.")
def _skill_get_uptime(brain, args: dict) -> str:
    return SystemMonitor.get_uptime()


@skill("get_local_ip", "Tells the user this PC's local network IP address (dashboard-only before Tier 14).")
def _skill_get_local_ip(brain, args: dict) -> str:
    return SystemMonitor.get_local_ip()


@skill("get_gpu_status", "Reports GPU utilization and temperature (NVIDIA GPUs only, needs the optional "
       "'nvidia-ml-py' package).")
def _skill_get_gpu_status(brain, args: dict) -> str:
    return SystemMonitor.get_gpu_status()


@skill("get_cpu_temperature", "Reports the current CPU temperature, if this system exposes one (often "
       "unavailable on Windows/macOS - that's an honest platform limitation, not a bug).")
def _skill_get_cpu_temperature(brain, args: dict) -> str:
    return SystemMonitor.get_cpu_temperature()


@skill("get_top_processes", "Lists the processes using the most CPU or memory right now - 'what's slowing "
       "down my pc', 'what's using all my cpu/ram'.",
       {"count": {"type": "integer", "description": "How many to list (default 5, max 15)."},
        "sort_by": {"type": "string", "enum": ["cpu", "memory"], "description": "Rank by cpu or memory usage - default cpu."}},
       [])
def _skill_get_top_processes(brain, args: dict) -> str:
    return SystemMonitor.get_top_processes(args.get("count", 5), args.get("sort_by", "cpu"))


@skill("list_services", "Lists OS-level background services currently running (NOT regular applications) - "
       "a read-only counterpart to start_service/stop_service.")
def _skill_list_services(brain, args: dict) -> str:
    try:
        names = get_backend().list_services()
    except NotSupported as e:
        return str(e)
    if not names:
        return "No running services were found."
    shown = names[:30]
    more = f" (and {len(names) - 30} more)" if len(names) > 30 else ""
    return f"{len(names)} running service(s): " + ", ".join(shown) + more + "."


@skill("get_disk_usage", "Reports usage for every mounted drive/partition, not just the system drive "
       "(get_pc_info only covers the system drive's total capacity).")
def _skill_get_disk_usage(brain, args: dict) -> str:
    return SystemMonitor.get_disk_usage()


@skill("get_battery_status", "Reports battery charge %, plugged-in state, time remaining, and - on Windows - "
       "a best-effort battery health/wear percentage (dashboard-only before Tier 14).")
def _skill_get_battery_status(brain, args: dict) -> str:
    return SystemMonitor.get_battery_status()


@skill("get_brightness_status", "Reports the current screen brightness percentage.")
def _skill_get_brightness_status(brain, args: dict) -> str:
    return AppController.get_brightness_status()


# Tier 20 (#88): "why did you do that?" / "why did you notify me?" - both
# read automation/security_log.py's ring buffer (already populated by
# brain/groq_core.py's _guard() for every guard decision, and by
# automation/proactive.py's _emit() for every proactive nudge, both built
# earlier in this same tier) rather than re-deriving an explanation from
# scratch at answer time. Two separate tools, not one, because they answer
# two different questions a person actually asks in those words - "why did
# you just do/refuse that" is about the last TOOL decision, "why did you
# just tell me that" is about the last NOTIFICATION - and conflating them
# would mean guessing which one the person meant from a single lookup.
_GUARD_EVENT_KINDS = ("guard_deny", "guard_confirm")


@skill("why_did_you_do_that", "Explains why Wave just asked for confirmation before an action, or refused "
       "one outright - use this for 'why did you ask me that', 'why won't you do that', 'why did you need "
       "to confirm'. Looks at the most recent such decision, not a specific past one.")
def _skill_why_did_you_do_that(brain, args: dict) -> str:
    for event in security_log.recent(limit=50):
        if event.kind in _GUARD_EVENT_KINDS:
            return event.reason
    return "I haven't had to ask for confirmation or refuse anything recently, so there's nothing to explain."


@skill("why_did_you_notify_me", "Explains why Wave just spoke up or notified the user unprompted (a battery "
       "warning, an upcoming event, a download finishing, and so on) - use this for 'why did you say that', "
       "'why did you just tell me that', 'what was that notification about'. Looks at the most recent such "
       "nudge, not a specific past one.")
def _skill_why_did_you_notify_me(brain, args: dict) -> str:
    for event in security_log.recent(limit=50):
        if event.kind == "proactive_notify":
            return event.reason
    return "I haven't notified you about anything recently, so there's nothing to explain."
