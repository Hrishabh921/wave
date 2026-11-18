"""
voice/capture.py — Tier 12: the continuous microphone stream
==============================================================

One `sounddevice` input stream that stays open, instead of
speech_recognition opening the microphone afresh for every phrase:

    mic callback (PortAudio thread)   ->  frame queue (never blocks)
    capture thread:  AEC per 20 ms frame  ->  level meter  ->  Endpointer
                     ->  finished Utterances queue  ->  SpeechReceiver.listen()

  * **Per-chunk AEC.** voice/aec.py works in 10 ms frames; it's now fed
    every 20 ms of live audio as it arrives, instead of a whole phrase
    after the fact (the "HONEST LIMITATION" Tier 7 documented there).
  * **Streaming endpointing** with a tracked noise floor, pre-roll and no
    fixed phrase cap (voice/endpoint.py).
  * **A watchdog.** If no audio arrives for WATCHDOG_S while running, or
    the stream reports an error, it is closed and reopened with backoff.
    The default input device is re-checked every DEVICE_POLL_S, so
    plugging headphones in or out moves capture to the new default
    without a restart.
  * **Partial audio for live captions.** While an utterance is in
    progress, `snapshot()` returns the audio so far.

Everything degrades: no `sounddevice` (or no device that opens) means
`available` is False and voice/stt.py keeps using speech_recognition's
listen(), exactly as before.
"""
from __future__ import annotations

import logging
import queue
import struct
import threading
import time
from typing import Callable, Optional

from voice.endpoint import EndpointConfig, Endpointer, Utterance, frame_dbfs

logger = logging.getLogger("voice.capture")

try:
    import sounddevice as _sd
    HAS_SOUNDDEVICE = True
except (ImportError, OSError):
    _sd = None
    HAS_SOUNDDEVICE = False

WATCHDOG_S = 2.5
DEVICE_POLL_S = 3.0
_MAX_BACKOFF_S = 10.0


def _resample_int16(data: bytes, src: int, dst: int) -> bytes:
    """Linear resample of mono int16 bytes (fine for speech at 48->16 kHz:
    the mic's own anti-aliasing filter already removed what matters)."""
    try:
        import numpy as np
        from voice.aec import resample_linear
        pcm = np.frombuffer(data, dtype=np.int16)
        return np.asarray(resample_linear(pcm, src, dst), dtype=np.int16).tobytes()
    except Exception:
        step = src / float(dst)
        n = len(data) // 2
        samples = struct.unpack(f"<{n}h", data[: n * 2])
        out = [samples[min(n - 1, int(i * step))] for i in range(int(n / step))]
        return struct.pack(f"<{len(out)}h", *out)


class CaptureEngine:
    """Owns the stream and the capture thread. Thread-safe to start/stop
    from any thread; `next_utterance()` is called from the mic worker."""

    def __init__(self, cfg: EndpointConfig = None, aec=None, sd_module=None,
                 clock: Callable[[], float] = time.monotonic, device=None):
        self.cfg = cfg or EndpointConfig()
        self._sd = sd_module if sd_module is not None else _sd
        self._aec = aec
        self._clock = clock
        self._device = device
        self._frames: "queue.Queue" = queue.Queue(maxsize=500)       # ~10 s of 20 ms frames
        self._utterances: "queue.Queue" = queue.Queue(maxsize=20)
        self._endpointer = Endpointer(self.cfg)
        self._stream = None
        self._thread = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._last_frame_at = 0.0
        self._last_device_check = 0.0
        self._device_name = None
        self._failures = 0
        self._next_retry_at = 0.0
        self._level_callback = None
        self._level_last = 0.0
        self._partial = bytearray()
        self._capture_rate = self.cfg.sample_rate
        self.frames_dropped = 0
        self.restarts = 0
        self.last_error = ""

    # ------------------------------------------------------------- state
    @property
    def available(self) -> bool:
        return self._sd is not None

    @property
    def running(self) -> bool:
        return self._running.is_set()

    @property
    def capture_rate(self) -> int:
        """The rate the device was opened at (resampled to 16 kHz if different)."""
        return self._capture_rate

    @property
    def noise_floor_dbfs(self) -> float:
        return self._endpointer.floor_dbfs

    def set_level_callback(self, callback):
        self._level_callback = callback

    # ------------------------------------------------------------ control
    def start(self) -> bool:
        with self._lock:
            if self._running.is_set():
                return True
            if not self.available:
                return False
            if not self._open_stream():
                return False
            self._running.set()
            self._thread = threading.Thread(target=self._run, daemon=True, name="MicCapture")
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            self._running.clear()
            self._close_stream()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None

    def _open_stream(self) -> bool:
        """16 kHz if the device takes it (MME/DirectSound resample for us);
        otherwise the device's own rate, resampled to 16 kHz here."""
        sd = self._sd
        self._device_name = self._default_input_name()
        rates = [self.cfg.sample_rate]
        native = self._default_input_rate()
        if native and native not in rates:
            rates.append(native)
        last = None
        for rate in rates:
            try:
                stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16",
                                        blocksize=int(rate * self.cfg.frame_ms / 1000), device=self._device,
                                        callback=self._callback)
                stream.start()
            except Exception as e:
                last = e
                continue
            self._stream = stream
            self._capture_rate = rate
            self._last_frame_at = self._clock()
            self._failures = 0
            if rate != self.cfg.sample_rate:
                logger.info(f"Mic opened at {rate} Hz; resampling to {self.cfg.sample_rate} Hz")
            return True
        self.last_error = str(last) if last else "no usable sample rate"
        logger.warning(f"Mic stream couldn't open: {self.last_error}")
        self._stream = None
        return False

    def _default_input_rate(self) -> Optional[int]:
        try:
            info = self._sd.query_devices(kind="input")
            rate = (info or {}).get("default_samplerate")
            return int(rate) if rate else None
        except Exception:
            return None

    def _close_stream(self):
        stream, self._stream = self._stream, None
        if stream is None:
            return
        for name in ("stop", "close"):
            try:
                getattr(stream, name)()
            except Exception:
                pass

    def _default_input_name(self) -> Optional[str]:
        try:
            info = self._sd.query_devices(kind="input")
            return (info or {}).get("name")
        except Exception:
            return None

    # ------------------------------------------------------------ the audio
    def _callback(self, indata, frames, time_info, status):
        """PortAudio thread: copy and hand off, nothing else."""
        try:
            data = bytes(indata)
        except Exception:
            return
        try:
            self._frames.put_nowait((data, self._clock()))
        except queue.Full:
            self.frames_dropped += 1

    def _run(self):
        frame_bytes = self._endpointer.frame_bytes
        pending = b""
        while self._running.is_set():
            try:
                data, t_end = self._frames.get(timeout=0.2)
            except queue.Empty:
                self._watchdog()
                continue
            self._last_frame_at = t_end
            if self._capture_rate != self.cfg.sample_rate:
                data = _resample_int16(data, self._capture_rate, self.cfg.sample_rate)
            pending += data
            while len(pending) >= frame_bytes:
                frame, pending = pending[:frame_bytes], pending[frame_bytes:]
                self._process_frame(frame, t_end)
            self._watchdog()
        utt = self._endpointer.flush(self._clock())
        if utt is not None:
            self._deliver(utt)

    def _process_frame(self, frame: bytes, t_end: float):
        if self._aec is not None and getattr(self._aec, "enabled", False):
            try:
                cleaned = self._aec.process_near_end(frame)
                if cleaned and len(cleaned) == len(frame):
                    frame = cleaned
            except Exception:
                pass
        if self._level_callback is not None and t_end - self._level_last >= 0.066:
            self._level_last = t_end
            try:
                level = frame_dbfs(frame)
                self._level_callback(max(0.0, min(1.0, (level + 60.0) / 50.0)))
            except Exception:
                pass
        was_speech = self._endpointer.in_speech
        utt = self._endpointer.feed(frame, t_end)
        if self._endpointer.in_speech:
            if not was_speech:
                self._partial = bytearray()
            self._partial.extend(frame)
        if utt is not None:
            self._partial = bytearray()
            self._deliver(utt)

    def _deliver(self, utt: Utterance):
        try:
            self._utterances.put_nowait(utt)
        except queue.Full:
            try:
                self._utterances.get_nowait()        # drop the oldest, keep the newest
                self._utterances.put_nowait(utt)
            except queue.Empty:
                pass

    # ----------------------------------------------------------- watchdog
    def _watchdog(self):
        now = self._clock()
        dead = self._stream is None or (now - self._last_frame_at) > WATCHDOG_S
        moved = False
        if not dead and now - self._last_device_check >= DEVICE_POLL_S:
            self._last_device_check = now
            name = self._default_input_name()
            moved = self._device is None and name is not None and self._device_name is not None and name != self._device_name
            if moved:
                logger.info(f"Default microphone changed ({self._device_name} -> {name}) - switching")
        if not (dead or moved) or now < self._next_retry_at:
            return
        with self._lock:
            if not self._running.is_set():
                return
            self._close_stream()
            if self._open_stream():
                self.restarts += 1
                logger.info("Mic stream reopened")
            else:
                self._failures += 1
                self._next_retry_at = now + min(_MAX_BACKOFF_S, 0.5 * (2 ** self._failures))

    # -------------------------------------------------------------- reads
    def next_utterance(self, timeout: float) -> Optional[Utterance]:
        try:
            return self._utterances.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def snapshot(self) -> bytes:
        """The audio of the utterance in progress so far ('' if none) - for
        live captions."""
        return bytes(self._partial) if self._endpointer.in_speech else b""

    def in_speech(self) -> bool:
        return self._endpointer.in_speech
