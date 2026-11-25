"""
tests/test_tier36_endpointing.py — Tier 36: don't cut the speaker off mid-sentence.

Wave ended an utterance after 450 ms of silence whatever had been said, so a
pause inside a sentence ("mujhe kal subah ... yaad dilana ki ...") made two
commands of it. SARA's adaptive endpointing waits longer when the words so
far end on "and", "aur", "ki", "to", an "um", or trail off. Wave now holds a
transcript that sounds unfinished for a moment and joins it with the rest
(voice/endpoint_cues.py, voice/stt.py's _continue_unfinished()).

    python -m unittest discover -s tests -p "test_tier36_endpointing.py"
"""
import queue
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402

from settings.config import config  # noqa: E402
from voice import endpoint_cues  # noqa: E402
from voice import stt as stt_module  # noqa: E402
from voice.stt import Transcript  # noqa: E402


class CueTests(unittest.TestCase):
    def test_sentences_that_are_cut_off(self):
        for text in ("Mujhe kal subah...", "remind me to", "play some music by the", "mujhe yaad dilana ki",
                     "open chrome and", "मुझे कल सुबह और", "set an alarm for 6 am,", "Wave, uske baad",
                     "what's the weather in um"):
            with self.subTest(text=text):
                self.assertTrue(endpoint_cues.sounds_unfinished(text))

    def test_whole_commands(self):
        for text in ("volume kam karo", "turn the wifi on", "close this tab", "what time is it?", "help me",
                     "log in", "kya hai", "and", "", "Open Chrome."):
            with self.subTest(text=text):
                self.assertFalse(endpoint_cues.sounds_unfinished(text))

    def test_joining(self):
        self.assertEqual(endpoint_cues.join("Mujhe kal subah...", "yaad dilana."), "Mujhe kal subah yaad dilana.")
        self.assertEqual(endpoint_cues.join("remind me to", "call mom"), "remind me to call mom")


class _Utt:
    def __init__(self, text):
        self.text = text
        self.pcm = b"\x00\x00" * 160
        self.sample_rate = 16000
        self.speech_end = time.monotonic()
        self.ended_at = time.monotonic()
        self.duration_s = 1.0
        self.snr_db = 20.0
        self.truncated = False


class _Engine:
    """A capture engine whose next utterances are scripted; `speaking_for`
    seconds of "still talking" before the next one arrives."""

    def __init__(self, parts, speaking_for=0.0):
        self.q = queue.Queue()
        self.parts = list(parts)
        self.speaking_until = time.monotonic() + speaking_for
        self.waits = []

    @property
    def speaking(self):
        return time.monotonic() < self.speaking_until

    def next_utterance(self, timeout):
        self.waits.append(timeout)
        if self.parts and not self.speaking:
            return _Utt(self.parts.pop(0))
        time.sleep(min(timeout, 0.05))
        return None


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.stt = stt_module.stt_engine
        p = mock.patch.object(config, "STT_CONTINUATION_WAIT_S", 0.3)
        p.start()
        self.addCleanup(p.stop)

    def _recognize(self):
        """_gate_and_recognize() stand-in: the scripted utterance's text."""
        def fake(audio, wake_gate, tts_overlap, timing, audio_s=None, snr_db=None):
            self.gates.append(wake_gate)
            text = self.texts.pop(0)
            if text is None:
                return None
            t = Transcript(text, engine="whisper")
            t.confidence = 0.8
            return t
        self.gates = []
        return mock.patch.object(self.stt, "_gate_and_recognize", side_effect=fake)

    def _first(self, text):
        t = Transcript(text, engine="whisper")
        t.confidence = 0.9
        return t

    def test_an_unfinished_sentence_is_joined_with_the_rest(self):
        self.texts = ["yaad dilana ki doctor ke paas jana hai."]
        with self._recognize():
            out = self.stt._continue_unfinished(_Engine(["part 2"]), self._first("Mujhe kal subah..."))
        self.assertEqual(str(out), "Mujhe kal subah yaad dilana ki doctor ke paas jana hai.")
        self.assertEqual(out.confidence, 0.8)
        self.assertEqual(self.gates, [None])            # the rest isn't wake-gated

    def test_a_whole_command_is_not_held(self):
        self.texts = []
        engine = _Engine(["something else"])
        with self._recognize():
            out = self.stt._continue_unfinished(engine, self._first("volume kam karo"))
        self.assertEqual(str(out), "volume kam karo")
        self.assertEqual(engine.waits, [])

    def test_nothing_following_means_the_first_part_goes_ahead(self):
        self.texts = []
        started = time.monotonic()
        with self._recognize():
            out = self.stt._continue_unfinished(_Engine([]), self._first("remind me to"))
        self.assertEqual(str(out), "remind me to")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_it_waits_while_the_speaker_is_still_talking(self):
        self.texts = ["call mom at six"]
        with self._recognize():
            out = self.stt._continue_unfinished(_Engine(["part 2"], speaking_for=0.8), self._first("remind me to"))
        self.assertEqual(str(out), "remind me to call mom at six")

    def test_not_while_wave_is_talking(self):
        self.texts = ["the rest"]
        first = self._first("open chrome and")
        first.tts_overlap = True
        with self._recognize():
            out = self.stt._continue_unfinished(_Engine(["part 2"]), first)
        self.assertEqual(str(out), "open chrome and")

    def test_another_voice_or_noise_after_it_is_not_joined(self):
        self.texts = [None]                               # the speaker check dropped it
        with self._recognize():
            out = self.stt._continue_unfinished(_Engine(["someone else"]), self._first("open chrome and"))
        self.assertEqual(str(out), "open chrome and")

    def test_it_can_be_turned_off(self):
        self.texts = ["the rest"]
        with mock.patch.object(config, "STT_CONTINUATION", False), self._recognize():
            out = self.stt._continue_unfinished(_Engine(["part 2"]), self._first("open chrome and"))
        self.assertEqual(str(out), "open chrome and")


if __name__ == "__main__":
    unittest.main()
