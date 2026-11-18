"""
voice/stt_vad.py - SARA-roadmap Tier 7: dedicated streaming-buffer
management and real per-frame Voice Activity Detection (VAD) for
voice/stt.py's mic capture, instead of speech_recognition's own built-in
energy-threshold-only endpointing.

Ports the SHAPE of SARA's sara/audio/stt/buffers.py (_PreBuffer,
_VADFilter, _SilenceGate) and the onset/continuation/silence state
machine inside sara/audio/stt/engine.py's _collect_speech(), adapted to
Wave's different calling model: SARA runs a continuous background
thread that feeds a ring buffer which _collect_speech() drains. Wave's
listen() is already called synchronously, once per loop iteration, from
ui/workers.py's ContinuousMicWorker - so collect_utterance() below just
reads directly from a mic stream for the duration of ONE utterance and
returns, matching speech_recognition's own open-stream / capture /
close-stream-per-call shape that voice/stt.py already has today.

Deliberately NOT ported: SARA's ring buffer + background feeder thread
(not needed for Wave's per-call model), live-caption partial-transcript
previews, and barge-in audio handoff - all genuinely separate SARA
features, not part of the roadmap's "buffers + proper VAD" ask.

Everything here is additive and optional: HAS_WEBRTCVAD gates whether
any of it is even usable, and voice/stt.py falls back to its proven,
untouched speech_recognition-based capture whenever this module isn't
usable or raises - see _listen_via_vad() there, and STT_VAD_ENABLED in
settings/config.py for the deliberate full opt-out.
"""
from __future__ import annotations

import collections
import time
from typing import Callable, List, Optional

from settings.config import config

try:
    import webrtcvad

    HAS_WEBRTCVAD = True
except ImportError:
    webrtcvad = None  # type: ignore
    HAS_WEBRTCVAD = False

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    HAS_NUMPY = False

# webrtcvad only accepts 8000/16000/32000/48000 Hz and 10/20/30ms frames.
# 16kHz also matches what _recognize_with_whisper() and voice/aec.py
# already resample to, so capturing at 16kHz directly means nothing
# downstream needs to resample either.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit PCM
FRAME_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)  # samples/frame (480)
FRAME_BYTES = FRAME_SIZE * SAMPLE_WIDTH  # bytes/frame (960)

# How much audio to keep rolling BEFORE speech is confirmed, so the first
# phoneme of a word isn't clipped by the few frames it takes to notice
# speech has started.
PRE_SPEECH_MS = int(getattr(config, "STT_VAD_PRE_SPEECH_MS", 300))


def rms(frame: bytes) -> float:
    """Root-mean-square energy of a 16-bit PCM frame. Returns 0.0 on any
    problem (empty frame, numpy unavailable) rather than raising - this
    is only ever used as a secondary hint alongside VAD, never the sole
    speech/silence signal, so a 0.0 just means "VAD alone decides"."""
    if not HAS_NUMPY or not frame:
        return 0.0
    try:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float64)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples ** 2)))
    except Exception:
        return 0.0


class _PreBuffer:
    """Keeps a rolling ~PRE_SPEECH_MS window of the most recent frames
    seen while still in WAITING (pre-speech). drain() hands back
    everything currently held and clears it - called exactly once, the
    moment speech is first confirmed, so those few pre-roll frames
    become the START of the captured utterance instead of being lost."""

    def __init__(self, pre_speech_ms: int = PRE_SPEECH_MS):
        max_frames = max(1, int(pre_speech_ms / FRAME_MS))
        self._frames: "collections.deque[bytes]" = collections.deque(maxlen=max_frames)

    def push(self, frame: bytes) -> None:
        self._frames.append(frame)

    def drain(self) -> bytes:
        out = b"".join(self._frames)
        self._frames.clear()
        return out


class _VADFilter:
    """Thin wrapper around webrtcvad.Vad - a no-op (always "not speech")
    if webrtcvad isn't installed or fails to initialize, so callers never
    need their own HAS_WEBRTCVAD check: is_speech() alone is always safe
    to call, and collect_utterance()'s energy_floor OR-condition still
    covers detection even when this reports nothing but False."""

    def __init__(self, aggressiveness: Optional[int] = None):
        self._vad = None
        if HAS_WEBRTCVAD:
            level = aggressiveness
            if level is None:
                level = int(getattr(config, "VAD_AGGRESSIVENESS", 2))
            level = max(0, min(3, level))
            try:
                self._vad = webrtcvad.Vad(level)
            except Exception:
                self._vad = None

    def is_speech(self, frame: bytes) -> bool:
        if self._vad is None or len(frame) != FRAME_BYTES:
            return False
        try:
            return bool(self._vad.is_speech(frame, SAMPLE_RATE))
        except Exception:
            return False


class _SilenceGate:
    """Adaptive "how much trailing silence ends the phrase" suggestion -
    mirrors SARA's approach of leaning on recent utterance lengths: after
    a run of short utterances it tightens up a little (snappier
    turn-taking); after a longer one it loosens a little (less chance of
    cutting someone off mid-thought). `base` (Wave's own already-tuned
    pause_threshold numbers) is always the anchor - suggest() only ever
    scales it within [base*0.7, base*1.6], so this refines Wave's tuning
    rather than overriding it."""

    def __init__(self, maxlen: int = 8):
        self._history: "collections.deque[float]" = collections.deque(maxlen=maxlen)

    def suggest(self, base: float) -> float:
        if not self._history:
            return base
        avg = sum(self._history) / len(self._history)
        if avg < 1.5:
            factor = 0.85  # recent utterances have been short - tighten up
        elif avg > 5.0:
            factor = 1.3  # recent utterances have run long - allow more room
        else:
            factor = 1.0
        return min(max(base * factor, base * 0.7), base * 1.6)

    def record(self, utterance_seconds: float) -> None:
        if utterance_seconds > 0:
            self._history.append(utterance_seconds)


def collect_utterance(
    read_frame: Callable[[], bytes],
    is_speech: Callable[[bytes], bool],
    energy_floor: float,
    pre_buffer: "_PreBuffer",
    timeout: float,
    max_duration: float,
    silence_limit: float,
) -> bytes:
    """Reads FRAME_BYTES-sized frames from `read_frame()` and returns the
    raw PCM bytes of one captured utterance (trailing silence trimmed),
    or b"" if nothing was said within `timeout` seconds of waiting, or if
    `read_frame` stops producing frames before any speech was detected.

    Onset: a single frame where `is_speech(frame)` is True, OR that
    frame's RMS energy clears `energy_floor` - a VAD-or-energy hybrid
    rather than trusting either signal alone, so one misclassified frame
    from either source never has to be the one that decides anything.
    Once onset fires, the drained pre-buffer is prepended so the first
    phoneme isn't clipped.

    Continuation: the same hybrid rule, with energy smoothed over the
    last 3 frames so one quiet consonant or one VAD miss mid-word isn't
    mistaken for silence starting.

    Ending: `silence_limit` seconds of continuous non-speech frames after
    onset, or `max_duration` seconds total, whichever comes first. The
    trailing silence itself is trimmed off the returned audio so it
    doesn't get sent to the recognizer.

    `read_frame` is any zero-argument callable returning FRAME_BYTES of
    raw PCM each time it's called (an empty/short return ends capture
    early, same as a closed stream). Production code passes a PyAudio
    stream's .read(); tests pass a canned sequence instead - this
    function never needs to know PyAudio exists.
    """
    frames_per_sec = SAMPLE_RATE / FRAME_SIZE
    silence_limit_frames = max(1, int(silence_limit * frames_per_sec))

    speech_frames: List[bytes] = []
    silence_run = 0
    energy_window: "collections.deque[float]" = collections.deque(maxlen=3)
    start = time.monotonic()
    speech_start: Optional[float] = None

    while True:
        frame = read_frame()
        if not frame or len(frame) != FRAME_BYTES:
            break  # stream closed/unreadable - stop with whatever we have

        if speech_start is None:
            if time.monotonic() - start > timeout:
                return b""  # nothing said in this window - normal silence
            energy = rms(frame)
            if is_speech(frame) or energy > energy_floor:
                pre = pre_buffer.drain()
                speech_frames = [pre, frame] if pre else [frame]
                speech_start = time.monotonic()
                silence_run = 0
                energy_window.clear()
                energy_window.append(energy)
            else:
                pre_buffer.push(frame)
            continue

        # Inside an utterance.
        if time.monotonic() - speech_start > max_duration:
            break

        energy = rms(frame)
        energy_window.append(energy)
        smoothed = sum(energy_window) / len(energy_window)
        voiced = is_speech(frame) or smoothed > energy_floor
        speech_frames.append(frame)
        if voiced:
            silence_run = 0
        else:
            silence_run += 1
            if silence_run >= silence_limit_frames:
                break

    if speech_start is None:
        return b""  # stream ended before any speech was ever seen

    if silence_run > 0 and len(speech_frames) > silence_run:
        speech_frames = speech_frames[:-silence_run]
    return b"".join(speech_frames)
