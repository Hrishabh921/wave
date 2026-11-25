"""
tests/test_tier41_style_profile.py — Tier 41: answers in the user's own style.

SARA learns the length its user likes (asking for detail or brevity,
interrupting) and answers Roman-script Hindi in Hinglish. Wave's prompt said
neither - and live, Groq's model answered "hello wave, kaise ho?" in
Devanagari. brain/style_profile.py adds both to the system prompt.

    python -m unittest discover -s tests -p "test_tier41_style_profile.py"
"""
import os
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                      typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

from brain import style_profile as sp  # noqa: E402
from settings.config import config  # noqa: E402


class StyleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        p = mock.patch.object(config, "LLM_STYLE_PROFILE", True)
        p.start()
        self.addCleanup(p.stop)
        self.profile = sp.StyleProfile(path=os.path.join(self.tmp, "style_profile.json"))

    def test_the_reply_follows_the_messages_script(self):
        self.assertIn("Roman", self.profile.style_lines("hello wave, kaise ho?"))
        self.assertIn("never in Devanagari", self.profile.style_lines("mujhe kal yaad dilana"))
        self.assertIn("reply in Hindi", self.profile.style_lines("आज मौसम कैसा है"))
        self.assertEqual(self.profile.style_lines("what is the capital of France?"), "")

    def test_a_short_message_goes_by_the_usual_script(self):
        for _ in range(3):
            self.profile.observe_user_turn("mujhe kal subah yaad dilana")
        self.assertIn("Roman", self.profile.style_lines("ok"))

    def test_asking_for_detail_makes_answers_longer(self):
        for _ in range(3):
            self.profile.observe_user_turn("isko detail mein samjhao")
        self.assertIn("detailed", self.profile.style_lines("what is gravity?"))

    def test_asking_for_brevity_or_interrupting_makes_them_shorter(self):
        self.profile.observe_user_turn("chhota batao yaar")
        for _ in range(3):
            self.profile.note_interrupt()
        self.assertIn("short, to-the-point", self.profile.style_lines("what is gravity?"))

    def test_one_request_isnt_a_preference_and_old_ones_fade(self):
        self.profile.observe_user_turn("explain in detail please")
        self.assertEqual(self.profile.style_lines("what is gravity?"), "")
        for _ in range(3):
            self.profile.observe_user_turn("in detail please")
        for _ in range(60):
            self.profile.observe_user_turn("what time is it")
        self.assertEqual(self.profile.style_lines("what is gravity?"), "")

    def test_it_is_saved_across_restarts(self):
        for _ in range(3):
            self.profile.observe_user_turn("explain in detail please")
        again = sp.StyleProfile(path=self.profile.path())
        self.assertIn("detailed", again.style_lines("what is gravity?"))

    def test_off_stops_the_learning_but_not_the_language_line(self):
        with mock.patch.object(config, "LLM_STYLE_PROFILE", False):
            for _ in range(3):
                self.profile.observe_user_turn("explain in detail please")
            self.assertFalse(os.path.exists(self.profile.path()))
            self.assertIn("Roman", self.profile.style_lines("kaise ho aap?"))

    def test_the_style_goes_in_the_system_prompt(self):
        from brain.groq_core import WaveBrain  # noqa: PLC0415
        brain = object.__new__(WaveBrain)
        with mock.patch.object(sp.style_profile, "style_lines", return_value="Reply in Hinglish, Roman letters."):
            content = brain.get_dynamic_system_prompt("kaise ho")["content"]
        self.assertIn("STYLE:\nReply in Hinglish, Roman letters.", content)


if __name__ == "__main__":
    unittest.main()
