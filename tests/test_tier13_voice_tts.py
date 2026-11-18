"""Tier 13 - bilingual voice: voice/tts.py's per-chunk language routing
and its use of voice/text_prep.py before synthesis.

Reuses test_phase5_tts_fallback.py's fake pygame/edge_tts harness rather
than redefining it - same pattern test_tier9_stt.py already uses for
test_phase5_stt_aec_wiring.py's fakes.

    python -m unittest discover -s tests -p "test_tier13_voice_tts.py"
"""
import sys
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)

from test_phase5_tts_fallback import _install_fakes


class _BaseTTSTestCase(unittest.TestCase):
    """Same shape as test_phase5_tts_fallback.py's _TTSFakeHarnessMixin,
    kept separate (rather than imported) so this file's test classes
    don't also inherit THAT file's own test_* methods."""

    def setUp(self):
        self.fake_pygame, self.fake_edge_tts, self.FakeCommunicate = _install_fakes(self)
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.tts as ttsmod  # noqa: PLC0415

        self.ttsmod = ttsmod
        self.engine = ttsmod.VoiceEngine()
        self.engine.rate = "+8%"
        self.ttsmod.phrase_cache.clear()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        self.FakeCommunicate.should_fail = False
        self.FakeCommunicate.call_count = 0


class CloudVoiceAndTextTests(_BaseTTSTestCase):
    """_cloud_voice_and_text() in isolation - the pure routing decision,
    before any of speak()'s playback machinery gets involved."""

    def test_devanagari_text_gets_the_hindi_voice(self):
        voice, prepared = self.engine._cloud_voice_and_text("आज मौसम कैसा है")
        self.assertEqual(voice, self.engine.voice_hi)
        self.assertEqual(voice, "hi-IN-SwaraNeural")

    def test_hinglish_text_stays_on_the_main_voice(self):
        """Romanized Hindi (Latin script) must NOT get the Hindi edge-tts
        voice - that voice is trained on Devanagari and tends to mangle
        romanized text letter by letter rather than read it naturally."""
        voice, prepared = self.engine._cloud_voice_and_text("wave chrome kholo")
        self.assertEqual(voice, self.engine.voice)
        self.assertNotEqual(voice, self.engine.voice_hi)

    def test_plain_english_stays_on_the_main_voice(self):
        voice, prepared = self.engine._cloud_voice_and_text("The weather today is sunny.")
        self.assertEqual(voice, self.engine.voice)

    def test_set_voice_hi_changes_the_hindi_routing_target(self):
        self.engine.set_voice_hi("hi-IN-MadhurNeural")
        voice, prepared = self.engine._cloud_voice_and_text("नमस्ते")
        self.assertEqual(voice, "hi-IN-MadhurNeural")

    def test_english_text_gets_risky_mode_indian_grouped_numbers_expanded(self):
        """"risky" mode fixes exactly what a cloud voice gets wrong for
        English: Indian digit grouping and currency - not plain numbers."""
        _, prepared = self.engine._cloud_voice_and_text("that costs ₹1,50,000")
        self.assertIn("lakh", prepared)
        self.assertNotIn("₹", prepared)

    def test_english_text_leaves_plain_numbers_alone_in_risky_mode(self):
        """A plain (non-Indian-grouped) number isn't what edge-tts gets
        wrong, so "risky" mode for English leaves it untouched."""
        _, prepared = self.engine._cloud_voice_and_text("I have 25 new messages")
        self.assertIn("25", prepared)

    def test_hindi_text_gets_full_number_expansion_even_in_risky_mode(self):
        """text_prep.expand_numbers() upgrades "risky" to full expansion
        whenever lang=="hi" - edge-tts's Hindi voice misreads plain
        digits generally, not just Indian-grouped ones."""
        _, prepared = self.engine._cloud_voice_and_text("मेरे पास 25 संदेश हैं")
        self.assertNotIn("25", prepared)
        self.assertIn("पच्चीस", prepared)  # "twenty-five"


class CachingAndSynthesisRoutingTests(_BaseTTSTestCase):
    """End-to-end through speak_and_wait(): the right voice/text combo is
    what actually gets cached and (fake-)synthesized."""

    def test_a_hindi_reply_is_cached_under_the_hindi_voice(self):
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True):
            self.assertTrue(self.engine.speak_and_wait("आज मौसम कैसा है"))
        key = ("आज मौसम कैसा है", self.engine.voice_hi, self.engine.rate)
        self.assertTrue(self.ttsmod.phrase_cache.contains(key))
        self.assertEqual(self.FakeCommunicate.call_count, 1)

    def test_an_english_reply_is_still_cached_under_the_main_voice(self):
        """Unchanged behavior for plain English - Tier 13 must not touch
        the existing, already-tested English/default path."""
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True):
            self.assertTrue(self.engine.speak_and_wait("good morning"))
        key = ("good morning", self.engine.voice, self.engine.rate)
        self.assertTrue(self.ttsmod.phrase_cache.contains(key))

    def test_hindi_and_english_sentences_of_one_streamed_reply_get_different_voices(self):
        """The actual Tier 13 scenario: ONE reply that switches language
        mid-stream gets each sentence spoken in the matching voice -
        proof that routing is per-chunk (classify()), not sticky."""
        with mock.patch.object(self.engine, "_speak_stream_piece", return_value=True) as m_piece:
            self.engine.speak_stream(iter(["The weather is sunny. ", "आज मौसम अच्छा है."]))
        pieces = [c.args[0] for c in m_piece.call_args_list]
        self.assertEqual(pieces, ["The weather is sunny.", "आज मौसम अच्छा है."])
        en_voice, _ = self.engine._cloud_voice_and_text(pieces[0])
        hi_voice, _ = self.engine._cloud_voice_and_text(pieces[1])
        self.assertEqual(en_voice, self.engine.voice)
        self.assertEqual(hi_voice, self.engine.voice_hi)

    def test_prefetch_caches_under_the_same_key_run_utterance_will_look_up(self):
        """prefetch() and _run_utterance() must agree on the cache key -
        otherwise a prefetched Hindi sentence's audio would never be
        found (and re-synthesized needlessly) by the time it plays."""
        self.assertTrue(self.engine.prefetch("नमस्ते दुनिया"))
        future = self.engine._prefetch_futures[("नमस्ते दुनिया", self.engine.voice_hi, self.engine.rate)]
        self.assertIsNotNone(future.result(timeout=2.0))


class OfflineFallbackNumberExpansionTests(_BaseTTSTestCase):
    """_speak_offline_fallback() (Piper/SAPI) gets the FULL numbers="all"
    treatment, not just "risky" - these local engines read raw digits far
    worse than edge-tts does."""

    def test_a_plain_number_is_fully_expanded_before_reaching_piper(self):
        with mock.patch.object(self.engine, "_speak_via_piper", return_value=True) as m_piper, \
             mock.patch.object(self.engine, "_speak_via_sapi") as m_sapi:
            self.assertTrue(self.engine._speak_offline_fallback("I have 25 new messages"))
        m_piper.assert_called_once_with("I have twenty-five new messages")
        m_sapi.assert_not_called()

    def test_sapi_gets_the_same_expanded_text_piper_declined(self):
        with mock.patch.object(self.engine, "_speak_via_piper", return_value=False) as m_piper, \
             mock.patch.object(self.engine, "_speak_via_sapi", return_value=True) as m_sapi:
            self.assertTrue(self.engine._speak_offline_fallback("I have 25 new messages"))
        m_piper.assert_called_once_with("I have twenty-five new messages")
        m_sapi.assert_called_once_with("I have twenty-five new messages")

    def test_the_tts_prefer_local_voice_path_also_gets_expanded_numbers(self):
        """_run_utterance()'s OWN direct Piper call (TTS_PREFER_LOCAL_VOICE,
        used before edge-tts is even tried) is a separate call site from
        _speak_offline_fallback() and must not be missed."""
        with mock.patch.object(self.ttsmod.config, "TTS_PREFER_LOCAL_VOICE", True), \
             mock.patch.object(self.engine, "_speak_via_piper", return_value=True) as m_piper:
            self.assertTrue(self.engine.speak_and_wait("I have 25 new messages"))
        m_piper.assert_called_once_with("I have twenty-five new messages")

    def test_text_with_no_numbers_is_unchanged(self):
        with mock.patch.object(self.engine, "_speak_via_piper", return_value=True) as m_piper:
            self.engine._speak_offline_fallback("hello world")
        m_piper.assert_called_once_with("hello world")


if __name__ == "__main__":
    unittest.main()
