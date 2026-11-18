"""
voice/endpoint.py — Tier 12: streaming endpointing
=====================================================

Decides, 20 ms frame by 20 ms frame, where an utterance starts and ends in
a continuous microphone stream (voice/capture.py). It replaces
speech_recognition's listen(), which blocked until a phrase was done using
one fixed energy threshold, a fixed silence timeout and a hard phrase cap.

What it does differently:

  * **A tracked noise floor** - "minimum statistics": the 10th percentile
    of the last FLOOR_WINDOW_S of frame levels, smoothed. Speech always
    has dips between syllables, so this follows the room, not the voice;
    a fan switching on (stationary noise that never dips) raises the floor
    within a few seconds instead of reading as one endless utterance, and
    a room going quiet lowers it again. speech_recognition calibrated once,
    at start-up.
  * **Speech = VAD AND energy.** A frame counts as speech when WebRTC's VAD
    says so (if `webrtcvad`/`webrtcvad-wheels` is installed) and it is at
    least START_DB above the floor - the VAD alone fires on steady noise,
    energy alone on any loud sound. Without webrtcvad, energy alone with a
    higher margin.
  * **Pre-roll.** The ~300 ms before the detected start is kept, so the
    first consonant ("**h**ey") isn't clipped.
  * **No fixed phrase cap.** An utterance ends after END_SILENCE_MS of
    non-speech (default 450 ms - shorter than speech_recognition's
    0.5-0.6 s plus its 64 ms buffer granularity), or at MAX_S (default 60 s)
    as a safety net only. A 30-second command is fine.
  * **An exact speech_end.** The time of the last speech frame is known,
    so time-to-first-audio no longer has to estimate it.

Pure logic: no audio device, no threads. Feed it frames, get Utterances.
"""
from __future__ import annotations

import math
import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional

try:
    import webrtcvad as _webrtcvad
    HAS_WEBRTCVAD = True
except ImportError:
    _webrtcvad = None
    HAS_WEBRTCVAD = False

_DBFS_FLOOR = -96.0


def frame_dbfs(frame: bytes) -> float:
    """RMS level of a mono int16 frame, in dB relative to full scale."""
    n = len(frame) // 2
    if n == 0:
        return _DBFS_FLOOR
    samples = struct.unpack(f"<{n}h", frame[: n * 2])
    rms = math.sqrt(sum(s * s for s in samples) / n)
    if rms <= 0:
        return _DBFS_FLOOR
    return max(_DBFS_FLOOR, 20.0 * math.log10(rms / 32768.0))


@dataclass
class Utterance:
    pcm: bytes                 # 16-bit mono PCM at sample_rate, pre-roll included
    sample_rate: int
    started_at: float          # monotonic time the first kept frame began
    speech_start: float        # monotonic time speech was first detected
    speech_end: float          # monotonic time the last speech frame ended
    ended_at: float            # monotonic time the endpoint was decided
    speech_ms: float           # how much of it was speech
    peak_dbfs: float
    mean_speech_dbfs: float    # average level of the speech frames
    floor_dbfs: float          # the noise floor at the time
    truncated: bool = False    # hit max_s

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / 2.0 / self.sample_rate

    @property
    def snr_db(self) -> float:
        return self.mean_speech_dbfs - self.floor_dbfs


@dataclass
class EndpointConfig:
    sample_rate: int = 16000
    frame_ms: int = 20
    preroll_ms: int = 300
    start_ms: int = 60              # this much consecutive speech starts an utterance
    end_silence_ms: int = 450       # this much non-speech ends it
    tail_ms: int = 120              # trailing audio kept after the last speech frame
    min_speech_ms: int = 200        # shorter bursts (a click, a cough) are dropped
    max_s: float = 60.0
    start_db: float = 9.0           # above the floor, with a VAD; energy-only uses +6
    floor_init_dbfs: float = -60.0
    floor_window_s: float = 3.0
    floor_percentile: float = 10.0
    floor_adapt: float = 0.05       # smoothing towards the window estimate, per frame
    floor_min_dbfs: float = -85.0
    floor_max_dbfs: float = -25.0
    # The gain floor: however quiet the room, nothing below this is speech.
    # Whisper normalises its input, so faint room tone handed to it comes
    # back as invented words ("Thank you."); real speech at a laptop mic is
    # around -45..-20 dBFS.
    min_speech_dbfs: float = -58.0
    vad_aggressiveness: int = 2


class Endpointer:
    """Feed frames of exactly frame_ms; get an Utterance back when one ends.

        ep = Endpointer(EndpointConfig())
        for frame, t_end in mic_frames:        # t_end = monotonic time the frame ended
            utt = ep.feed(frame, t_end)
            if utt: handle(utt)
    """

    def __init__(self, cfg: EndpointConfig = None, vad: Optional[Callable[[bytes, int], bool]] = None):
        self.cfg = cfg or EndpointConfig()
        c = self.cfg
        self.frame_bytes = int(c.sample_rate * c.frame_ms / 1000) * 2
        self._frame_s = c.frame_ms / 1000.0
        if vad is None and HAS_WEBRTCVAD and c.frame_ms in (10, 20, 30) and c.sample_rate in (8000, 16000, 32000, 48000):
            _v = _webrtcvad.Vad(max(0, min(3, int(c.vad_aggressiveness))))
            vad = lambda frame, sr: _v.is_speech(frame, sr)  # noqa: E731
        self._vad = vad
        self.floor_dbfs = c.floor_init_dbfs
        self._levels: Deque[float] = deque(maxlen=max(10, int(c.floor_window_s * 1000 / c.frame_ms)))
        self._preroll: Deque = deque(maxlen=max(1, c.preroll_ms // c.frame_ms))
        self._reset_utterance()
        self._run = 0                 # consecutive speech frames before a start

    # ------------------------------------------------------------------
    def _reset_utterance(self):
        self._frames: List[bytes] = []
        self._in_speech = False
        self._silence_run = 0
        self._speech_frames = 0
        self._speech_levels: List[float] = []
        self._peak = _DBFS_FLOOR
        self._started_at = 0.0
        self._speech_start = 0.0
        self._last_speech_end = 0.0
        self._last_speech_index = -1

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def is_speech_frame(self, frame: bytes, level: float) -> bool:
        # Energy alone has to be stricter than energy confirmed by a VAD.
        margin = self.cfg.start_db if self._vad is not None else self.cfg.start_db + 3.0
        if level < max(self.floor_dbfs + margin, self.cfg.min_speech_dbfs):
            return False
        if self._vad is None:
            return True
        try:
            return bool(self._vad(frame, self.cfg.sample_rate))
        except Exception:
            return True               # can't score it - trust the energy test

    def _track_floor(self, level: float):
        """Every frame, speech or not - see the module docstring."""
        c = self.cfg
        self._levels.append(level)
        if len(self._levels) < 10:
            return
        ordered = sorted(self._levels)
        estimate = ordered[min(len(ordered) - 1, int(len(ordered) * c.floor_percentile / 100.0))]
        self.floor_dbfs += c.floor_adapt * (estimate - self.floor_dbfs)
        self.floor_dbfs = max(c.floor_min_dbfs, min(c.floor_max_dbfs, self.floor_dbfs))

    def feed(self, frame: bytes, t_end: float) -> Optional[Utterance]:
        """One frame (exactly frame_bytes), ending at monotonic time t_end."""
        if len(frame) != self.frame_bytes:
            return None
        c = self.cfg
        level = frame_dbfs(frame)
        speech = self.is_speech_frame(frame, level)
        self._track_floor(level)

        if not self._in_speech:
            if speech:
                self._run += 1
                self._preroll.append((frame, t_end, level, True))
                if self._run * c.frame_ms >= c.start_ms:
                    self._start(t_end)
            else:
                self._run = 0
                self._preroll.append((frame, t_end, level, False))
            return None

        self._frames.append(frame)
        self._peak = max(self._peak, level)
        if speech:
            self._silence_run = 0
            self._speech_frames += 1
            self._speech_levels.append(level)
            self._last_speech_end = t_end
            self._last_speech_index = len(self._frames) - 1
        else:
            self._silence_run += 1
        if self._silence_run * c.frame_ms >= c.end_silence_ms:
            return self._finish(t_end, truncated=False)
        if len(self._frames) * self._frame_s >= c.max_s:
            return self._finish(t_end, truncated=True)
        return None

    def _start(self, t_end: float):
        c = self.cfg
        self._reset_utterance()
        self._in_speech = True
        pre = list(self._preroll)
        self._preroll.clear()
        n_speech = self._run
        self._run = 0
        for frame, t, level, is_speech in pre:
            self._frames.append(frame)
            self._peak = max(self._peak, level)
            if is_speech:
                self._speech_frames += 1
                self._speech_levels.append(level)
                self._last_speech_end = t
                self._last_speech_index = len(self._frames) - 1
        self._started_at = (pre[0][1] - self._frame_s) if pre else t_end - self._frame_s
        self._speech_start = t_end - n_speech * self._frame_s

    def _finish(self, t_end: float, truncated: bool) -> Optional[Utterance]:
        c = self.cfg
        keep_tail = max(0, c.tail_ms // c.frame_ms)
        last = min(len(self._frames), self._last_speech_index + 1 + keep_tail)
        frames = self._frames[:last]
        speech_ms = self._speech_frames * c.frame_ms
        utt = None
        if speech_ms >= c.min_speech_ms and frames:
            levels = self._speech_levels or [self._peak]
            utt = Utterance(
                pcm=b"".join(frames), sample_rate=c.sample_rate, started_at=self._started_at,
                speech_start=self._speech_start, speech_end=self._last_speech_end, ended_at=t_end,
                speech_ms=float(speech_ms), peak_dbfs=round(self._peak, 1),
                mean_speech_dbfs=round(sum(levels) / len(levels), 1), floor_dbfs=round(self.floor_dbfs, 1),
                truncated=truncated)
        self._reset_utterance()
        return utt

    def flush(self, t_end: float) -> Optional[Utterance]:
        """Ends an utterance in progress (the stream is closing)."""
        if not self._in_speech:
            return None
        return self._finish(t_end, truncated=False)
