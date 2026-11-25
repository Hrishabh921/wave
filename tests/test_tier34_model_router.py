"""
tests/test_tier34_model_router.py — Tier 34: the right model for each request.

SARA routes each request to a cheap/fast or a main model and learns from
how its answers land. Wave sent every turn to openai/gpt-oss-120b and moved
to a backup only after a 429 - while on Groq's free tier each model has its
own per-minute quota. brain/model_router.py sends everyday turns to a fast
model and the heavy ones to the main model, and learns from corrections.

    python -m unittest discover -s tests -p "test_tier34_model_router.py"
"""
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

import brain.model_router as mr  # noqa: E402
from brain.groq_core import WaveBrain  # noqa: E402
from settings.config import config  # noqa: E402
import test_tier29_reply_reliability as t29  # noqa: E402

MAIN_MODEL, FAST_MODEL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"


class ClassifyTests(unittest.TestCase):
    def test_everyday_turns_go_to_the_fast_model(self):
        for text in ("hello wave, kaise ho?", "what's the capital of France?", "aaj kya plan hai",
                     "tell me a joke", "मौसम कैसा है"):
            with self.subTest(text=text):
                self.assertEqual(mr.classify(text).tier, mr.FAST)

    def test_heavy_requests_go_to_the_main_model(self):
        cases = {
            "write a python function to reverse a list": "code",
            "ek leave application likho principal ke liye": "writing",
            "explain how a transformer model works step by step": "detail",
            "difference between RAM and ROM samjhao": "detail",
            "open chrome and then search for the weather after that": "multi-step",
            "solve this equation 2x + 3 = 11": "maths",
            "x" * 300: "long request",
        }
        for text, reason in cases.items():
            with self.subTest(text=text[:40]):
                route = mr.classify(text)
                self.assertEqual((route.tier, route.reason), (mr.MAIN, reason))

    def test_an_attachment_needs_the_main_model(self):
        self.assertEqual(mr.classify("what is this?", {"kind": "document"}).tier, mr.MAIN)

    def test_buckets_are_language_length_and_kind(self):
        self.assertEqual(mr.bucket_of("what time is it?"), "en:short:question")
        self.assertEqual(mr.bucket_of("mujhe kal yaad dilana"), "hinglish:short:other")
        self.assertEqual(mr.bucket_of("आज मौसम कैसा है"), "hi:short:other")

    def test_corrections_and_repeats(self):
        for text in ("nahi yaar ye galat hai", "that's wrong", "phir se batao", "not what i asked", "गलत जवाब"):
            self.assertTrue(mr.is_correction(text), text)
        for text in ("thanks", "ok", "acha theek hai", "no thanks, that's all"):
            self.assertFalse(mr.is_correction(text), text)
        self.assertTrue(mr.is_repeat("what is the capital of peru", "what is the capital of peru?"))
        self.assertFalse(mr.is_repeat("what is the capital of peru", "play some music"))


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wave_router_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = [1000.0]
        self.router = self._router()

    def _router(self):
        return mr.ModelRouter(path=os.path.join(self.tmp, "model_router.json"), clock=lambda: self.now[0])

    def _turn(self, router, text, then):
        route = router.choose(text)
        self.now[0] += 20
        router.observe_user_turn(then)
        return route

    def test_corrected_fast_answers_move_that_kind_of_request_to_the_main_model(self):
        for _ in range(3):
            self.assertEqual(self._turn(self.router, "who won the match yesterday?", "galat hai yaar").tier, mr.FAST)
        route = self.router.choose("who is the prime minister of japan?")
        self.assertEqual((route.tier, route.learned), (mr.MAIN, True))
        # ...and that's remembered across restarts.
        self.assertEqual(self._router().choose("who invented the telephone?").tier, mr.MAIN)
        # Other kinds of request are unaffected.
        self.assertEqual(self.router.choose("chalo music bajao").tier, mr.FAST)

    def test_asking_the_same_thing_again_counts_as_a_miss(self):
        for _ in range(3):
            self._turn(self.router, "what is the capital of peru", "what is the capital of peru?")
        self.assertEqual(self.router.choose("what is the tallest mountain").tier, mr.MAIN)

    def test_answers_that_land_keep_the_fast_model(self):
        for _ in range(10):
            self._turn(self.router, "what's the weather like?", "thanks!")
        self.assertEqual(self.router.choose("what's the date today?").tier, mr.FAST)

    def test_a_late_reaction_says_nothing(self):
        for _ in range(3):
            self.router.choose("who won the match?")
            self.now[0] += mr.JUDGE_WITHIN_S + 5
            self.router.observe_user_turn("galat hai")
        self.assertEqual(self.router.choose("who won the race?").tier, mr.FAST)

    def test_the_fast_model_gets_another_try_days_later(self):
        for _ in range(3):
            self._turn(self.router, "who won the match yesterday?", "wrong")
        self.assertEqual(self.router.choose("who won?").tier, mr.MAIN)
        self.now[0] += mr.RETRY_FAST_AFTER_S + 1
        self.assertEqual(self.router.choose("who won?").tier, mr.FAST)


class BrainRoutingTests(unittest.TestCase):
    """brain/groq_core.py: the turn's tier decides the order of models."""

    def _brain(self, script, tier=None):
        b = t29._brain(script)
        b._route_tls = threading.local()
        b._route_tls.tier = tier
        return b

    def test_a_fast_turn_asks_the_fast_model_first_then_the_main_one(self):
        b = self._brain({}, tier=mr.FAST)
        self.assertEqual(b._models_for_request(), [FAST_MODEL, MAIN_MODEL])

    def test_a_main_turn_keeps_tier_29s_order(self):
        b = self._brain({}, tier=mr.MAIN)
        self.assertEqual(b._models_for_request(), [MAIN_MODEL, FAST_MODEL])

    def test_a_model_sitting_out_a_rate_limit_goes_last(self):
        b = self._brain({}, tier=mr.FAST)
        b._model_cooldown[FAST_MODEL] = 10 ** 12
        self.assertEqual(b._models_for_request(), [MAIN_MODEL, FAST_MODEL])

    def test_a_fast_model_that_fails_hands_the_turn_to_the_main_one(self):
        b = self._brain({FAST_MODEL: [RuntimeError("model decommissioned")], MAIN_MODEL: ["answer"]}, tier=mr.FAST)
        self.assertEqual(b._llm_create(model=b.model, messages=[]), "answer")
        self.assertEqual(b.client.calls, [FAST_MODEL, MAIN_MODEL])
        self.assertEqual(b._answered_by(), MAIN_MODEL)

    def test_the_main_model_failing_still_fails_the_call(self):
        b = self._brain({MAIN_MODEL: [RuntimeError("bad request")]}, tier=mr.MAIN)
        with self.assertRaises(RuntimeError):
            b._llm_create(model=b.model, messages=[])

    def test_routing_a_turn_follows_the_mode(self):
        b = self._brain({})
        with mock.patch.object(mr.model_router, "choose", return_value=mr.Route(mr.FAST, "everyday", "en:short:other")):
            with mock.patch.object(config, "LLM_ROUTER", "on"):
                b._route_turn("hi")
                self.assertEqual(b._turn_tier(), mr.FAST)
            with mock.patch.object(config, "LLM_ROUTER", "shadow"):
                b._route_turn("hi")
                self.assertIsNone(b._turn_tier())      # decided and logged, but the main model answers
            b._route_tls.tier = None
            with mock.patch.object(config, "LLM_ROUTER", "off"):
                b._route_turn("hi")
                self.assertIsNone(b._turn_tier())

    def test_the_fast_model_setting(self):
        b = self._brain({})
        with mock.patch.object(config, "LLM_FAST_MODEL", ""):
            self.assertEqual(b._fast_model(), FAST_MODEL)
        with mock.patch.object(config, "LLM_FAST_MODEL", "llama-3.1-8b-instant"):
            self.assertEqual(b._fast_model(), "llama-3.1-8b-instant")
        with mock.patch.object(config, "LLM_FAST_MODEL", "off"):
            self.assertIsNone(b._fast_model())
        other = t29._brain({}, provider_name="openai")
        with mock.patch.object(config, "LLM_FAST_MODEL", ""):
            self.assertIsNone(other._fast_model())

    def test_background_jobs_use_the_fast_model(self):
        cfg = type("Cfg", (), {"name": "groq", "model": MAIN_MODEL})()
        with mock.patch.object(config, "LLM_FAST_MODEL", ""):
            self.assertEqual(mr.background_model(cfg), FAST_MODEL)
        with mock.patch.object(config, "LLM_FAST_MODEL", "off"):
            self.assertEqual(mr.background_model(cfg), MAIN_MODEL)

    def test_an_empty_answer_is_asked_again_with_little_reasoning(self):
        """Seen live: gpt-oss-120b spent its whole budget reasoning and said nothing."""
        b = self._brain({})
        answer = type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": " Here it is. "})()})()]})()
        with mock.patch.object(b, "_chat_completion", return_value=answer) as call:
            self.assertEqual(b._retry_empty_answer([{"role": "user", "content": "explain"}]), "Here it is.")
        kwargs = call.call_args[1]
        self.assertEqual((kwargs["reasoning_effort"], kwargs["tool_choice"]), ("low", "none"))
        self.assertGreater(kwargs["max_tokens"], 1600)
        with mock.patch.object(b, "_chat_completion", side_effect=RuntimeError("down")):
            self.assertEqual(b._retry_empty_answer([]), "")

    def test_a_new_turn_forgets_the_last_tier_and_lets_the_router_learn(self):
        b = self._brain({}, tier=mr.FAST)
        with mock.patch.object(mr.model_router, "observe_user_turn") as observe:
            b._begin_turn_routing("galat hai")
        observe.assert_called_once_with("galat hai")
        self.assertIsNone(b._turn_tier())


if __name__ == "__main__":
    unittest.main()
