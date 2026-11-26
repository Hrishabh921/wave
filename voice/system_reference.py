"""
voice/system_reference.py — Tier 49: everything the PC plays as the echo reference.

The echo canceller (voice/aec.py) only knew Wave's own voice - the samples
it plays. With YouTube, Spotify or a call playing, the mic heard that too,
and Whisper wrote it down (song lyrics, a video's dialogue) along with -
or instead of - what the user said. Windows hands any program a copy of
the final mix going to the speakers (WASAPI "loopback"), and that copy is
what the room's echo is made of. SARA feeds it to its canceller as the
reference and measured 4 dB of echo removed without it, 26 dB with it.
This does the same for Wave: a background thread reads the loopback of
the speakers in use (the physical device when the default one is a virtual
"enhancer" like FxSound or Voicemeeter - its loopback carries what the
speakers really get) at 16 kHz, 30 ms at a time, and hands it to
aec_processor.feed_external_reference(), which uses it only while Wave
itself is quiet (Wave's own samples are the reference while it talks)
and delays the mic by 80 ms meanwhile so the reference comes first.

Needs the `soundcard` package (pip install soundcard); without it, or with
no loopback device, nothing changes. AEC_SYSTEM_REFERENCE=false turns it
off; AEC_LOOPBACK_DEVICE picks a device by part of its name.
"""
import logging
import threading

from settings.config import config

logger = logging.getLogger("SystemReference")

RATE = 16000
BLOCK = 480          # 30 ms a read
_VIRTUAL = ("fxsound", "voicemeeter", "vb-audio", "cable", "virtual", "equalizer apo", "enhancer")


def _enabled() -> bool:
    return bool(getattr(config, "AEC_SYSTEM_REFERENCE", True))


def pick_loopback(sc, hint: str = ""):
    """The loopback 'microphone' to read from `sc` (the soundcard module), or None."""
    loops = [m for m in sc.all_microphones(include_loopback=True) if getattr(m, "isloopback", False)]
    if not loops:
        return None
    hint = (hint or "").strip().lower()
    if hint:
        for m in loops:
            if hint in m.name.lower():
                return m
    try:
        default = sc.default_speaker().name
    except Exception:
        default = ""
    if default and any(v in default.lower() for v in _VIRTUAL):
        for m in loops:
            if not any(v in m.name.lower() for v in _VIRTUAL):
                return m
    for m in loops:
        if m.name == default:
            return m
    return loops[0]


class SystemAudioReference:
    """A daemon thread: loopback blocks -> aec.feed_external_reference()."""

    def __init__(self, aec, hint: str = "", loader=None):
        self._aec = aec
        self._hint = hint
        self._loader = loader
        self._stop = threading.Event()
        self._thread = None
        self.device_name = ""
        self.blocks = 0
        self.last_error = ""

    def _soundcard(self):
        if self._loader is not None:
            return self._loader()
        import soundcard  # noqa: PLC0415
        return soundcard

    def start(self) -> bool:
        """True when a reader thread is running (or already was)."""
        if not _enabled() or not getattr(self._aec, "enabled", False):
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        try:
            self._soundcard()
        except Exception as e:
            logger.info(f"System-audio echo reference unavailable ({type(e).__name__}: {e}) - "
                        f"`pip install soundcard` to use it.")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="AEC-SystemReference", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import numpy as np  # noqa: PLC0415
        backoff = 1.0
        while not self._stop.is_set():
            try:
                sc = self._soundcard()
                mic = pick_loopback(sc, self._hint)
                if mic is None:
                    raise RuntimeError("no loopback device")
                if mic.name != self.device_name:
                    self.device_name = mic.name
                    logger.info(f"System-audio echo reference: '{mic.name}'.")
                with mic.recorder(samplerate=RATE, channels=1, blocksize=BLOCK) as rec:
                    backoff = 1.0
                    while not self._stop.is_set():
                        # Blocks while nothing plays - Windows only delivers loopback while a stream renders.
                        data = rec.record(numframes=BLOCK)
                        pcm = np.clip(np.asarray(data)[:, 0] * 32767.0, -32768, 32767).astype(np.int16)
                        self.blocks += 1
                        self._aec.feed_external_reference(pcm)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                logger.debug(f"System-audio reference stopped: {self.last_error}")
                if self._stop.wait(backoff):
                    return
                backoff = min(30.0, backoff * 2)
