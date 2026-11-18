"""
voice/buffers.py — Tier 7 (roadmap: "the structural piece worth copying
is dedicated buffers.py/helpers.py-style streaming buffer management and
proper VAD, not just energy-threshold gating").

SARA's stt/engine.py is 1979 lines - bigger than this whole voice/ folder -
and the roadmap is explicit that the line count isn't the point, the
STRUCTURE is: a small, dedicated module for chopping raw audio into
frames and deciding which ones are actually speech, kept separate from
the engine/orchestration code that calls it. This file is that piece,
scoped to what's genuinely useful to bolt onto Wave's EXISTING capture
path rather than a ground-up reimplementation of it.

That scoping matters: wave_project's mic capture goes through
speech_recognition's Microphone()/listen(), which blocks until a whole
phrase is captured (using its OWN internal energy-threshold endpointing)
and only then hands back one complete AudioData blob - voice/aec.py's own
HONEST LIMITATION note already flags that there's no per-frame hook while
a phrase is still being spoken, and that replacing listen()'s snapshot
capture with a continuous low-level mic stream would be "a bigger change,
deliberately not made speculatively" without real hardware to verify it
against. That reasoning applies here just as much - this module does NOT
try to replace recognizer.listen()'s live endpointing with a frame-by-
frame VAD-driven capture loop. What it DOES do is give stt.py a real,
model-capable speech/non-speech check to run over audio AFTER it's been
captured, in place of "assume anything recognizer.listen() handed back
must contain speech" - which is the actual gap: today nothing distinguishes
a capture that completed (wasn't a plain timeout) but turned out to be
pure silence/noise - e.g. the energy_threshold "guess" stt.py's own
docstring already admits to, overshooting right as TTS finishes speaking -
from a capture that's actually worth spending a Whisper/Google recognition
call on.

Tier 12 update: the "bigger change" above has now been made -
voice/capture.py + voice/endpoint.py are the continuous, frame-by-frame
capture loop (WebRTC VAD + a tracked noise floor, per 20 ms frame), and
the legacy speech_recognition path described above - with this module's
after-capture check - is only the fallback.

What's here:
  - FrameBuffer - splits a block of raw PCM into fixed-size frames (the
    "buffers.py"-style accumulator the roadmap names; both VAD backends
    below want fixed, known-size frames rather than re-deriving slicing
    logic themselves).
  - EnergyVad - the always-available default. Zero new dependencies (pure
    stdlib struct/math - NOT numpy, so this works even in the minimal
    install): a chunk "contains speech" if any fixed-size frame inside it
    has RMS energy above a threshold. The same kind of signal stt.py's
    own energy_threshold already uses for LIVE endpointing, reused here
    as a one-shot check over audio that's already been fully captured.
    Deliberately simple - no spectral features, no noise-floor adaptation -
    the point isn't to out-perform a model-based VAD, it's to have SOME
    real check always available with no optional dependency at all.
  - WebRtcVad - a thin wrapper over the optional 'webrtcvad' package
    (Google's WebRTC VAD - the same model-based approach voice/aec.py's
    AEC already comes from). Deliberately NOT added to requirements.txt,
    for the identical reason voice/tts.py's HAS_PIPER block gives for
    piper-tts: its Windows-wheel coverage isn't something this could
    verify with confidence, and a bad guess here would silently break
    `pip install -r requirements.txt` on a CI runner with no C/C++ build
    toolchain. Install it yourself (`pip install webrtcvad`) for more
    accurate, model-based speech/non-speech classification; EnergyVad
    keeps working exactly as before if you don't.
  - get_vad() - picks WebRtcVad if installed and not disabled via config,
    else EnergyVad. The same HAS_X-and-fall-back-gracefully pattern this
    project uses everywhere else (faster-whisper, Piper, sentence-
    transformers, aec-audio-processing, ...).

Both VAD classes implement the same contains_speech(pcm_bytes,
sample_rate, sample_width=2) -> bool, so stt.py's call site doesn't need
to know or care which backend it actually got.
"""
from __future__ import annotations

import logging
import math
import struct
from typing import List

logger = logging.getLogger("voice.buffers")

try:
    import webrtcvad

    HAS_WEBRTCVAD = True
except ImportError:
    webrtcvad = None  # type: ignore
    HAS_WEBRTCVAD = False


class FrameBuffer:
    """Splits one block of raw PCM into fixed-size frames of `frame_ms`
    milliseconds each, dropping a final short partial frame - both a
    clean RMS window and WebRTC VAD's own API (which only accepts
    10/20/30ms frames at its native sample rates) want fixed, known-size
    frames, so this is the one place that slicing logic lives rather than
    every caller re-deriving it."""

    def __init__(self, sample_rate: int, sample_width: int = 2, frame_ms: int = 20):
        self.sample_rate = sample_rate
        self.sample_width = sample_width
        self.frame_ms = frame_ms
        self.frame_bytes = int(sample_rate * (frame_ms / 1000.0)) * sample_width

    def split(self, pcm_bytes: bytes) -> List[bytes]:
        if self.frame_bytes <= 0 or not pcm_bytes:
            return []
        n_frames = len(pcm_bytes) // self.frame_bytes
        return [pcm_bytes[i * self.frame_bytes: (i + 1) * self.frame_bytes] for i in range(n_frames)]


def _rms_int16(frame: bytes) -> float:
    """RMS energy of a mono 16-bit PCM frame. Not using Python's stdlib
    `audioop` (it had exactly this: audioop.rms) because it was removed in
    Python 3.13 - struct.unpack + a sum-of-squares loop is a few lines
    more but has no version ceiling and no dependency at all."""
    n_samples = len(frame) // 2
    if n_samples == 0:
        return 0.0
    samples = struct.unpack(f"<{n_samples}h", frame[: n_samples * 2])
    return math.sqrt(sum(s * s for s in samples) / n_samples)


class EnergyVad:
    """See the module docstring. threshold is an RMS level on the same
    scale as int16 samples (-32768..32767) - 150 is deliberately low (a
    near-silent/room-noise frame typically sits well under 100 RMS; real
    speech picked up by a mic is usually in the thousands), since this is
    a coarse "is there ANYTHING here" backstop sitting behind
    recognizer.listen()'s own, already-tuned energy_threshold - not a
    second attempt at that same tuning problem."""

    def __init__(self, threshold: float = 150.0, frame_ms: int = 20):
        self.threshold = threshold
        self.frame_ms = frame_ms

    def contains_speech(self, pcm_bytes: bytes, sample_rate: int, sample_width: int = 2) -> bool:
        if not pcm_bytes:
            return False
        if sample_width != 2:
            # Can't score a format this can't read - don't silently treat
            # unscoreable audio as "no speech" and drop it.
            return True
        frames = FrameBuffer(sample_rate, sample_width, self.frame_ms).split(pcm_bytes)
        if not frames:
            frames = [pcm_bytes]  # shorter than one frame - score it whole rather than skip it
        return any(_rms_int16(frame) >= self.threshold for frame in frames)


class WebRtcVad:
    """Thin wrapper over the optional 'webrtcvad' package. Only usable at
    its native 8/16/32/48kHz rates with 10/20/30ms frames (webrtcvad's own
    constraint, not this project's) - anything else is treated as "can't
    score it, don't drop it" rather than raising, so a caller never has to
    know this backend's constraints just to use the shared
    contains_speech() interface."""

    _VALID_RATES = (8000, 16000, 32000, 48000)
    _VALID_FRAME_MS = (10, 20, 30)

    def __init__(self, aggressiveness: int = 2, frame_ms: int = 20):
        self._vad = webrtcvad.Vad(max(0, min(3, int(aggressiveness))))
        self.frame_ms = frame_ms if frame_ms in self._VALID_FRAME_MS else 20

    def contains_speech(self, pcm_bytes: bytes, sample_rate: int, sample_width: int = 2) -> bool:
        if not pcm_bytes:
            return False
        if sample_rate not in self._VALID_RATES or sample_width != 2:
            return True
        frames = FrameBuffer(sample_rate, sample_width, self.frame_ms).split(pcm_bytes)
        for frame in frames:
            try:
                if self._vad.is_speech(frame, sample_rate):
                    return True
            except Exception:
                continue  # one malformed frame shouldn't sink the whole check
        return False


def get_vad():
    """Picks WebRtcVad if the optional package is installed and
    config.VAD_BACKEND isn't forced to "energy", else the always-available
    EnergyVad. Imports settings.config lazily (inside the function, not at
    module import time) so importing voice.buffers never requires
    settings.config to already be configured - the same reason several
    other voice/ modules keep their config access inside methods rather
    than at class-definition time."""
    from settings.config import config

    backend = (getattr(config, "VAD_BACKEND", "auto") or "auto").strip().lower()
    if backend != "energy" and HAS_WEBRTCVAD:
        try:
            return WebRtcVad(aggressiveness=getattr(config, "VAD_AGGRESSIVENESS", 2))
        except Exception as e:
            logger.warning(f"webrtcvad available but failed to initialize, using energy-based VAD: {e}")
    return EnergyVad(threshold=getattr(config, "VAD_ENERGY_THRESHOLD", 150.0))
