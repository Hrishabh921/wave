"""
brain/unmatched_log.py — Tier 17: "Unmatched-utterance log plus a small
review script, so real misses become new rules."

Every message that reaches the LLM because NEITHER followup_resolver NOR
intent_router recognized it is a potential future fast-path rule. Most
logged lines are ordinary conversation that was never going to be a
command - that's expected and fine. What this log makes POSSIBLE is
noticing the other case: the same unrecognized phrasing showing up again
and again, which is exactly the signal that should turn into a new
intent_router rule in a later tier. See scripts/review_unmatched_utterances.py
for the companion tool that turns this raw log into that kind of summary.

Logged at exactly the two existing fast-path-miss fall-through points in
WaveBrain.think()/think_stream() (brain/groq_core.py) - right after
followup_resolver.resolve_followup() and intent_router.route() have both
already returned None for this turn, and right before the normal
LLM-handling code continues exactly as it already did before this tier.
No new state threaded through either method, no change to what happens
next for the turn - this module is called, and then immediately forgotten
about, same spirit as followup_resolver's own "read-only, zero changes to
the hot dispatch path" design (see that module's docstring).

Deliberately NOT correlated with automation/audit.py's dispatch log at
log time - that would mean waiting to see whether the LLM goes on to call
a tool before writing anything, which means threading new state through
every one of think()/think_stream()'s several exit points (the plan
path, the no-tool-calls path, the tool-loop path, a cancelled stream,
...) for a single diagnostic log. Far simpler and safer to log the miss
immediately, unconditionally, and let scripts/review_unmatched_utterances.py
do that correlation afterward, offline, by timestamp - see that script's
own docstring.

Local-only, append-only JSONL, best-effort: a problem writing this
diagnostic log must never be allowed to break the user's actual turn, so
log_unmatched() swallows every failure mode silently (same reasoning
automation/audit.py's own module docstring gives for never letting
logging break the feature it's logging). Raw user text already gets
written to the chat-history DB unconditionally by db.save_message() at
the top of every think()/think_stream() call, so this adds no new
category of local-only data exposure - just a second, purpose-built view
over the same already-persisted text, narrowed to the turns worth a
human's attention.
"""
import json
import os
import threading
from datetime import datetime

from settings.config import config

_LOCK = threading.Lock()
_DEFAULT_PATH = os.path.join("data", "unmatched_utterances.jsonl")


def log_path() -> str:
    """The file log_unmatched() writes to - a fixed default, or
    config.UNMATCHED_LOG_PATH when that's set (tests use this to redirect
    into a tmpdir rather than touching the real data/ folder)."""
    configured = getattr(config, "UNMATCHED_LOG_PATH", "") or ""
    return configured.strip() or _DEFAULT_PATH


def log_unmatched(user_text: str) -> None:
    """Best-effort append of one {ts, text} row. Never raises."""
    if not config.UNMATCHED_LOG_ENABLED:
        return
    if not user_text or not user_text.strip():
        return
    try:
        path = log_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "text": user_text.strip()}
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass
