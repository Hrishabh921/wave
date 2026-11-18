"""
telemetry/ttfa.py — Tier 11: time-to-first-audio (TTFA)
=========================================================

How long after you stop talking does Wave start answering out loud? SARA
wrote a protocol for measuring that and never filled in the table. This
module measures it on every voice turn, always (no special build), and
keeps the numbers so they can be published - see docs/TTFA_PROTOCOL.md and
`python -m telemetry.ttfa_report`.

One turn at a time (Wave answers one thing at a time), so this is a single
"active turn" behind a lock rather than telemetry/trace.py's per-thread
model - the marks for one voice turn come from four different threads (the
mic loop, the main window, the reply worker, the TTS speaker).

    from telemetry import ttfa
    ttfa.tracker.begin("voice", speech_end=t.speech_end, captured_at=t.captured_at,
                       recognized_at=t.recognized_at, heard=str(t))
    ttfa.tracker.mark("first_token")          # first reply text
    # voice/tts.py calls tracker.on_audio_start() when audio starts
    ttfa.tracker.finish("ok")

Timeline of a voice turn (all relative to speech_end, in ms):

    speech_end ── capture_end ── recognized ── dispatched ── first_token ── first_audio
    (you stop)    (silence       (transcript)  (sent to the  (first text   (first sound
                   timeout over)                brain)        of the reply) out of Wave)

    ttfa_ms = first_audio - speech_end

speech_end is an estimate: the recognizer only knows you stopped once its
silence timeout has passed, so capture_end - pause_threshold is used.
"first_audio" is the moment audio is handed to the output device; the
device's own buffering (typically 10-50 ms) comes on top and isn't
measurable from software.

Every public function is a no-op that never raises when TELEMETRY_ENABLED
is off or there is no active turn, the same rule telemetry/trace.py
follows: a tracer that can break the turn it measures is worse than none.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from settings.config import config

logger = logging.getLogger("telemetry.ttfa")

STAGES = ("capture_end", "recognized", "dispatched", "first_token", "first_audio",
          "audio_stopped", "reply_done")
SOURCES = ("voice", "typed", "wake", "stop")
_LOG_MAX_BYTES = 2 * 1024 * 1024


def _enabled() -> bool:
    return bool(getattr(config, "TELEMETRY_ENABLED", True)) and bool(getattr(config, "TTFA_ENABLED", True))


def default_log_path() -> str:
    """Next to the database - the per-machine data folder (not the
    telemetry/ source package, which is what DATA_DIR/telemetry would be
    when running from source). TTFA_LOG_PATH overrides it (read on every
    call, so the test suite can point it at a temp file)."""
    override = (os.getenv("TTFA_LOG_PATH") or "").strip()
    return override or os.path.join(config.DB_DIR, "ttfa.jsonl")


class _Turn:
    __slots__ = ("turn_id", "source", "t0", "wall", "marks", "meta")

    def __init__(self, source: str, t0: float, meta: Dict[str, Any]):
        self.turn_id = uuid.uuid4().hex[:12]
        self.source = source
        self.t0 = t0
        self.wall = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self.marks: Dict[str, float] = {}
        self.meta = meta


class TTFATracker:
    def __init__(self, log_path: Optional[str] = None, ring_size: int = 200, persist: bool = True):
        self._lock = threading.Lock()
        self._active: Optional[_Turn] = None
        self._ring: Deque[Dict[str, Any]] = deque(maxlen=ring_size)
        self._log_path = log_path
        self._persist = persist

    # -------------------------------------------------------------- turn
    def begin(self, source: str, speech_end: Optional[float] = None, captured_at: Optional[float] = None,
              recognized_at: Optional[float] = None, heard: str = "", **meta: Any) -> Optional[str]:
        """Starts timing a turn. `speech_end`/`captured_at`/`recognized_at`
        are time.monotonic() values from voice/stt.py's Transcript (None for
        a typed message: its clock starts now). A turn still open is closed
        first as "superseded"."""
        if not _enabled():
            return None
        now = time.monotonic()
        t0 = speech_end if speech_end is not None else (captured_at if captured_at is not None else now)
        turn = _Turn(source if source in SOURCES else "voice", t0,
                     {"heard": (heard or "")[:80], **meta})
        if captured_at is not None:
            turn.marks["capture_end"] = captured_at
        if recognized_at is not None:
            turn.marks["recognized"] = recognized_at
        with self._lock:
            previous, self._active = self._active, turn
        if previous is not None:
            self._finalize(previous, "superseded")
        return turn.turn_id

    def _current(self, turn_id: Optional[str]) -> Optional[_Turn]:
        """The active turn - or None if `turn_id` is given and names a turn
        that is no longer the active one (a late mark from a superseded
        reply must not land on the next turn). Call with the lock held."""
        turn = self._active
        if turn is None or (turn_id is not None and turn.turn_id != turn_id):
            return None
        return turn

    def mark(self, stage: str, t: Optional[float] = None, turn_id: Optional[str] = None) -> None:
        """Records `stage` on the active turn - first occurrence wins."""
        with self._lock:
            turn = self._current(turn_id)
            if turn is None or stage in turn.marks:
                return
            turn.marks[stage] = time.monotonic() if t is None else t

    def annotate(self, turn_id: Optional[str] = None, **fields: Any) -> None:
        with self._lock:
            turn = self._current(turn_id)
            if turn is not None:
                turn.meta.update(fields)

    def on_audio_start(self) -> None:
        """voice/tts.py's on_playback_start listener."""
        self.mark("first_audio")

    def active_id(self) -> Optional[str]:
        with self._lock:
            return self._active.turn_id if self._active is not None else None

    def active_source(self) -> Optional[str]:
        with self._lock:
            return self._active.source if self._active is not None else None

    def finish(self, outcome: str = "ok", turn_id: Optional[str] = None, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            turn = self._current(turn_id)
            if turn is None:
                return None
            self._active = None
        turn.meta.update(fields)
        return self._finalize(turn, outcome)

    # ------------------------------------------------------------- output
    def _finalize(self, turn: _Turn, outcome: str) -> Optional[Dict[str, Any]]:
        try:
            rel = {stage: round((t - turn.t0) * 1000.0, 1) for stage, t in turn.marks.items()}
            row: Dict[str, Any] = {
                "turn_id": turn.turn_id,
                "at": turn.wall,
                "source": turn.source,
                "outcome": outcome,
                "marks_ms": dict(sorted(rel.items(), key=lambda kv: kv[1])),
                "ttfa_ms": rel.get("first_audio"),
                "first_token_ms": rel.get("first_token"),
                "stop_ms": rel.get("audio_stopped"),
                "stt_ms": (round((turn.marks["recognized"] - turn.marks["capture_end"]) * 1000.0, 1)
                           if "recognized" in turn.marks and "capture_end" in turn.marks else None),
                **{k: v for k, v in turn.meta.items() if v not in (None, "")},
            }
        except Exception:
            logger.debug("ttfa: building the row failed", exc_info=True)
            return None
        with self._lock:
            self._ring.append(row)
        if self._persist:
            self._append_to_log(row)
        return row

    def _append_to_log(self, row: Dict[str, Any]) -> None:
        path = self._log_path or default_log_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path) and os.path.getsize(path) > _LOG_MAX_BYTES:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("ttfa: couldn't append to %s", path, exc_info=True)

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = list(self._ring)
        return list(reversed(rows))[:max(0, limit)]

    def clear(self) -> None:
        with self._lock:
            self._ring.clear()
            self._active = None


def percentile(values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile (pct 0-100) - no numpy needed."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, math.ceil(pct / 100.0 * len(vals)) - 1))
    return vals[k]


tracker = TTFATracker()
