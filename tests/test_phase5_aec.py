"""Phase 5 - real AEC (voice/aec.py).

Runs against a FAKE aec_audio_processing.AudioProcessor (mimicking its
documented API: set_stream_format/set_reverse_stream_format/
set_stream_delay/process_stream/has_voice/*_enabled) so this needs no
native library, no audio hardware, and no network - pure logic:
frame re-chunking, padding/trim correctness, the rate-limited error-log
helper, the buffer growth guards, and the far-end-method name probe.
numpy is required (as it already is by faster-whisper in
requirements.txt); skipped automatically if it's ever missing.

    python -m unittest discover -s tests -p "test_phase5_*.py"
    (pytest will also collect these if you have it)
"""
import importlib
import sys
import time
import types
import unittest

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


class _FakeAudioProcessor:
    """Stands in for aec_audio_processing.AudioProcessor. Records every
    frame it's given so tests can assert on exact call counts/sizes,
    and just echoes near-end frames back unchanged (a real
    "cancellation" isn't the point here - the re-chunking logic around
    it is)."""

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.stream_delay_ms = None
        self.near_calls = []
        self.reverse_calls = []

    def set_stream_format(self, **kwargs):
        self.stream_format = kwargs

    def set_reverse_stream_format(self, rate, channels):
        self.reverse_format = (rate, channels)

    def set_stream_delay(self, ms):
        self.stream_delay_ms = ms

    def process_stream(self, frame_bytes):
        self.near_calls.append(frame_bytes)
        return frame_bytes

    def process_reverse_stream(self, frame_bytes):
        self.reverse_calls.append(frame_bytes)

    def has_voice(self):
        return True

    def aec_enabled(self):
        return True

    def ns_enabled(self):
        return True

    def agc_enabled(self):
        return False


class _FakeAudioProcessorNoReverseMethod:
    """Same as above but WITHOUT any of the candidate far-end method
    names - exercises the "couldn't find it, warn and disable far-end
    feeding without crashing" path."""

    def __init__(self, **kwargs):
        pass

    def set_stream_format(self, **kwargs):
        pass

    def set_reverse_stream_format(self, rate, channels):
        pass

    def set_stream_delay(self, ms):
        pass

    def process_stream(self, frame_bytes):
        return frame_bytes

    def has_voice(self):
        return False


def _install_fake_lib(processor_cls):
    """Injects a fake aec_audio_processing module into sys.modules and
    (re)imports voice.aec against it, returning the fresh module."""
    fake_mod = types.ModuleType("aec_audio_processing")
    fake_mod.AudioProcessor = processor_cls
    sys.modules["aec_audio_processing"] = fake_mod
    sys.modules.pop("voice.aec", None)
    import voice.aec as aecmod  # noqa: PLC0415

    return aecmod


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class AECFrameHandlingTests(unittest.TestCase):
    def setUp(self):
        self.aecmod = _install_fake_lib(_FakeAudioProcessor)
        self.addCleanup(sys.modules.pop, "aec_audio_processing", None)

    def test_disabled_without_the_native_library(self):
        fake_mod = types.ModuleType("aec_audio_processing_missing")
        # Simulate the library genuinely not being installed by removing it
        # and reloading - HAS_AEC_LIB should go False and the processor
        # should be a safe no-op passthrough, never raising.
        sys.modules.pop("aec_audio_processing", None)
        sys.modules.pop("voice.aec", None)

        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *a, **k):
            if name == "aec_audio_processing":
                raise ImportError("simulated: not installed")
            return real_import(name, *a, **k)

        builtins.__import__ = blocking_import
        try:
            import voice.aec as aecmod
        finally:
            builtins.__import__ = real_import

        self.assertFalse(aecmod.HAS_AEC_LIB)
        p = aecmod.AECProcessor(sample_rate=16000)
        self.assertFalse(p.enabled)
        # Must be a harmless no-op, not a crash, with nothing installed.
        self.assertEqual(p.process_near_end(b"\x00" * 777), b"\x00" * 777)
        p.feed_far_end(np.zeros(100, dtype=np.int16), 16000)  # must not raise
        del fake_mod  # unused, just documenting intent

    def test_resolves_process_reverse_stream_by_default(self):
        p = self.aecmod.AECProcessor(sample_rate=16000)
        self.assertTrue(p.enabled)
        self.assertEqual(p._reverse_method_name, "process_reverse_stream")
        self.assertEqual(p._frame_bytes, 320)  # 16000Hz * 10ms * 2 bytes/sample

    def test_feed_far_end_chunks_into_10ms_frames_and_buffers_remainder(self):
        p = self.aecmod.AECProcessor(sample_rate=16000)
        # 25ms of audio -> exactly two 10ms frames processed, 5ms buffered.
        samples = np.zeros(int(16000 * 0.025), dtype=np.int16)
        p.feed_far_end(samples, 16000)
        self.assertEqual(len(p._ap.reverse_calls), 2)
        self.assertEqual(len(p._far_buf), 160)  # 5ms * 16000Hz * 2 bytes = 160
        for frame in p._ap.reverse_calls:
            self.assertEqual(len(frame), 320)

    def test_feed_far_end_resamples_when_source_rate_differs(self):
        p = self.aecmod.AECProcessor(sample_rate=16000)
        # 20ms of 8kHz audio should become ~20ms of 16kHz audio (2 frames),
        # not silently misinterpreted at the wrong rate.
        samples = np.zeros(int(8000 * 0.020), dtype=np.int16)
        p.feed_far_end(samples, 8000)
        self.assertEqual(len(p._ap.reverse_calls), 2)

    def test_process_near_end_output_length_always_matches_input(self):
        """The real caller (stt.py) hands this one whole captured
        utterance and expects every byte back - a non-frame-aligned
        size must come back out at the SAME length, not truncated to
        the nearest 10ms frame."""
        p = self.aecmod.AECProcessor(sample_rate=16000)
        for size in (0, 1, 160, 319, 320, 321, 777, 3201):
            with self.subTest(size=size):
                raw = bytes(range(256)) * (size // 256 + 1)
                raw = raw[:size]
                out = p.process_near_end(raw)
                self.assertEqual(len(out), size)

    def test_process_near_end_calls_process_stream_once_per_frame(self):
        p = self.aecmod.AECProcessor(sample_rate=16000)
        p.process_near_end(bytes(777))  # 777 -> pad to 960 = 3 frames
        self.assertEqual(len(p._ap.near_calls), 3)
        self.assertTrue(all(len(f) == 320 for f in p._ap.near_calls))

    def test_near_end_growth_guard_resets_instead_of_growing_unboundedly(self):
        p = self.aecmod.AECProcessor(sample_rate=16000)
        huge = bytes(p._frame_bytes * 100)  # well past _BUF_MAX_FRAMES
        out = p.process_near_end(huge)
        self.assertEqual(out, huge)  # fails safe: hands the caller its audio back
        self.assertEqual(len(p._near_buf), 0)

    def test_far_end_growth_guard_clears_when_no_reverse_method(self):
        p = _install_fake_lib(_FakeAudioProcessorNoReverseMethod).AECProcessor(sample_rate=16000)
        self.assertIsNone(p._reverse_method_name)
        huge = np.zeros(p._frame_bytes * 100, dtype=np.int16)
        p.feed_far_end(huge, 16000)  # must not raise even with nowhere to send it
        self.assertEqual(len(p._far_buf), 0)

    def test_process_near_end_is_a_passthrough_when_disabled(self):
        p = self.aecmod.AECProcessor(sample_rate=16000)
        p.enabled = False
        raw = b"not actually touched"
        self.assertEqual(p.process_near_end(raw), raw)
        self.assertEqual(len(p._ap.near_calls), 0)

    def test_process_near_end_fails_safe_on_processing_error(self):
        p = self.aecmod.AECProcessor(sample_rate=16000)

        def boom(frame):
            raise RuntimeError("simulated native failure")

        p._ap.process_stream = boom
        raw = bytes(320)
        out = p.process_near_end(raw)
        self.assertEqual(out, raw)  # original frame passed through, not dropped
        self.assertTrue(p.enabled)  # a single bad frame shouldn't disable AEC entirely

    def test_close_is_idempotent_and_disables(self):
        p = self.aecmod.AECProcessor(sample_rate=16000)
        p.close()
        p.close()  # must not raise the second time
        self.assertFalse(p.enabled)
        self.assertEqual(p.process_near_end(b"x" * 320), b"x" * 320)


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class RateLimitedLoggingTests(unittest.TestCase):
    def setUp(self):
        self.aecmod = _install_fake_lib(_FakeAudioProcessor)
        self.addCleanup(sys.modules.pop, "aec_audio_processing", None)
        self.p = self.aecmod.AECProcessor(sample_rate=16000)

    def test_first_call_always_logs_then_suppresses_until_elapsed(self):
        self.assertTrue(self.p._should_log_now("far_end"))
        self.assertFalse(self.p._should_log_now("far_end"))  # immediate repeat: suppressed
        # Simulate enough time passing without an actual 60s sleep.
        self.p._last_far_end_error_log = time.monotonic() - 61
        self.assertTrue(self.p._should_log_now("far_end"))

    def test_categories_are_independent(self):
        self.assertTrue(self.p._should_log_now("process"))
        # A different category's rate limit shouldn't be affected by "process".
        self.assertTrue(self.p._should_log_now("far_end"))


@unittest.skipUnless(HAS_NUMPY, "numpy not installed")
class ResampleLinearTests(unittest.TestCase):
    def setUp(self):
        self.aecmod = _install_fake_lib(_FakeAudioProcessor)
        self.addCleanup(sys.modules.pop, "aec_audio_processing", None)

    def test_same_rate_is_a_true_passthrough(self):
        arr = np.array([1, -2, 3, -4], dtype=np.int16)
        out = self.aecmod.resample_linear(arr, 16000, 16000)
        self.assertIs(out, arr)

    def test_upsampling_roughly_doubles_length(self):
        arr = np.zeros(100, dtype=np.int16)
        out = self.aecmod.resample_linear(arr, 8000, 16000)
        self.assertEqual(len(out), 200)
        self.assertEqual(out.dtype, np.int16)

    def test_downsampling_roughly_halves_length(self):
        arr = np.zeros(100, dtype=np.int16)
        out = self.aecmod.resample_linear(arr, 16000, 8000)
        self.assertEqual(len(out), 50)

    def test_never_overflows_int16_range(self):
        arr = np.full(10, 32767, dtype=np.int16)
        out = self.aecmod.resample_linear(arr, 8000, 16000)
        self.assertTrue(np.all(out <= 32767))
        self.assertTrue(np.all(out >= -32768))

    def test_empty_input_does_not_raise(self):
        arr = np.array([], dtype=np.int16)
        out = self.aecmod.resample_linear(arr, 8000, 16000)
        self.assertEqual(len(out), 0)


if __name__ == "__main__":
    unittest.main()
