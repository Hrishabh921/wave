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
TTS_CLONE_* docstring). Since Tier 31 the model runs in a process of its own
(voice/clone_client.py's CloneVoiceProcess, voice/clone_worker.py). So this
file covers:

  - _clone_reference_ready()'s cheap gate (package/flag/file-exists, no
    model load).
  - _get_clone_engine()'s start/cache-the-failure branches, a new process
    for a new sample, reset_clone().
  - _speak_via_clone()'s language routing and float->int16 PCM handoff.
  - _run_utterance()'s priority: cloning is tried BEFORE edge-tts/
    TTS_PREFER_LOCAL_VOICE, and is a complete no-op (false, fall through)
    for anyone who hasn't set up a reference sample.
  - prefetch(): the clone is rendered ahead once its process is up; until
    then the edge-tts voice is prefetched, since that's what will speak.

No real voice process is started here - CloneVoiceProcess is replaced by
fakes throughout, the same way test_tier13_kokoro.py mocks Kokoro and never
touches a real onnxruntime model (tests/test_tier31_voice_clone_live.py
drives the real process plumbing with a stand-in worker).

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


def _fake_process_cls(record, fail=False):
    """Stands in for voice/clone_client.py's CloneVoiceProcess."""

    class _FakeProcess:
        def __init__(self, model_name, reference_path, device="auto"):
            record.append({"model_name": model_name, "reference_path": reference_path, "device": device})
            self.model_name, self.reference_path, self.device = model_name, reference_path, device
            self.alive = True
            self.closed = False

        def start(self):
            if fail:
                raise RuntimeError("simulated: torch couldn't load this checkpoint")
            return {"ready": True, "device": "cpu", "load_s": 1.0, "latents_s": 0.1}

        def close(self):
            self.closed = True
            self.alive = False

    return _FakeProcess


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
        unlike _get_clone_engine(), it must never start the voice process."""
        class _ExplodingIfConstructed:
            def __init__(self, *a, **k):
                raise AssertionError("_clone_reference_ready() must not load the model")

        with self._cfg(), \
             mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
             mock.patch.object(self.ttsmod, "CloneVoiceProcess", _ExplodingIfConstructed):
            self.assertTrue(self.engine._clone_reference_ready())  # no explosion


class GetCloneEngineTests(_BaseCloneTestCase):
    """Mirrors test_tier13_kokoro.py's GetKokoroVoiceTests."""

    def _patched(self, record, fail=False, **cfg):
        return self._cfg(**cfg), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
            mock.patch.object(self.ttsmod, "CloneVoiceProcess", _fake_process_cls(record, fail))

    def test_disabled_by_config_returns_none_even_with_a_real_reference_file(self):
        record = []
        cfg, has, proc = self._patched(record, TTS_CLONE_ENABLED=False)
        with cfg, has, proc:
            self.assertIsNone(self.engine._get_clone_engine())
        self.assertEqual(record, [])

    def test_not_installed_returns_none_even_when_enabled(self):
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", False):
            self.assertIsNone(self.engine._get_clone_engine())

    def test_no_reference_file_returns_none(self):
        with self._cfg(TTS_CLONE_REFERENCE_PATH=""), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True):
            self.assertIsNone(self.engine._get_clone_engine())

    def test_successful_start_passes_the_configured_model_sample_and_device(self):
        record = []
        cfg, has, proc = self._patched(record, TTS_CLONE_MODEL_NAME="tts_models/multilingual/multi-dataset/xtts_v2",
                                       TTS_CLONE_DEVICE="auto")
        with cfg, has, proc:
            engine = self.engine._get_clone_engine()
        self.assertIsNotNone(engine)
        self.assertEqual(record, [{"model_name": "tts_models/multilingual/multi-dataset/xtts_v2",
                                   "reference_path": self.reference_path, "device": "auto"}])

    def test_a_working_engine_is_cached_across_calls(self):
        record = []
        cfg, has, proc = self._patched(record)
        with cfg, has, proc:
            first = self.engine._get_clone_engine()
            second = self.engine._get_clone_engine()
        self.assertIs(first, second)
        self.assertEqual(len(record), 1)

    def test_a_start_failure_is_caught_and_marks_load_failed_without_retry(self):
        record = []
        cfg, has, proc = self._patched(record, fail=True)
        with cfg, has, proc:
            self.assertIsNone(self.engine._get_clone_engine())
            self.assertIsNone(self.engine._get_clone_engine())
        self.assertTrue(self.engine._clone_load_failed)
        self.assertEqual(len(record), 1)  # second call short-circuited on the cached failure

    def test_a_new_voice_sample_gets_a_new_process_and_the_old_one_is_stopped(self):
        record = []
        other = os.path.join(self.tmpdir, "new_voice.wav")
        with open(other, "wb") as f:
            f.write(b"FAKE-WAV-BYTES")
        cfg, has, proc = self._patched(record)
        with cfg, has, proc:
            first = self.engine._get_clone_engine()
            with mock.patch.object(self.ttsmod.config, "TTS_CLONE_REFERENCE_PATH", other):
                second = self.engine._get_clone_engine()
        self.assertIsNot(first, second)
        self.assertTrue(first.closed)
        self.assertEqual(second.reference_path, other)

    def test_reset_clone_stops_the_process_and_forgets_a_failure(self):
        record = []
        cfg, has, proc = self._patched(record)
        with cfg, has, proc:
            engine = self.engine._get_clone_engine()
        self.engine._clone_load_failed = True
        self.engine.reset_clone()
        self.assertTrue(engine.closed)
        self.assertIsNone(self.engine._clone_engine)
        self.assertFalse(self.engine._clone_load_failed)


class SpeakViaCloneTests(_BaseCloneTestCase):
    """_speak_via_clone()'s language routing and audio handoff."""

    def setUp(self):
        super().setUp()
        import numpy as np  # noqa: PLC0415
        self.np = np

    def _fake_clone_engine(self, samples=None, error=None):
        if samples is None:
            samples = self.np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=self.np.float32)
        calls = []
        reference = self.reference_path

        class _FakeEngine:
            alive = True
            reference_path = reference

            def synthesize(self, text, lang):
                calls.append({"text": text, "lang": lang})
                if error is not None:
                    raise error
                return samples, 24000

        return _FakeEngine(), calls

    def _speak(self, text, lang, fake_engine, play=None):
        """The clone process is up (what _speak_via_clone() checks) and is
        what the clone worker renders with."""
        self.engine._clone_engine = fake_engine
        play = play if play is not None else mock.Mock(return_value=True)
        with self._cfg(), \
             mock.patch.object(self.engine, "_get_clone_engine", return_value=fake_engine), \
             mock.patch.object(self.engine, "_play_pcm", side_effect=play):
            return self.engine._speak_via_clone(text, lang)

    def test_hindi_lang_passes_hi_language_code(self):
        fake_engine, calls = self._fake_clone_engine()
        self.assertTrue(self._speak("नमस्ते", self.ttsmod.language.HI, fake_engine))
        self.assertEqual(calls[0]["lang"], "hi")

    def test_english_lang_passes_en_language_code(self):
        fake_engine, calls = self._fake_clone_engine()
        self._speak("hello", self.ttsmod.language.EN, fake_engine)
        self.assertEqual(calls[0]["lang"], "en")

    def test_hinglish_lang_also_passes_en_language_code(self):
        """Only language.HI routes to "hi" - HINGLISH (romanized Hindi)
        falls to "en", same as _speak_via_kokoro()'s own routing."""
        fake_engine, calls = self._fake_clone_engine()
        self._speak("aaj mausam kaisa hai", self.ttsmod.language.HINGLISH, fake_engine)
        self.assertEqual(calls[0]["lang"], "en")

    def test_float_samples_are_converted_to_clipped_int16_pcm_at_the_right_rate(self):
        samples = self.np.array([0.0, 0.5, -0.5, 1.0, -1.0, 2.0, -2.0], dtype=self.np.float32)
        fake_engine, _ = self._fake_clone_engine(samples=samples)
        captured = {}

        def _capture_play_pcm(pcm, sample_rate):
            captured["pcm"] = pcm
            captured["sample_rate"] = sample_rate
            return True

        self.assertTrue(self._speak("hi", self.ttsmod.language.EN, fake_engine, play=_capture_play_pcm))
        self.assertEqual(captured["sample_rate"], 24000)
        self.assertEqual(captured["pcm"].dtype, self.np.int16)
        self.assertEqual(captured["pcm"][-2], 32767)
        self.assertEqual(captured["pcm"][-1], -32767)

    def test_until_the_process_is_up_it_returns_false_and_starts_it(self):
        """The edge-tts voice speaks this one instead of a ~15 s silence."""
        self.engine._clone_engine = None
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
             mock.patch.object(self.engine, "warm_up_clone") as warm:
            self.assertFalse(self.engine._speak_via_clone("hi", self.ttsmod.language.EN))
        warm.assert_called_once_with()

    def test_empty_samples_returns_false(self):
        fake_engine, _ = self._fake_clone_engine(samples=self.np.array([], dtype=self.np.float32))
        self.assertFalse(self._speak("hi", self.ttsmod.language.EN, fake_engine))

    def test_synthesis_exception_is_caught_and_returns_false(self):
        fake_engine, _ = self._fake_clone_engine(error=RuntimeError("simulated: torch inference failure"))
        self.assertFalse(self._speak("hi", self.ttsmod.language.EN, fake_engine))


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
    def test_prefetch_renders_the_clone_instead_of_edge_tts_once_its_process_is_up(self):
        """Tier 31: no edge-tts round-trip that _run_utterance() would throw
        away - the sentence is queued on the clone worker instead (see
        tests/test_tier31_voice_clone_live.py)."""
        rendered = []
        self.engine._clone_engine = mock.Mock(alive=True, reference_path=self.reference_path)
        with self._cfg(), \
             mock.patch.object(self.engine, "_clone_render", side_effect=lambda t, lang, gen: rendered.append(t)):
            self.assertTrue(self.engine.prefetch("hello there"))
            self.engine._clone_futures["hello there"].result(timeout=5)
        self.assertEqual(len(self.engine._prefetch_futures), 0)
        self.assertEqual(rendered, ["hello there"])

    def test_until_the_process_is_up_prefetch_fetches_the_edge_tts_voice_and_starts_it(self):
        # (a sentence no other test speaks - the shared phrase cache may
        # already hold "hello there", and then nothing needs prefetching)
        self.engine._clone_engine = None
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
             mock.patch.object(self.engine, "warm_up_clone") as warm:
            self.assertTrue(self.engine.prefetch("The clone voice is still loading."))
        warm.assert_called_once_with()
        self.assertEqual(len(self.engine._prefetch_futures), 1)
        self.assertEqual(self.engine._clone_futures, {})

    def test_prefetch_works_normally_when_cloning_is_not_set_up(self):
        with self._cfg(TTS_CLONE_REFERENCE_PATH=""), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", False):
            self.assertTrue(self.engine.prefetch("hello there"))


if __name__ == "__main__":
    unittest.main()
