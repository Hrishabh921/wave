"""
voice/aec.py
Real Acoustic Echo Cancellation (AEC) - WebRTC Audio Processing Module via
the optional `aec-audio-processing` package (a Windows-wheel-friendly
binding of Google's WebRTC APM: AEC + Noise Suppression + AGC + VAD).

WHY THIS FILE EXISTS
---------------------
voice/stt.py used to tell real speech apart from W.A.V.E.'s own voice
leaking through the speakers using only energy_threshold/pause_threshold
heuristics (raise the threshold while TTS is speaking, guess a pause
length). That's a guess, not cancellation - it can't tell "quiet real
speech" from "loud echo" and has to trade off missed interruptions
against self-triggering. This module does real reference-signal-based
cancellation instead: it is given the EXACT samples W.A.V.E. is sending
to the speaker (the "far-end"/reference signal) and subtracts the
correlated part of that out of the microphone signal (the "near-end"),
using the same WebRTC algorithm Chrome/Meet/Teams use for this.

HOW IT'S WIRED (see tts.py / stt.py for the other end of this)
-----------------------------------------------------------------
  - tts.py's sounddevice playback callback pushes the exact PCM samples
    being written to the output device into feed_far_end() as they play.
  - stt.py runs each captured utterance's raw PCM through
    process_near_end() before handing it to faster-whisper/Google.
  - One AECProcessor instance (`aec_processor` below) is shared between
    tts.py and stt.py so the adaptive filter has one continuous view of
    "what we actually played" to correlate the mic signal against.

HOW IT'S FED (Tier 12 closed the old "honest limitation")
------------------------------------------------------------------------------
Until Tier 12, mic capture went through `speech_recognition`'s
`recognizer.listen()`, which only hands back a whole phrase, so
process_near_end() ran once per utterance in a catch-up loop. Now
voice/capture.py keeps ONE continuous sounddevice InputStream open and
calls process_near_end() on every 20 ms frame as it arrives, and
voice/audio_out.py keeps one output stream open while Wave talks and feeds
every block it plays - silence included - to feed_far_end(), so the
adaptive filter sees a continuous duplex stream. The per-utterance path is
still used when the continuous stream isn't available (CAPTURE_ENGINE=legacy,
no sounddevice): voice/stt.py's _apply_aec() then cleans the phrase after
capture exactly as before. See docs/CAPTURE_PIPELINE.md.

CONCURRENCY DESIGN NOTE (matches the reasoning sara_project's own aec.py
documents for the identical design - repeated here because it's correct,
not copied blind)
-----------------------------------------------------------------
feed_far_end() and process_near_end() can be called concurrently from two
different threads (tts.py's playback callback / background feeder thread,
and stt.py's recognition flow). This is the INTENDED usage pattern for a
real-time duplex echo canceller - WebRTC APM is designed around one path
continuously feeding the "render"/reverse stream while another feeds the
"capture" stream. Each side has its OWN lock (`_far_lock`, `_near_lock`)
guarding its own buffer bookkeeping; there is deliberately no single lock
across both sides, since that would serialize two independent audio paths
against each other for no clear correctness benefit (and real risk of
stalling the real-time playback callback behind STT's recognition work).

API-NAME SAFETY NOTE
---------------------
`aec-audio-processing`'s documented API is AudioProcessor(...),
set_stream_format(...), set_reverse_stream_format(...),
set_stream_delay(...), process_stream(...), has_voice(),
aec_enabled()/ns_enabled()/agc_enabled() - it does NOT document the
far-end/reverse-stream *processing* call's name. Standard WebRTC APM
bindings universally call this `process_reverse_stream`, so that's tried
first, but `_resolve_reverse_method()` probes a short list of plausible
names and, if none exist, disables far-end feeding with a loud (rate
limited) warning instead of a silent no-op, and logs `dir(ap)` so the
real name can be read off directly and added to the candidate list.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from settings.config import config

logger = logging.getLogger("AEC")

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    HAS_NUMPY = False

try:
    from aec_audio_processing import AudioProcessor as _WebRTCAudioProcessor

    HAS_AEC_LIB = True
except (ImportError, OSError) as _e:
    _WebRTCAudioProcessor = None
    HAS_AEC_LIB = False
    logger.info(
        f"'aec-audio-processing' not available - real echo cancellation "
        f"disabled, voice/stt.py stays on its energy-threshold heuristics "
        f"(pip install aec-audio-processing for real AEC). ({_e})"
    )

# Candidate method names for pushing the far-end/reverse (reference)
# stream into the processor, tried in order - see API-NAME SAFETY NOTE.
_REVERSE_METHOD_CANDIDATES = (
    "process_reverse_stream",
    "process_render_stream",
    "process_reverse",
    "reverse_stream",
    "analyze_reverse_stream",
)

_BYTES_PER_SAMPLE = 2  # int16 PCM

# How often (seconds) a given category of recurring error is allowed to
# print again once it's already printed once - keeps a persistent problem
# visible over time without spamming on every single 10ms frame.
_ERROR_LOG_RATE_LIMIT_S = 60.0

# Safety valve: if a buffer somehow grows beyond this many frames' worth
# of bytes (e.g. downstream stalls), drop the backlog instead of growing
# memory unboundedly.
_BUF_MAX_FRAMES = 50


def resample_linear(pcm_i16, src_rate: int, dst_rate: int):
    """Lightweight dependency-free resampler (linear interpolation) used
    only for feeding the AEC far-end/near-end reference - this never
    touches audio that gets played back or transcribed, so perfect
    fidelity isn't required, only correct timing/shape so the adaptive
    filter can correlate it against the echo in the mic signal."""
    if not HAS_NUMPY or src_rate == dst_rate or len(pcm_i16) == 0:
        return pcm_i16
    src_n = len(pcm_i16)
    dst_n = max(1, int(round(src_n * (dst_rate / float(src_rate)))))
    src_x = np.linspace(0.0, 1.0, num=src_n, endpoint=False)
    dst_x = np.linspace(0.0, 1.0, num=dst_n, endpoint=False)
    out = np.interp(dst_x, src_x, pcm_i16.astype(np.float32))
    return np.clip(out, -32768.0, 32767.0).astype(np.int16)


class AECProcessor:
    """Thread-safe wrapper around WebRTC APM providing:
      - feed_far_end(pcm_i16, source_sample_rate): push whatever audio is
        ACTUALLY being sent to the speaker right now (called from tts.py's
        real-time sounddevice output callback / its feeder thread).
      - process_near_end(pcm_bytes) -> bytes: run raw mic audio through
        AEC/NS and get back echo-cancelled audio (called from stt.py once
        per captured utterance).

    Both sides re-frame arbitrary-length chunks into the fixed 10ms frames
    WebRTC APM requires, buffering any remainder across calls. If the
    underlying library (or numpy) is unavailable, or init fails, every
    method degrades to safe passthrough (near-end unmodified, far-end
    silently ignored) rather than raising into a real-time audio thread.
    """

    def __init__(
        self,
        sample_rate: Optional[int] = None,
        channels: int = 1,
        stream_delay_ms: Optional[int] = None,
        enable_ns: Optional[bool] = None,
        enable_agc: Optional[bool] = None,
        enable_vad: Optional[bool] = None,
    ) -> None:
        self.enabled = bool(getattr(config, "AEC_ENABLED", True)) and HAS_AEC_LIB and HAS_NUMPY

        self.sample_rate = int(sample_rate or getattr(config, "AEC_SAMPLE_RATE", 16000))
        self.channels = channels
        self._frame_samples = max(1, int(self.sample_rate * 0.01))  # 10ms, per WebRTC APM
        self._frame_bytes = self._frame_samples * _BYTES_PER_SAMPLE * self.channels

        self._ap = None
        self._reverse_method_name: Optional[str] = None

        self._near_buf = bytearray()
        self._near_lock = threading.Lock()
        self._far_buf = bytearray()
        self._far_lock = threading.Lock()

        self._closed = False
        self._close_lock = threading.Lock()

        # Rate-limited error logging state. Each category tracks the
        # monotonic timestamp it last logged at. Starts as None (not 0.0)
        # so the FIRST error in each category always logs even if the
        # process has been up under _ERROR_LOG_RATE_LIMIT_S seconds.
        self._log_state_lock = threading.Lock()
        self._last_far_end_error_log: Optional[float] = None
        self._last_process_error_log: Optional[float] = None

        if not self.enabled:
            if not HAS_NUMPY:
                logger.info("AEC disabled: numpy unavailable.")
            elif not HAS_AEC_LIB:
                pass  # already logged at import time
            else:
                logger.info("AEC disabled via AEC_ENABLED=false - mic audio passes through unmodified.")
            return

        try:
            self._ap = _WebRTCAudioProcessor(
                enable_aec=True,
                enable_ns=bool(getattr(config, "AEC_ENABLE_NS", True) if enable_ns is None else enable_ns),
                enable_agc=bool(getattr(config, "AEC_ENABLE_AGC", False) if enable_agc is None else enable_agc),
                enable_vad=bool(getattr(config, "AEC_ENABLE_VAD", False) if enable_vad is None else enable_vad),
            )
            self._ap.set_stream_format(
                sample_rate_in=self.sample_rate,
                channel_count_in=self.channels,
                sample_rate_out=self.sample_rate,
                channel_count_out=self.channels,
            )
            self._ap.set_reverse_stream_format(self.sample_rate, self.channels)

            delay_ms = int(
                stream_delay_ms if stream_delay_ms is not None
                else getattr(config, "AEC_STREAM_DELAY_MS", 80)
            )
            self._ap.set_stream_delay(delay_ms)

            self._reverse_method_name = self._resolve_reverse_method()

            logger.info(
                f"AEC ready - rate={self.sample_rate}Hz delay={delay_ms}ms "
                f"far-end method='{self._reverse_method_name or 'NOT FOUND - see warning above'}'"
            )
        except Exception as e:
            logger.error(f"AEC init failed, falling back to passthrough (no echo cancellation): {e}")
            self._ap = None
            self.enabled = False

    # ── Method discovery (see API-NAME SAFETY NOTE at top of file) ─────

    def _resolve_reverse_method(self) -> Optional[str]:
        for name in _REVERSE_METHOD_CANDIDATES:
            if hasattr(self._ap, name):
                return name
        available = [m for m in dir(self._ap) if not m.startswith("_")]
        logger.warning(
            f"Could not find a far-end/reverse-stream method on AudioProcessor "
            f"among candidates {_REVERSE_METHOD_CANDIDATES}. Echo cancellation "
            f"will NOT work correctly (near-end processing still runs, but with "
            f"no reference signal to cancel against - equivalent to AEC being "
            f"off). Actual available methods: {available}. Update "
            f"_REVERSE_METHOD_CANDIDATES in voice/aec.py with the correct name."
        )
        return None

    # ── Rate-limited error logging helper ───────────────────────────────

    def _should_log_now(self, category: str) -> bool:
        now = time.monotonic()
        with self._log_state_lock:
            if category == "far_end":
                last = self._last_far_end_error_log
                if last is None or (now - last) >= _ERROR_LOG_RATE_LIMIT_S:
                    self._last_far_end_error_log = now
                    return True
                return False
            if category == "process":
                last = self._last_process_error_log
                if last is None or (now - last) >= _ERROR_LOG_RATE_LIMIT_S:
                    self._last_process_error_log = now
                    return True
                return False
        return False

    # ── Far-end (reference / reverse stream) ────────────────────────────

    def feed_far_end(self, pcm_i16, source_sample_rate: int) -> None:
        """Push whatever audio is actually being sent to the speaker RIGHT
        NOW. Called from tts.py's real-time playback path - must stay
        fast and must never raise."""
        if not self.enabled or self._ap is None or pcm_i16 is None or len(pcm_i16) == 0:
            return
        try:
            if source_sample_rate != self.sample_rate:
                pcm_i16 = resample_linear(pcm_i16, source_sample_rate, self.sample_rate)
            pcm_bytes = pcm_i16.astype(np.int16, copy=False).tobytes()

            with self._far_lock:
                self._far_buf.extend(pcm_bytes)
                if self._reverse_method_name is None:
                    if len(self._far_buf) > self._frame_bytes * _BUF_MAX_FRAMES:
                        self._far_buf.clear()
                    return
                method = getattr(self._ap, self._reverse_method_name)
                while len(self._far_buf) >= self._frame_bytes:
                    frame = bytes(self._far_buf[: self._frame_bytes])
                    del self._far_buf[: self._frame_bytes]
                    method(frame)
        except Exception as e:
            if self._should_log_now("far_end"):
                logger.warning(
                    f"feed_far_end error (further identical errors suppressed "
                    f"for {_ERROR_LOG_RATE_LIMIT_S:.0f}s): {e}"
                )

    # ── Near-end (microphone / forward stream) ──────────────────────────

    def process_near_end(self, pcm_bytes: bytes) -> bytes:
        """Run raw mic audio through AEC + NS. Returns echo-cancelled
        audio. On any failure this returns the ORIGINAL audio unmodified
        (fail-safe passthrough) so a transient error never silences the
        mic entirely."""
        if not self.enabled or self._ap is None or not pcm_bytes:
            return pcm_bytes

        try:
            with self._near_lock:
                self._near_buf.extend(pcm_bytes)

                if len(self._near_buf) > self._frame_bytes * _BUF_MAX_FRAMES:
                    if self._should_log_now("process"):
                        logger.warning(
                            f"near-end buffer exceeded {_BUF_MAX_FRAMES} frames "
                            f"worth of audio and was reset - processing may be "
                            f"falling behind."
                        )
                    self._near_buf.clear()
                    return pcm_bytes

                # Pad the final partial frame with silence so every byte
                # the caller gave us comes back out the other side - the
                # caller (stt.py) is handing us one whole captured
                # utterance, not a live stream, so dropping a trailing
                # fraction of a frame would quietly shorten the audio
                # faster-whisper/Google actually get to hear.
                pad = (-len(self._near_buf)) % self._frame_bytes
                if pad:
                    self._near_buf.extend(b"\x00" * pad)

                out = bytearray()
                while len(self._near_buf) >= self._frame_bytes:
                    frame = bytes(self._near_buf[: self._frame_bytes])
                    del self._near_buf[: self._frame_bytes]
                    try:
                        processed = self._ap.process_stream(frame)
                        out.extend(processed if processed else frame)
                    except Exception as e:
                        if self._should_log_now("process"):
                            logger.warning(
                                f"process_stream error (further identical errors "
                                f"suppressed for {_ERROR_LOG_RATE_LIMIT_S:.0f}s, "
                                f"passing raw audio through): {e}"
                            )
                        out.extend(frame)
                # Trim the padding back off before handing audio back.
                if pad:
                    out = out[:-pad]
                return bytes(out)
        except Exception as e:
            logger.error(f"process_near_end fatal error, disabling AEC for safety: {e}")
            self.enabled = False
            return pcm_bytes

    def has_voice(self) -> bool:
        """Exposes WebRTC APM's own VAD result (only meaningful if
        AEC_ENABLE_VAD=True). Not currently consumed by stt.py, which
        keeps its own energy-threshold endpointing - available for
        future use."""
        if not self.enabled or self._ap is None:
            return False
        try:
            return bool(self._ap.has_voice())
        except Exception:
            return False

    def reset_stream_delay(self, delay_ms: int) -> None:
        """Allows runtime re-tuning of AEC_STREAM_DELAY_MS without a
        restart, e.g. from a future Settings > Voice control."""
        if not self.enabled or self._ap is None:
            return
        try:
            self._ap.set_stream_delay(max(0, min(500, int(delay_ms))))
        except Exception as e:
            logger.warning(f"reset_stream_delay failed: {e}")

    def close(self) -> None:
        """Releases any resources held by this processor. Safe to call
        multiple times. The underlying native AudioProcessor doesn't
        currently require explicit teardown, but this gives the app one
        consistent shutdown pattern and a place to add real cleanup if a
        future library version needs it."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.enabled = False
            self._ap = None


# Shared instance - constructed once, imported by both voice/tts.py
# (feed_far_end) and voice/stt.py (process_near_end) so the adaptive
# filter has one continuous view of what W.A.V.E. has actually played.
aec_processor = AECProcessor()
