"""
telemetry/store.py — Tier 4 item #13: durable persistence for telemetry/
trace.py's finished-turn rows.
================================================================================
trace.end_turn() hands a finished row to submit() below and returns
immediately - the actual SQLite write happens on one dedicated background
daemon thread, batched, so a slow or momentarily-contended disk can never
add latency to a think()/think_stream() reply (the thing telemetry exists
to MEASURE should never be the thing it SLOWS DOWN). This mirrors why
automation/audit.py's own writes stay cheap and inline instead: those
happen through a single already-open connection-per-call and are
comparable-or-lower frequency (one audit row per TOOL call; telemetry
writes once per finished TURN, which may contain several tool calls), so
either design is defensible - this one is the one that can never regress
if that balance ever shifts (a burst of fast consecutive turns, a slow
disk, antivirus scanning the DB file, ...).

    from telemetry import store
    store.submit(row)              # called by trace.end_turn(), not directly
    store.flush(timeout=2.0)       # tests: block until the queue drains

Rows go into database/db_manager.py's turn_trace table (db.insert_traces -
see that module for the schema). The writer thread also prunes down to
TELEMETRY_MAX_ROWS periodically, same idea as action_log's AUDIT_MAX_ENTRIES.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from settings.config import config

logger = logging.getLogger("telemetry.store")

_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=max(1, config.TELEMETRY_QUEUE_MAX))
_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()
_stop = threading.Event()
_dropped = 0
_dropped_lock = threading.Lock()
_PRUNE_EVERY_N_FLUSHES = 20


def _db():
    # Imported lazily (not at module top) so importing telemetry never
    # forces database/db_manager.py to run its own import-time setup
    # before settings.config has finished configuring things - same
    # reasoning brain/groq_core.py already applies to several of its own
    # imports, just extended to this module.
    from database.db_manager import db
    return db


def _ensure_thread() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_writer_loop, name="telemetry-writer", daemon=True)
        _thread.start()


def submit(row: Dict[str, Any]) -> bool:
    """Queues `row` for the background writer; starts that thread on first
    use. Never blocks the caller - a full queue (the writer falling behind
    a sustained burst) drops the row rather than stalling whoever called
    trace.end_turn(), and is counted (see dropped_count()) rather than
    silently lost without any signal at all."""
    if not config.TELEMETRY_ENABLED:
        return False
    _ensure_thread()
    try:
        _queue.put_nowait(dict(row))
        return True
    except queue.Full:
        global _dropped
        with _dropped_lock:
            _dropped += 1
        logger.warning("telemetry queue full (%d max) - dropping a trace row (%d dropped so far)",
                       config.TELEMETRY_QUEUE_MAX, _dropped)
        return False


def dropped_count() -> int:
    with _dropped_lock:
        return _dropped


def _encode(row: Dict[str, Any]) -> Dict[str, Any]:
    """turn_trace's stages/meta columns are TEXT - json-encode the two
    fields trace.py builds as Python list/dict before handing the row to
    db.insert_traces (same convention action_log's args/result use)."""
    import json
    out = dict(row)
    for key in ("stages", "meta"):
        value = out.get(key)
        if not isinstance(value, str):
            try:
                out[key] = json.dumps(value if value is not None else ([] if key == "stages" else {}))
            except (TypeError, ValueError):
                out[key] = "[]" if key == "stages" else "{}"
    return out


def _drain_batch() -> List[Dict[str, Any]]:
    """Pulls up to TELEMETRY_BATCH_MAX items without blocking past the
    first one - called after a blocking get() already found something."""
    batch: List[Dict[str, Any]] = []
    while len(batch) < max(1, config.TELEMETRY_BATCH_MAX):
        try:
            batch.append(_queue.get_nowait())
        except queue.Empty:
            break
    return batch


def _writer_loop() -> None:
    flushes = 0
    while not _stop.is_set():
        try:
            first = _queue.get(timeout=max(0.1, config.TELEMETRY_FLUSH_INTERVAL_S))
        except queue.Empty:
            continue
        batch = [first] + _drain_batch()
        try:
            _db().insert_traces([_encode(r) for r in batch])
        except Exception:
            logger.debug("telemetry writer: insert_traces failed for a batch of %d", len(batch), exc_info=True)
        finally:
            for _ in batch:
                _queue.task_done()
        flushes += 1
        if flushes % _PRUNE_EVERY_N_FLUSHES == 0:
            try:
                _db().prune_traces(config.TELEMETRY_MAX_ROWS)
            except Exception:
                logger.debug("telemetry writer: prune_traces failed", exc_info=True)


def flush(timeout: float = 2.0) -> bool:
    """Blocks until every currently-queued row has been written (or
    `timeout` elapses). Tests use this before asserting on
    db.get_recent_traces() - production code never needs to call it."""
    _ensure_thread()
    done = threading.Event()

    def _wait():
        _queue.join()
        done.set()

    waiter = threading.Thread(target=_wait, daemon=True)
    waiter.start()
    waiter.join(timeout)
    return done.is_set()


def shutdown(timeout: float = 2.0) -> None:
    """Stops the writer thread (tests only - production lets the daemon
    thread die with the process)."""
    global _thread
    flush(timeout)
    _stop.set()
    with _thread_lock:
        if _thread is not None:
            _thread.join(timeout)
            _thread = None
