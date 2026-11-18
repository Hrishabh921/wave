"""
planning/trigger.py — Tier 4 item #11: a cheap, pure, offline gate deciding
whether a request is even WORTH the extra propose_plan LLM round-trip.
================================================================================
Same reasoning as SARA's sara/core/planning/trigger.py: most turns are a
single request ("volume up", "what's the weather") the normal one-tool loop
already handles in one round - running an extra LLM call to ask "does this
need a plan?" on every single turn would double latency for the common
case for no benefit. This function is regex/keyword-only (no LLM, no I/O,
microseconds) so planning/__init__.py can cheaply skip straight past
planning for the overwhelming majority of turns.

Extended past SARA's (English-only) keyword list with the Hindi/Hinglish
connector words Wave's own golden bench set (bench/golden/intents_hi.jsonl,
intents_hinglish.jsonl) already treats as first-class - "phir"/"uske baad"
carry exactly the same "and then do this too" signal "then"/"after that" do.

This is a HINT, not a gate that blocks anything: a false negative here just
means a genuinely multi-step request gets handled by the normal loop one
round at a time instead (slower, not wrong - the loop already supports
several rounds). A false positive costs one extra propose_plan call that
planner.py/__init__.py may still decide not to use (e.g. it comes back
with a single-step plan - see try_plan_and_execute's own single-step
check). Both failure directions are cheap; this never needs to be exact.
"""
from __future__ import annotations

import re
from typing import List

# Two-plus distinct actions joined by a connector - the strongest signal.
# Kept as whole phrases (not single words like "and") since a single "and"
# shows up in ordinary requests too often to mean anything ("check email
# and calendar" is arguably still one compound lookup, not two tool steps -
# but it's cheap enough either way to just let the propose_plan call decide
# once triggered; see the module docstring on false positives being cheap).
_CONNECTORS = [
    r"\band then\b", r"\bthen also\b", r"\bafter that\b", r"\bonce (?:that'?s|that is|done)\b",
    r"\bfollowed by\b", r"\bnext,? (?:please )?\b", r"\bfirst\b.*\bthen\b",
    # Hindi / Hinglish
    r"\bphir\b", r"\buske baad\b", r"\buska baad\b", r"\btab\b.*\bphir\b",
    r"\bpehle\b.*\bphir\b", r"\bpehle\b.*\bfir\b", r"\bfir\b", r"\bor phir\b",
]

# Imperative-list shapes: numbered/bulleted/semicolon-or-comma-separated
# instructions in one message - a strong signal regardless of language.
_LIST_PATTERNS = [
    re.compile(r"(?:^|\n)\s*[1-9]\.\s"),               # "1. do X"
    re.compile(r"(?:^|\n)\s*[-*•]\s"),                  # "- do X"
]

_CONNECTOR_RE = re.compile("|".join(_CONNECTORS), re.IGNORECASE)

# A handful of verbs that, repeated 2+ times in one message, suggest
# distinct actions even with no connector word at all ("close chrome open
# spotify play lofi").
_ACTION_VERBS = re.compile(
    r"\b(open|close|play|set|create|delete|send|search|take|lock|mute|unmute|remind|schedule|"
    r"khol|band|bhejo|banao|bana do|dhoondo|dhundo)\b", re.IGNORECASE)


def should_attempt_planning(text: str, *, min_length: int = 12) -> bool:
    """True when `text` looks like it names more than one distinct action -
    worth the extra propose_plan round-trip to check. False for anything
    short or single-shot - let the normal loop handle those as it always
    has."""
    if not text or len(text.strip()) < min_length:
        return False
    if _CONNECTOR_RE.search(text):
        return True
    if any(p.search(text) for p in _LIST_PATTERNS):
        return True
    if len(_ACTION_VERBS.findall(text)) >= 2:
        return True
    return False
