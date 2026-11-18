"""
telemetry/trace.py — Tier 4 item #13: a lightweight, in-memory per-turn
latency/outcome tracer for WaveBrain.think()/think_stream().
================================================================================
Wave has no equivalent today of "why was that reply slow" or "how often do
requests actually need the full tool-calling loop vs. the fast
intent_router path vs. falling back to Ollama" - the only existing signal
is automation/audit.py's action_log, which records WHAT a tool did, not
how the turn as a whole was handled or how long any of it took. This module
is that missing layer: a cheap, always-on-by-default tracer that follows one
turn from the moment think()/think_stream() is called to the moment it
returns/finishes yielding, and hands a finished summary row to
telemetry/store.py for durable storage once the turn ends.

    from telemetry import trace
    trace.begin_turn(source="think")
    trace.mark("intent_router_hit", skill="volume_up")
    trace.end_turn(outcome="ok", route="intent_router")

Design, on purpose
-------------------
* One active turn per THREAD (threading.local()), not one global "current
  turn" - Wave's worker threads (ui/workers.py's AIWorker/AIStreamWorker)
  and the proactive-engine/reminder background threads can all call
  think()/think_stream() independently, and a thread-local pointer means
  none of them can stomp on another's in-flight turn without any locking
  needed for the pointer itself (the SQLite hand-off in store.py is where
  real cross-thread synchronization actually happens - see that module).
* Every public function is a no-op, and NEVER raises, when
  config.TELEMETRY_ENABLED is False or there's no active turn for the
  calling thread (a stray mark()/end_turn() with nothing begun is simply
  dropped, not an error - callers should never have to guard every call
  site with "if telemetry is on"). A tracer that can crash the turn it's
  trying to measure would be strictly worse than no tracer at all.
* Three kinds of signal, kept deliberately simple:
    - mark(stage, **meta)   - an ordered, timestamped event ("this happened")
    - add_duration(key, s)  - accumulates seconds into a numeric field
                              (llm_ms, tool_ms) across however many rounds/
                              calls a turn makes - the caller times its own
                              work and reports it, rather than this module
                              trying to wrap every possible call site
    - count(key, n=1)       - accumulates an integer field (tools_called)
  plus annotate(**fields) for one-off metadata (route, model, provider,
  skill/rule, error) set once a turn's shape is known.
* get_recent()/get_summary() read an in-memory ring buffer of the last
  _RING_SIZE finished turns - no SQLite round-trip - so a future
  diagnostics skill ("why was that slow") or run_self_diagnostics can
  answer instantly without touching the database at all. telemetry/dump.py
  and anything wanting durable, cross-restart history goes through
  telemetry/store.py / database/db_manager.py's turn_trace table instead.

Tier 19 item: log-line correlation IDs
---------------------------------------
Everything above is the heavier, config.TELEMETRY_ENABLED-gated tracer.
This module ALSO carries a second, much lighter, ALWAYS-ON mechanism -
current_turn_id()/correlate() - whose only job is answering "what turn id,
if any, should the log line this thread is about to emit be tagged with",
for settings/logging_config.py's handler filter to stamp onto every
record (%(turn_id)s). It is deliberately independent of
config.TELEMETRY_ENABLED: a team that doesn't want the ring-buffer/DB
overhead of full tracing may still want log lines from the same user-turn
groupable by grep, and the two are cheap enough to decouple.

    STT (ContinuousMicWorker)  --\\
                                   \\__ same ttfa_turn id, handed across
    GUI thread (dispatch)      --/      threads via trace.correlate(id)
                                   \\
    worker thread: think()/think_stream() (router -> LLM -> tools)
        - begin_turn()/end_turn() ADOPT whatever id an outer correlate()
          already tagged this thread with, rather than minting a second,
          different one for the same user-turn (see _correlated_id())
                                   \\
    TTS (speak_stream(), same thread for the streaming reply path,
         or a separate TTSWorker thread for the non-streaming one -
         either way wrapped in the same trace.correlate(id))

current_turn_id() is what a thread's log lines should carry RIGHT NOW:
an active begin_turn()...end_turn() pair on THIS thread wins (most
specific); otherwise whatever an enclosing correlate() tagged this thread
with (a thread that is only ACTING ON BEHALF of a turn it didn't itself
begin - STT before think() is ever called, TTS after it already
returned); otherwise None, which the logging filter renders as "-".
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterator, List, Optional

from settings.config import config

logger = logging.getLogger("telemetry.trace")

_RING_SIZE = 50
_ring: Deque[Dict[str, Any]] = deque(maxlen=_RING_SIZE)
_ring_lock = threading.Lock()
_local = threading.local()


@dataclass
class _ActiveTurn:
    turn_id: str
    source: str
    started_at: float = field(default_factory=time.monotonic)
    started_at_wall: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
    marks: List[Dict[str, Any]] = field(default_factory=list)
    durations: Dict[str, float] = field(default_factory=dict)
    counters: Dict[str, int] = field(default_factory=dict)
    meta: Dict[str, Any] = field(default_factory=dict)


def _active() -> Optional[_ActiveTurn]:
    return getattr(_local, "turn", None)


# ---------------------------------------------------- log-line correlation
def _log_stack() -> List[str]:
    stack = getattr(_local, "log_stack", None)
    if stack is None:
        stack = []
        _local.log_stack = stack
    return stack


def _correlated_id() -> Optional[str]:
    """The id (if any) an OUTER trace.correlate() context has already
    tagged this thread with - used only as begin_turn()'s default, so a
    turn that began on another thread (ttfa's cross-thread id, handed to
    this thread via correlate()) is adopted rather than a second,
    different id being minted for the same user-turn. Deliberately not
    the same lookup as current_turn_id(): an existing begin_turn_id from a
    PRIOR, already-finished call on this same (possibly reused) thread
    must never leak into a fresh, unrelated begin_turn() call."""
    stack = getattr(_local, "log_stack", None)
    return stack[-1] if stack else None


def current_turn_id() -> Optional[str]:
    """The turn id this thread's NEXT log line should be tagged with, or
    None (settings/logging_config.py's handler filter renders that as
    "-"). Always available regardless of config.TELEMETRY_ENABLED - see
    the module docstring's "Tier 19 item" section. An active
    begin_turn()...end_turn() pair on this thread wins; otherwise whatever
    an enclosing correlate() tagged this thread with."""
    begun = getattr(_local, "begin_turn_id", None)
    if begun:
        return begun
    stack = getattr(_local, "log_stack", None)
    return stack[-1] if stack else None


@contextmanager
def correlate(turn_id: Optional[str]) -> Iterator[None]:
    """Tags the calling thread's log lines with `turn_id` for the
    duration of the `with` block, then restores whatever was tagged
    before (nests correctly, per-thread). For a thread that is only
    ACTING ON BEHALF of a turn that began (or will begin) elsewhere -
    STT recognizing the utterance before think() is ever called, a
    separate TTSWorker speaking a reply after think() has already
    returned - rather than running begin_turn()/end_turn() itself, which
    already tags its own thread (see _correlated_id()).

    A no-op context when `turn_id` is falsy (ttfa/telemetry both
    disabled, so there is genuinely no id to hand this thread) - still
    yields, tags nothing, never raises."""
    if not turn_id:
        yield
        return
    stack = _log_stack()
    stack.append(turn_id)
    try:
        yield
    finally:
        # Only pop our own entry - a mismatched top (some other imbalance
        # elsewhere) is left alone rather than popping someone else's tag.
        if stack and stack[-1] == turn_id:
            stack.pop()


def begin_turn(source: str = "think", turn_id: Optional[str] = None) -> Optional[str]:
    """Starts a new active turn for the calling thread. Returns the new
    turn_id, or None when telemetry is disabled (nothing to pass around -
    every other function here already no-ops with no active turn, so a
    caller never needs to check this return value).

    `turn_id`: reuse an id telemetry/ttfa.py already generated for this
    same user-turn instead of minting a second, different one for it.
    Most callers don't need to pass this explicitly - when omitted, an id
    an enclosing trace.correlate() already tagged this thread with is
    adopted automatically (see _correlated_id()); only when NEITHER is
    given is a fresh id minted.

    Also always stamps the resulting id onto this thread's log-line
    correlation state (current_turn_id()) EVEN WHEN telemetry itself is
    disabled and this returns None - log-line correlation across
    STT/router/LLM/tools/TTS only needs an id to tag threads with, not the
    heavier ring-buffer/store machinery TELEMETRY_ENABLED otherwise gates
    (see the module docstring's "Tier 19 item" section)."""
    resolved_id = turn_id or _correlated_id() or uuid.uuid4().hex[:12]
    _local.begin_turn_id = resolved_id
    if not config.TELEMETRY_ENABLED:
        return None
    _local.turn = _ActiveTurn(turn_id=resolved_id, source=source)
    return resolved_id


def _redact_fields(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Tier 20: scrubs secret-shaped text out of string values before they
    land in the ring buffer / telemetry/store.py's DB (an LLM provider error
    that echoes back an API key is the likeliest leak path here). Only
    string values are touched - a number/bool/None passes through
    untouched, and a failure of any kind returns `fields` unchanged rather
    than dropping telemetry over it."""
    if not fields:
        return fields
    try:
        from brain import redaction
        return {
            k: (redaction.redact(v) if isinstance(v, str) else v)
            for k, v in fields.items()
        }
    except Exception:
        return fields


def mark(stage: str, **meta: Any) -> None:
    """Records one ordered, timestamped event on the active turn."""
    turn = _active()
    if turn is None:
        return
    try:
        meta = _redact_fields(meta)
        turn.marks.append({
            "stage": stage,
            "t_ms": round((time.monotonic() - turn.started_at) * 1000.0, 3),
            **({"meta": meta} if meta else {}),
        })
    except Exception:
        logger.debug("telemetry.mark failed", exc_info=True)


def add_duration(key: str, seconds: float) -> None:
    """Accumulates `seconds` into a numeric field (e.g. 'llm_ms', 'tool_ms',
    reported in milliseconds) - a caller times its own work (an LLM round, a
    tool dispatch) and reports it once done, rather than this module
    wrapping every call site itself."""
    turn = _active()
    if turn is None:
        return
    try:
        turn.durations[key] = turn.durations.get(key, 0.0) + max(0.0, seconds) * 1000.0
    except Exception:
        logger.debug("telemetry.add_duration failed", exc_info=True)


def count(key: str, n: int = 1) -> None:
    """Accumulates an integer field (e.g. 'tools_called') on the active turn."""
    turn = _active()
    if turn is None:
        return
    try:
        turn.counters[key] = turn.counters.get(key, 0) + n
    except Exception:
        logger.debug("telemetry.count failed", exc_info=True)


def annotate(**fields: Any) -> None:
    """Sets/merges one-off metadata on the active turn (route, model,
    provider, skill, rule, error, ...). Later calls overwrite earlier
    values for the same key."""
    turn = _active()
    if turn is None:
        return
    try:
        turn.meta.update(_redact_fields(fields))
    except Exception:
        logger.debug("telemetry.annotate failed", exc_info=True)


def end_turn(outcome: str = "ok", **fields: Any) -> Optional[Dict[str, Any]]:
    """Finalises the active turn: merges any last-minute `fields` (same
    shape as annotate()), computes total_ms, builds the row, appends it to
    the in-memory ring buffer, hands it to telemetry/store.py for durable
    persistence, and clears the thread's active turn. Returns the finished
    row (mainly for tests); never raises - a turn with no begin_turn() is
    silently a no-op, same as every other function here."""
    # Always clears, even when telemetry is disabled (begin_turn() always
    # sets this - see there) - so current_turn_id() correctly falls back
    # to an enclosing correlate()'s tag (if any) once this turn is done,
    # rather than this thread's log lines keeping a stale id forever.
    _local.begin_turn_id = None
    turn = _active()
    if turn is None:
        return None
    try:
        _local.turn = None
        turn.meta.update(_redact_fields(fields))
        total_ms = round((time.monotonic() - turn.started_at) * 1000.0, 3)
        row: Dict[str, Any] = {
            "turn_id": turn.turn_id,
            "started_at": turn.started_at_wall,
            "ended_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "source": turn.source,
            "route": turn.meta.get("route"),
            "outcome": outcome,
            "model": turn.meta.get("model"),
            "provider": turn.meta.get("provider"),
            "rounds": turn.meta.get("rounds"),
            "tools_called": turn.counters.get("tools_called", 0) or turn.meta.get("tools_called"),
            "total_ms": total_ms,
            "llm_ms": round(turn.durations.get("llm_ms", 0.0), 3) or None,
            "tool_ms": round(turn.durations.get("tool_ms", 0.0), 3) or None,
            "stages": turn.marks,
            "meta": {k: v for k, v in turn.meta.items()
                     if k not in ("route", "model", "provider", "rounds", "tools_called", "error")},
            "error": turn.meta.get("error"),
        }
    except Exception:
        logger.debug("telemetry.end_turn failed while building the row", exc_info=True)
        return None

    with _ring_lock:
        _ring.append(row)
    _local.last_finished = row

    try:
        from telemetry import store
        store.submit(row)
    except Exception:
        logger.debug("telemetry.end_turn: handing off to store failed", exc_info=True)
    return row


def last_finished() -> Optional[Dict[str, Any]]:
    """Tier 11: the row of the turn most recently finished ON THE CALLING
    THREAD (None if none) - lets a worker that just drained think_stream()
    learn which route its own turn took, without guessing from the shared
    ring (which other threads also append to)."""
    return getattr(_local, "last_finished", None)


def get_recent(limit: int = 20) -> List[Dict[str, Any]]:
    """Newest-first finished turns from the in-memory ring (no DB access) -
    for a diagnostics tool or test to inspect what just happened."""
    with _ring_lock:
        items = list(_ring)
    return list(reversed(items))[:max(0, limit)]


def get_summary() -> Dict[str, Any]:
    """A tiny at-a-glance rollup of the in-memory ring: turn count, outcome
    counts, and average total_ms - cheap enough to call from
    run_self_diagnostics without worrying about cost."""
    with _ring_lock:
        items = list(_ring)
    if not items:
        return {"turns": 0}
    outcomes: Dict[str, int] = {}
    routes: Dict[str, int] = {}
    total = 0.0
    for row in items:
        outcomes[row.get("outcome") or "unknown"] = outcomes.get(row.get("outcome") or "unknown", 0) + 1
        routes[row.get("route") or "unknown"] = routes.get(row.get("route") or "unknown", 0) + 1
        total += row.get("total_ms") or 0.0
    return {
        "turns": len(items),
        "avg_total_ms": round(total / len(items), 1),
        "by_outcome": outcomes,
        "by_route": routes,
    }
