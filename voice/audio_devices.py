"""
voice/audio_devices.py
=============================================================================
Tier 21 roadmap item: "real mic and speaker picker with a level test" in the
setup wizard (ui/onboarding.py). Everything here degrades gracefully to an
empty list / 0.0 / False rather than raising or fabricating a number - there
is no real audio hardware in this project's own sandbox, so the honest
behavior on a machine with nothing to enumerate or record is "found
nothing" / "measured silence", never a made-up reading.

Kept separate from voice/stt.py (which already owns ongoing, continuous
mic capture) because this module's job is one-shot: list devices, measure
a level, play a test tone - exactly what a setup wizard's "Test" buttons
need, each callable from a worker thread so it never blocks the UI.
"""
import logging
import math
from array import array

logger = logging.getLogger("AudioDevices")

try:
    import pyaudio
    HAS_PYAUDIO = True
except ImportError:
    pyaudio = None
    HAS_PYAUDIO = False

try:
    import speech_recognition as sr
    HAS_SR = True
except ImportError:
    sr = None
    HAS_SR = False


def list_input_devices():
    """[(index, name), ...] for every microphone speech_recognition can see
    (the same enumeration voice/stt.py's SpeechReceiver ultimately relies
    on via sr.Microphone). Returns [] - never raises - if nothing can be
    listed."""
    if not HAS_SR:
        return []
    try:
        return list(enumerate(sr.Microphone.list_microphone_names()))
    except Exception:
        logger.debug("couldn't list microphones", exc_info=True)
        return []


def list_output_devices():
    """[(index, name), ...] for every PyAudio output-capable device.
    Returns [] - never raises - if PyAudio isn't installed or enumeration
    fails (no audio subsystem in this sandbox, a missing driver, ...)."""
    if not HAS_PYAUDIO:
        return []
    pa = None
    devices = []
    try:
        pa = pyaudio.PyAudio()
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxOutputChannels", 0) > 0:
                devices.append((i, info.get("name", f"Device {i}")))
    except Exception:
        logger.debug("couldn't list output devices", exc_info=True)
        return []
    finally:
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
    return devices


def measure_input_level(device_index=None, duration: float = 1.0) -> float:
    """Records `duration` seconds from `device_index` (None = system
    default) and returns a 0-100 loudness estimate (RMS, lightly gained so
    ordinary speech doesn't read as a sliver). Returns 0.0 - never raises -
    if the device can't be opened or read."""
    if not HAS_PYAUDIO:
        return 0.0
    pa = None
    stream = None
    try:
        pa = pyaudio.PyAudio()
        rate, chunk = 16000, 1024
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate, input=True,
                          input_device_index=device_index, frames_per_buffer=chunk)
        samples = array("h")
        for _ in range(max(1, int(rate / chunk * duration))):
            samples.frombytes(stream.read(chunk, exception_on_overflow=False))
        if not samples:
            return 0.0
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        return min(100.0, (rms / 32768.0) * 100.0 * 6.0)
    except Exception:
        logger.debug("mic level test failed", exc_info=True)
        return 0.0
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass


def play_test_tone(device_index=None, duration: float = 0.4, freq: float = 440.0) -> bool:
    """Plays a short sine-wave beep through `device_index` (None = system
    default output). Returns whether it actually played - never raises."""
    if not HAS_PYAUDIO:
        return False
    pa = None
    stream = None
    try:
        pa = pyaudio.PyAudio()
        rate = 16000
        n = int(rate * duration)
        buf = array("h", [int(12000 * math.sin(2 * math.pi * freq * (i / rate))) for i in range(n)])
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate,
                          output=True, output_device_index=device_index)
        stream.write(buf.tobytes())
        return True
    except Exception:
        logger.debug("test tone playback failed", exc_info=True)
        return False
    finally:
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
