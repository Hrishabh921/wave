"""tests/test_tier58_backup_provider.py - Tier 58: a second provider (Gemini)
answers when every model of the main one is rate-limited, or it's down.

The owner asked (2026-11-28) why Wave doesn't move to another model when
Groq's free minute (8,000 tokens per model) runs out: the failover only
rotated between Groq's own two models, then tried a local Ollama that
isn't installed. Nothing here makes a real request.
"""
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402,F401  (adds the repo root to sys.path)

import brain.groq_core as gc  # noqa: E402
from brain import llm_provider as lp  # noqa: E402
from brain.groq_core import WaveBrain  # noqa: E402


class ResolveTests(unittest.TestCase):
    def test_auto_picks_gemini_when_its_key_is_there(self):
        cfg = lp.resolve_backup_provider({"GROQ_API_KEY": "gsk_main", "GEMINI_API_KEY": "test-gemini-key"})
        self.assertEqual((cfg.name, cfg.model, cfg.key_var), ("gemini", "gemini-3.8-flash", "GEMINI_API_KEY"))
        self.assertEqual(cfg.base_url, "https://generativelanguage.googleapis.com/v1beta/openai")
        self.assertFalse(cfg.use_sdk)

    def test_no_key_or_off_means_no_backup(self):
        self.assertIsNone(lp.resolve_backup_provider({"GROQ_API_KEY": "gsk_main"}))
        self.assertIsNone(lp.resolve_backup_provider({"GEMINI_API_KEY": "k", "LLM_BACKUP_PROVIDER": "off"}))
        self.assertIsNone(lp.resolve_backup_provider({"GEMINI_API_KEY": "your_gemini_api_key_here"}))   # placeholder

    def test_the_main_providers_settings_stay_its_own(self):
        cfg = lp.resolve_backup_provider({"GEMINI_API_KEY": "test-gemini-key", "LLM_API_KEY": "main-only",
                                          "LLM_MODEL": "openai/gpt-oss-120b", "LLM_BACKUP_MODEL": "gemini-x-flash"})
        self.assertEqual((cfg.api_key, cfg.model), ("test-gemini-key", "gemini-x-flash"))

    def test_the_main_provider_is_never_its_own_backup(self):
        env = {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "k", "OPENROUTER_API_KEY": "sk-or-x"}
        self.assertEqual(lp.resolve_backup_provider(env).name, "openrouter")
        self.assertIsNone(lp.resolve_backup_provider({"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "k"}))
        self.assertEqual(lp.resolve_backup_provider({"LLM_BACKUP_PROVIDER": "google", "GEMINI_API_KEY": "k"}).name,
                         "gemini")

    def test_gemini_can_be_the_main_provider_too(self):
        cfg = lp.resolve_provider({"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "k"})
        self.assertTrue(cfg.configured)
        self.assertEqual(cfg.name, "gemini")

    def test_the_newest_plain_flash_is_chosen(self):
        ids = ["models/gemini-2.5-flash", "models/gemini-3.8-flash-lite", "models/gemini-3.8-flash-image",
               "models/gemini-3.8-flash", "models/gemini-4-hydrogen", "models/gemini-embedding-2",
               "models/gemini-3.9-flash-preview"]
        self.assertEqual(lp.best_flash_model(ids), "gemini-3.8-flash")
        self.assertEqual(lp.best_flash_model(["gemini-3.8-flash-lite"]), "gemini-3.8-flash-lite")
        self.assertIsNone(lp.best_flash_model(["gemini-4-hydrogen"]))


class _RateLimited(Exception):
    status_code = 429

    def __init__(self, seconds):
        super().__init__(f"Rate limit reached. Please try again in {seconds}s.")


class _Status(Exception):
    def __init__(self, status, text="boom"):
        super().__init__(text)
        self.status_code = status


class _Client:
    """chat.completions.create() playing a script per model; records the calls."""

    def __init__(self, script):
        self.script, self.calls = script, []
        self.ids = ["models/gemini-2.5-flash", "models/gemini-4.1-flash", "models/gemini-4.1-flash-tts"]
        outer = self

        class Completions:
            def create(self, **kw):
                outer.calls.append(kw)
                step = outer.script[kw["model"]].pop(0)
                if isinstance(step, Exception):
                    raise step
                return step

        class Chat:
            completions = Completions()
        self.chat = Chat()

    def model_ids(self):
        return self.ids


def _brain(main, backup=None):
    b = object.__new__(WaveBrain)
    b.model = "openai/gpt-oss-120b"
    b.provider = type("P", (), {"name": "groq"})()
    b.client = _Client(main)
    b._model_cooldown = {}
    b._ollama_down_until = 0.0
    if backup is not None:
        b.backup_provider = lp.ProviderConfig(name="gemini", model="gemini-3.8-flash")
        b.backup_client = _Client(backup)
    return b


def _limited():
    """Both Groq models rate-limited - a fresh script each time (the client pops from it)."""
    return {"openai/gpt-oss-120b": [_RateLimited(30)], "openai/gpt-oss-20b": [_RateLimited(30)]}


class FailoverTests(unittest.TestCase):
    def test_when_every_groq_model_is_limited_gemini_answers_at_once(self):
        b = _brain(_limited(), {"gemini-3.8-flash": ["gemini answer"]})
        t = time.monotonic()
        with mock.patch.object(gc.time, "sleep") as sleep:
            self.assertEqual(b._llm_create(model=b.model, messages=[], tools=[{"x": 1}], stream=True), "gemini answer")
        sleep.assert_not_called()                                     # no waiting for Groq to free up
        self.assertLess(time.monotonic() - t, 1.0)
        sent = b.backup_client.calls[0]
        self.assertEqual((sent["model"], sent["tools"], sent["stream"]), ("gemini-3.8-flash", [{"x": 1}], True))

    def test_without_a_backup_nothing_changes(self):
        b = _brain(_limited())
        with self.assertRaises(_RateLimited):
            b._llm_create(model=b.model, messages=[])

    def test_a_limited_backup_rests_too_and_the_old_path_follows(self):
        b = _brain({"openai/gpt-oss-120b": [_RateLimited(30), _RateLimited(30)],
                    "openai/gpt-oss-20b": [_RateLimited(30), _RateLimited(30)]},
                   {"gemini-3.8-flash": [_RateLimited(40)]})
        with self.assertRaises(_RateLimited):
            b._llm_create(model=b.model, messages=[])
        self.assertGreater(b._model_cooldown["backup:gemini:gemini-3.8-flash"], time.monotonic() + 30)
        b._model_cooldown = {k: v for k, v in b._model_cooldown.items() if k.startswith("backup:")}
        with self.assertRaises(_RateLimited):
            b._llm_create(model=b.model, messages=[])
        self.assertEqual(len(b.backup_client.calls), 1)                 # not asked again while it rests

    def test_a_provider_thats_down_hands_over_too(self):
        b = _brain({"openai/gpt-oss-120b": [_Status(503), _Status(503)]}, {"gemini-3.8-flash": ["ok"]})
        with mock.patch.object(gc.time, "sleep"), mock.patch.object(WaveBrain, "_backup_model", lambda self: None):
            self.assertEqual(b._llm_create(model=b.model, messages=[]), "ok")

    def test_a_bad_request_is_not_the_backups_business(self):
        b = _brain({"openai/gpt-oss-120b": [_Status(400, "bad tool schema")]}, {"gemini-3.8-flash": ["ok"]})
        with mock.patch.object(WaveBrain, "_backup_model", lambda self: None), self.assertRaises(_Status):
            b._llm_create(model=b.model, messages=[])
        self.assertEqual(b.backup_client.calls, [])

    def test_a_retired_gemini_model_is_swapped_for_the_newest_flash(self):
        b = _brain(_limited(), {"gemini-3.8-flash": [_Status(404, "model not found")],
                                   "gemini-4.1-flash": ["answer"]})
        self.assertEqual(b._llm_create(model=b.model, messages=[]), "answer")
        self.assertEqual(b.backup_provider.model, "gemini-4.1-flash")      # remembered for next time

    def test_an_overloaded_backup_rests_a_minute(self):
        # seen live: Gemini answered "This model is currently experiencing high demand" (503)
        b = _brain(_limited(), {"gemini-3.8-flash": [_Status(503, "high demand")]})
        with self.assertRaises(_RateLimited):
            b._llm_create(model=b.model, messages=[])
        self.assertGreater(b._model_cooldown["backup:gemini:gemini-3.8-flash"], time.monotonic() + 50)

    def test_flash_lite_stands_behind_an_overloaded_flash(self):
        # seen live: gemini-3.8-flash said "high demand" while gemini-3.1-flash-lite answered in ~1 s
        b = _brain(_limited(), {"gemini-3.8-flash": [_Status(503, "high demand")],
                                "gemini-3.1-flash-lite": ["lite answer", "lite again"]})
        b.backup_client.ids += ["models/gemini-3.1-flash-lite", "models/gemini-2.5-flash-lite"]
        self.assertEqual(b._llm_create(model=b.model, messages=[]), "lite answer")
        b.client.script.update(_limited())
        for m in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
            b._model_cooldown.pop(m, None)
        self.assertEqual(b._llm_create(model=b.model, messages=[]), "lite again")
        self.assertEqual([c["model"] for c in b.backup_client.calls],
                         ["gemini-3.8-flash", "gemini-3.1-flash-lite", "gemini-3.1-flash-lite"])   # Flash rests

    def test_after_a_slow_failure_no_second_model_in_the_same_turn(self):
        clock = [1000.0]
        b = _brain(_limited(), {"gemini-3.8-flash": [], "gemini-3.1-flash-lite": ["too late"]})
        b.backup_client.ids += ["models/gemini-3.1-flash-lite"]

        def slow_timeout(**kw):
            clock[0] += 12.0
            raise _Status(504, "deadline exceeded")
        b.backup_client.chat.completions.create = slow_timeout
        with mock.patch.object(gc.time, "monotonic", lambda: clock[0]), mock.patch.object(gc.time, "sleep"), \
                self.assertRaises(_RateLimited):
            b._llm_create(model=b.model, messages=[])
        self.assertEqual(b.backup_client.script["gemini-3.1-flash-lite"], ["too late"])   # never asked

    def test_a_backup_answers_fast_or_steps_aside(self):
        cfg = lp.resolve_backup_provider({"GEMINI_API_KEY": "k", "LLM_TIMEOUT": "120", "LLM_MAX_RETRIES": "4"})
        self.assertEqual((cfg.timeout, cfg.max_retries), (12.0, 0))      # not the main provider's patience
        cfg = lp.resolve_backup_provider({"GEMINI_API_KEY": "k", "LLM_BACKUP_TIMEOUT": "35"})
        self.assertEqual(cfg.timeout, 35.0)

    def test_the_busy_reply_still_comes_when_nothing_can_answer(self):
        b = _brain(_limited(), {"gemini-3.8-flash": [_Status(500, "down")]})
        with self.assertRaises(_RateLimited) as ctx:
            b._llm_create(model=b.model, messages=[])
        with mock.patch.object(gc.config, "OLLAMA_ENABLED", False):
            self.assertIn("busy", b._reply_after_failure(ctx.exception, "hello"))


class BuildTests(unittest.TestCase):
    def test_tests_never_get_a_real_backup(self):
        import os
        self.assertEqual(os.environ.get("LLM_BACKUP_PROVIDER"), "off")       # tests/_phase4_support.py
        self.assertIsNone(lp.resolve_backup_provider())


if __name__ == "__main__":
    unittest.main()
