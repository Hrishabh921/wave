"""Tier 48 - numbers said right, conversions worked out, and Wave's own voice not taken for the user's.

SpeechFormTests: voice/text_prep.py - phone numbers, versions, more units, ranges, fractions, "5 pm".
UnitAnswerTests: brain/exact_answers.py - "5 km in miles", "2 kilo kitne pound".
EchoGuardTests: voice/echo_guard.py with a fake clock.
SttTests: voice/stt.py drops a transcript of Wave's own sentence heard while it talked.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: E402,F401

from brain import exact_answers  # noqa: E402
from settings.config import config  # noqa: E402
from voice import echo_guard as eg  # noqa: E402
from voice import text_prep as tp  # noqa: E402


class SpeechFormTests(unittest.TestCase):
    def check(self, text, expected, lang="en", mode="all"):
        self.assertEqual(tp.prepare(text, lang, mode), expected, text)

    def test_phone_numbers_digit_by_digit_in_every_voice(self):
        self.check("Call 9876543210", "Call nine eight seven six five, four three two one zero")
        self.check("Call 9876543210", "Call nine eight seven six five, four three two one zero", mode="risky")
        self.check("+91 98765 43210", "plus nine one, nine eight seven six five, four three two one zero")
        self.check("नंबर 9876543210", "नंबर नौ आठ सात छह पाँच, चार तीन दो एक शून्य", lang="hi")
        self.check("₹1,50,000", "one lakh fifty thousand rupees")              # an amount is still an amount

    def test_versions_dates_and_decimals(self):
        self.check("Python 3.12.1", "Python three point twelve point one")
        self.check("v2.3.1 and version 4.0", "version two point three point one and version four point zero")
        self.check("on 25.11.2026", "on November twenty-fifth, twenty twenty-six")
        self.check("pi is 3.14", "pi is three point one four")

    def test_more_units(self):
        self.check("3.5GHz, 5000mAh, 100 Mbps",
                   "three point five gigahertz, five thousand milliamp hours, one hundred megabits per second")
        self.check("60 km/h or 80 mph", "sixty kilometres per hour or eighty miles per hour")
        self.check("3.5GHz", "3.5GHz", mode="risky")                            # cloud voices manage these

    def test_ranges_but_not_scores(self):
        self.check("It takes 10-15 minutes", "It takes ten to fifteen minutes")
        self.check("between 5-10 people", "between five and ten people")
        self.check("India won 3-2", "India won three-two")
        self.check("10-15 minute lagenge", "दस से पंद्रह minute lagenge", lang="hi")

    def test_fractions_and_hours(self):
        self.check("Add 1/2 cup, open 24/7", "Add half cup, open twenty-four seven")
        self.check("at 5 pm or 10am", "at five p.m. or ten a.m.")
        self.check("at 5:30 PM", "at five thirty p.m.")


class UnitAnswerTests(unittest.TestCase):
    def test_conversions(self):
        for text, expected in [
            ("5 km in miles", "5 km is 3.11 miles."),
            ("what is 5 km in miles?", "5 km is 3.11 miles."),
            ("convert 100 fahrenheit to celsius", "100 fahrenheit is 37.78 celsius."),
            ("how many cm in a foot", "1 foot is 30.48 cm."),
            ("how many miles are in 10 km", "10 km is 6.21 miles."),
            ("5 km is how many miles", "5 km is 3.11 miles."),
            ("2 kilo kitne pound hote hain", "2 kilo 4.41 pound hote hain."),
            ("10 inch kitne cm", "10 inch 25.4 cm hote hain."),
            ("37 degrees celsius in fahrenheit", "37 degrees celsius is 98.6 fahrenheit."),
            ("2.5 lbs to kg", "2.5 lbs is 1.13 kg."),
        ]:
            self.assertEqual(exact_answers.answer(text), expected, text)

    def test_everything_else_goes_to_the_model(self):
        for text in ["I ran 5 km in 30 minutes", "I need 2 kg to make this", "100 dollars in rupees",
                     "5 kg in miles", "5 in to cm"]:
            self.assertIsNone(exact_answers.answer(text), text)


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class EchoGuardTests(unittest.TestCase):
    def setUp(self):
        saved = config.ECHO_GUARD
        config.ECHO_GUARD = True
        self.addCleanup(setattr, config, "ECHO_GUARD", saved)
        self.clock = _Clock()
        self.guard = eg.EchoGuard(clock=self.clock)

    def _say(self, text, seconds=3.0):
        token = self.guard.started(text)
        self.clock.t += seconds
        self.guard.finished(token)

    def test_what_counts_as_the_same_words(self):
        said = eg.words("The weather in Delhi is sunny and warm today.")
        self.assertTrue(eg.similar(eg.words("weather in delhi is sunny"), said))
        self.assertTrue(eg.similar(eg.words("and warm today"), said))                  # the sentence's tail
        self.assertTrue(eg.similar(eg.words("today"), said))                           # a lone last word
        self.assertFalse(eg.similar(eg.words("stop"), said))
        self.assertFalse(eg.similar(eg.words("what about mumbai"), said))
        self.assertFalse(eg.similar(eg.words("is it warm in jaipur too"), said))

    def test_heard_while_wave_talked_or_just_after(self):
        start = self.clock.t
        self._say("The weather in Delhi is sunny and warm today.")
        self.assertTrue(self.guard.is_echo("weather in Delhi is sunny", heard_from=start + 1, heard_to=start + 2.5))
        self.assertTrue(self.guard.is_echo("and warm today", heard_from=start + 3.4, heard_to=start + 4.0))
        self.assertFalse(self.guard.is_echo("ruk jao", heard_from=start + 1, heard_to=start + 1.5))

    def test_speech_that_began_after_wave_stopped_is_the_users(self):
        start = self.clock.t
        self._say("Shall I open Chrome?")
        self.assertFalse(self.guard.is_echo("open chrome", heard_from=start + 4.5, heard_to=start + 5.2))
        self.assertTrue(self.guard.is_echo("open chrome", heard_from=start + 2.0, heard_to=start + 2.8))

    def test_unsure_transcripts_need_a_little_less(self):
        start = self.clock.t
        self._say("Your meeting with the design team starts at four.")
        heard = "meeting with design team starts for"
        self.assertTrue(self.guard.is_echo(heard, heard_from=start + 1, heard_to=start + 2, confidence=0.3))

    def test_switched_off(self):
        start = self.clock.t
        self._say("Systems active.")
        config.ECHO_GUARD = False
        self.assertFalse(self.guard.is_echo("systems active", heard_from=start + 0.5, heard_to=start + 1.5))


class SttTests(unittest.TestCase):
    """voice/stt.py's _gate_and_recognize() drops the echo, keeps the user."""

    def setUp(self):
        from voice.stt import Transcript, stt_engine  # noqa: PLC0415
        self.Transcript, self.stt = Transcript, stt_engine
        saved = config.ECHO_GUARD
        config.ECHO_GUARD = True
        self.addCleanup(setattr, config, "ECHO_GUARD", saved)

    def _run(self, text, is_echo):
        with mock.patch.object(self.stt, "_speaker_check", return_value=None), \
                mock.patch.object(self.stt, "_recognize", return_value=self.Transcript(text, engine="whisper")), \
                mock.patch.object(eg.echo_guard, "is_echo", return_value=is_echo) as check:
            out = self.stt._gate_and_recognize(object(), None, True, {"speech_end": 50.0}, audio_s=1.5)
        return out, check

    def test_wave_heard_back_is_dropped(self):
        out, check = self._run("the weather in delhi is sunny", is_echo=True)
        self.assertEqual(str(out), "")
        self.assertEqual(check.call_args.kwargs["heard_from"], 48.5)

    def test_the_user_gets_through(self):
        out, _check = self._run("open chrome", is_echo=False)
        self.assertEqual(str(out), "open chrome")


if __name__ == "__main__":
    unittest.main()
