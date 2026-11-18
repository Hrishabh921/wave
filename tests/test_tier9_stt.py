"""Tier 9 - logic tests for voice/stt.py's SpeechReceiver, beyond what
test_phase5_stt_aec_wiring.py already covers.

That file's 9 tests are almost entirely about _apply_aec() (3 tests) and
_passes_vad() (4 tests), plus 2 tests proving listen() respects a False
VAD verdict. That leaves listen()'s OWN substantial logic completely
untested: the headphone-vs-speaker dynamic-sensitivity branching, the
local-Whisper-then-Google fallback order, and the three-way
None/""/text return contract its own docstring spells out (including the
deliberate "only stop TTS on CONFIRMED real speech" fix) - plus
_scaled_threshold(), set_language(), is_headphone_connected(),
_whisper_language_code(), _get_whisper_model() and
_recognize_with_whisper(), none of which are exercised by any existing
test at all (every existing listen() test mocks _recognize_with_whisper
straight out).

Reuses test_phase5_stt_aec_wiring.py's fake speech_recognition/pyaudio/
pygame/edge_tts harness rather than redefining it.

    python -m unittest discover -s tests -p "test_tier9_stt.py"
"""
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)

from test_phase5_stt_aec_wiring import _install_fakes, _FakeAudioData


def _fake_pyaudio_module(device_name=None, construct_raises=False):
    """Stands in for the real `pyaudio` module just enough for
    is_headphone_connected()'s PyAudio().get_default_host_api_info() /
    get_device_info_by_index() / terminate() chain."""
    mod = types.ModuleType("pyaudio")

    class _FakePyAudio:
        def __init__(self):
            if construct_raises:
                raise RuntimeError("no audio backend available")

        def get_default_host_api_info(self):
            return {"defaultOutputDevice": 0}

        def get_device_info_by_index(self, idx):
            return {"name": device_name or ""}

        def terminate(self):
            pass

    mod.PyAudio = _FakePyAudio
    return mod


class _FakeArray:
    """Stands in for a numpy array through exactly the chain
    _recognize_with_whisper() uses it: .astype(...) then / 32768.0 -
    without needing real numpy installed or caring about real numeric
    values (the fake model.transcribe() below never inspects it)."""

    def astype(self, dtype):
        return self

    def __truediv__(self, other):
        return self


def _fake_numpy_module():
    return types.SimpleNamespace(
        frombuffer=mock.Mock(return_value=_FakeArray()),
        int16="int16", float32="float32",
    )


class _Segment:
    def __init__(self, text):
        self.text = text


class _BaseSTTTestCase(unittest.TestCase):
    """Shared setUp/tearDown for every class below: fresh fake
    speech_recognition/pyaudio/pygame/edge_tts modules, a real
    SpeechReceiver built on top of them (constructor-time mic
    calibration is a no-op under the fakes), cleaned up after."""

    def setUp(self):
        _install_fakes(self)
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.stt as sttmod  # noqa: PLC0415

        self.sttmod = sttmod
        self.receiver = sttmod.SpeechReceiver()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)


class ScaledThresholdTests(_BaseSTTTestCase):
    def test_floor_wins_in_a_quiet_room(self):
        self.receiver.baseline_energy = 10.0
        self.assertEqual(self.receiver._scaled_threshold(200, 1.0, 500), 200)

    def test_scales_up_with_a_noisier_room(self):
        self.receiver.baseline_energy = 300.0
        self.assertEqual(self.receiver._scaled_threshold(200, 1.0, 500), 300.0)

    def test_ceiling_caps_an_extremely_noisy_room(self):
        self.receiver.baseline_energy = 10_000.0
        self.assertEqual(self.receiver._scaled_threshold(200, 1.0, 500), 500)


class RecalibrateTests(_BaseSTTTestCase):
    """Tier 21: recalibrate() - pulled out of __init__'s inline calibration
    so the setup wizard's "Wake Word Calibration" step can re-measure
    ambient noise on demand instead of only ever getting the one reading
    taken at startup."""

    def test_recalibrate_returns_the_three_measured_numbers(self):
        self.receiver.recognizer.energy_threshold = 123.0
        result = self.receiver.recalibrate()
        self.assertEqual(result["baseline_energy"], 123.0)
        self.assertEqual(result["idle_threshold"], self.receiver._scaled_threshold(200, 1.0, 500))
        self.assertEqual(result["headphone_threshold"], self.receiver._scaled_threshold(400, 2.0, 1000))

    def test_recalibrate_updates_baseline_energy_in_place(self):
        self.receiver.baseline_energy = 1.0
        self.receiver.recognizer.energy_threshold = 999.0
        self.receiver.recalibrate()
        self.assertEqual(self.receiver.baseline_energy, 999.0)

    def test_recalibrate_never_raises_even_if_the_microphone_is_unavailable(self):
        def _boom(*a, **k):
            raise OSError("no default input device")
        with mock.patch.object(self.sttmod.sr, "Microphone", side_effect=_boom):
            result = self.receiver.recalibrate()   # must not raise
        self.assertIn("baseline_energy", result)

    def test_initial_calibration_failure_at_construction_falls_back_to_zero(self):
        """Same not-fatal-at-startup guarantee the original inline version
        had (a bad reading must never stop the app starting), now routed
        through the shared _calibrate() helper instead of being special-
        cased only in __init__."""
        _install_fakes(self)
        import importlib
        sttmod = importlib.reload(self.sttmod)
        with mock.patch.object(sttmod.sr, "Microphone", side_effect=OSError("no mic")):
            receiver = sttmod.SpeechReceiver()   # must not raise
        self.assertEqual(receiver.baseline_energy, 0.0)


class SetLanguageTests(_BaseSTTTestCase):
    def test_sets_the_language_attribute_verbatim(self):
        self.receiver.set_language("hi-IN")
        self.assertEqual(self.receiver.language, "hi-IN")


class WhisperLanguageCodeTests(_BaseSTTTestCase):
    def test_narrows_a_google_style_tag_to_the_bare_iso_code(self):
        self.receiver.language = "hi-IN"
        self.assertEqual(self.receiver._whisper_language_code(), "hi")

    def test_lowercases_the_result(self):
        self.receiver.language = "EN-US"
        self.assertEqual(self.receiver._whisper_language_code(), "en")

    def test_defaults_to_english_when_language_is_falsy(self):
        self.receiver.language = ""
        self.assertEqual(self.receiver._whisper_language_code(), "en")


class IsHeadphoneConnectedTests(_BaseSTTTestCase):
    def _check(self, device_name=None, construct_raises=False):
        with mock.patch.object(self.sttmod, "pyaudio", _fake_pyaudio_module(device_name, construct_raises)):
            return self.receiver.is_headphone_connected()

    def test_true_for_a_device_name_containing_headphone(self):
        self.assertTrue(self._check("Realtek Headphone Jack"))

    def test_true_for_bluetooth_case_insensitively(self):
        self.assertTrue(self._check("BLUETOOTH Hands-Free AG Audio"))

    def test_true_for_airpods(self):
        self.assertTrue(self._check("Someone's AirPods Pro"))

    def test_false_for_a_plain_speaker_name(self):
        self.assertFalse(self._check("Speakers (Realtek High Definition Audio)"))

    def test_false_when_pyaudio_itself_is_unavailable(self):
        self.assertFalse(self._check("doesn't matter", construct_raises=True))


class GetWhisperModelTests(_BaseSTTTestCase):
    def test_disabled_when_the_package_is_not_installed(self):
        with mock.patch.object(self.sttmod, "HAS_FASTER_WHISPER", False), \
             mock.patch.object(self.sttmod, "WhisperModel", mock.Mock(), create=True) as m_cls:
            self.assertIsNone(self.receiver._get_whisper_model())
        m_cls.assert_not_called()

    def test_disabled_in_settings_even_if_the_package_is_installed(self):
        with mock.patch.object(self.sttmod, "HAS_FASTER_WHISPER", True), \
             mock.patch.object(self.sttmod.config, "FASTER_WHISPER_ENABLED", False), \
             mock.patch.object(self.sttmod, "WhisperModel", mock.Mock(), create=True) as m_cls:
            self.assertIsNone(self.receiver._get_whisper_model())
        m_cls.assert_not_called()

    def test_a_cached_model_is_returned_without_reconstructing(self):
        sentinel = object()
        self.receiver._whisper_model = sentinel
        with mock.patch.object(self.sttmod, "HAS_FASTER_WHISPER", True), \
             mock.patch.object(self.sttmod.config, "FASTER_WHISPER_ENABLED", True), \
             mock.patch.object(self.sttmod, "WhisperModel", mock.Mock(), create=True) as m_cls:
            self.assertIs(self.receiver._get_whisper_model(), sentinel)
        m_cls.assert_not_called()

    def test_a_previous_load_failure_is_remembered_and_never_retried(self):
        self.receiver._whisper_load_failed = True
        with mock.patch.object(self.sttmod, "HAS_FASTER_WHISPER", True), \
             mock.patch.object(self.sttmod.config, "FASTER_WHISPER_ENABLED", True), \
             mock.patch.object(self.sttmod, "WhisperModel", mock.Mock(), create=True) as m_cls:
            self.assertIsNone(self.receiver._get_whisper_model())
        m_cls.assert_not_called()

    def test_successful_construction_is_cached_and_uses_the_configured_settings(self):
        fake_model = object()
        m_cls = mock.Mock(return_value=fake_model)
        with mock.patch.object(self.sttmod, "HAS_FASTER_WHISPER", True), \
             mock.patch.object(self.sttmod.config, "FASTER_WHISPER_ENABLED", True), \
             mock.patch.object(self.sttmod.config, "FASTER_WHISPER_MODEL", "small"), \
             mock.patch.object(self.sttmod.config, "FASTER_WHISPER_DEVICE", "cpu"), \
             mock.patch.object(self.sttmod.config, "FASTER_WHISPER_COMPUTE_TYPE", "int8"), \
             mock.patch.object(self.sttmod, "WhisperModel", m_cls, create=True):
            result = self.receiver._get_whisper_model()
        self.assertIs(result, fake_model)
        self.assertIs(self.receiver._whisper_model, fake_model)
        m_cls.assert_called_once_with("small", device="cpu", compute_type="int8")

    def test_a_construction_failure_is_cached_so_a_second_call_never_retries(self):
        m_cls = mock.Mock(side_effect=OSError("no internet to fetch weights"))
        with mock.patch.object(self.sttmod, "HAS_FASTER_WHISPER", True), \
             mock.patch.object(self.sttmod.config, "FASTER_WHISPER_ENABLED", True), \
             mock.patch.object(self.sttmod, "WhisperModel", m_cls, create=True):
            self.assertIsNone(self.receiver._get_whisper_model())
            self.assertIsNone(self.receiver._get_whisper_model())
        self.assertTrue(self.receiver._whisper_load_failed)
        m_cls.assert_called_once()  # not retried the second time


class RecognizeWithWhisperTests(_BaseSTTTestCase):
    def setUp(self):
        super().setUp()
        self.audio = _FakeAudioData(b"raw-bytes", 16000, 2)

    def test_returns_none_without_numpy(self):
        with mock.patch.object(self.sttmod, "HAS_NUMPY", False), \
             mock.patch.object(self.receiver, "_get_whisper_model") as m_get:
            self.assertIsNone(self.receiver._recognize_with_whisper(self.audio))
        m_get.assert_not_called()

    def test_returns_none_when_no_model_is_available(self):
        with mock.patch.object(self.sttmod, "HAS_NUMPY", True), \
             mock.patch.object(self.sttmod, "np", _fake_numpy_module(), create=True), \
             mock.patch.object(self.receiver, "_get_whisper_model", return_value=None):
            self.assertIsNone(self.receiver._recognize_with_whisper(self.audio))

    def test_happy_path_joins_and_strips_segment_text_and_lets_whisper_detect_language(self):
        """Tier 13: the language passed to Whisper now comes from
        voice/language.py's tracker, not self.receiver.language/Settings -
        on a fresh conversation (nothing decisive heard yet) that's
        `None`, letting Whisper detect it itself, even when Settings says
        Hindi. (self._whisper_language_code() - what this used to use -
        still exists and is still correct; it just now only feeds
        Google's fallback, covered by its own WhisperLanguageCodeTests
        above.)"""
        self.receiver.language = "hi-IN"
        fake_model = mock.Mock()
        fake_model.transcribe.return_value = ([_Segment("Namaste "), _Segment("duniya ")], object())
        with mock.patch.object(self.sttmod, "HAS_NUMPY", True), \
             mock.patch.object(self.sttmod, "np", _fake_numpy_module(), create=True), \
             mock.patch.object(self.receiver, "_get_whisper_model", return_value=fake_model):
            result = self.receiver._recognize_with_whisper(self.audio)
        self.assertEqual(result, "Namaste duniya")
        fake_model.transcribe.assert_called_once()
        args, kwargs = fake_model.transcribe.call_args
        self.assertIsNone(kwargs["language"])
        self.assertIsNone(kwargs["initial_prompt"])
        self.assertEqual(kwargs["beam_size"], 5)
        self.assertTrue(kwargs["vad_filter"])

    def test_once_the_tracker_has_decided_hindi_whisper_gets_hi_and_a_hindi_prompt(self):
        """Same call, but this time the conversation has already said
        something decisively Hindi - voice/language.py's tracker is no
        longer undecided, so Whisper gets told "hi" and a Hindi decoding
        hint, regardless of self.receiver.language."""
        self.receiver.language = "en-IN"
        self.sttmod.language.tracker.observe("आज मौसम कैसा है")
        fake_model = mock.Mock()
        fake_model.transcribe.return_value = ([_Segment("Theek "), _Segment("hai ")], object())
        with mock.patch.object(self.sttmod, "HAS_NUMPY", True), \
             mock.patch.object(self.sttmod, "np", _fake_numpy_module(), create=True), \
             mock.patch.object(self.receiver, "_get_whisper_model", return_value=fake_model):
            result = self.receiver._recognize_with_whisper(self.audio)
        self.assertEqual(result, "Theek hai")
        args, kwargs = fake_model.transcribe.call_args
        self.assertEqual(kwargs["language"], "hi")
        self.assertIsInstance(kwargs["initial_prompt"], str)
        self.assertIn("वेव", kwargs["initial_prompt"])

    def test_records_whispers_own_language_guess_for_the_tracker_to_read(self):
        """_info.language/_info.language_probability (faster-whisper's own
        detection, distinct from what WE told it to decode with) land in
        self.last_detected_language/last_language_probability - this is
        how _recognize() feeds the tracker when nothing decisive has been
        heard from the WORDS alone yet (see voice/language.py's
        observe())."""
        fake_model = mock.Mock()
        fake_info = types.SimpleNamespace(language="hi", language_probability=0.93)
        fake_model.transcribe.return_value = ([_Segment("haan ")], fake_info)
        with mock.patch.object(self.sttmod, "HAS_NUMPY", True), \
             mock.patch.object(self.sttmod, "np", _fake_numpy_module(), create=True), \
             mock.patch.object(self.receiver, "_get_whisper_model", return_value=fake_model):
            self.receiver._recognize_with_whisper(self.audio)
        self.assertEqual(self.receiver.last_detected_language, "hi")
        self.assertAlmostEqual(self.receiver.last_language_probability, 0.93)

    def test_detected_language_resets_to_none_on_every_call(self):
        """A stale guess from a PREVIOUS utterance must never survive into
        one where Whisper isn't even reached (e.g. no model available) -
        otherwise _recognize() could feed it to the tracker as if it were
        this utterance's own signal."""
        self.receiver.last_detected_language = "hi"
        self.receiver.last_language_probability = 0.9
        with mock.patch.object(self.sttmod, "HAS_NUMPY", True), \
             mock.patch.object(self.sttmod, "np", _fake_numpy_module(), create=True), \
             mock.patch.object(self.receiver, "_get_whisper_model", return_value=None):
            self.assertIsNone(self.receiver._recognize_with_whisper(self.audio))
        self.assertIsNone(self.receiver.last_detected_language)
        self.assertIsNone(self.receiver.last_language_probability)

    def test_empty_segments_returns_an_empty_string_not_none(self):
        fake_model = mock.Mock()
        fake_model.transcribe.return_value = ([], object())
        with mock.patch.object(self.sttmod, "HAS_NUMPY", True), \
             mock.patch.object(self.sttmod, "np", _fake_numpy_module(), create=True), \
             mock.patch.object(self.receiver, "_get_whisper_model", return_value=fake_model):
            self.assertEqual(self.receiver._recognize_with_whisper(self.audio), "")

    def test_a_transcription_error_fails_safe_to_none_not_a_raise(self):
        fake_model = mock.Mock()
        fake_model.transcribe.side_effect = RuntimeError("boom")
        with mock.patch.object(self.sttmod, "HAS_NUMPY", True), \
             mock.patch.object(self.sttmod, "np", _fake_numpy_module(), create=True), \
             mock.patch.object(self.receiver, "_get_whisper_model", return_value=fake_model):
            self.assertIsNone(self.receiver._recognize_with_whisper(self.audio))


# =============================================================================
# listen()'s own branching: dynamic sensitivity (headphone vs speaker,
# speaking vs not) and the three-way None / "" / text return contract its
# docstring spells out. _apply_aec() and _passes_vad() are mocked out as
# pass-through/True in every test here - both have their own dedicated
# coverage in test_phase5_stt_aec_wiring.py - so these tests isolate
# exactly the logic that was previously only reachable by accident (via
# is_headphone_connected()'s own exception-safety under the bare fake
# pyaudio module) or not reached at all.
# =============================================================================
class ListenBranchingTests(_BaseSTTTestCase):
    def setUp(self):
        super().setUp()
        self._scripted_audio = _FakeAudioData(b"captured-bytes", 16000, 2)
        self.receiver.recognizer.listen = lambda source, timeout=1, phrase_time_limit=8: self._scripted_audio
        self.receiver.recognizer.recognize_google = mock.Mock(return_value="hello there")
        # mock.DEFAULT so .start() hands back real, individually
        # reconfigurable Mocks in the returned dict (passing already-built
        # Mocks here instead would use them as-is, but patch.multiple()
        # then omits them from that dict entirely).
        self._ctx = mock.patch.multiple(
            self.receiver,
            _apply_aec=mock.DEFAULT,
            _passes_vad=mock.DEFAULT,
            _recognize_with_whisper=mock.DEFAULT,
        )
        self.mocks = self._ctx.start()
        self.addCleanup(self._ctx.stop)
        self.mocks["_apply_aec"].side_effect = lambda a: a
        self.mocks["_passes_vad"].return_value = True
        self.mocks["_recognize_with_whisper"].return_value = None  # forces Google by default
        self.sttmod.tts_engine.is_speaking = False
        self.sttmod.tts_engine.stop = mock.Mock()

    # ---------- mic-capture failures short-circuit before AEC/VAD/recognition ----------
    def test_wait_timeout_returns_none_without_touching_aec_or_vad(self):
        self.receiver.recognizer.listen = mock.Mock(side_effect=self.sttmod.sr.WaitTimeoutError())
        self.assertIsNone(self.receiver.listen())
        self.mocks["_apply_aec"].assert_not_called()

    def test_an_unexpected_capture_error_also_returns_none(self):
        self.receiver.recognizer.listen = mock.Mock(side_effect=OSError("mic unplugged"))
        self.assertIsNone(self.receiver.listen())
        self.mocks["_apply_aec"].assert_not_called()

    # ---------- dynamic sensitivity: headphone vs speaker x speaking vs not ----------
    def test_headphone_mode_while_wave_is_speaking_uses_the_short_pause_threshold(self):
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=True):
            self.sttmod.tts_engine.is_speaking = True
            self.receiver.listen()
        self.assertEqual(self.receiver.recognizer.pause_threshold, 0.35)
        self.assertEqual(self.receiver.recognizer.energy_threshold, self.receiver._scaled_threshold(400, 2.0, 1000))

    def test_headphone_mode_while_idle_uses_the_longer_pause_threshold(self):
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=True):
            self.sttmod.tts_engine.is_speaking = False
            self.receiver.listen()
        self.assertEqual(self.receiver.recognizer.pause_threshold, 0.5)

    def test_speaker_mode_while_wave_is_speaking_raises_the_energy_threshold_to_avoid_self_trigger(self):
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            self.sttmod.tts_engine.is_speaking = True
            self.receiver.listen()
        self.assertEqual(self.receiver.recognizer.energy_threshold, 1800)
        self.assertEqual(self.receiver.recognizer.pause_threshold, 0.35)

    def test_speaker_mode_while_idle_uses_the_calibrated_scaled_threshold(self):
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            self.sttmod.tts_engine.is_speaking = False
            self.receiver.listen()
        self.assertEqual(self.receiver.recognizer.energy_threshold, self.receiver._scaled_threshold(200, 1.0, 500))
        self.assertEqual(self.receiver.recognizer.pause_threshold, 0.6)

    # ---------- recognition fallback order + the three-way return contract ----------
    def test_whisper_success_is_used_directly_and_skips_google(self):
        self.mocks["_recognize_with_whisper"].return_value = "namaste"
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            result = self.receiver.listen()
        self.assertEqual(result, "namaste")
        self.assertEqual(result.engine, "whisper")
        self.receiver.recognizer.recognize_google.assert_not_called()
        # Tier 11: listen() no longer stops Wave's speech itself - the
        # mic worker's barge-in gate (voice/commands.py decide()) does,
        # because only it knows whether the speech was addressed to Wave.
        self.assertFalse(self.sttmod.tts_engine.stop.called)

    def test_whisper_unavailable_falls_back_to_google(self):
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            result = self.receiver.listen()
        self.assertEqual(result, "hello there")
        self.assertEqual(result.engine, "google")
        self.receiver.recognizer.recognize_google.assert_called_once()
        self.assertFalse(self.sttmod.tts_engine.stop.called)   # see the test above (Tier 11)

    def test_blank_recognition_result_is_empty_string_and_does_not_stop_tts(self):
        """The deliberate fix this guards: TTS must only be interrupted on
        CONFIRMED real speech, never merely because *some* audio was
        captured and handed to the recognizer."""
        self.receiver.recognizer.recognize_google.return_value = "   "
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            result = self.receiver.listen()
        self.assertEqual(result, "")
        self.assertFalse(self.sttmod.tts_engine.stop.called)

    def test_unknown_value_error_returns_empty_string(self):
        self.receiver.recognizer.recognize_google.side_effect = self.sttmod.sr.UnknownValueError()
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            result = self.receiver.listen()
        self.assertEqual(result, "")
        self.assertFalse(self.sttmod.tts_engine.stop.called)

    def test_request_error_returns_empty_string(self):
        self.receiver.recognizer.recognize_google.side_effect = self.sttmod.sr.RequestError("no internet")
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            result = self.receiver.listen()
        self.assertEqual(result, "")

    def test_an_unexpected_recognition_error_is_swallowed_to_an_empty_string(self):
        self.receiver.recognizer.recognize_google.side_effect = RuntimeError("boom")
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            result = self.receiver.listen()
        self.assertEqual(result, "")

    # ---------- Tier 13: the language tracker gets fed, and feeds Google ----------
    def test_a_recognized_result_is_fed_to_the_language_tracker(self):
        self.receiver.recognizer.recognize_google.return_value = "aaj mausam kaisa hai"
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            self.receiver.listen()
        self.assertEqual(self.sttmod.language.tracker.current, self.sttmod.language.HINGLISH)

    def test_a_blank_result_is_not_fed_to_the_language_tracker(self):
        """Guards the `if text:` in _recognize() - an empty/unusable
        recognition must not count as evidence of ANY language, or a
        silent/failed utterance could flip the tracker via classify("")."""
        self.receiver.recognizer.recognize_google.return_value = "   "
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            self.receiver.listen()
        self.assertEqual(self.sttmod.language.tracker.current, self.sttmod.language.EN)
        self.assertEqual(len(self.sttmod.language.tracker._history), 0)

    def test_google_is_told_the_trackers_language_not_the_configured_one(self):
        """Settings says Hindi, but nothing decisive has been heard yet -
        voice/language.py's google_language() deliberately overrides a
        configured Hindi tag to en-IN until the conversation itself turns
        out to be in Hindi (see its own docstring/tests)."""
        self.receiver.language = "hi-IN"
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            self.receiver.listen()
        self.receiver.recognizer.recognize_google.assert_called_once_with(
            mock.ANY, language="en-IN")

    def test_google_is_told_hindi_once_the_tracker_has_decided_hindi(self):
        self.receiver.language = "hi-IN"
        self.sttmod.language.tracker.observe("आज मौसम कैसा है")
        with mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            self.receiver.listen()
        self.receiver.recognizer.recognize_google.assert_called_once_with(
            mock.ANY, language="hi-IN")


if __name__ == "__main__":
    unittest.main()
