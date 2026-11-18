"""
brain/taint.py — Tier 1 item #4: per-turn taint tracking.
================================================================================
brain/content_trust.py already wraps any one piece of untrusted text (a web
result, OCR'd screen text, an attached file) so the model can tell "this is
data to read" from "this is an instruction to follow." What nothing
remembered, until now, is that it happened at all - each wrap_untrusted()
call was a closed question about one blob of text, forgotten the moment the
next line of code ran. A web page that slips past the scanner (nothing
catches everything) and nudges the model toward "and now call delete_file"
a few lines later in the same reply looked, to _guard(), identical to the
user having typed that themselves.

This module is the fix: a thread-local flag that says "untrusted content
was read THIS TURN," so brain/groq_core.py's _guard() can be more
cautious for the rest of a turn that touched something it didn't fully
trust - not just about the one piece of text that triggered it.

    from brain import taint
    taint.clear_turn()                                  # once, at turn start
    taint.mark_turn("a web search", flagged=False)       # inside wrap_untrusted()
    if taint.turn_tainted(): ...                         # inside _guard()

Design, on purpose - same shape as telemetry/trace.py, deliberately NOT
SARA's lock+generation-dict version of the same idea (SARA's taint.py
needs that because it supports a web UI thread reading another thread's
in-flight turn; Wave has no such cross-thread read today, so the simpler
pattern Wave already uses for telemetry is the right one to reuse here,
not something to copy for its own sake):
* One active turn's taint state per THREAD (threading.local()), not one
  global flag - Wave's worker threads (ui/workers.py's AIWorker/
  AIStreamWorker) and the proactive-engine/reminder background threads can
  all call think()/think_stream() independently, and a thread-local record
  means none of them can taint another's in-flight turn. No lock is needed
  for the record itself, for the same reason trace.py's doesn't need one -
  see that module's docstring.
* Every public function is a no-op, and NEVER raises, when
  config.RISK_TIER_GUARD_ENABLED is False or nothing has marked anything
  yet for the calling thread. A taint tracker that could crash the turn
  it's trying to protect would be strictly worse than no tracker at all.
* Deliberately NOT cleared by end_turn()/a context manager - just
  clear_turn() at the start of the next one (see WaveBrain.__init__ and
  think()/think_stream() in brain/groq_core.py). There's no "finished row"
  to build the way telemetry has one; the only thing worth keeping once a
  turn ends is a clean slate for the next one.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import List, Tuple

from settings.config import config

logger = logging.getLogger("brain.taint")

_local = threading.local()


@dataclass
class _TurnTaint:
    # (source, flagged) per wrap_untrusted() call this turn, oldest first.
    # A list, not a set: order matters for turn_sources() (most recent
    # reads most naturally in a message like "...from a web search and an
    # attached file"), and a source can legitimately appear more than once
    # in one turn (two separate web searches, say).
    touches: List[Tuple[str, bool]] = field(default_factory=list)


def _active() -> _TurnTaint:
    turn = getattr(_local, "turn", None)
    if turn is None:
        turn = _local.turn = _TurnTaint()
    return turn


def clear_turn() -> None:
    """Resets the calling thread's taint record. Call once at the start of
    a turn (think()/think_stream()) and once in WaveBrain.__init__ (so a
    freshly constructed brain - including one built fresh in every test's
    setUp() - never inherits taint some earlier, unrelated call left on
    this thread)."""
    _local.turn = _TurnTaint()


def mark_turn(source: str, flagged: bool = False) -> None:
    """Records that untrusted content from `source` was read this turn.
    Called from brain/content_trust.py's wrap_untrusted(), which already
    computes the scan result this needs - `flagged` is just that result's
    .flagged, not a second scan. A no-op when the guard feature is off, so
    nothing pays even the cost of a list append for a feature nobody
    enabled."""
    if not config.RISK_TIER_GUARD_ENABLED or not source:
        return
    try:
        _active().touches.append((source, bool(flagged)))
    except Exception:
        logger.debug("taint.mark_turn failed", exc_info=True)


def turn_tainted() -> bool:
    """True if ANY untrusted content was read this turn, flagged or not -
    this is deliberately the broad signal ("the model read something from
    the internet this turn") that brain/groq_core.py's _guard() escalates
    a normally-auto-run tool to confirm-required over. turn_flagged()
    below is the narrower, stronger signal."""
    return bool(_active().touches)


def turn_flagged() -> bool:
    """True if at least one of this turn's untrusted reads was flagged by
    content_trust.scan() as actually looking like an injection attempt -
    a stronger, rarer signal than turn_tainted(). Not currently used to
    change _guard()'s decision (T3+tainted already denies regardless), but
    kept available: a future, sharper policy ("deny only when flagged, not
    just tainted") is a one-line change in _guard() rather than a new
    module."""
    return any(flagged for _, flagged in _active().touches)


def turn_sources() -> Tuple[str, ...]:
    """Which sources contributed to this turn's taint, in the order they
    were read, de-duplicated (keeping the first occurrence) so a turn with
    two web searches reports "a web search" once, not twice. Used to build
    a specific, honest deny/confirm message instead of a vague "something
    untrusted" - see brain/confirm_gate.py's denied_message()."""
    seen: List[str] = []
    for source, _flagged in _active().touches:
        if source not in seen:
            seen.append(source)
    return tuple(seen)
