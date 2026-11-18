"""Tier 10 - regression tests for the four small real bugs fixed alongside
the repo cleanup:

  1. The mic worker's "go to sleep" check was a substring test, so
     "unmute", "PC ko sleep karo" or "mute kar do" put Wave to sleep
     (voice/commands.py's is_sleep_command(), used by ui/workers.py).
  2. TTS markdown cleanup removed EVERY underscore and '#', mangling file
     names and "C#" (voice/tts.py's clean_for_speech()).
  3. The image path's "no Groq API key" error, wrong since Phase 4's
     provider-agnostic client (WaveBrain._think_with_image).
  4. The hard-coded 8-second phrase limit in SpeechReceiver.listen().

    python -m unittest tests.test_tier10_fixes
"""
import sys
import unittest
from unittest import mock

import _phase4_support as S

from voice.commands import is_sleep_command, normalize_utterance


class SleepCommandTests(unittest.TestCase):
    def test_the_old_false_positives_are_no_longer_sleep_commands(self):
        for text in (
            "unmute", "unmute the volume", "mute", "mute the volume", "mute kar do", "volume mute karo",
            "put the pc to sleep", "PC ko sleep karo", "I can't sleep, play some music",
            "set my status to standby later", "sleep timer laga do 30 minute ka",
        ):
            with self.subTest(text=text):
                self.assertFalse(is_sleep_command(text))

    def test_real_sleep_requests_still_work(self):
        for text in (
            "sleep", "Go to sleep.", "standby", "stop listening", "mute yourself",
            "so jao", "ab so jao", "mic band karo", "सो जाओ", "माइक बंद करो।",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_sleep_command(text))

    def test_wake_word_and_polite_prefixes_are_allowed(self):
        self.assertTrue(is_sleep_command("Hey Wave, go to sleep"))
        self.assertTrue(is_sleep_command("wave please stop listening"))
        self.assertTrue(is_sleep_command("okay so jao"))

    def test_custom_wake_phrases_are_stripped_too(self):
        self.assertFalse(is_sleep_command("jarvis sleep"))
        self.assertTrue(is_sleep_command("jarvis sleep", extra_prefixes=["jarvis"]))

    def test_a_prefix_must_be_a_whole_word(self):
        # "wavelength" must not be read as "wave" + "length".
        self.assertFalse(is_sleep_command("wavelength sleep"))

    def test_empty_and_none(self):
        self.assertFalse(is_sleep_command(""))
        self.assertFalse(is_sleep_command(None))

    def test_normalize_keeps_devanagari_marks(self):
        self.assertEqual(normalize_utterance("  सो   जाओ।! "), "सो जाओ")


class _VoiceFakes:
    """Same fake speech_recognition/pyaudio/pygame/edge_tts harness the
    other voice tests use - reused, not redefined."""

    @staticmethod
    def install(testcase):
        # _install_fakes(testcase) registers its own restoration of
        # speech_recognition/pyaudio/pygame/edge_tts via
        # testcase.addCleanup internally now (patching the real pygame/
        # edge_tts modules' attributes in place rather than swapping out
        # their sys.modules entries - see that function's own docstring
        # for why a blind pop of "pygame" causes a circular-import
        # AttributeError in unrelated test files), so cleanup() below only
        # needs to evict the wave-side modules.
        from test_phase5_stt_aec_wiring import _install_fakes
        _install_fakes(testcase)
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)

    @staticmethod
    def cleanup():
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)


class CleanForSpeechTests(unittest.TestCase):
    def setUp(self):
        _VoiceFakes.install(self)
        self.addCleanup(_VoiceFakes.cleanup)
        import voice.tts as ttsmod  # noqa: PLC0415
        self.clean = ttsmod.clean_for_speech

    def test_underscores_inside_words_survive(self):
        self.assertEqual(self.clean("Saved to my_report_v2.docx"), "Saved to my_report_v2.docx")
        self.assertEqual(self.clean("Run build_exe.bat"), "Run build_exe.bat")

    def test_emphasis_markers_are_removed(self):
        self.assertEqual(self.clean("**Done** - that was _easy_."), "Done - that was easy.")
        self.assertEqual(self.clean("`pip install` it"), "pip install it")

    def test_headings_are_removed_but_c_sharp_keeps_its_hash(self):
        self.assertEqual(self.clean("## Steps\nC# is fun"), "Steps\nC# is fun")
        self.assertEqual(self.clean("Item #1"), "Item 1")

    def test_empty(self):
        self.assertEqual(self.clean(""), "")
        self.assertEqual(self.clean(None), "")

    def test_speak_task_speaks_the_cleaned_text(self):
        import voice.tts as ttsmod  # noqa: PLC0415
        engine = ttsmod.VoiceEngine()
        spoken = []
        with mock.patch.object(ttsmod.config, "TTS_PREFER_LOCAL_VOICE", True), \
             mock.patch.object(engine, "_speak_via_piper", side_effect=lambda t: spoken.append(t) or True):
            engine.speak_and_wait("**Saved** to my_file.txt")
        self.assertEqual(spoken, ["Saved to my_file.txt"])


class PhraseTimeLimitTests(unittest.TestCase):
    def setUp(self):
        _VoiceFakes.install(self)
        self.addCleanup(_VoiceFakes.cleanup)
        import voice.stt as sttmod  # noqa: PLC0415
        self.sttmod = sttmod
        self.receiver = sttmod.SpeechReceiver()

    def test_listen_uses_the_configured_limits(self):
        self.receiver.recognizer.listen = mock.Mock(side_effect=self.sttmod.sr.WaitTimeoutError())
        with mock.patch.object(self.sttmod.config, "STT_PHRASE_TIME_LIMIT_S", 21.0), \
             mock.patch.object(self.sttmod.config, "STT_LISTEN_TIMEOUT_S", 3.0), \
             mock.patch.object(self.receiver, "is_headphone_connected", return_value=False):
            self.assertIsNone(self.receiver.listen())
        _, kwargs = self.receiver.recognizer.listen.call_args
        self.assertEqual(kwargs["phrase_time_limit"], 21.0)
        self.assertEqual(kwargs["timeout"], 3.0)

    def test_default_phrase_limit_is_no_longer_8_seconds(self):
        from settings.config import config
        self.assertGreaterEqual(config.STT_PHRASE_TIME_LIMIT_S, 15)


class VisionErrorMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None,
                              write=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        from brain import groq_core  # noqa: PLC0415
        cls.gc = groq_core

    def _brain(self, problem):
        brain = object.__new__(self.gc.WaveBrain)
        brain.client = None
        brain.provider_info = lambda: {"problem": problem}
        return brain

    def test_message_names_the_real_problem_not_groq(self):
        with mock.patch.object(self.gc.db, "save_message"):
            reply = self.gc.WaveBrain._think_with_image(self._brain("OPENAI_API_KEY is missing"), "what is this", {})
        self.assertIn("OPENAI_API_KEY is missing", reply)
        self.assertNotIn("Groq", reply)

    def test_message_without_a_known_problem_points_at_the_env(self):
        with mock.patch.object(self.gc.db, "save_message"):
            reply = self.gc.WaveBrain._think_with_image(self._brain(""), "what is this", {})
        self.assertIn("LLM_PROVIDER", reply)


if __name__ == "__main__":
    unittest.main()
