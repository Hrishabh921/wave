"""
voice/audio_out.py — Tier 12: one persistent output stream
============================================================

Before this, every sentence Wave spoke opened its own `sounddevice`
OutputStream and closed it again - an audible gap and a device open/close
between sentences of a streamed reply, and a far-end (echo reference)
signal for voice/aec.py that started and stopped with each sentence.

PersistentOutput keeps ONE stream open while Wave is talking (and for a
short idle time after), plays queued utterances back to back from its
callback, and feeds every block it outputs - silence included - to the
echo canceller, so the AEC sees a continuous reference.

  play(pcm, rate, stop_event, on_start) blocks until that audio has been
  played (True), was cut off by stop_event (True - an interruption is not a
  failure), or the device stopped working (False - voice/tts.py then falls
  back to its other playback paths).

The stream closes itself after IDLE_CLOSE_S without playback (so the
device isn't held open forever) and reopens on the next play(). A stream
whose callback stops being called (device unplugged) is declared dead
and reopened next time, with backoff.

Needs numpy + sounddevice; voice/tts.py only uses it when both import.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Callable, Optional

logger = logging.getLogger("voice.audio_out")

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:          # pragma: no cover
    np = None
    HAS_NUMPY = False

IDLE_CLOSE_S = 60.0
_STALL_S = 1.5


class _Item:
    __slots__ = ("pcm", "pos", "done", "started", "cancelled")

    def __init__(self, pcm):
        self.pcm = pcm
        self.pos = 0
        self.done = threading.Event()
        self.started = False
        self.cancelled = False


class PersistentOutput:
    def __init__(self, sd_module, samplerate: int = 24000, block_ms: int = 20, aec=None,
                 resample: Callable = None, clock: Callable[[], float] = time.monotonic):
        self._sd = sd_module
        self.rate = int(samplerate)
        self.blocksize = max(64, int(self.rate * block_ms / 1000))
        self._aec = aec
        self._resample = resample
        self._clock = clock
        self._lock = threading.Lock()
        self._queue: deque = deque()
        self._cur: Optional[_Item] = None
        self._stream = None
        self._last_callback = 0.0
        self._last_play = 0.0
        self._dead_until = 0.0
        self._failures = 0
        self._far_q: "queue.Queue" = queue.Queue(maxsize=256)
        self._feeder = None
        self._feeder_stop = threading.Event()
        self._idle_timer = None
        self.opens = 0

    # ------------------------------------------------------------ stream
    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def _open(self) -> bool:
        if self._stream is not None:
            return True
        if self._clock() < self._dead_until:
            return False
        try:
            stream = self._sd.OutputStream(samplerate=self.rate, channels=1, dtype="int16",
                                           blocksize=self.blocksize, callback=self._callback)
            stream.start()
        except Exception as e:
            self._failures += 1
            self._dead_until = self._clock() + min(30.0, 2.0 ** self._failures)
            logger.warning(f"Persistent output stream couldn't open ({e}) - per-utterance playback is used")
            return False
        self._stream = stream
        self._last_callback = self._clock()
        self._failures = 0
        self.opens += 1
        self._start_feeder()
        return True

    def close(self):
        with self._lock:
            stream, self._stream = self._stream, None
            pending = list(self._queue)
            self._queue.clear()
            cur, self._cur = self._cur, None
        for item in pending + ([cur] if cur else []):
            item.cancelled = True
            item.done.set()
        if stream is not None:
            for name in ("stop", "close"):
                try:
                    getattr(stream, name)()
                except Exception:
                    pass
        self._feeder_stop.set()

    def _start_feeder(self):
        if self._aec is None or not getattr(self._aec, "enabled", False):
            return
        if self._feeder is not None and self._feeder.is_alive():
            return
        self._feeder_stop.clear()

        def feed():
            while not self._feeder_stop.is_set():
                try:
                    block = self._far_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    self._aec.feed_far_end(block, self.rate)
                except Exception:
                    pass

        self._feeder = threading.Thread(target=feed, daemon=True, name="TTS-AEC-FarEnd-Persistent")
        self._feeder.start()

    # ---------------------------------------------------------- callback
    def _callback(self, outdata, frames, time_info, status):
        """PortAudio thread: copy samples out, nothing that can block."""
        self._last_callback = self._clock()
        out = outdata[:, 0] if getattr(outdata, "ndim", 1) == 2 else outdata
        filled = 0
        while filled < frames:
            item = self._cur
            if item is None:
                with self._lock:
                    item = self._cur = (self._queue.popleft() if self._queue else None)
                if item is None:
                    out[filled:] = 0
                    break
            if item.cancelled:
                item.done.set()
                self._cur = None
                continue
            n = min(frames - filled, len(item.pcm) - item.pos)
            out[filled:filled + n] = item.pcm[item.pos:item.pos + n]
            item.started = True
            filled += n
            item.pos += n
            if item.pos >= len(item.pcm):
                item.done.set()
                self._cur = None
        if self._aec is not None and getattr(self._aec, "enabled", False):
            try:
                self._far_q.put_nowait(np.array(out[:frames], dtype=np.int16, copy=True))
            except queue.Full:
                pass

    # -------------------------------------------------------------- play
    def play(self, pcm, rate: int, stop_event: threading.Event, on_start: Callable = None) -> bool:
        if not HAS_NUMPY or pcm is None or len(pcm) == 0:
            return False
        pcm = np.ascontiguousarray(pcm, dtype=np.int16)
        if int(rate) != self.rate:
            if self._resample is None:
                return False
            pcm = np.ascontiguousarray(self._resample(pcm, int(rate), self.rate), dtype=np.int16)
        with self._lock:
            if not self._open():
                return False
            item = _Item(pcm)
            self._queue.append(item)
        self._cancel_idle_close()
        started_reported = False
        budget = len(pcm) / float(self.rate) + 5.0
        t0 = self._clock()
        try:
            while not item.done.wait(0.02):
                if not started_reported and item.started:
                    started_reported = True
                    if on_start:
                        on_start()
                if stop_event is not None and stop_event.is_set():
                    item.cancelled = True
                    with self._lock:
                        try:
                            self._queue.remove(item)
                        except ValueError:
                            pass
                    return True
                now = self._clock()
                if now - self._last_callback > _STALL_S or now - t0 > budget:
                    logger.warning("Persistent output stream stalled - closing it; falling back for this utterance")
                    self._failures += 1
                    self._dead_until = now + min(30.0, 2.0 ** self._failures)
                    self.close()
                    return False
            if not started_reported and item.started and on_start:
                on_start()
            # Cancelled without a stop request = the stream was closed under
            # us (device gone): let the caller fall back.
            stopped = stop_event is not None and stop_event.is_set()
            return not (item.cancelled and not stopped)
        finally:
            self._last_play = self._clock()
            self._schedule_idle_close()

    # --------------------------------------------------------- idle close
    def _schedule_idle_close(self):
        self._cancel_idle_close()
        t = threading.Timer(IDLE_CLOSE_S, self._idle_close)
        t.daemon = True
        self._idle_timer = t
        t.start()

    def _cancel_idle_close(self):
        t, self._idle_timer = self._idle_timer, None
        if t is not None:
            t.cancel()

    def _idle_close(self):
        with self._lock:
            busy = self._cur is not None or bool(self._queue)
        if not busy and self._clock() - self._last_play >= IDLE_CLOSE_S - 0.5:
            self.close()
