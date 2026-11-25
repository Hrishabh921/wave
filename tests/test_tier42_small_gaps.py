"""
tests/test_tier42_small_gaps.py — Tier 42: the smaller gaps to SARA.

  * exact answers worked out in code (date differences, a date N days away,
    weekdays, percentages) instead of a model's guess (brain/exact_answers.py);
  * the model is told to look time-sensitive things up (get_live_information)
    instead of answering from months-old training;
  * the wake / sleep announcements are rendered once in the cloned voice and
    kept on disk, so they play at once (voice/tts.py).

    python -m unittest discover -s tests -p "test_tier42_small_gaps.py"
"""
import os
import shutil
import tempfile
import unittest
from datetime import date
from unittest import mock

import numpy as np

import _phase4_support as S  # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                      typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

from brain import exact_answers  # noqa: E402
import test_tier25_voice_clone as t25  # noqa: E402

WED = date(2026, 11, 25)


class ExactAnswerTests(unittest.TestCase):
    def test_dates_and_percentages(self):
        cases = {
            "how many days until december 25?": "30 days until Friday, 25 December 2026.",
            "25 december mein kitne din bache hain?": "25 December mein 30 din baaki hain.",
            "how many days until new year": "37 days until Friday, 1 January 2027.",
            "how many days since 15 august 1947?": "28957 days since Friday, 15 August 1947.",
            "what day was 15 august 1947?": "15 August 1947 was a Friday.",
            "26 january 2027 ko kaunsa din hai?": "26 January 2027 ko Tuesday hai.",
            "45 din baad kaunsi date hogi?": "Us din Saturday, 9 January 2027 hoga.",
            "what date will it be 3 weeks from today?": "That will be Wednesday, 16 December 2026.",
            "what was the date 100 days ago?": "That was Monday, 17 August 2026.",
            "2400 ka 15 percent?": "2400 ka 15 percent 360 hai.",
            "what is 15% of 2400?": "15% of 2400 is 360.",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(exact_answers.answer(question, WED), expected)

    def test_everything_else_goes_to_the_model(self):
        for question in ("what is photosynthesis?", "remind me in 3 days", "aaj kya date hai?",
                         "diwali mein kitne din bache hain?", "play some music"):
            with self.subTest(question=question):
                self.assertIsNone(exact_answers.answer(question, WED))


class PromptRuleTests(unittest.TestCase):
    def test_time_sensitive_questions_are_looked_up(self):
        from brain.groq_core import WaveBrain  # noqa: PLC0415
        content = object.__new__(WaveBrain).get_dynamic_system_prompt("who won?")["content"]
        self.assertIn("call get_live_information first", content)


class PrewarmTests(t25._BaseCloneTestCase):
    def setUp(self):
        super().setUp()
        self.cache_dir = tempfile.mkdtemp(prefix="wave_voice_cache_")
        self.addCleanup(shutil.rmtree, self.cache_dir, True)
        p = mock.patch.object(type(self.engine), "_clone_phrase_dir", staticmethod(lambda: self.cache_dir))
        p.start()
        self.addCleanup(p.stop)
        self.rendered = []

        def render(text, lang, gen):
            self.rendered.append(text)
            return np.array([1, 2, 3], dtype=np.int16), 24000

        self.render = render

    def _on(self):
        return self._cfg(TTS_CLONE_PREWARM=True), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True)

    def test_the_fixed_phrases_are_rendered_once_and_kept_on_disk(self):
        cfg, coqui = self._on()
        with cfg, coqui, mock.patch.object(self.engine, "_clone_render", side_effect=self.render):
            self.engine._prewarm_clone_phrases()
            self.engine._prewarm_clone_phrases()            # already there: nothing again
        self.assertEqual(self.rendered, ["Systems active.", "Standing by."])
        self.assertEqual(len(os.listdir(self.cache_dir)), 2)

    def test_a_prewarmed_phrase_plays_at_once_even_before_the_clone_loads(self):
        cfg, coqui = self._on()
        with cfg, coqui, mock.patch.object(self.engine, "_clone_render", side_effect=self.render):
            self.engine._prewarm_clone_phrases()
        fresh = self.ttsmod.VoiceEngine()                    # a restart: memory empty, disk kept
        played = []
        with cfg, coqui, mock.patch.object(type(fresh), "_clone_phrase_dir", staticmethod(lambda: self.cache_dir)), \
                mock.patch.object(fresh, "_play_pcm", side_effect=lambda pcm, rate: played.append(rate) or True), \
                mock.patch.object(fresh, "_clone_engine_if_ready", return_value=None) as not_loaded:
            lang = self.ttsmod.language.classify("Systems active.")
            self.assertTrue(fresh._speak_via_clone("Systems active.", lang))
        self.assertEqual(played, [24000])
        not_loaded.assert_not_called()

    def test_a_new_voice_sample_means_new_renderings(self):
        cfg, coqui = self._on()
        with cfg, coqui, mock.patch.object(self.engine, "_clone_render", side_effect=self.render):
            self.engine._prewarm_clone_phrases()
            lang = self.ttsmod.language.classify("Systems active.")
            self.assertIsNotNone(self.engine._clone_phrase_cached("Systems active.", lang))
            with open(self.reference_path, "ab") as f:
                f.write(b"-a different sample")
            self.assertIsNone(self.engine._clone_phrase_cached("Systems active.", lang))

    def test_live_speech_comes_first_and_other_text_is_never_cached(self):
        cfg, coqui = self._on()
        with cfg, coqui, mock.patch.object(self.engine, "_clone_render", side_effect=self.render):
            self.engine._utterance_active = True
            self.engine._prewarm_clone_phrases()
        self.assertEqual(self.rendered, [])
        self.assertIsNone(self.engine._clone_phrase_cached("Hello there.", "en"))

    def test_it_can_be_turned_off(self):
        cfg, coqui = self._cfg(TTS_CLONE_PREWARM=False), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True)
        with cfg, coqui, mock.patch.object(self.engine, "_clone_render", side_effect=self.render):
            self.engine._prewarm_clone_phrases()
        self.assertEqual(self.rendered, [])


if __name__ == "__main__":
    unittest.main()
