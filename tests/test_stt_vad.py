"""SARA-roadmap Tier 7 - the STT dedicated-buffers + VAD pipeline.

Three groups:
  - CollectUtteranceTests: the core state machine (voice/stt_vad.py's
    collect_utterance()) against a canned sequence of frames and a fake
    is_speech() - no webrtcvad, no PyAudio, no real audio anywhere.
  - VadPrimitiveTests: _PreBuffer/_SilenceGate/_VADFilter in isolation,
    including _VADFilter's no-op behavior with no webrtcvad installed
    (the actual state of this sandbox) and its behavior with a fake
    webrtcvad module standing in for the real one.
  - SttVadWiringTests: voice/stt.py's listen()/_listen_via_vad() - just
    the wiring (falls back on failure, doesn't double-wait on genuine
    silence, a VAD success flows into the same shared AEC/recognition
    path as before) using a fake pyaudio input stream.

    python -m unittest discover -s tests -p "test_stt_vad.py"
"""
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)

import voice.stt_vad as stt_vad


def _silent_frame() -> bytes:
    return b"\x00\x00" * stt_vad.FRAME_SIZE


def _loud_frame() -> bytes:
    # int16 max repeated - comfortably clears any energy_floor used below.
    return b"\xff\x7f" * stt_vad.FRAME_SIZE


class _FrameFeed:
    """A read_frame() callable backed by a fixed list of frames, then b""
    (stream exhausted) forever after - exactly collect_utterance()'s
    documented "empty/short return ends capture" contract."""

    def __init__(self, frames):
        self._frames = list(frames)
        self._i = 0

    def __call__(self) -> bytes:
        if self._i >= len(self._frames):
            return b""
        f = self._frames[self._i]
        self._i += 1
        return f


class CollectUtteranceTests(unittest.TestCase):
    def setUp(self):
        self.pre_buffer = stt_vad._PreBuffer(pre_speech_ms=stt_vad.FRAME_MS * 2)

    def test_pure_silence_until_stream_ends_returns_empty(self):
        feed = _FrameFeed([_silent_frame()] * 5)
        out = stt_vad.collect_utterance(
            read_frame=feed, is_speech=lambda f: False, energy_floor=1e9,
            pre_buffer=self.pre_buffer, timeout=100.0, max_duration=10.0, silence_limit=0.5,
        )
        self.assertEqual(out, b"")

    def test_timeout_with_no_speech_returns_empty(self):
        # read_frame never runs out (timeout must be what ends this, not
        # the feed) - a silent frame generator that never exhausts.
        def never_ending_silence():
            return _silent_frame()

        out = stt_vad.collect_utterance(
            read_frame=never_ending_silence, is_speech=lambda f: False, energy_floor=1e9,
            pre_buffer=self.pre_buffer, timeout=0.01, max_duration=10.0, silence_limit=0.5,
        )
        self.assertEqual(out, b"")

    def test_vad_triggered_onset_captures_speech_and_trims_trailing_silence(self):
        # energy_floor=1e9 makes the energy term always false, isolating
        # this to a purely VAD-driven onset/continuation/end - the energy
        # term (and its smoothing-lag behavior) is covered separately by
        # the energy-only tests below.
        frames_per_sec = stt_vad.SAMPLE_RATE / stt_vad.FRAME_SIZE
        silence_limit = 0.3
        n_silence_to_end = int(silence_limit * frames_per_sec) + 1
        frames = [_silent_frame(), _loud_frame(), _loud_frame()] + [_silent_frame()] * n_silence_to_end
        is_speech_calls = iter([False, True, True] + [False] * n_silence_to_end)

        out = stt_vad.collect_utterance(
            read_frame=_FrameFeed(frames),
            is_speech=lambda f, _it=is_speech_calls: next(_it, False),
            energy_floor=1e9,
            pre_buffer=self.pre_buffer, timeout=5.0, max_duration=10.0, silence_limit=silence_limit,
        )
        # Pre-buffer held the one leading silent frame; onset fires on the
        # first VAD-positive frame; every trailing (VAD-negative) silence
        # frame is trimmed off since nothing here ever touches the energy
        # path (energy_floor=1e9), so there's no smoothing lag to absorb.
        self.assertEqual(out, _silent_frame() + _loud_frame() * 2)

    def test_energy_only_onset_without_vad_still_triggers(self):
        """is_speech() always False - onset must still fire on energy
        alone, confirming the OR-condition (VAD isn't a hard requirement)."""
        frames = [_loud_frame()] * 2 + [_silent_frame()] * 20
        out = stt_vad.collect_utterance(
            read_frame=_FrameFeed(frames), is_speech=lambda f: False, energy_floor=100.0,
            pre_buffer=self.pre_buffer, timeout=5.0, max_duration=10.0, silence_limit=0.1,
        )
        self.assertTrue(out, "a loud frame must trigger onset even with no VAD support")

    def test_pre_buffer_is_prepended_on_onset(self):
        quiet = _silent_frame()
        frames = [quiet, quiet, _loud_frame()] + [_silent_frame()] * 20
        out = stt_vad.collect_utterance(
            read_frame=_FrameFeed(frames), is_speech=lambda f: False, energy_floor=100.0,
            pre_buffer=self.pre_buffer, timeout=5.0, max_duration=10.0, silence_limit=0.1,
        )
        # The 2 pre-buffered quiet frames, then the loud onset frame, must
        # be the START of the captured audio. Exactly how many trailing
        # frames follow before the silence cut lands depends on the
        # energy-smoothing window still "remembering" the loud spike for a
        # couple of frames (by design - see collect_utterance()'s
        # docstring) - a separate concern from what this test checks.
        self.assertTrue(out.startswith(quiet + quiet + _loud_frame()))

    def test_max_duration_forces_an_end_even_without_silence(self):
        frames = [_loud_frame()] * 1000  # speech that never stops on its own
        # collect_utterance() measures WALL-CLOCK time, so a tight loop over
        # a list (real elapsed time ~microseconds) would never trip
        # max_duration on its own - advance a fake clock 10ms per call
        # instead, so 0.1s of simulated time passes after a handful of frames.
        fake_now = iter(t * 0.01 for t in range(100000))
        with mock.patch.object(stt_vad.time, "monotonic", side_effect=lambda: next(fake_now)):
            out = stt_vad.collect_utterance(
                read_frame=_FrameFeed(frames), is_speech=lambda f: True, energy_floor=100.0,
                pre_buffer=self.pre_buffer, timeout=5.0, max_duration=0.1, silence_limit=10.0,
            )
        self.assertGreater(len(out), 0)
        self.assertLess(len(out) // len(_loud_frame()), 1000, "max_duration must cut this off well before frame 1000")

    def test_stream_ending_before_any_speech_returns_empty(self):
        feed = _FrameFeed([_silent_frame()] * 3)  # exhausts, then b"" forever
        out = stt_vad.collect_utterance(
            read_frame=feed, is_speech=lambda f: False, energy_floor=1e9,
            pre_buffer=self.pre_buffer, timeout=100.0, max_duration=10.0, silence_limit=0.5,
        )
        self.assertEqual(out, b"")


class VadPrimitiveTests(unittest.TestCase):
    def test_prebuffer_drain_clears_it_and_respects_maxlen(self):
        pb = stt_vad._PreBuffer(pre_speech_ms=stt_vad.FRAME_MS * 2)
        pb.push(b"a"); pb.push(b"b"); pb.push(b"c")  # maxlen=2 -> "a" evicted
        self.assertEqual(pb.drain(), b"bc")
        self.assertEqual(pb.drain(), b"")  # drained and cleared

    def test_silence_gate_defaults_to_base_with_no_history(self):
        gate = stt_vad._SilenceGate()
        self.assertEqual(gate.suggest(0.6), 0.6)

    def test_silence_gate_tightens_after_short_utterances(self):
        gate = stt_vad._SilenceGate()
        for _ in range(5):
            gate.record(0.8)
        self.assertLess(gate.suggest(0.6), 0.6)

    def test_silence_gate_loosens_after_long_utterances(self):
        gate = stt_vad._SilenceGate()
        for _ in range(5):
            gate.record(8.0)
        self.assertGreater(gate.suggest(0.6), 0.6)

    def test_silence_gate_never_exceeds_the_1_6x_band(self):
        gate = stt_vad._SilenceGate()
        for _ in range(20):
            gate.record(100.0)
        self.assertLessEqual(gate.suggest(0.6), 0.6 * 1.6 + 1e-9)

    def test_vadfilter_is_a_safe_noop_without_webrtcvad(self):
        # This sandbox genuinely has no webrtcvad installed, so this
        # exercises the real HAS_WEBRTCVAD=False path, not a simulation.
        self.assertFalse(stt_vad.HAS_WEBRTCVAD)
        vad = stt_vad._VADFilter()
        self.assertFalse(vad.is_speech(_loud_frame()))
        self.assertFalse(vad.is_speech(b"too short"))

    def test_vadfilter_uses_a_fake_webrtcvad_when_present(self):
        fake_module = types.ModuleType("webrtcvad")

        class _FakeVad:
            def __init__(self, level):
                self.level = level

            def is_speech(self, frame, sample_rate):
                return frame == _loud_frame()

        fake_module.Vad = _FakeVad
        with mock.patch.object(stt_vad, "webrtcvad", fake_module), \
             mock.patch.object(stt_vad, "HAS_WEBRTCVAD", True):
            vad = stt_vad._VADFilter(aggressiveness=3)
            self.assertTrue(vad.is_speech(_loud_frame()))
            self.assertFalse(vad.is_speech(_silent_frame()))

    def test_vadfilter_clamps_aggressiveness_to_0_3(self):
        fake_module = types.ModuleType("webrtcvad")
        seen = {}

        class _FakeVad:
            def __init__(self, level):
                seen["level"] = level

            def is_speech(self, frame, sample_rate):
                return False

        fake_module.Vad = _FakeVad
        with mock.patch.object(stt_vad, "webrtcvad", fake_module), \
             mock.patch.object(stt_vad, "HAS_WEBRTCVAD", True):
            stt_vad._VADFilter(aggressiveness=99)
            self.assertEqual(seen["level"], 3)


def _install_stt_fakes(device_name="Laptop Speakers", vad_frames=None):
    """A fuller fake pyaudio than test_phase5_stt_aec_wiring.py needs
    (that one never opens a real input stream) - this one provides
    PyAudio.open() returning a controllable fake stream, plus the device-
    info calls is_headphone_connected() makes, alongside the same
    speech_recognition/pygame/edge_tts fakes used elsewhere."""
    fake_sr = types.ModuleType("speech_recognition")

    class _FakeAudioData:
        def __init__(self, raw, rate, width):
            self.raw, self.rate, self.width = raw, rate, width

        def get_raw_data(self, convert_rate=None, convert_width=None):
            return self.raw

    class _FakeRecognizer:
        def __init__(self):
            self.energy_threshold = 300
            self.pause_threshold = 0.8
            self.non_speaking_duration = 0.3
            self.dynamic_energy_threshold = True

        def adjust_for_ambient_noise(self, source, duration=1):
            pass

        def listen(self, source, timeout=None, phrase_time_limit=None):
            raise AssertionError("the old capture path should not run when VAD succeeds")

        def recognize_google(self, audio, language=None):
            return "fallback text"

    class _FakeMicrophone:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class WaitTimeoutError(Exception):
        pass

    class UnknownValueError(Exception):
        pass

    class RequestError(Exception):
        pass

    fake_sr.AudioData = _FakeAudioData
    fake_sr.Recognizer = _FakeRecognizer
    fake_sr.Microphone = _FakeMicrophone
    fake_sr.WaitTimeoutError = WaitTimeoutError
    fake_sr.UnknownValueError = UnknownValueError
    fake_sr.RequestError = RequestError
    sys.modules["speech_recognition"] = fake_sr

    class _FakeStream:
        def __init__(self, frames):
            self._frames = list(frames or [])
            self._i = 0

        def read(self, n, exception_on_overflow=False):
            if self._i >= len(self._frames):
                return b""
            f = self._frames[self._i]
            self._i += 1
            return f

        def stop_stream(self):
            pass

        def close(self):
            pass

    class _FakePyAudio:
        def __init__(self):
            pass

        def open(self, **kwargs):
            return _FakeStream(vad_frames)

        def terminate(self):
            pass

        def get_default_host_api_info(self):
            return {"defaultOutputDevice": 0}

        def get_device_info_by_index(self, idx):
            return {"name": device_name}

    fake_pyaudio = types.ModuleType("pyaudio")
    fake_pyaudio.PyAudio = _FakePyAudio
    fake_pyaudio.paInt16 = 8
    sys.modules["pyaudio"] = fake_pyaudio

    fake_pygame = types.ModuleType("pygame")
    fake_mixer = types.SimpleNamespace(
        init=lambda **kw: None, get_init=lambda: (24000, 1), stop=lambda: None,
        music=types.SimpleNamespace(unload=lambda: None, stop=lambda: None, set_volume=lambda v: None),
    )
    fake_pygame.mixer = fake_mixer
    fake_pygame.time = types.SimpleNamespace(wait=lambda ms: None)
    fake_pygame.sndarray = types.SimpleNamespace(array=lambda s: None, make_sound=lambda a: None)
    sys.modules["pygame"] = fake_pygame

    fake_edge_tts = types.ModuleType("edge_tts")

    class _FC:
        def __init__(self, *a, **k):
            pass

        async def save(self, out_path):
            with open(out_path, "wb") as f:
                f.write(b"FAKE")

    fake_edge_tts.Communicate = _FC
    sys.modules["edge_tts"] = fake_edge_tts
    sys.modules.pop("sounddevice", None)


class SttVadWiringTests(unittest.TestCase):
    def _reload(self, device_name="Laptop Speakers", vad_frames=None):
        _install_stt_fakes(device_name=device_name, vad_frames=vad_frames)
        for mod in ("voice.stt", "voice.stt_vad", "voice.tts", "voice.tts_cache", "voice.aec"):
            sys.modules.pop(mod, None)
        import voice.stt as sttmod  # noqa: PLC0415

        self.sttmod = sttmod
        self.addCleanup(self._cleanup)
        return sttmod

    def _cleanup(self):
        for mod in ("speech_recognition", "pyaudio", "pygame", "edge_tts",
                    "voice.stt", "voice.stt_vad", "voice.tts", "voice.tts_cache", "voice.aec"):
            sys.modules.pop(mod, None)

    def test_vad_disabled_by_config_never_calls_vad_path(self):
        sttmod = self._reload()
        engine = sttmod.SpeechReceiver.__new__(sttmod.SpeechReceiver)
        with mock.patch.object(sttmod.config, "STT_VAD_ENABLED", False), \
             mock.patch.object(engine, "_listen_via_vad") as m_vad, \
             mock.patch.object(engine, "_listen_via_speech_recognition", return_value=None) as m_old:
            self.assertIsNone(sttmod.SpeechReceiver.listen(engine))
            m_vad.assert_not_called()
            m_old.assert_called_once()

    def test_vad_unavailable_falls_back_without_double_silence_wait(self):
        sttmod = self._reload()
        engine = sttmod.SpeechReceiver.__new__(sttmod.SpeechReceiver)
        with mock.patch.object(sttmod.config, "STT_VAD_ENABLED", True), \
             mock.patch.object(sttmod.stt_vad, "HAS_WEBRTCVAD", True), \
             mock.patch.object(engine, "_listen_via_vad", return_value=sttmod._VAD_UNAVAILABLE), \
             mock.patch.object(engine, "_listen_via_speech_recognition", return_value=None) as m_old:
            self.assertIsNone(sttmod.SpeechReceiver.listen(engine))
            m_old.assert_called_once()

    def test_vad_genuine_silence_does_not_fall_back_to_old_path(self):
        sttmod = self._reload()
        engine = sttmod.SpeechReceiver.__new__(sttmod.SpeechReceiver)
        with mock.patch.object(sttmod.config, "STT_VAD_ENABLED", True), \
             mock.patch.object(sttmod.stt_vad, "HAS_WEBRTCVAD", True), \
             mock.patch.object(engine, "_listen_via_vad", return_value=None), \
             mock.patch.object(engine, "_listen_via_speech_recognition") as m_old:
            self.assertIsNone(sttmod.SpeechReceiver.listen(engine))
            m_old.assert_not_called()

    def test_vad_success_flows_into_the_shared_recognition_path(self):
        sttmod = self._reload()
        engine = sttmod.SpeechReceiver.__new__(sttmod.SpeechReceiver)
        audio = object()
        with mock.patch.object(sttmod.config, "STT_VAD_ENABLED", True), \
             mock.patch.object(sttmod.stt_vad, "HAS_WEBRTCVAD", True), \
             mock.patch.object(engine, "_listen_via_vad", return_value=audio), \
             mock.patch.object(engine, "_apply_aec", side_effect=lambda a: a) as m_aec, \
             mock.patch.object(engine, "_recognize_with_whisper", return_value="hello"), \
             mock.patch.object(engine, "_listen_via_speech_recognition") as m_old, \
             mock.patch.object(sttmod.tts_engine, "stop"):
            result = sttmod.SpeechReceiver.listen(engine)
            self.assertEqual(result, "hello")
            m_aec.assert_called_once_with(audio)
            m_old.assert_not_called()

    def test_end_to_end_vad_capture_via_fake_pyaudio_stream(self):
        """No method-level mocking of _listen_via_vad itself - drives a
        real fake PyAudio stream through collect_utterance() and confirms
        a loud-enough synthetic utterance comes out the other side as
        text, via the REAL _listen_via_vad()."""
        frames_per_sec = 16000 / 480  # SAMPLE_RATE / FRAME_SIZE, avoiding an import-order dependency
        # +10 frames of margin over the bare silence_limit math: the energy
        # window's 3-frame smoothing (see collect_utterance()'s docstring)
        # keeps a couple of post-onset frames reading as "voiced" a little
        # after the real loud frames end, same as in the unit tests above -
        # this just needs to comfortably clear that, not hit an exact count.
        n_silence = int(0.6 * frames_per_sec) + 10
        vad_frames = [_loud_frame()] * 3 + [_silent_frame()] * n_silence
        sttmod = self._reload(device_name="Laptop Speakers", vad_frames=vad_frames)
        engine = sttmod.SpeechReceiver.__new__(sttmod.SpeechReceiver)
        engine.recognizer = sttmod.sr.Recognizer()
        engine.baseline_energy = 0.0
        engine._vad = sttmod.stt_vad._VADFilter()
        engine._pre_buffer = sttmod.stt_vad._PreBuffer()
        engine._silence_gate = sttmod.stt_vad._SilenceGate()

        with mock.patch.object(sttmod.tts_engine, "is_speaking", False):
            audio = engine._listen_via_vad()
        self.assertIsNotNone(audio)
        self.assertNotEqual(audio, sttmod._VAD_UNAVAILABLE)
        self.assertGreater(len(audio.raw), 0)
