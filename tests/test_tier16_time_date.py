"""
tests/test_tier16_time_date.py — Tier 16: get_current_time/get_current_date
(brain/handlers/productivity.py) and the two intent_router.py fast-path
rules added alongside them (brain/intent_router.py).

Turns the ad-hoc smoke-test verification already done while building this
tier into a formal, repeatable regression test - in particular the one
real bench bug this tier's own work caused and fixed: a stale negative
row ("what time is it right now" -> "chat") that now correctly matches
the new get_current_time rule and had to move to intents_en.jsonl (see
bench/golden/intents_en.jsonl and bench/golden/negatives.jsonl).

Run: python -m unittest discover -s tests -p "test_tier16_time_date.py"
(pytest will also collect it.)
"""
import re
import unittest

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from brain import intent_router as ir                               # noqa: E402
from brain.skills_registry import skills                            # noqa: E402
import brain.handlers  # noqa: E402,F401  (side effect: registers every built-in skill)


# ---------------------------------------------------------------------------
# brain/handlers/productivity.py - get_current_time / get_current_date
# ---------------------------------------------------------------------------
class GetCurrentTimeDateSkillTests(unittest.TestCase):
    """Both skills read the real wall clock (datetime.datetime.now()) with
    no way to inject a fixed time, and that's deliberate - see the file's
    own module docstring: these are a real clock read, not a guess, so
    pinning the exact value isn't the point. A format check is what
    actually matters here (in particular, get_current_time's %I format
    plus .lstrip('0') - confirms a single leading zero on the hour is
    stripped ("01:05 PM" -> "1:05 PM") without eating anything else)."""

    def _call(self, name, **kwargs):
        return skills.get_handler(name)(None, kwargs)

    def test_get_current_time_format(self):
        result = self._call("get_current_time")
        self.assertRegex(result, r"^It's \d{1,2}:\d{2} (AM|PM)\.$")
        # the hour must never have a leading zero after .lstrip('0')
        hour_str = result.split("It's ")[1].split(":")[0]
        self.assertFalse(hour_str.startswith("0"), f"leading zero survived in {result!r}")

    def test_get_current_date_format(self):
        result = self._call("get_current_date")
        self.assertRegex(result, r"^It's [A-Za-z]+, \d{2} [A-Za-z]+ \d{4}\.$")

    def test_both_skills_ignore_any_args_passed(self):
        # Neither tool declares parameters (see their @skill(...) calls) -
        # extra args should simply be ignored, not raise.
        self.assertRegex(self._call("get_current_time", unexpected="arg"),
                          r"^It's \d{1,2}:\d{2} (AM|PM)\.$")
        self.assertRegex(self._call("get_current_date", unexpected="arg"),
                          r"^It's [A-Za-z]+, \d{2} [A-Za-z]+ \d{4}\.$")


# ---------------------------------------------------------------------------
# brain/intent_router.py - the two new fast-path rules.
# ---------------------------------------------------------------------------
class IntentRouterTimeDateFastPathTests(unittest.TestCase):
    def assertRoutes(self, text, skill):
        routed = ir.route(text)
        self.assertIsNotNone(routed, f"expected {text!r} to route, got None")
        self.assertEqual(routed.skill, skill)
        self.assertEqual(routed.args, {})

    def assertNoRoute(self, text):
        self.assertIsNone(ir.route(text), f"expected {text!r} NOT to route, got a match")

    def test_english_time_phrases(self):
        for text in ("what time is it", "what's the time", "whats the time", "tell me the time",
                     "current time", "do you know the time"):
            self.assertRoutes(text, "get_current_time")

    def test_hinglish_time_phrases(self):
        for text in ("time kya hai", "kitne baje hain", "kitna baja hai",
                     "abhi kitne baje hain", "abhi kitna baja hai"):
            self.assertRoutes(text, "get_current_time")

    def test_devanagari_time_phrases(self):
        for text in ("क्या समय हुआ है", "अभी कितने बजे हैं", "टाइम क्या है"):
            self.assertRoutes(text, "get_current_time")

    def test_english_date_phrases(self):
        for text in ("what's the date", "whats the date", "what is the date", "what's today's date",
                     "what day is it today", "what's the date today", "today's date", "current date"):
            self.assertRoutes(text, "get_current_date")

    def test_hinglish_date_phrases(self):
        for text in ("aaj ki tarikh kya hai", "aaj ki tareekh kya hai",
                     "aaj kaunsa din hai", "aaj kya date hai"):
            self.assertRoutes(text, "get_current_date")

    def test_devanagari_date_phrases(self):
        for text in ("आज की तारीख क्या है", "आज कौन सा दिन है"):
            self.assertRoutes(text, "get_current_date")

    def test_regression_the_bench_negative_row_that_moved_now_routes_correctly(self):
        # bench/golden/negatives.jsonl used to carry this exact phrase as
        # a "chat" (no-route) row, written before get_current_time
        # existed. It now correctly matches - see that file's history and
        # intents_en.jsonl's corresponding row for the full story.
        self.assertRoutes("what time is it right now", "get_current_time")

    def test_mentioning_time_or_date_in_passing_does_not_false_trigger(self):
        # Tier 17: "mark the todo as date done" used to live in this list,
        # but it's a genuine (if contrived) match for the new complete_todo
        # rule now ("mark X done" -> query="the todo as date") - replaced
        # with a phrase that still mentions "date" in passing without
        # also happening to look like a "mark ... done" command.
        for text in ("I have a date tonight", "I don't have time for this",
                     "set a timer for 10 minutes", "remind me to call mom tomorrow at 5pm",
                     "what's on my calendar today", "I need to update the date on this form"):
            self.assertNoRoute(text)

    def test_compound_message_defers_to_the_llm_instead_of_fast_pathing(self):
        # " then " is one of _COMPOUND_INDICATORS - a message asking for
        # the time AND something else should never fast-path just the
        # time half and silently drop the rest.
        self.assertNoRoute("what time is it then remind me to call mom")


if __name__ == "__main__":
    unittest.main()
