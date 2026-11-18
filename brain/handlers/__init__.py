"""
brain/handlers/ — Tier 4: WaveBrain's ~50 @skill(...)-decorated tool
handlers, grouped by domain instead of living as _skill_* methods inside
one 1942+-line class. Mirrors SARA's own orchestrator/handlers/ shape in
spirit (grouped-by-domain handler files, a thin coordinator left behind
in the orchestrator itself) more than its exact file list - SARA splits
into 9 (automation, calendar, info, media, memory_intents, misc, notes,
system, timers); the original Tier 4 split landed on 8 (automation, media,
memory, reminders, calendar, vision, web, system), sized to how Wave's own
50 tools actually clustered at the time rather than a 1:1 port of SARA's
file names. Tier 5 adds a 9th - timers - once Wave actually had standalone
timers/a stopwatch for it to hold (see timers.py's own module docstring for
why that's its own file rather than folded into reminders.py). Tier 8 adds
a 10th - fun - for get_joke and the streak check-in tools, which don't fit
any of the other 9 domains either (see fun.py's own module docstring for
why these are plain raw tools here rather than routed through skills/,
despite the roadmap item behind them having been loosely labelled a
"composed skill"). Tier 16 adds an 11th - productivity - for quick notes,
to-dos, the calculator, modes, and deterministic time/date, each its own
small feature with nowhere else to live (see productivity.py's own module
docstring); summarize_my_notes/quiz_me_on land in system.py instead,
next to the search_my_notes/reindex_notes they share automation/
notes_rag.py's index with; summarize_page lands in web.py next to the
other web lookups, and play_on_spotify lands in media.py next to
play_youtube, the other "play something" command. Tier 25 adds a 12th -
avatar - for show_ui/hide_ui, the two tools that toggle the full dashboard
window now that there's an avatar companion to leave running behind it
(see avatar/control.py for the Qt-thread-safety seam those two skills
call into, and that file's own docstring for why it isn't imported here
by name any more than the others are).

Importing this package is what registers every built-in skill - each
submodule's @skill(...) decorators run at module-import time, exactly as
they did when they were methods inside groq_core.py's class body. Nothing
in this package is imported BY NAME anywhere else (brain/groq_core.py
imports the package itself, purely for this side effect - see its own
top), which is why this __init__.py re-exports nothing: there's nothing
for a caller to need from it directly, only the registration that
happens just by this package existing in the import graph at all.

brain/handlers/media.py is the one exception worth knowing up front: two
of its plain (non-@skill) functions, adjust_system_volume() and
toggle_system_mute(), ARE imported by name - by groq_core.py's own
keyword/deterministic dispatch path, which needs the exact same pycaw
volume helpers its AI-tool-calling counterpart (_skill_volume_up/_skill_
volume_down/_skill_toggle_mute, now in media.py too) does. See that
file's own module docstring for why the import runs in that direction
and not the other.
"""
from brain.handlers import automation  # noqa: F401
from brain.handlers import media  # noqa: F401
from brain.handlers import memory  # noqa: F401
from brain.handlers import reminders  # noqa: F401
from brain.handlers import calendar  # noqa: F401
from brain.handlers import vision  # noqa: F401
from brain.handlers import web  # noqa: F401
from brain.handlers import system  # noqa: F401
from brain.handlers import timers  # noqa: F401
from brain.handlers import fun  # noqa: F401
from brain.handlers import productivity  # noqa: F401
from brain.handlers import avatar  # noqa: F401
