"""Tier 25 - the optional voice-CLONING local engine (Coqui XTTS-v2),
voice/tts.py's 4th TTS engine. Mirrors test_tier13_kokoro.py's structure
closely (lazy-load/cache-the-failure branches, audio-handoff conversion,
routing-priority tests) - see that file's own module docstring for why
this shape works well for an optional, mocked-out local engine.

Unlike Piper/Kokoro, cloning has no download-and-verify dance of its own
(no fixed model files to fetch and checksum here - the real XTTS-v2
checkpoint download is entirely Coqui TTS's own concern, not this
project's) and no separate consent flag (TTS_CLONE_REFERENCE_PATH itself,
pointing at a real file, IS the opt-in - see settings/config.py's
TTS_CLONE_* docstring). So this file covers:

  - _clone_reference_ready()'s cheap gate (package/flag/file-exists, no
    model load).
  - _get_clone_engine()'s lazy-load/cache-the-failure branches.
  - _speak_via_clone()'s language routing and float->int16 PCM handoff.
  - _run_utterance()'s priority: cloning is tried BEFORE edge-tts/
    TTS_PREFER_LOCAL_VOICE, and is a complete no-op (false, fall through)
    for anyone who hasn't set up a reference sample.
  - prefetch()'s skip of the now-pointless edge-tts network round-trip
    once cloning is actually ready.

The real `TTS` (coqui-tts) package is never imported or installed here -
CoquiTTS is mocked throughout, the same way test_tier13_kokoro.py mocks
Kokoro and never touches a real onnxruntime model.

    python -m unittest discover -s tests -p "test_tier25_voice_clone.py"
"""
import os
import sys
import tempfile
import shutil
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)

from test_phase5_tts_fallback import _install_fakes


class _BaseCloneTestCase(unittest.TestCase):
    def setUp(self):
        _install_fakes(self)
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.tts as ttsmod  # noqa: PLC0415

        self.ttsmod = ttsmod
        self.engine = ttsmod.VoiceEngine()
        self.tmpdir = tempfile.mkdtemp(prefix="wave_clone_test_")
        self.reference_path = os.path.join(self.tmpdir, "my_voice.wav")
        with open(self.reference_path, "wb") as f:
            f.write(b"FAKE-WAV-BYTES")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)

    def _cfg(self, **overrides):
        values = {
            "TTS_CLONE_ENABLED": True,
            "TTS_CLONE_REFERENCE_PATH": self.reference_path,
            "TTS_CLONE_MODEL_NAME": "tts_models/multilingual/multi-dataset/xtts_v2",
        }
        values.update(overrides)
        return mock.patch.multiple(self.ttsmod.config, **values)


class CloneReferenceReadyTests(_BaseCloneTestCase):
    def test_ready_when_installed_enabled_and_file_exists(self):
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True):
            self.assertTrue(self.engine._clone_reference_ready())

    def test_not_ready_when_package_not_installed(self):
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", False):
            self.assertFalse(self.engine._clone_reference_ready())

    def test_not_ready_when_disabled_by_config(self):
        with self._cfg(TTS_CLONE_ENABLED=False), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True):
            self.assertFalse(self.engine._clone_reference_ready())

    def test_not_ready_when_reference_path_blank(self):
        with self._cfg(TTS_CLONE_REFERENCE_PATH=""), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True):
            self.assertFalse(self.engine._clone_reference_ready())

    def test_not_ready_when_reference_file_does_not_exist(self):
        with self._cfg(TTS_CLONE_REFERENCE_PATH=os.path.join(self.tmpdir, "missing.wav")), \
             mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True):
            self.assertFalse(self.engine._clone_reference_ready())

    def test_never_loads_the_engine_itself(self):
        """The whole point of this being a separate, cheap method -
        unlike _get_clone_engine(), it must never construct CoquiTTS."""
        class _ExplodingIfConstructed:
            def __init__(self, *a, **k):
                raise AssertionError("_clone_reference_ready() must not load the model")

        with self._cfg(), \
             mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
             mock.patch.object(self.ttsmod, "CoquiTTS", _ExplodingIfConstructed):
            self.assertTrue(self.engine._clone_reference_ready())  # no explosion


class GetCloneEngineTests(_BaseCloneTestCase):
    """Mirrors test_tier13_kokoro.py's GetKokoroVoiceTests."""

    def _fake_coqui_cls(self):
        constructed = {}

        class _FakeCoquiTTS:
            def __init__(self, model_name):
                constructed["model_name"] = model_name

        return _FakeCoquiTTS, constructed

    def test_disabled_by_config_returns_none_even_with_a_real_reference_file(self):
        fake_cls, _ = self._fake_coqui_cls()
        with self._cfg(TTS_CLONE_ENABLED=False), \
             mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
             mock.patch.object(self.ttsmod, "CoquiTTS", fake_cls):
            self.assertIsNone(self.engine._get_clone_engine())

    def test_not_installed_returns_none_even_when_enabled(self):
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", False):
            self.assertIsNone(self.engine._get_clone_engine())

    def test_no_reference_file_returns_none(self):
        with self._cfg(TTS_CLONE_REFERENCE_PATH=""), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True):
            self.assertIsNone(self.engine._get_clone_engine())

    def test_successful_load_passes_the_configured_model_name(self):
        fake_cls, constructed = self._fake_coqui_cls()
        with self._cfg(TTS_CLONE_MODEL_NAME="tts_models/multilingual/multi-dataset/xtts_v2"), \
             mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
             mock.patch.object(self.ttsmod, "CoquiTTS", fake_cls):
            engine = self.engine._get_clone_engine()
        self.assertIsNotNone(engine)
        self.assertEqual(constructed["model_name"], "tts_models/multilingual/multi-dataset/xtts_v2")

    def test_a_working_engine_is_cached_across_calls(self):
        fake_cls, constructed = self._fake_coqui_cls()
        with self._cfg(), \
             mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
             mock.patch.object(self.ttsmod, "CoquiTTS", fake_cls):
            first = self.engine._get_clone_engine()
            second = self.engine._get_clone_engine()
        self.assertIs(first, second)
        self.assertEqual(len(constructed), 1)

    def test_a_load_exception_is_caught_and_marks_load_failed_without_retry(self):
        calls = {"n": 0}

        class _RaisingCoquiTTS:
            def __init__(self, model_name):
                calls["n"] += 1
                raise RuntimeError("simulated: torch couldn't load this checkpoint")

        with self._cfg(), \
             mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
             mock.patch.object(self.ttsmod, "CoquiTTS", _RaisingCoquiTTS):
            self.assertIsNone(self.engine._get_clone_engine())
            self.assertIsNone(self.engine._get_clone_engine())
        self.assertTrue(self.engine._clone_load_failed)
        self.assertEqual(calls["n"], 1)  # second call short-circuited on the cached failure


class SpeakViaCloneTests(_BaseCloneTestCase):
    """_speak_via_clone()'s language routing and audio handoff."""

    def setUp(self):
        super().setUp()
        import numpy as np  # noqa: PLC0415
        self.np = np

    def _fake_clone_engine(self, samples=None):
        if samples is None:
            samples = self.np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=self.np.float32)
        calls = []

        class _FakeEngine:
            synthesizer = type("S", (), {"output_sample_rate": 24000})()

            def tts(self, text, speaker_wav, language):
                calls.append({"text": text, "speaker_wav": speaker_wav, "language": language})
                return samples

        return _FakeEngine(), calls

    def test_hindi_lang_passes_hi_language_code(self):
        fake_engine, calls = self._fake_clone_engine()
        with self._cfg(), \
             mock.patch.object(self.engine, "_get_clone_engine", return_value=fake_engine), \
             mock.patch.object(self.engine, "_play_pcm", return_value=True):
            result = self.engine._speak_via_clone("नमस्ते", self.ttsmod.language.HI)
        self.assertTrue(result)
        self.assertEqual(calls[0]["language"], "hi")
        self.assertEqual(calls[0]["speaker_wav"], self.reference_path)

    def test_english_lang_passes_en_language_code(self):
        fake_engine, calls = self._fake_clone_engine()
        with self._cfg(), \
             mock.patch.object(self.engine, "_get_clone_engine", return_value=fake_engine), \
             mock.patch.object(self.engine, "_play_pcm", return_value=True):
            self.engine._speak_via_clone("hello", self.ttsmod.language.EN)
        self.assertEqual(calls[0]["language"], "en")

    def test_hinglish_lang_also_passes_en_language_code(self):
        """Only language.HI routes to "hi" - HINGLISH (romanized Hindi)
        falls to "en", same as _speak_via_kokoro()'s own routing."""
        fake_engine, calls = self._fake_clone_engine()
        with self._cfg(), \
             mock.patch.object(self.engine, "_get_clone_engine", return_value=fake_engine), \
             mock.patch.object(self.engine, "_play_pcm", return_value=True):
            self.engine._speak_via_clone("aaj mausam kaisa hai", self.ttsmod.language.HINGLISH)
        self.assertEqual(calls[0]["language"], "en")

    def test_float_samples_are_converted_to_clipped_int16_pcm_at_the_right_rate(self):
        samples = self.np.array([0.0, 0.5, -0.5, 1.0, -1.0, 2.0, -2.0], dtype=self.np.float32)
        fake_engine, _ = self._fake_clone_engine(samples=samples)
        captured = {}

        def _capture_play_pcm(pcm, sample_rate):
            captured["pcm"] = pcm
            captured["sample_rate"] = sample_rate
            return True

        with self._cfg(), \
             mock.patch.object(self.engine, "_get_clone_engine", return_value=fake_engine), \
             mock.patch.object(self.engine, "_play_pcm", side_effect=_capture_play_pcm):
            self.assertTrue(self.engine._speak_via_clone("hi", self.ttsmod.language.EN))
        self.assertEqual(captured["sample_rate"], 24000)
        self.assertEqual(captured["pcm"].dtype, self.np.int16)
        self.assertEqual(captured["pcm"][-2], 32767)
        self.assertEqual(captured["pcm"][-1], -32767)

    def test_engine_unavailable_returns_false(self):
        with self._cfg(), mock.patch.object(self.engine, "_get_clone_engine", return_value=None):
            self.assertFalse(self.engine._speak_via_clone("hi", self.ttsmod.language.EN))

    def test_empty_samples_returns_false(self):
        fake_engine, _ = self._fake_clone_engine(samples=self.np.array([], dtype=self.np.float32))
        with self._cfg(), mock.patch.object(self.engine, "_get_clone_engine", return_value=fake_engine):
            self.assertFalse(self.engine._speak_via_clone("hi", self.ttsmod.language.EN))

    def test_synthesis_exception_is_caught_and_returns_false(self):
        class _RaisingEngine:
            def tts(self, text, speaker_wav, language):
                raise RuntimeError("simulated: torch inference failure")

        with self._cfg(), mock.patch.object(self.engine, "_get_clone_engine", return_value=_RaisingEngine()):
            self.assertFalse(self.engine._speak_via_clone("hi", self.ttsmod.language.EN))


class RunUtterancePriorityTests(_BaseCloneTestCase):
    """_run_utterance() tries the clone voice before anything else - and
    is a complete no-op for anyone who hasn't set one up."""

    def test_successful_clone_short_circuits_before_edge_tts_is_touched(self):
        with self._cfg(), \
             mock.patch.object(self.engine, "_speak_via_clone", return_value=True) as m_clone, \
             mock.patch.object(self.engine, "_cloud_voice_and_text") as m_cloud:
            self.engine._run_utterance("hello there")
        m_clone.assert_called_once()
        m_cloud.assert_not_called()
        self.assertTrue(self.engine.last_playback_ok)

    def test_no_reference_set_up_falls_straight_through_to_the_normal_path(self):
        """The common case (nobody has configured cloning) - _speak_via_
        clone()'s own cheap gate makes this a true no-op, not something
        this test needs to separately mock away."""
        with self._cfg(TTS_CLONE_REFERENCE_PATH=""), \
             mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", False), \
             mock.patch.object(self.engine, "_ensure_mixer", return_value=False), \
             mock.patch.object(self.engine, "_speak_offline_fallback", return_value=True) as m_fallback:
            self.engine._run_utterance("hello there")
        m_fallback.assert_called_once()


class PrefetchSkipsWhenCloneReadyTests(_BaseCloneTestCase):
    def test_prefetch_is_skipped_once_a_clone_reference_is_ready(self):
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True):
            self.assertFalse(self.engine.prefetch("hello there"))
        self.assertEqual(len(self.engine._prefetch_futures), 0)

    def test_prefetch_works_normally_when_cloning_is_not_set_up(self):
        with self._cfg(TTS_CLONE_REFERENCE_PATH=""), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", False):
            self.assertTrue(self.engine.prefetch("hello there"))


if __name__ == "__main__":
    unittest.main()
