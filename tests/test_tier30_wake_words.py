"""
tests/test_tier30_wake_words.py — the user's own wake words.

The user wanted "baby" as the main wake word, plus "babu", "dodo" and
"suar". Wave's own Whisper model, asked about such short Hinglish words with
no hint, wrote them as Japanese/Chinese/Urdu ("ドードー", "做了", "بابو") or
as "So", and a lone "Baby" often scored under the confidence floor and was
dropped. So: while asleep the wake words lead Whisper's prompt (tested: 23 of
24 synthetic utterances came out right), a few known alternative spellings
also count, and an unsure transcript that is *only* a wake word still wakes.

    python -m unittest discover -s tests -p "test_tier30_wake_words.py"
"""
import json
import os
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from voice import commands as vc  # noqa: E402
from voice.stt import Transcript, stt_engine  # noqa: E402
from webui.voice_worker import WebContinuousMicWorker  # noqa: E402

PHRASES = ["baby", "babu", "dodo", "suar", "hey wave", "hello wave", "wake up"]
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class MatchingTests(unittest.TestCase):
    def test_each_wake_word_and_its_known_spellings_address_wave(self):
        for heard, rest in (("Baby.", ""), ("Baby, what's the time?", "what's the time?"), ("Babu", ""),
                            ("Dodo.", ""), ("Do-do, what is the time?", "what is the time?"), ("Do do", ""),
                            ("Suar.", ""), ("Sua, time kya hua hai", "time kya hua hai"), ("बेबी", ""),
                            ("बाबू, गाना चलाओ", "गाना चलाओ"), ("ドードー。", ""), ("Hey wave", "")):
            with self.subTest(heard=heard):
                self.assertEqual(vc.strip_wake_phrase(heard, PHRASES, allow_bare_name=False), (True, rest))

    def test_everyday_words_whisper_sometimes_wrote_instead_do_not(self):
        for heard in ("So what is the time?", "Sour cream please", "Babe come here", "The baby is sleeping",
                      "Swar badal gaya"):
            with self.subTest(heard=heard):
                self.assertFalse(vc.strip_wake_phrase(heard, PHRASES, allow_bare_name=False)[0])

    def test_asleep_the_main_word_wakes_and_a_command_after_it_goes_through(self):
        self.assertEqual(vc.decide("Baby.", "SLEEPING", PHRASES, tts_active=False).action, vc.WAKE)
        d = vc.decide("Baby, what's the time?", "SLEEPING", PHRASES, tts_active=False)
        self.assertEqual((d.action, d.text), (vc.WAKE_AND_COMMAND, "what's the time?"))
        self.assertEqual(vc.decide("Do-do", "SLEEPING", PHRASES, tts_active=False).action, vc.WAKE)
        self.assertEqual(vc.decide("Can you pass the water", "SLEEPING", PHRASES, tts_active=False).action, vc.IGNORE)

    def test_stop_and_sleep_work_after_a_custom_wake_word_too(self):
        self.assertTrue(vc.is_stop_command("Dodo, stop", PHRASES))
        self.assertTrue(vc.is_stop_command("Do-do stop", PHRASES))


def _empty(rejected):
    t = Transcript("", engine="whisper")
    t.rejected_text = rejected
    return t


class MicWorkerTests(unittest.TestCase):
    def _worker(self):
        modes = []
        w = WebContinuousMicWorker(wake_phrases=PHRASES, on_mode_changed=lambda m, a: modes.append((m, a)))
        return w, modes

    def test_an_unsure_lone_wake_word_still_wakes(self):
        w, modes = self._worker()
        w.handle_text(_empty("Baby"))
        self.assertEqual(w.mode, "AWAKE")
        self.assertEqual(modes[-1], ("AWAKE", True))

    def test_an_unsure_sentence_or_other_word_does_not(self):
        for rejected in ("Baby what is happening here", "So", None, ""):
            with self.subTest(rejected=rejected):
                w, _ = self._worker()
                w.handle_text(_empty(rejected))
                self.assertEqual(w.mode, "SLEEPING")

    def test_the_whisper_hint_lists_the_wake_words_main_first(self):
        w, _ = self._worker()
        self.assertEqual(w._wake_hint(), "Baby, Babu, Dodo, Suar, Hey Wave, Hello Wave, Wake Up.")


class SttHintTests(unittest.TestCase):
    """_recognize_with_whisper puts the hint first in the prompt, and keeps a
    rejected transcript for the mic worker."""

    def _run(self, hint, judged_text, reason):
        import voice.stt as stt_mod
        captured = {}

        class Seg:
            text, avg_logprob, no_speech_prob, compression_ratio, start, end = "Baby", -1.2, 0.0, 1.0, 0.0, 0.5

        class Model:
            def transcribe(self, audio, **kw):
                captured.update(kw)
                return [Seg()], type("I", (), {"language": "en", "language_probability": 0.6})()

        verdict = type("V", (), {"text": judged_text, "reason": reason, "confidence": 0.3})()
        audio = type("A", (), {"get_raw_data": lambda self, **kw: b"\x00\x00" * 8000})()
        old = stt_engine.wake_hint
        self.addCleanup(setattr, stt_engine, "wake_hint", old)
        stt_engine.wake_hint = hint
        with mock.patch.object(stt_engine, "_get_whisper_model", return_value=Model()), \
                mock.patch.object(stt_mod.transcript_filter, "judge", return_value=verdict), \
                mock.patch.object(stt_mod.language.tracker, "initial_prompt", return_value="Hinglish hint"):
            out = stt_engine._recognize_with_whisper(audio)
        return out, captured

    def test_asleep_the_wake_words_lead_the_prompt(self):
        _, kw = self._run("Baby, Dodo.", "Baby", None)
        self.assertEqual(kw["initial_prompt"], "Baby, Dodo. Hinglish hint")

    def test_awake_the_prompt_is_unchanged(self):
        _, kw = self._run(None, "Baby", None)
        self.assertEqual(kw["initial_prompt"], "Hinglish hint")

    def test_a_low_confidence_transcript_is_kept_aside(self):
        out, _ = self._run("Baby.", "", "low confidence 0.30")
        self.assertEqual(out, "")
        self.assertEqual(stt_engine.last_rejected_text, "Baby")


class SettingsTests(unittest.TestCase):
    def test_the_bridge_starts_with_the_saved_wake_words(self):
        import webui.bridge as bridge_module
        from database.db_manager import db
        tmp = S.make_tmpdir(self)
        path = os.path.join(tmp, "wave_settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"wake_phrases": ["Baby", "dodo"]}, f)
        old_db = db.db_path
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", old_db)
        with mock.patch.object(bridge_module, "SETTINGS_FILE", path):
            bridge = bridge_module.WaveWebBridge(brain=object())
        self.addCleanup(bridge._reminder_worker.stop)
        self.assertEqual(bridge._wake_phrases, ["baby", "dodo"])

    def test_settings_page_takes_several_custom_words(self):
        with open(os.path.join(REPO, "webui", "static", "js", "pages2.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("function parseWakeWords(v)", js)
        self.assertIn("split(/[,\\n;]+/)", js)
        self.assertIn("(main)", js)


if __name__ == "__main__":
    unittest.main()
