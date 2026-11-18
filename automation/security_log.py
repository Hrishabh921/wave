"""
automation/security_log.py — Tier 20: a focused security event log,
separate from automation/audit.py's general "what did Wave just do" trail.

Why a second log when audit.py already exists
-----------------------------------------------
audit.py already captures every tool call Wave makes, including a
"blocked" row (status="blocked") whenever brain/groq_core.py's _guard()
denies a tool outright. That's real coverage, but it's aimed at "what did
Wave do" (one row per tool call, undo recipes, a plain result string) - it
doesn't carry the POLICY context behind a security decision (which tier,
whether the turn was tainted and by what source, which SECURITY_MODE was
active) or cover events that are security-relevant but aren't a tool call
at all (brain/redaction.py actually scrubbing something out of a log
line, or the person changing SECURITY_MODE itself in Settings).

This module is that second, narrower log: RAM-only (same "Activity page
reads from RAM, nothing touches SQLite on the GUI thread" posture as
audit.py's hot path - see that module's own docstring), capped at
_MAX_EVENTS, thread-safe. It's the data source for:
  * Settings > Security's "recent security events" list (ui/settings_panel.py)
  * Task #88's "why did you do that?" / "why did you notify me?" - a
    SecurityEvent's `reason` field IS the answer to that question for a
    guard decision, already in plain language, no re-deriving it from
    scratch at answer time.

Design, on purpose - same philosophy as every other Tier 20 module: every
public function is a no-op, and NEVER raises, on bad input. A security log
that could crash the guard decision it's trying to record would be
strictly worse than no log at all.
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from settings.config import config

logger = logging.getLogger("SecurityLog")

_MAX_EVENTS = 300

_lock = threading.Lock()
_events: Deque["SecurityEvent"] = deque(maxlen=_MAX_EVENTS)

_VALID_MODES = ("off", "standard", "strict")


@dataclass
class SecurityEvent:
    ts: str
    kind: str                       # guard_deny | guard_confirm | redaction | mode_change
    reason: str                     # plain-language "why" - already redacted
    tool: Optional[str] = None
    source: Optional[str] = None    # ai | keyword | ui | app
    tier: Optional[str] = None
    tainted: Optional[bool] = None
    taint_sources: List[str] = field(default_factory=list)
    mode: str = "standard"

    @property
    def time_text(self) -> str:
        try:
            return datetime.fromisoformat(self.ts).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            return self.ts


def effective_mode() -> str:
    """config.SECURITY_MODE, defensively re-validated (Settings can change
    it at runtime via a combo box, not just .env at startup) - falls back
    to "standard" for anything unrecognized rather than letting a bad
    value silently disable or over-tighten every guard decision."""
    mode = getattr(config, "SECURITY_MODE", "standard")
    return mode if mode in _VALID_MODES else "standard"


def set_mode(mode: str) -> str:
    """Settings > Security's mode selector calls this. Validates, applies
    immediately (no restart needed - the same pattern settings_panel.py's
    other toggles already use), logs a mode_change event, and returns the
    mode that actually took effect (so a caller passing garbage can tell
    it was ignored)."""
    resolved = mode if mode in _VALID_MODES else "standard"
    try:
        previous = effective_mode()
        config.SECURITY_MODE = resolved
        if resolved != previous:
            record("mode_change", reason=f"Security mode changed from '{previous}' to '{resolved}'.",
                   mode=resolved)
    except Exception:
        logger.debug("security_log.set_mode failed", exc_info=True)
    return resolved


def _redacted(text: str) -> str:
    try:
        from brain import redaction
        return redaction.redact(text)
    except Exception:
        return text


def record(kind: str, reason: str, tool: Optional[str] = None, source: Optional[str] = None,
           tier: Optional[str] = None, tainted: Optional[bool] = None,
           taint_sources: Optional[List[str]] = None, mode: Optional[str] = None) -> None:
    """Appends one event to the ring buffer. Never raises - a failure here
    should never take down the guard decision or redaction call that
    triggered it."""
    try:
        event = SecurityEvent(
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            kind=kind,
            reason=_redacted(reason or ""),
            tool=tool,
            source=source,
            tier=tier,
            tainted=tainted,
            taint_sources=list(taint_sources or []),
            mode=mode or effective_mode(),
        )
        with _lock:
            _events.append(event)
    except Exception:
        logger.debug("security_log.record failed", exc_info=True)


def recent(limit: int = 50) -> List[SecurityEvent]:
    """Most recent events first, newest-to-oldest, capped at `limit`."""
    try:
        with _lock:
            items = list(_events)
        items.reverse()
        return items[:max(0, limit)]
    except Exception:
        logger.debug("security_log.recent failed", exc_info=True)
        return []


def clear() -> None:
    """Settings > Privacy > "Clear All Local Data" also wipes this (RAM-only,
    so there's nothing on disk to delete, but the running list should
    empty too)."""
    try:
        with _lock:
            _events.clear()
    except Exception:
        logger.debug("security_log.clear failed", exc_info=True)
