"""
tests/test_tier37_vocabulary.py — Tier 37: the names Whisper should know.

SARA lists the user's names in Whisper's prompt (its measurement: English
word errors 5.7% -> 2.1%). voice/vocabulary.py builds that list for Wave from
STT_EXTRA_VOCABULARY and the names in Wave's memory; voice/stt.py adds it to
the decoding prompt.

    python -m unittest discover -s tests -p "test_tier37_vocabulary.py"
"""
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402

from settings.config import config  # noqa: E402
from voice import vocabulary  # noqa: E402


class BuildTests(unittest.TestCase):
    def test_names_come_from_memory_and_the_extra_list(self):
        facts = [("name", "Rahul"), ("wife", "priya"), ("city", "Jaipur (Thu 26 Nov 2026)"),
                 ("favorite_singer", "Arijit Singh"), ("favorite_color", "blue"),
                 ("doctor_appointment", "kal (Thu 26 Nov 2026) Dr Mehta ke paas"),
                 ("notes", "x" * 80 + " Ignored")]
        with mock.patch.object(config, "STT_EXTRA_VOCABULARY", "Dodo, Arijit Singh"):
            words = vocabulary.build_vocabulary(facts)
        self.assertEqual(words[:2], ["Dodo", "Arijit Singh"])
        for name in ("Rahul", "Priya", "Jaipur", "Mehta", "WhatsApp"):
            self.assertIn(name, words)
        self.assertNotIn("blue", words)
        self.assertNotIn("Ignored", words)        # long facts aren't scanned
        self.assertNotIn("Thu", words)            # the date note isn't a name
        self.assertEqual(len(words), len({w.lower() for w in words}))

    def test_the_list_stays_short(self):
        facts = [(f"friend_{i}", f"Name{i:02d}") for i in range(60)]
        self.assertLessEqual(len(vocabulary.build_vocabulary(facts)), vocabulary.MAX_WORDS)

    def test_devanagari_names_are_kept_as_written(self):
        self.assertIn("प्रिया", vocabulary.build_vocabulary([("wife", "प्रिया")]))


class HintTests(unittest.TestCase):
    def setUp(self):
        vocabulary.reset_cache()
        self.addCleanup(vocabulary.reset_cache)

    def test_the_hint_reads_memory_and_is_cached(self):
        from database.db_manager import db  # noqa: PLC0415
        with mock.patch.object(config, "STT_VOCABULARY", "auto"), \
                mock.patch.object(config, "STT_EXTRA_VOCABULARY", ""), \
                mock.patch.object(db, "get_all_memory", return_value=[("wife", "Priya")]) as facts:
            self.assertEqual(vocabulary.vocabulary_hint(), "Priya, WhatsApp, YouTube, Spotify, Instagram.")
            vocabulary.vocabulary_hint()
        self.assertEqual(facts.call_count, 1)

    def test_off_means_nothing(self):
        with mock.patch.object(config, "STT_VOCABULARY", "off"):
            self.assertEqual(vocabulary.vocabulary_hint(), "")

    def test_whisper_gets_the_names_after_its_other_hints(self):
        import voice.stt as stt_mod  # noqa: PLC0415
        captured = {}

        class Seg:
            text, avg_logprob, no_speech_prob, compression_ratio, start, end = "Priya ko call karo", -0.2, 0.0, 1.0, 0.0, 1.5

        class Model:
            def transcribe(self, audio, **kw):
                captured.update(kw)
                return [Seg()], type("I", (), {"language": "en", "language_probability": 0.9})()

        audio = type("A", (), {"get_raw_data": lambda self, **kw: b"\x00\x00" * 16000})()
        engine = stt_mod.stt_engine
        old = engine.wake_hint
        self.addCleanup(setattr, engine, "wake_hint", old)
        engine.wake_hint = None
        with mock.patch.object(engine, "_get_whisper_model", return_value=Model()), \
                mock.patch.object(stt_mod.language.tracker, "initial_prompt", return_value="Haan, theek hai."), \
                mock.patch.object(stt_mod.vocabulary, "vocabulary_hint", return_value="Priya, WhatsApp."):
            engine._recognize_with_whisper(audio)
        self.assertEqual(captured["initial_prompt"], "Haan, theek hai. Priya, WhatsApp.")


if __name__ == "__main__":
    unittest.main()
