"""
tests/test_voice_buffers.py — Tier 7: voice/buffers.py's FrameBuffer and
VAD backends (EnergyVad always, WebRtcVad only if the optional
'webrtcvad' package happens to be installed). All synthetic PCM bytes -
no microphone, no real audio hardware, no optional dependency required
for the suite to pass.

    python -m unittest discover -s tests -p "test_voice_buffers.py"
"""
import struct
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)

from voice.buffers import EnergyVad, FrameBuffer, HAS_WEBRTCVAD, WebRtcVad, get_vad


def _pcm16(*samples: int) -> bytes:
    """Packs int16 samples into little-endian raw PCM bytes."""
    return struct.pack(f"<{len(samples)}h", *samples)


def _silence(n_samples: int) -> bytes:
    return _pcm16(*([0] * n_samples))


def _tone(n_samples: int, amplitude: int = 12000) -> bytes:
    """A crude but effective "definitely not silence" signal - alternating
    +/- amplitude, well above any sane energy threshold, without needing
    a real sine-wave generator for what's just a speech-vs-not test."""
    return _pcm16(*([amplitude, -amplitude] * (n_samples // 2) + ([amplitude] if n_samples % 2 else [])))


class FrameBufferTests(unittest.TestCase):
    def test_splits_into_the_expected_number_of_fixed_size_frames(self):
        # 16kHz, 16-bit mono, 20ms frames -> 320 samples -> 640 bytes/frame.
        fb = FrameBuffer(sample_rate=16000, sample_width=2, frame_ms=20)
        self.assertEqual(fb.frame_bytes, 640)
        pcm = _silence(320 * 3)  # exactly 3 whole frames
        frames = fb.split(pcm)
        self.assertEqual(len(frames), 3)
        self.assertTrue(all(len(f) == 640 for f in frames))

    def test_drops_a_trailing_partial_frame_rather_than_padding_it(self):
        fb = FrameBuffer(sample_rate=16000, sample_width=2, frame_ms=20)
        pcm = _silence(320 * 2 + 50)  # 2 whole frames plus a short remainder
        frames = fb.split(pcm)
        self.assertEqual(len(frames), 2)

    def test_empty_input_produces_no_frames(self):
        fb = FrameBuffer(sample_rate=16000)
        self.assertEqual(fb.split(b""), [])

    def test_shorter_than_one_frame_produces_no_frames(self):
        fb = FrameBuffer(sample_rate=16000, sample_width=2, frame_ms=20)
        self.assertEqual(fb.split(_silence(10)), [])


class EnergyVadTests(unittest.TestCase):
    def setUp(self):
        self.vad = EnergyVad(threshold=150.0, frame_ms=20)

    def test_pure_silence_has_no_speech(self):
        self.assertFalse(self.vad.contains_speech(_silence(16000), 16000))

    def test_a_loud_tone_is_detected_as_speech(self):
        self.assertTrue(self.vad.contains_speech(_tone(16000), 16000))

    def test_speech_in_only_one_frame_of_many_is_still_detected(self):
        """contains_speech means ANY frame qualifies, not the average over
        the whole utterance - a short word inside an otherwise-silent
        capture must not get diluted away."""
        silence_before = _silence(320 * 10)
        one_loud_frame = _tone(320)
        silence_after = _silence(320 * 10)
        pcm = silence_before + one_loud_frame + silence_after
        self.assertTrue(self.vad.contains_speech(pcm, 16000))

    def test_empty_bytes_has_no_speech(self):
        self.assertFalse(self.vad.contains_speech(b"", 16000))

    def test_unsupported_sample_width_is_not_scored_and_defaults_to_true(self):
        """Can't analyze 8-bit or 32-bit PCM with this backend - the
        honest answer is "can't tell", and the safe default for "can't
        tell" is to NOT drop the audio, never to silently treat it as
        confirmed-silence."""
        self.assertTrue(self.vad.contains_speech(b"\x01\x02\x03", 16000, sample_width=1))

    def test_threshold_is_configurable(self):
        quiet_vad = EnergyVad(threshold=5.0)
        loud_vad = EnergyVad(threshold=30000.0)
        moderate = _tone(16000, amplitude=1000)
        self.assertTrue(quiet_vad.contains_speech(moderate, 16000))
        self.assertFalse(loud_vad.contains_speech(moderate, 16000))


@unittest.skipUnless(HAS_WEBRTCVAD, "webrtcvad not installed")
class WebRtcVadTests(unittest.TestCase):
    def test_pure_silence_has_no_speech(self):
        vad = WebRtcVad(aggressiveness=2)
        self.assertFalse(vad.contains_speech(_silence(16000), 16000))

    def test_unsupported_sample_rate_is_not_scored_and_defaults_to_true(self):
        vad = WebRtcVad(aggressiveness=2)
        self.assertTrue(vad.contains_speech(_tone(1000), 22050))

    def test_aggressiveness_is_clamped_to_the_valid_0_to_3_range(self):
        # Must not raise for an out-of-range value - webrtcvad.Vad() itself
        # would reject anything outside 0-3.
        WebRtcVad(aggressiveness=99)
        WebRtcVad(aggressiveness=-5)


class GetVadTests(unittest.TestCase):
    def test_backend_energy_always_returns_energy_vad_even_if_webrtcvad_is_installed(self):
        with mock.patch("settings.config.config.VAD_BACKEND", "energy"):
            self.assertIsInstance(get_vad(), EnergyVad)

    def test_default_auto_backend_never_raises_regardless_of_what_is_installed(self):
        # Whichever backend this sandbox actually has, get_vad() must
        # hand back SOMETHING usable, not raise.
        vad = get_vad()
        self.assertTrue(hasattr(vad, "contains_speech"))
        vad.contains_speech(_silence(1600), 16000)  # must not raise

    def test_energy_threshold_config_is_applied(self):
        with mock.patch("settings.config.config.VAD_BACKEND", "energy"), \
             mock.patch("settings.config.config.VAD_ENERGY_THRESHOLD", 99999.0):
            vad = get_vad()
        self.assertEqual(vad.threshold, 99999.0)
        self.assertFalse(vad.contains_speech(_tone(16000, amplitude=5000), 16000))


if __name__ == "__main__":
    unittest.main()
