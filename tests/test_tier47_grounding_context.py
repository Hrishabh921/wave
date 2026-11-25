"""Tier 47 - time-sensitive questions answered from a fresh search, and one line about the PC.

GroundingTests / ContextLineTests: brain/grounding.py and brain/context_line.py on their own.
WiredIntoThinkTests: the search result reaches the model as marked web content (and the turn
counts as having read the web); the PC line is in the system prompt; neither when switched off.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: E402,F401
from _fake_openai import Reply, completion  # noqa: E402
from test_phase4_brain import BrainBase  # noqa: E402

from brain import context_line, grounding, taint  # noqa: E402
from settings.config import config  # noqa: E402


def _flags(testcase, **values):
    saved = {k: getattr(config, k) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    testcase.addCleanup(lambda: [setattr(config, k, v) for k, v in saved.items()])


class GroundingTests(unittest.TestCase):
    def setUp(self):
        _flags(self, WEB_GROUNDING=True)

    def test_time_sensitive_questions(self):
        for text in ["petrol ka rate kya hai aaj?", "who is the current RBI governor?", "what is the latest iPhone price?",
                     "who won the match yesterday?", "aaj ka gold rate kitna hai", "dollar ka rate kya hai",
                     "how much does a tesla cost right now?", "what's the current version of python?",
                     "who is the ceo of twitter?", "india ka captain kaun hai"]:
            self.assertTrue(grounding.needs_grounding(text), text)

    def test_everything_else_goes_to_the_model_as_before(self):
        for text in ["what is photosynthesis?", "what is my current city?", "what is the weather today?",
                     "open the latest download", "latest news batao", "what time is it", "tell me a joke",
                     "set a reminder for the latest episode", "is my battery charging right now?", "latest?"]:
            self.assertFalse(grounding.needs_grounding(text), text)
        config.WEB_GROUNDING = False
        self.assertFalse(grounding.needs_grounding("petrol ka rate kya hai aaj?"))

    def test_a_hinglish_question_is_searched_the_way_engines_understand(self):
        self.assertEqual(grounding.search_query("petrol ka rate kya hai aaj Delhi mein?"), "petrol rate today Delhi")
        self.assertEqual(grounding.search_query("aaj ka gold rate kitna hai"), "today gold rate")
        self.assertEqual(grounding.search_query("who is the current RBI governor?"), "who is the current RBI governor?")

    def test_the_search_has_a_hard_time_limit(self):
        self.assertEqual(grounding.fetch("q", lambda q: "Petrol: 94.72/litre"), "Petrol: 94.72/litre")
        self.assertIsNone(grounding.fetch("q", lambda q: (time.sleep(1), "late")[1], timeout_s=0.1))
        self.assertIsNone(grounding.fetch("q", lambda q: ""))
        self.assertIsNone(grounding.fetch("q", lambda q: "I couldn't find live information on that right now."))
        self.assertIsNone(grounding.fetch("q", lambda q: 1 / 0))
        self.assertEqual(len(grounding.fetch("q", lambda q: "x" * 5000)), 1500)


class SearchRelevanceTests(unittest.TestCase):
    """web/web_ops.py's ask_internet: results that don't name the question's words are dropped."""

    def test_unrelated_pages_are_dropped(self):
        from web.web_ops import WebManager  # noqa: PLC0415
        results = [{"title": "Netflix", "body": "Netflix is a video streaming company"},
                   {"title": "Petrol Price in New Delhi", "body": "Petrol costs Rs 94.72 per litre in Delhi today"},
                   {"title": "Ampere", "body": "electric current is expressed in amperes"}]
        self.assertEqual(WebManager.relevant_results("petrol price in Delhi today", results),
                         ["Petrol Price in New Delhi: Petrol costs Rs 94.72 per litre in Delhi today"])
        self.assertEqual(WebManager.relevant_results("who is the current RBI governor?", results[2:]), [])

    def test_the_newer_search_package_is_used_and_filtered(self):
        from web.web_ops import WebManager  # noqa: PLC0415
        fake = mock.Mock()
        fake.DDGS.return_value.text.return_value = [
            {"title": "Governor of the Reserve Bank of India", "body": "Sanjay Malhotra is the current RBI governor."},
            {"title": "Netflix", "body": "streaming"}]
        with mock.patch.dict(sys.modules, {"ddgs": fake}):
            text = WebManager.ask_internet("who is the current RBI governor?")
        self.assertEqual(text, "- Governor of the Reserve Bank of India: Sanjay Malhotra is the current RBI governor.")
        fake.DDGS.return_value.text.side_effect = RuntimeError("no network")
        with mock.patch.dict(sys.modules, {"ddgs": fake}):
            self.assertEqual(WebManager.ask_internet("who is the current RBI governor?"), "")
        self.assertEqual(WebManager.ask_internet("   "), "")


class ContextLineTests(unittest.TestCase):
    def setUp(self):
        _flags(self, CONTEXT_LINE=True, CONTEXT_WINDOW_TITLES=False)
        context_line.reset()
        self.addCleanup(context_line.reset)

    def build(self, fg=("chrome.exe", "Inbox (3) - Gmail - Google Chrome"), bat=(61, True)):
        context_line.reset()
        return context_line.build(foreground=lambda: fg, battery=lambda: bat)

    def test_the_app_in_front_and_the_battery(self):
        self.assertEqual(self.build(), "ON THE PC RIGHT NOW: Google Chrome is in front; battery 61% (charging). "
                                       "Use it only when it matters to what's asked.")
        self.assertIn("VS Code is in front", self.build(fg=("Code.exe", "x")))
        self.assertIn("the desktop is in front", self.build(fg=("explorer.exe", "Program Manager")))
        self.assertIn("battery 20% (on battery)", self.build(bat=(20, False)))

    def test_never_wave_itself_and_nothing_when_nothing_is_known(self):
        self.assertEqual(self.build(fg=("", ""), bat=None), "")
        self.assertEqual(self.build(fg=(None, None), bat=(50, True)),
                         "ON THE PC RIGHT NOW: battery 50% (charging). Use it only when it matters to what's asked.")

    def test_window_titles_only_when_asked_for_and_never_private_ones(self):
        self.assertNotIn("Gmail", self.build())
        config.CONTEXT_WINDOW_TITLES = True
        self.assertIn('("Inbox (3) - Gmail - Google Chrome")', self.build())
        self.assertNotIn("Bank", self.build(fg=("chrome.exe", "HDFC Bank NetBanking - Login")))
        self.assertNotIn("private", self.build(fg=("msedge.exe", "New InPrivate tab")).lower())

    def test_read_at_most_every_few_seconds(self):
        calls = []
        clock = [100.0]

        def fg():
            calls.append(1)
            return "notepad.exe", ""
        context_line.reset()
        context_line.build(foreground=fg, battery=lambda: None, now=lambda: clock[0])
        context_line.build(foreground=fg, battery=lambda: None, now=lambda: clock[0] + 2)
        self.assertEqual(len(calls), 1)
        context_line.build(foreground=fg, battery=lambda: None, now=lambda: clock[0] + 6)
        self.assertEqual(len(calls), 2)

    def test_switched_off(self):
        config.CONTEXT_LINE = False
        self.assertEqual(self.build(), "")


class WiredIntoThinkTests(BrainBase):
    def setUp(self):
        super().setUp()
        _flags(self, WEB_GROUNDING=True, CONTEXT_LINE=False)
        self.search = mock.patch("web.web_ops.WebManager.ask_internet",
                                 return_value="Petrol costs Rs 94.72 a litre in Delhi today.")
        self.ask = self.search.start()
        self.addCleanup(self.search.stop)

    def _system_texts(self):
        req = self.srv.posts()[-1].json
        return [m["content"] for m in req["messages"] if m["role"] == "system"]

    def test_a_time_sensitive_question_is_searched_first(self):
        self.srv.script = [Reply(200, completion("Rs 94.72 a litre."))]
        reply = self.brain.think("petrol ka rate kya hai aaj?")
        self.assertEqual(reply, "Rs 94.72 a litre.")
        self.ask.assert_called_once_with("petrol rate today")
        grounded = [t for t in self._system_texts() if "94.72" in t]
        self.assertEqual(len(grounded), 1)
        self.assertIn("web search", grounded[0].lower())
        self.assertTrue(taint.turn_tainted())                     # read the web: T1 actions now ask first
        self.assertIn("a web search", taint.turn_sources())

    def test_think_stream_too(self):
        self.srv.script = [Reply(200, completion("Rs 94.72 a litre."))]
        list(self.brain.think_stream("petrol ka rate kya hai aaj?"))
        self.assertTrue(any("94.72" in t for t in self._system_texts()))

    def test_other_questions_and_a_failed_search_go_to_the_model_as_before(self):
        self.srv.script = [Reply(200, completion("Plants make food from light."))]
        self.brain.think("what is photosynthesis?")
        self.ask.assert_not_called()
        self.ask.return_value = ""
        self.srv.script = [Reply(200, completion("I'm not sure."))]
        self.brain.think("petrol ka rate kya hai aaj?")
        self.assertFalse(any("94.72" in t for t in self._system_texts()))

    def test_the_pc_line_is_in_the_system_prompt(self):
        config.CONTEXT_LINE = True
        with mock.patch.object(context_line, "build", return_value="ON THE PC RIGHT NOW: Notepad is in front."):
            content = self.brain.get_dynamic_system_prompt("kya chal raha hai?")["content"]
        self.assertIn("ON THE PC RIGHT NOW: Notepad is in front.", content)


if __name__ == "__main__":
    unittest.main()
